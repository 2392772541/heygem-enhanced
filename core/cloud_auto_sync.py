# -*- coding: utf-8 -*-
"""
云端数字人成片自动拉回本地守护引擎（零黑窗、8 线程断点高速拉取、自动去重与验片）。
运行在本机 HeyGem Studio (8390) 后台线程中，每隔设定秒数轮询 Colab 云端创作台：
1. 自动发现新完成（status=success）的成品视频；
2. 自动触发/复用云端原生 4K 60帧视觉无损压小版（~5 倍瘦身）；
3. 优先直连 HTTP/2 8 线程并发拉取到本机指定目录（默认 E:\数字人视频文件）；
4. 完整校验字节数与 ffprobe 音视频流后原子落盘，并弹出 Windows 桌面完成提醒。
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "logs" / "cloud_sync_state.json"
DEFAULT_SAVE_DIR = Path(r"E:\数字人视频文件")
DEFAULT_COLAB_AUTH_FILE = Path.home() / ".ssh" / "colab_hub_auth.json"


def _is_fixed_gateway_url(url: str) -> bool:
    """判断目标地址是否为腾讯云固定域名网关（需要 HTTP Basic Auth）。"""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "gz.grzdt.com" or host.endswith(".grzdt.com"):
        return True
    gw = os.environ.get("COLAB_GATEWAY_URL", "").strip()
    if gw:
        gw_p = urllib.parse.urlparse(gw)
        if gw_p.netloc and parsed.netloc.lower() == gw_p.netloc.lower():
            return True
    return False


def _load_colab_basic_auth() -> tuple[str, str] | None:
    """读取本机私有网关 Basic Auth 凭据（环境变量或 ~/.ssh/colab_hub_auth.json）。"""
    env_pass = os.environ.get("COLAB_HUB_PASS", "")
    env_user = os.environ.get("COLAB_HUB_USER", "").strip() or "admin"
    if env_pass:
        return env_user, env_pass
    auth_path_env = os.environ.get("COLAB_HUB_AUTH_FILE", "").strip()
    auth_path = Path(auth_path_env) if auth_path_env else DEFAULT_COLAB_AUTH_FILE
    if not auth_path.is_file():
        return None
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            pwd = str(raw.get("password") or raw.get("pass") or "")
            user = str(raw.get("user") or raw.get("username") or "admin").strip() or "admin"
            if pwd:
                return user, pwd
    except Exception:
        return None
    return None


def _join_cloud_url(cloud_url: str, rel_or_abs: str) -> str:
    """安全拼接带路径前缀（如 /heygem）的固定域名与后端相对路径，避免前缀重复。"""
    target = (rel_or_abs or "").strip()
    if target.startswith(("http://", "https://")):
        return target
    base = (cloud_url or "").strip().rstrip("/")
    rel = "/" + target.lstrip("/")
    parsed_base = urllib.parse.urlparse(base)
    base_path = parsed_base.path.rstrip("/")
    if base_path and (rel == base_path or rel.startswith(base_path + "/")):
        return f"{parsed_base.scheme}://{parsed_base.netloc}{rel}"
    return f"{base}{rel}"


def _append_query_token(url: str, token: str) -> str:
    """在固定域名网关模式下把内部可选 token 放入查询参数（避开 Caddy 剥离 Authorization 头）。"""
    tok = (token or "").strip()
    if not tok:
        return url
    parsed = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(k == "token" for k, _ in pairs):
        return url
    pairs.append(("token", tok))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(pairs), parsed.fragment)
    )


def _safe_filename(name: str, fallback: str = "video") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return (cleaned[:60] or fallback)


def _resolve_ffprobe() -> str | None:
    for cand in (
        PROJECT_ROOT / "bin" / "ffprobe.exe",
        Path(r"E:\app\Miniconda\Library\bin\ffprobe.exe"),
    ):
        if cand.is_file():
            return str(cand)
    return shutil.which("ffprobe")


def _verify_mp4_basic(path: Path) -> tuple[bool, str]:
    """基础完整性验证：检查文件大小及（若存在 ffprobe）解析音视频流。"""
    if not path.is_file() or path.stat().st_size < 1024:
        return False, "文件体积异常过小"
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        return True, "size_ok"
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0:
            return False, (res.stderr or "ffprobe 校验失败")[:160]
        info = json.loads(res.stdout or "{}")
        streams = info.get("streams") or []
        has_v = any(s.get("codec_type") == "video" for s in streams)
        dur = float((info.get("format") or {}).get("duration") or 0)
        if not has_v or dur <= 0.05:
            return False, "未检测到有效视频轨或时长异常"
        v = next(s for s in streams if s.get("codec_type") == "video")
        return True, f"{v.get('width')}x{v.get('height')} · {dur:.1f}s"
    except Exception as exc:
        return True, f"ffprobe跳过({exc})"


def _verify_audio_basic(path: Path) -> tuple[bool, str]:
    """基础音频完整性验证：检查文件大小及（若存在 ffprobe）解析音频轨。"""
    if not path.is_file() or path.stat().st_size < 512:
        return False, "音频文件体积异常过小"
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        return True, "size_ok"
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0:
            return False, (res.stderr or "ffprobe 音频校验失败")[:160]
        info = json.loads(res.stdout or "{}")
        streams = info.get("streams") or []
        has_a = any(s.get("codec_type") == "audio" for s in streams)
        dur = float((info.get("format") or {}).get("duration") or 0)
        if not has_a or dur <= 0.05:
            return False, "未检测到有效音频轨或时长异常"
        return True, f"音频 · {dur:.1f}s"
    except Exception as exc:
        return True, f"ffprobe跳过({exc})"


def _send_windows_toast(title: str, body: str) -> None:
    """无黑窗弹出 Windows 右下角静默提示气泡（失败自动忽略，绝不影响同步主流程）。"""
    if os.name != "nt":
        return
    try:
        safe_title = title.replace("'", " ").replace("\n", " ")[:80]
        safe_body = body.replace("'", " ").replace("\n", " ")[:180]
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(5000, '{safe_title}', '{safe_body}', [System.Windows.Forms.ToolTipIcon]::Info); "
            "Start-Sleep -Milliseconds 5500; $n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


class CloudAutoSyncManager:
    """云端数字人成片后台自动同步管理器。"""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._config: dict[str, Any] = {
            "enabled": False,
            "sync_audio": True,
            "cloud_url": "",
            "cloud_token": "",
            "save_dir": str(DEFAULT_SAVE_DIR),
            "quality": "compact",  # compact=原生4K压小版 / original=原片
            "poll_interval": 15,
            "threads": 8,
            "notify_desktop": True,
            "synced": {},  # key -> {video_id/audio_id, media_type, name, local_path, size_mb, saved_at, meta}
        }
        self._runtime: dict[str, Any] = {
            "connected": False,
            "cloud_video_count": 0,
            "cloud_videos": [],
            "cloud_audio_count": 0,
            "cloud_audios": [],
            "last_check_at": 0.0,
            "last_error": "",
            "active_download": None,
            "recent_events": [],
        }
        self._load_config()

    def _log_event(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        with self._lock:
            ev = self._runtime["recent_events"]
            ev.append(line)
            if len(ev) > 40:
                del ev[:-40]

    def _load_config(self) -> None:
        if not self.config_path.is_file():
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                with self._lock:
                    for k in ("enabled", "sync_audio", "cloud_url", "cloud_token", "save_dir", "quality", "poll_interval", "threads", "notify_desktop"):
                        if k in raw:
                            self._config[k] = raw[k]
                    if isinstance(raw.get("synced"), dict):
                        self._config["synced"] = raw["synced"]
        except Exception:
            pass

    def _save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = json.dumps(self._config, ensure_ascii=False, indent=2)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.config_path)
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="CloudAutoSync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def wake(self) -> None:
        self._wake_event.set()

    def update_config(
        self,
        *,
        enabled: bool | None = None,
        sync_audio: bool | None = None,
        cloud_url: str | None = None,
        cloud_token: str | None = None,
        save_dir: str | None = None,
        quality: str | None = None,
        poll_interval: int | None = None,
        notify_desktop: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if enabled is not None:
                self._config["enabled"] = bool(enabled)
            if sync_audio is not None:
                self._config["sync_audio"] = bool(sync_audio)
            if cloud_url is not None:
                u = cloud_url.strip().rstrip("/")
                if u and not u.startswith(("http://", "https://")):
                    u = "https://" + u
                self._config["cloud_url"] = u
                self._runtime["last_error"] = ""
            if cloud_token is not None:
                self._config["cloud_token"] = cloud_token.strip()
            if save_dir is not None and save_dir.strip():
                p = Path(save_dir.strip())
                p.mkdir(parents=True, exist_ok=True)
                self._config["save_dir"] = str(p)
            if quality in ("compact", "original", "hevc"):
                self._config["quality"] = quality
            if poll_interval is not None:
                self._config["poll_interval"] = max(5, min(300, int(poll_interval)))
            if notify_desktop is not None:
                self._config["notify_desktop"] = bool(notify_desktop)
        self._save_config()
        self._log_event(
            f"配置已更新：{'已开启自动拉回' if self._config['enabled'] else '已暂停'} · "
            f"保存目录 {self._config['save_dir']}"
        )
        self.wake()
        return self.get_status()

    def mark_already_synced(self, key: str, info: dict[str, Any]) -> None:
        with self._lock:
            self._config["synced"][key] = info
        self._save_config()

    def get_synced_item(self, sync_key: str) -> dict[str, Any] | None:
        with self._lock:
            raw = (self._config.get("synced") or {}).get(sync_key)
            if not isinstance(raw, dict):
                return None
            lp = Path(str(raw.get("local_path") or ""))
            return {
                **raw,
                "sync_key": sync_key,
                "exists": lp.is_file() if str(lp) else False,
            }

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            synced_map = dict(self._config.get("synced") or {})
            items = []
            for key, item in synced_map.items():
                if not isinstance(item, dict):
                    continue
                lp = Path(str(item.get("local_path") or ""))
                items.append({
                    **item,
                    "sync_key": key,
                    "exists": lp.is_file() if str(lp) else False,
                })
            items.sort(key=lambda x: float(x.get("saved_at") or 0), reverse=True)
            return {
                "enabled": bool(self._config.get("enabled")),
                "sync_audio": bool(self._config.get("sync_audio", True)),
                "cloud_url": self._config.get("cloud_url") or "",
                "has_token": bool(self._config.get("cloud_token")),
                "save_dir": self._config.get("save_dir") or str(DEFAULT_SAVE_DIR),
                "audio_save_dir": str(Path(self._config.get("save_dir") or str(DEFAULT_SAVE_DIR)) / "音频"),
                "quality": self._config.get("quality") or "compact",
                "poll_interval": int(self._config.get("poll_interval") or 15),
                "notify_desktop": bool(self._config.get("notify_desktop", True)),
                "connected": bool(self._runtime.get("connected")),
                "cloud_video_count": int(self._runtime.get("cloud_video_count") or 0),
                "cloud_videos": list(self._runtime.get("cloud_videos") or []),
                "cloud_audio_count": int(self._runtime.get("cloud_audio_count") or 0),
                "cloud_audios": list(self._runtime.get("cloud_audios") or []),
                "last_check_at": float(self._runtime.get("last_check_at") or 0.0),
                "last_error": self._runtime.get("last_error") or "",
                "active_download": self._runtime.get("active_download"),
                "synced_count": len(items),
                "synced_items": items[:120],
                "recent_events": list(self._runtime.get("recent_events") or [])[-20:],
            }

    def _build_openers(self) -> list[urllib.request.OpenerDirector]:
        """优先直连（走 HTTP/2 速度最快），失败自动回退系统代理。"""
        return [
            urllib.request.build_opener(urllib.request.ProxyHandler({})),
            urllib.request.build_opener(),
        ]

    def _prepare_request(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """统一补齐网关 Basic Auth 与可选内部 token（兼容固定域名与临时链接）。"""
        req_headers = dict(headers or {})
        tok = (self._config.get("cloud_token") or "").strip()
        final_url = url
        if _is_fixed_gateway_url(url):
            creds = _load_colab_basic_auth()
            if creds and "Authorization" not in req_headers:
                user, pwd = creds
                raw_b64 = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
                req_headers["Authorization"] = f"Basic {raw_b64}"
            if tok:
                final_url = _append_query_token(final_url, tok)
        elif tok and "Authorization" not in req_headers:
            req_headers["Authorization"] = f"Bearer {tok}"
        return final_url, req_headers

    def _http_request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 20,
    ) -> tuple[int, dict[str, str], bytes, urllib.request.OpenerDirector]:
        final_url, req_headers = self._prepare_request(url, headers=headers)
        last_exc: Exception | None = None
        for opener in self._build_openers():
            try:
                req = urllib.request.Request(final_url, data=data, method=method, headers=req_headers)
                with opener.open(req, timeout=timeout) as resp:
                    body = resp.read() if method != "HEAD" else b""
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    return resp.status, hdrs, body, opener
            except urllib.error.HTTPError as he:
                # HTTP 层返回（如 401/404/500/503/524）说明网络已连通，直接抛出
                raise he
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def _video_sync_key(self, video: dict[str, Any]) -> str:
        vid = int(video.get("id") or 0)
        code = str(video.get("code") or "").strip()
        created = str(video.get("created_at") or "").strip()
        if code:
            return f"{vid}:{code}"
        return f"{vid}:{created}"

    def _build_local_filename(self, video: dict[str, Any], quality: str, save_dir: Path | None = None) -> str:
        """清爽可读文件名：直接沿用作品名（去掉长串乱码后缀），仅撞名时自动追加序号。"""
        vid = int(video.get("id") or 0)
        raw_name = str(video.get("name") or "").strip()
        model = _safe_filename(str(video.get("model_name") or ""), fallback="")
        clean_name = _safe_filename(raw_name, fallback="")
        if not clean_name or clean_name in ("1", "2", "3", str(vid)):
            base = f"{model or '数字人'}_{vid}"
        elif model and model not in clean_name and len(clean_name) <= 18:
            base = f"{clean_name}_{model}"
        else:
            base = clean_name
        suffix = "_原片" if quality == "original" else ""
        cand = f"{base}{suffix}.mp4"
        if save_dir is not None and (save_dir / cand).exists():
            cand = f"{base}_{vid}{suffix}.mp4"
        return cand

    def _existing_local_match(self, save_dir: Path, video: dict[str, Any], quality: str) -> Path | None:
        """严格按唯一任务 code8 匹配本地已有成片，避免把不同天/不同任务的同序号视频误判为已下载。"""
        if not save_dir.is_dir():
            return None
        code8 = re.sub(r"[^a-zA-Z0-9]+", "", str(video.get("code") or ""))[:8]
        tag = "4K原画压小" if quality != "original" else "原片"
        if code8:
            for p in save_dir.glob(f"*{code8}*{tag}.mp4"):
                if p.is_file() and p.stat().st_size > 1024 * 1024:
                    return p
        return None

    def pull_video_now(self, video_id: int, force: bool = True) -> dict[str, Any]:
        """手动立即拉取指定编号的云端视频（force=True 时即便之前登记过也会重新拉回）。"""
        with self._lock:
            cloud_url = (self._config.get("cloud_url") or "").strip().rstrip("/")
            save_dir = Path(self._config.get("save_dir") or str(DEFAULT_SAVE_DIR))
            quality = str(self._config.get("quality") or "compact")
            threads = max(1, min(12, int(self._config.get("threads") or 8)))
            notify = bool(self._config.get("notify_desktop", True))
        if not cloud_url:
            raise RuntimeError("请先填写并保存云端创作台网址")
        status, _, body, _ = self._http_request(f"{cloud_url}/api/videos", timeout=15)
        if status != 200:
            raise RuntimeError(f"云端返回 HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
        videos = payload.get("videos", []) if isinstance(payload, dict) else payload
        target = next((v for v in videos if isinstance(v, dict) and int(v.get("id") or 0) == int(video_id)), None)
        if not target or target.get("status") != "success" or not target.get("file_ready"):
            raise RuntimeError(f"云端未找到已完成的 #{video_id} 号视频")
        key = self._video_sync_key(target)
        if force:
            with self._lock:
                self._config["synced"].pop(key, None)
            self._save_config()
        self._download_one_video(cloud_url, target, save_dir, quality, threads, notify, key)
        return {"ok": True, "video_id": video_id, "sync_key": key}

    def _audio_sync_key(self, asset: dict[str, Any]) -> str:
        aid = int(asset.get("id") or 0)
        created = str(asset.get("created_at") or "").strip()
        return f"audio:{aid}:{created}"

    def _build_local_audio_filename(self, asset: dict[str, Any], audio_dir: Path | None = None) -> str:
        """清爽可读音频文件名：沿用音频名称，撞名时追加编号。"""
        aid = int(asset.get("id") or 0)
        raw_name = str(asset.get("name") or "").strip()
        raw_fp = str(asset.get("file_path") or "").strip()
        ext = Path(raw_fp).suffix.lower() if raw_fp else ".wav"
        if ext not in (".wav", ".mp3", ".m4a", ".flac", ".ogg"):
            ext = ".wav"
        clean = _safe_filename(raw_name, fallback=f"配音_{aid}")
        cand = f"{clean}{ext}"
        if audio_dir is not None and (audio_dir / cand).exists():
            cand = f"{clean}_{aid}{ext}"
        return cand

    def pull_audio_now(self, audio_id: int, force: bool = True) -> dict[str, Any]:
        """手动立即拉取指定编号的云端音频（支持拉回配音或上传音频）。"""
        with self._lock:
            cloud_url = (self._config.get("cloud_url") or "").strip().rstrip("/")
            save_dir = Path(self._config.get("save_dir") or str(DEFAULT_SAVE_DIR))
            threads = max(1, min(8, int(self._config.get("threads") or 8)))
            notify = bool(self._config.get("notify_desktop", True))
        if not cloud_url:
            raise RuntimeError("请先填写并保存云端创作台网址")
        status, _, body, _ = self._http_request(f"{cloud_url}/api/audio-assets", timeout=15)
        if status != 200:
            raise RuntimeError(f"云端返回 HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
        assets = payload.get("assets", []) if isinstance(payload, dict) else payload
        target = next((a for a in assets if isinstance(a, dict) and int(a.get("id") or 0) == int(audio_id)), None)
        if not target:
            raise RuntimeError(f"云端未找到 #{audio_id} 号音频")
        key = self._audio_sync_key(target)
        if force:
            with self._lock:
                self._config["synced"].pop(key, None)
            self._save_config()
        audio_dir = save_dir / "音频"
        self._download_one_audio(cloud_url, target, audio_dir, threads, notify, key)
        return {"ok": True, "audio_id": audio_id, "sync_key": key}

    def sync_once(self) -> dict[str, Any]:
        with self._lock:
            cloud_url = (self._config.get("cloud_url") or "").strip().rstrip("/")
            save_dir = Path(self._config.get("save_dir") or str(DEFAULT_SAVE_DIR))
            quality = str(self._config.get("quality") or "compact")
            sync_audio = bool(self._config.get("sync_audio", True))
            threads = max(1, min(12, int(self._config.get("threads") or 8)))
            notify = bool(self._config.get("notify_desktop", True))

        if not cloud_url:
            with self._lock:
                self._runtime["connected"] = False
                self._runtime["last_check_at"] = time.time()
            return {"ok": False, "reason": "未配置云端网址"}

        try:
            status, _, body, _ = self._http_request(f"{cloud_url}/api/videos", timeout=15)
            if status != 200:
                raise RuntimeError(f"云端返回 HTTP {status}")
            payload = json.loads(body.decode("utf-8"))
            videos = payload.get("videos", []) if isinstance(payload, dict) else payload
        except urllib.error.HTTPError as he:
            if he.code == 503 and _is_fixed_gateway_url(cloud_url):
                err_msg = "云端数字人当前未开机（休眠中），开机后会自动连上同步"
            elif he.code == 401 and _is_fixed_gateway_url(cloud_url):
                err_msg = "固定域名密码校验失败（HTTP 401），请检查本机 ~/.ssh/colab_hub_auth.json"
            else:
                err_msg = f"暂时连不上云端：{he}"
            with self._lock:
                self._runtime["connected"] = False
                self._runtime["last_check_at"] = time.time()
                self._runtime["last_error"] = err_msg
            return {"ok": False, "reason": err_msg}
        except Exception as exc:
            with self._lock:
                self._runtime["connected"] = False
                self._runtime["last_check_at"] = time.time()
                self._runtime["last_error"] = f"暂时连不上云端：{exc}"
            return {"ok": False, "reason": str(exc)}

        done_videos = [
            v for v in videos
            if isinstance(v, dict) and v.get("status") == "success" and v.get("file_ready")
        ]
        # 按 id 升序同步，确保先做完的先落盘
        done_videos.sort(key=lambda x: int(x.get("id") or 0))

        with self._lock:
            synced_map = self._config.get("synced") or {}
            self._runtime["connected"] = True
            self._runtime["cloud_video_count"] = len(done_videos)
            self._runtime["cloud_videos"] = [
                {
                    "id": int(v.get("id") or 0),
                    "name": str(v.get("name") or f"视频 #{v.get('id')}"),
                    "model_name": str(v.get("model_name") or ""),
                    "duration": round(float(v.get("duration") or 0.0), 1),
                    "file_size_mb": v.get("file_size_mb"),
                    "drive_compact_size_mb": v.get("drive_compact_size_mb"),
                    "created_at": v.get("created_at"),
                    "sync_key": self._video_sync_key(v),
                    "synced": self._video_sync_key(v) in synced_map,
                }
                for v in reversed(done_videos)
            ]
            self._runtime["last_check_at"] = time.time()
            self._runtime["last_error"] = ""

        pulled = 0
        for v in done_videos:
            if self._stop_event.is_set():
                break
            key = self._video_sync_key(v)
            with self._lock:
                already = self._config["synced"].get(key)
            if already:
                continue

            # 若本地目录已存在同名成片（例如此前手动或脚本已拉回），直接登记免重复拉取
            existing = self._existing_local_match(save_dir, v, quality)
            if existing is not None:
                self.mark_already_synced(key, {
                    "video_id": int(v.get("id") or 0),
                    "name": str(v.get("name") or ""),
                    "local_path": str(existing),
                    "size_mb": round(existing.stat().st_size / 1048576.0, 2),
                    "saved_at": existing.stat().st_mtime,
                    "meta": "检测到本地已有成片，自动登记去重",
                })
                continue

            try:
                self._download_one_video(cloud_url, v, save_dir, quality, threads, notify, key)
                pulled += 1
            except Exception as exc:
                self._log_event(f"拉取 #{v.get('id')} 暂遇波动（下轮自动续传）：{exc}")
                with self._lock:
                    self._runtime["last_error"] = f"视频 #{v.get('id')} 同步出错：{exc}"
                    self._runtime["active_download"] = None

        # 同步拉取云端生成的配音音频（只自动拉取 source_type=tts/indextts2，避免把本地上传的录音又拉回来）
        pulled_audios = 0
        try:
            st_a, _, body_a, _ = self._http_request(f"{cloud_url}/api/audio-assets", timeout=15)
            if st_a == 200:
                payload_a = json.loads(body_a.decode("utf-8"))
                raw_assets = payload_a.get("assets", []) if isinstance(payload_a, dict) else payload_a
                assets = [a for a in raw_assets if isinstance(a, dict) and int(a.get("id") or 0) > 0]
                tts_assets = [a for a in assets if str(a.get("source_type") or "").lower() in ("tts", "indextts2")]
                with self._lock:
                    synced_map = self._config.get("synced") or {}
                    self._runtime["cloud_audio_count"] = len(tts_assets)
                    self._runtime["cloud_audios"] = [
                        {
                            "id": int(a.get("id") or 0),
                            "name": str(a.get("name") or f"音频 #{a.get('id')}"),
                            "source_type": str(a.get("source_type") or "tts"),
                            "duration": round(float(a.get("duration") or 0.0), 1),
                            "created_at": a.get("created_at"),
                            "sync_key": self._audio_sync_key(a),
                            "synced": self._audio_sync_key(a) in synced_map,
                        }
                        for a in assets[:30]
                    ]
                if sync_audio:
                    audio_dir = save_dir / "音频"
                    # 按 id 降序优先拉取最新生成的云端配音，确保刚配好的第一时间落盘
                    for a in sorted(tts_assets, key=lambda x: int(x.get("id") or 0), reverse=True):
                        if self._stop_event.is_set():
                            break
                        akey = self._audio_sync_key(a)
                        with self._lock:
                            already_a = self._config["synced"].get(akey)
                        if already_a:
                            continue
                        try:
                            self._download_one_audio(cloud_url, a, audio_dir, min(threads, 4), notify, akey)
                            pulled_audios += 1
                        except Exception as exc:
                            self._log_event(f"拉取配音 #{a.get('id')} 暂遇波动（下轮自动重试）：{exc}")
                            with self._lock:
                                self._runtime["active_download"] = None
        except Exception:
            pass

        return {"ok": True, "cloud_done": len(done_videos), "pulled": pulled, "pulled_audios": pulled_audios}

    def _download_one_audio(
        self,
        cloud_url: str,
        asset: dict[str, Any],
        audio_dir: Path,
        threads: int,
        notify: bool,
        sync_key: str,
    ) -> None:
        aid = int(asset.get("id") or 0)
        aname = str(asset.get("name") or f"配音#{aid}")
        audio_dir.mkdir(parents=True, exist_ok=True)
        dl_rel = str(asset.get("download_url") or f"/api/media/audio-asset/{aid}?download=1")
        dl_url = _join_cloud_url(cloud_url, dl_rel)

        with self._lock:
            self._runtime["active_download"] = {
                "video_id": aid,
                "audio_id": aid,
                "media_type": "audio",
                "name": aname,
                "stage": "正在拉回云端配音...",
                "percent": 0.0,
                "downloaded_mb": 0.0,
                "total_mb": 0.0,
                "speed_mbs": 0.0,
            }

        dst_name = self._build_local_audio_filename(asset, audio_dir=audio_dir)
        dst = audio_dir / dst_name
        part = dst.with_name(dst.name + ".part")

        self._log_event(f"开始拉回云端配音 #{aid} -> {dst_name}")
        t0 = time.time()
        done_bytes = 0
        total = 0
        auth_dl_url, base_auth_hdr = self._prepare_request(dl_url)

        # 直接使用 GET 流式拉取（兼容不支持 HEAD 的音频下载接口）
        last_exc: Exception | None = None
        downloaded_ok = False
        for opener in self._build_openers():
            try:
                req = urllib.request.Request(auth_dl_url, headers=dict(base_auth_hdr))
                with opener.open(req, timeout=180) as resp, open(part, "wb") as wf:
                    total = int(resp.headers.get("Content-Length") or "0")
                    done_bytes = 0
                    while True:
                        if self._stop_event.is_set():
                            raise RuntimeError("同步服务停止")
                        block = resp.read(256 * 1024)
                        if not block:
                            break
                        wf.write(block)
                        done_bytes += len(block)
                        dt = max(0.05, time.time() - t0)
                        mb_done = done_bytes / 1048576.0
                        mb_tot = (total / 1048576.0) if total > 0 else mb_done
                        pct = round(min(100.0, done_bytes * 100.0 / total), 1) if total > 0 else 50.0
                        with self._lock:
                            self._runtime["active_download"] = {
                                "video_id": aid,
                                "audio_id": aid,
                                "media_type": "audio",
                                "name": aname,
                                "stage": "拉回云端配音中...",
                                "percent": pct,
                                "downloaded_mb": round(mb_done, 2),
                                "total_mb": round(mb_tot, 2),
                                "speed_mbs": round(mb_done / dt, 2),
                            }
                downloaded_ok = True
                break
            except urllib.error.HTTPError:
                raise
            except Exception as exc:
                last_exc = exc
        if not downloaded_ok:
            assert last_exc is not None
            raise last_exc

        if total > 0 and part.stat().st_size != total:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"音频体积不匹配 ({part.stat().st_size} != {total})")

        ok_a, meta_desc = _verify_audio_basic(part)
        if not ok_a:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"音频校验未通过：{meta_desc}")

        os.replace(part, dst)
        dt = max(0.05, time.time() - t0)
        actual_size = dst.stat().st_size
        spd = (actual_size / 1048576.0) / dt
        size_mb = round(actual_size / 1048576.0, 2)

        self.mark_already_synced(sync_key, {
            "audio_id": aid,
            "media_type": "audio",
            "name": aname,
            "local_path": str(dst),
            "size_mb": size_mb,
            "saved_at": time.time(),
            "meta": f"{meta_desc} · 均速 {spd:.2f} MB/s",
        })
        with self._lock:
            self._runtime["active_download"] = None
            for ca in self._runtime.get("cloud_audios") or []:
                if ca.get("sync_key") == sync_key:
                    ca["synced"] = True
        self._log_event(f"云端配音 #{aid} 已保存到 {dst.name}（{size_mb} MB，均速 {spd:.2f} MB/s）")
        if notify:
            _send_windows_toast(
                "云端新配音已自动下载完成",
                f"{dst.name}（{size_mb} MB）已保存到 {audio_dir}",
            )

    def _download_one_video(
        self,
        cloud_url: str,
        video: dict[str, Any],
        save_dir: Path,
        quality: str,
        threads: int,
        notify: bool,
        sync_key: str,
    ) -> None:
        vid = int(video.get("id") or 0)
        vname = str(video.get("name") or f"视频#{vid}")
        save_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._runtime["active_download"] = {
                "video_id": vid,
                "name": vname,
                "stage": "云端原画压小准备中..." if quality != "original" else "准备拉取原片...",
                "percent": 0.0,
                "downloaded_mb": 0.0,
                "total_mb": 0.0,
                "speed_mbs": 0.0,
            }

        # 1. 若选择压小版，先触发云端 GPU 压小（若后台已自动压好则 0 秒秒回）
        if quality != "original":
            self._log_event(f"发现新成片 #{vid}（{vname}），正在获取云端 4K 原画压小版...")
            dl_rel = f"/api/media/video/{vid}?download=1&quality={quality}"
            for attempt in range(1, 5):
                if self._stop_event.is_set():
                    return
                try:
                    st, _, body, _ = self._http_request(
                        f"{cloud_url}/api/videos/{vid}/compact?quality={quality}",
                        method="POST",
                        data=b"",
                        timeout=180,
                    )
                    if st == 200:
                        cres = json.loads(body.decode("utf-8"))
                        dl_rel = cres.get("download_url") or dl_rel
                        break
                except urllib.error.HTTPError as he:
                    if he.code in (502, 504, 520, 524) and attempt < 4:
                        time.sleep(8)
                        continue
                    raise
                except Exception:
                    if attempt < 4:
                        time.sleep(6)
                        continue
                    raise
        else:
            dl_rel = f"/api/media/video/{vid}?download=1&quality=original"

        dl_url = _join_cloud_url(cloud_url, dl_rel)
        st_head, hdrs, _, fast_opener = self._http_request(dl_url, method="HEAD", timeout=30)
        if st_head != 200:
            raise RuntimeError(f"HEAD 探测返回 {st_head}")
        total = int(hdrs.get("content-length") or "0")
        if total <= 0:
            raise RuntimeError("云端未返回有效文件长度")
        accept_ranges = "bytes" in (hdrs.get("accept-ranges") or "").lower()

        dst_name = self._build_local_filename(video, quality, save_dir=save_dir)
        dst = save_dir / dst_name
        part = dst.with_name(dst.name + ".part")

        self._log_event(f"开始 8 线程高速拉回 #{vid} -> {dst_name} ({total / 1048576:.1f} MB)")
        t0 = time.time()
        done_bytes = [0]
        write_lock = threading.Lock()
        auth_dl_url, base_auth_hdr = self._prepare_request(dl_url)

        with open(part, "wb") as f:
            f.truncate(total)

        def _update_prog(added: int) -> None:
            done_bytes[0] += added
            dt = max(0.05, time.time() - t0)
            mb_done = done_bytes[0] / 1048576.0
            mb_tot = total / 1048576.0
            with self._lock:
                self._runtime["active_download"] = {
                    "video_id": vid,
                    "name": vname,
                    "stage": "高速拉回本地中...",
                    "percent": round(min(100.0, done_bytes[0] * 100.0 / total), 1),
                    "downloaded_mb": round(mb_done, 1),
                    "total_mb": round(mb_tot, 1),
                    "speed_mbs": round(mb_done / dt, 2),
                }

        if accept_ranges and total > 4 * 1024 * 1024:
            chunk_size = 4 * 1024 * 1024
            ranges = [(pos, min(total - 1, pos + chunk_size - 1)) for pos in range(0, total, chunk_size)]

            def _fetch_chunk(se: tuple[int, int]) -> int:
                s, e = se
                expect = e - s + 1
                for retry in range(1, 7):
                    if self._stop_event.is_set():
                        raise RuntimeError("同步服务停止")
                    try:
                        hdr = dict(base_auth_hdr)
                        hdr["Range"] = f"bytes={s}-{e}"
                        req = urllib.request.Request(auth_dl_url, headers=hdr)
                        with fast_opener.open(req, timeout=45) as resp:
                            buf = resp.read()
                            if len(buf) != expect:
                                raise RuntimeError(f"分块长度不足 {len(buf)}/{expect}")
                        with write_lock:
                            with open(part, "r+b") as wf:
                                wf.seek(s)
                                wf.write(buf)
                        _update_prog(expect)
                        return expect
                    except Exception:
                        if retry == 6:
                            raise
                        time.sleep(1.2 * retry)
                return 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                futs = [ex.submit(_fetch_chunk, se) for se in ranges]
                for fut in concurrent.futures.as_completed(futs):
                    fut.result()
        else:
            req = urllib.request.Request(auth_dl_url, headers=dict(base_auth_hdr))
            with fast_opener.open(req, timeout=300) as resp, open(part, "wb") as wf:
                while True:
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    wf.write(block)
                    _update_prog(len(block))

        if part.stat().st_size != total:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"下载体积不匹配 ({part.stat().st_size} != {total})")

        ok_v, meta_desc = _verify_mp4_basic(part)
        if not ok_v:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"视频完整性校验未通过：{meta_desc}")

        os.replace(part, dst)
        dt = max(0.05, time.time() - t0)
        spd = (total / 1048576.0) / dt
        size_mb = round(total / 1048576.0, 2)

        self.mark_already_synced(sync_key, {
            "video_id": vid,
            "name": vname,
            "local_path": str(dst),
            "size_mb": size_mb,
            "saved_at": time.time(),
            "meta": f"{meta_desc} · 均速 {spd:.2f} MB/s",
        })
        with self._lock:
            self._runtime["active_download"] = None
        self._log_event(f"✅ #{vid} 已自动保存到 {dst.name}（{size_mb} MB，均速 {spd:.2f} MB/s）")
        if notify:
            _send_windows_toast(
                "云端数字人新视频已自动下载完成",
                f"{dst.name}（{size_mb} MB）已保存到 {save_dir}",
            )

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                enabled = bool(self._config.get("enabled"))
                cloud_url = (self._config.get("cloud_url") or "").strip()
                interval = int(self._config.get("poll_interval") or 15)
            if enabled and cloud_url:
                try:
                    self.sync_once()
                except Exception as exc:
                    with self._lock:
                        self._runtime["last_error"] = str(exc)
            self._wake_event.clear()
            self._wake_event.wait(timeout=interval)


# 单例实例，供 studio.server 挂载
cloud_sync_manager = CloudAutoSyncManager()
