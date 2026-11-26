from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.cloud_auto_sync import cloud_sync_manager
from . import system_ctrl
from .db import (
    CREATE_NO_WINDOW,
    clean_local_path,
    find_delivered_video,
    get_conn,
    init_studio_schema,
    probe_media_info,
    resolve_delivery_root,
    resolve_ffmpeg_exe,
    resolve_output_root,
    resolve_studio_assets_root,
    resolve_voice_root,
    sanitize_filename,
    upsert_job_meta,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STUDIO_API_KEY = os.getenv("HEYGEM_STUDIO_API_KEY", "").strip()


def _verify_api_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    if not STUDIO_API_KEY:
        return
    token = (x_api_key or "").strip()
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token != STUDIO_API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_studio_schema()
    resolve_studio_assets_root()
    cloud_sync_manager.start()
    try:
        yield
    finally:
        cloud_sync_manager.stop()


app = FastAPI(
    title="HeyGem Studio · 数字人一体化工作台",
    version="2.1.0",
    description=(
        "本地与服务器通用的可视化数字人创作工作台（融合 Duix.Avatar 渲染引擎与 IndexTTS-2 语音合成），"
        "同时提供可选的自动化接口。\n\n"
        "支持单条/批量口播制作、独立 IndexTTS-2 调音试听、实时帧级进度追踪、随时中断自愈、成品分类管理与下载。"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _friendly_tts_conn_error(exc: Exception) -> str:
    """将底层 socket/urlopen 连接异常转换为清晰易懂的中文提示，避免裸露 WinError 堆栈。"""
    msg = str(exc or "")
    low = msg.lower()
    if any(k in low for k in ("10061", "refused", "积极拒绝", "cannot connect", "connection reset", "timed out", "timeout", "urlopen error")):
        return "IndexTTS-2 语音引擎 (18180) 当前未启动或无响应，请先点击页面顶部「一键启动服务」后再试"
    return f"IndexTTS-2 语音引擎 (18180) 暂不可用：{msg}"


def _proxy_tts_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    url = f"{system_ctrl.TTS_BASE_URL}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("msg") or body
        except Exception:
            msg = body or str(exc)
        raise HTTPException(status_code=exc.code if 400 <= exc.code < 600 else 502, detail=msg)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_friendly_tts_conn_error(exc))


def _convert_to_wav(src: Path, dst: Path, sample_rate: int = 22050) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_ffmpeg_exe(),
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        str(dst),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=CREATE_NO_WINDOW,
    )
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size <= 0:
        raise RuntimeError(f"FFmpeg 音频转码失败：{res.stderr.strip()[:300]}")
    return dst


_IMAGE_ONLY_FORMATS = {"image2", "jpeg_pipe", "png_pipe", "bmp_pipe", "webp_pipe", "tiff_pipe", "ppm_pipe", "svg_pipe"}


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_local_client(request: Request) -> bool:
    """判断请求是否来自本机回环地址（远程局域网或经反向代理访问时禁止在宿主机桌面弹窗）。"""
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        first_ip = xff.split(",")[0].strip().lower()
        if first_ip and first_ip not in _LOCAL_HOSTS:
            return False
    x_real = (request.headers.get("x-real-ip") or "").strip().lower()
    if x_real and x_real not in _LOCAL_HOSTS:
        return False
    host = ((request.client.host if request.client else "") or "").strip().lower()
    return host in _LOCAL_HOSTS


def _require_local_client(request: Request, action_hint: str) -> None:
    if not _is_local_client(request):
        raise HTTPException(status_code=403, detail=f"当前为远程浏览器访问，暂不支持在宿主机{action_hint}，请改用网页上传或下载")


def _probe_media_streams(path: Path) -> tuple[bool, bool]:
    """用 ffprobe 检测媒体文件是否包含真实视频流（排除静态图/MP3 封面图）与音频流。"""
    from .db import resolve_ffprobe_exe

    try:
        res = subprocess.run(
            [
                resolve_ffprobe_exe(),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=format_name:stream=codec_type,codec_name:stream_disposition=attached_pic",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return False, False
        payload = json.loads(res.stdout)
        fmt_names = {x.strip().lower() for x in str((payload.get("format") or {}).get("format_name") or "").split(",") if x.strip()}
        if fmt_names and fmt_names.issubset(_IMAGE_ONLY_FORMATS):
            return False, False
        streams = payload.get("streams") or []
        has_video = any(
            s.get("codec_type") == "video" and int((s.get("disposition") or {}).get("attached_pic") or 0) == 0
            for s in streams
        )
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return has_video, has_audio
    except Exception:
        # ffprobe 不可用时放行交由后续 ffmpeg 判定
        return True, True


def _model_thumb_path(model_id: int) -> Path:
    thumbs_dir = resolve_studio_assets_root() / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    return thumbs_dir / f"model_{model_id}.jpg"


def _video_thumb_path(job_id: int) -> Path:
    thumbs_dir = resolve_studio_assets_root() / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    return thumbs_dir / f"video_{job_id}.jpg"


def _remove_model_thumbnail(model_id: int) -> None:
    try:
        _model_thumb_path(model_id).unlink(missing_ok=True)
    except OSError:
        pass


def _remove_video_thumbnail(job_id: int) -> None:
    try:
        _video_thumb_path(job_id).unlink(missing_ok=True)
    except OSError:
        pass


def _extract_video_thumbnail(video_path: Path, thumb: Path, label: str) -> Path | None:
    """为视频提取并缓存轻量 JPEG 封面图（解决 4K HEVC .mov 在浏览器中黑屏及成片封面展示）。"""
    if not video_path.is_file():
        return None
    try:
        v_mtime = video_path.stat().st_mtime
        if thumb.is_file() and thumb.stat().st_size > 0 and thumb.stat().st_mtime >= v_mtime:
            return thumb
    except OSError:
        pass

    tmp_thumb = thumb.with_name(f"_tmp_{label}_{uuid.uuid4().hex[:6]}.jpg")
    ffmpeg_exe = resolve_ffmpeg_exe()
    for seek_pos in ("0.5", "0"):
        try:
            res = subprocess.run(
                [
                    ffmpeg_exe,
                    "-y",
                    "-analyzeduration",
                    "2000000",
                    "-probesize",
                    "2000000",
                    "-ss",
                    seek_pos,
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(480,iw)':-2",
                    "-q:v",
                    "4",
                    str(tmp_thumb),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0 and tmp_thumb.is_file() and tmp_thumb.stat().st_size > 0:
                os.replace(tmp_thumb, thumb)
                return thumb
        except Exception:
            pass
        finally:
            tmp_thumb.unlink(missing_ok=True)
    return None


def _ensure_model_thumbnail(model_id: int, video_path: Path) -> Path | None:
    return _extract_video_thumbnail(video_path, _model_thumb_path(model_id), f"m{model_id}")


def _ensure_video_thumbnail(job_id: int, video_path: Path) -> Path | None:
    return _extract_video_thumbnail(video_path, _video_thumb_path(job_id), f"v{job_id}")


def _resolve_model_files(row: dict[str, Any]) -> dict[str, Any]:
    output_root = resolve_output_root()
    voice_root = resolve_voice_root()
    vp = str(row.get("video_path") or "").strip()
    ap = str(row.get("audio_path") or "").strip()

    video_abs = output_root / vp.lstrip("/\\") if vp else None
    audio_abs = None
    if ap:
        cand = Path(ap)
        audio_abs = cand if cand.is_absolute() else (voice_root / ap.lstrip("/\\"))

    video_exists = bool(video_abs and video_abs.is_file())
    audio_exists = bool(audio_abs and audio_abs.is_file())
    video_size_mb = 0.0
    video_mtime = 0
    if video_exists and video_abs:
        try:
            st = video_abs.stat()
            video_size_mb = round(st.st_size / (1024 * 1024), 2)
            video_mtime = int(st.st_mtime)
        except OSError:
            pass

    row["video_abs_path"] = str(video_abs) if video_abs else ""
    row["audio_abs_path"] = str(audio_abs) if audio_abs else ""
    row["video_exists"] = video_exists
    row["audio_exists"] = audio_exists
    row["has_voice"] = bool(row.get("voice_id") and audio_exists)
    row["video_size_mb"] = video_size_mb
    row["preview_url"] = f"/api/v1/media/model/{row['id']}" if video_exists else ""
    row["thumb_url"] = f"/api/v1/media/model/{row['id']}/thumb?v={video_mtime}" if video_exists else ""
    row["voice_preview_url"] = f"/api/v1/media/voice/{row['voice_id']}" if row.get("voice_id") and audio_exists else ""
    return row


def _enrich_job_row(row: dict[str, Any]) -> dict[str, Any]:
    vid = int(row["id"])
    output_root = resolve_output_root()
    code = str(row.get("code") or "").strip()
    file_path = str(row.get("file_path") or "").strip()

    temp_video_path: Path | None = None
    if file_path:
        cand = output_root / file_path.lstrip("/\\")
        if cand.is_file():
            temp_video_path = cand
    if temp_video_path is None and code:
        cand = output_root / f"{code}-r.mp4"
        if cand.is_file():
            temp_video_path = cand

    delivered_str = str(row.get("delivered_path") or "").strip()
    delivered_path: Path | None = Path(delivered_str) if delivered_str and Path(delivered_str).is_file() else None
    if delivered_path is None and row.get("status") == "success":
        delivered_path = find_delivered_video(vid, code, row.get("name"))
        if delivered_path:
            upsert_job_meta(vid, delivered_path=str(delivered_path))

    playable_path = delivered_path or temp_video_path
    size_mb = 0.0
    playable_mtime = 0
    if playable_path:
        try:
            st = playable_path.stat()
            size_mb = round(st.st_size / (1024 * 1024), 2)
            playable_mtime = int(st.st_mtime)
        except OSError:
            pass

    model_video_rel = str(row.get("model_video_path") or "").strip()
    model_video_abs = (output_root / model_video_rel.lstrip("/\\")) if model_video_rel else None
    model_video_exists = bool(model_video_abs and model_video_abs.is_file())
    thumb_url = ""
    if playable_path:
        thumb_url = f"/api/v1/media/video/{vid}/thumb?v={playable_mtime}"
    elif row.get("model_id") and model_video_exists:
        thumb_url = f"/api/v1/media/model/{row['model_id']}/thumb"

    tts_params = {}
    try:
        tts_params = json.loads(row.get("tts_params_json") or "{}")
    except Exception:
        tts_params = {}

    recent_logs = []
    try:
        recent_logs = json.loads(row.get("recent_logs_json") or "[]")
    except Exception:
        recent_logs = []

    hb = float(row.get("heartbeat_at") or 0.0)
    hb_age = round(max(0.0, time.time() - hb), 1) if hb > 0 else None

    status = str(row.get("status") or "waiting")
    phase = str(row.get("phase") or "")
    if not phase:
        phase = {
            "waiting": "queued",
            "pending": "face2face_rendering",
            "success": "success",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(status, status)

    progress_float = float(row.get("progress_float") or 0.0)
    db_prog = float(row.get("progress") or 0)
    if status == "success":
        progress_float = 100.0
    elif progress_float <= 0 and db_prog > 0:
        progress_float = db_prog

    source_audio_path = str(row.get("source_audio_path") or "").strip()
    if not source_audio_path and row.get("audio_path"):
        cand_a = output_root / str(row["audio_path"]).lstrip("/\\")
        source_audio_path = str(cand_a)

    return {
        "id": vid,
        "name": row.get("name") or f"视频_{vid}",
        "status": status,
        "phase": phase,
        "stage_label": row.get("stage_label") or row.get("message") or "",
        "message": row.get("message") or "",
        "progress": int(round(progress_float)),
        "progress_float": round(progress_float, 1),
        "rendered_frames": int(row.get("rendered_frames") or 0),
        "total_frames": int(row.get("total_frames") or 0),
        "render_fps": float(row.get("render_fps") or 0.0),
        "eta_seconds": int(row.get("eta_seconds") if row.get("eta_seconds") is not None else -1),
        "heartbeat_at": hb,
        "heartbeat_age_seconds": hb_age,
        "model_id": row.get("model_id"),
        "model_name": row.get("model_name") or "",
        "voice_id": row.get("voice_id"),
        "category_id": row.get("category_id"),
        "category_name": row.get("category_name") or "未分类",
        "category_color": row.get("category_color") or "#64748b",
        "text_content": row.get("text_content") or "",
        "audio_path": row.get("audio_path") or "",
        "code": code,
        "duration": row.get("duration"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("meta_updated_at") or row.get("created_at"),
        "retry_count": int(row.get("retry_count") or 0),
        "max_retries": int(row.get("max_retries") or 3),
        "error_code": row.get("error_code") or "",
        "error_detail": row.get("error_detail") or "",
        "tts_params": tts_params,
        "recent_logs": recent_logs,
        "webhook_url": row.get("webhook_url") or "",
        "paths": {
            "delivered_path": str(delivered_path) if delivered_path else "",
            "temp_video_path": str(temp_video_path) if temp_video_path else "",
            "playable_path": str(playable_path) if playable_path else "",
            "source_audio_path": source_audio_path,
            "model_video_path": str(output_root / str(row.get("model_video_path") or "").lstrip("/\\")) if row.get("model_video_path") else "",
        },
        "file_exists": playable_path is not None,
        "file_size_mb": size_mb,
        "download_url": f"/api/v1/jobs/{vid}/download" if playable_path else "",
        "stream_url": f"/api/v1/media/video/{vid}" if playable_path else "",
        "thumb_url": thumb_url,
    }


def _fetch_job_by_id(job_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT v.*,
                   fm.name AS model_name,
                   fm.video_path AS model_video_path,
                   m.category_id,
                   c.name AS category_name,
                   c.color AS category_color,
                   m.phase, m.stage_label, m.progress_float,
                   m.rendered_frames, m.total_frames, m.render_fps, m.eta_seconds,
                   m.heartbeat_at, m.tts_params_json, m.source_audio_path, m.delivered_path,
                   m.retry_count, m.max_retries, m.error_code, m.error_detail,
                   m.webhook_url, m.recent_logs_json, m.updated_at AS meta_updated_at
            FROM video v
            LEFT JOIN f2f_model fm ON fm.id = v.model_id
            LEFT JOIN studio_job_meta m ON m.video_id = v.id
            LEFT JOIN studio_categories c ON c.id = m.category_id
            WHERE v.id = ?
            """,
            (job_id,),
        ).fetchone()
        return _enrich_job_row(dict(row)) if row else None
    finally:
        conn.close()


# ==================== Pydantic Request Schemas ====================

class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    color: str = Field(default="#3b82f6", max_length=20)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    color: str | None = Field(default=None, max_length=20)


class ModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    group_id: int | None = None


class PresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class TtsSynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice_id: int | None = None
    reference_audio: str | None = None
    tts_params: dict[str, Any] = Field(default_factory=dict)
    preset_id: int | None = None
    name: str | None = None
    category_id: int | None = None


class JobCreateItem(BaseModel):
    model_id: int = Field(..., description="数字人模特 ID")
    name: str | None = Field(default=None, description="作品标题，留空自动生成")
    text: str | None = Field(default=None, description="口播文案（文本配音模式）")
    voice_id: int | None = Field(default=None, description="指定音色 ID，留空默认使用模特自带音色")
    tts_params: dict[str, Any] = Field(default_factory=dict, description="IndexTTS2 覆盖参数（情感/语速/步数/版本等）")
    preset_id: int | None = Field(default=None, description="引用已保存的 IndexTTS2 预设 ID")
    audio_asset_id: int | None = Field(default=None, description="引用音频库资产 ID（音频直驱模式）")
    audio_path: str | None = Field(default=None, description="直接指定本机音频文件绝对路径（音频直驱模式）")
    category_id: int | None = Field(default=None, description="作品分类 ID")
    category_name: str | None = Field(default=None, description="作品分类名称（不存在时自动创建，方便 AI 自动化调用）")
    webhook_url: str | None = Field(default=None, description="任务完成或失败时的回调 Webhook URL")
    max_retries: int = Field(default=3, ge=0, le=10)


class BatchJobsCreateRequest(BaseModel):
    items: list[JobCreateItem] = Field(..., min_length=1)


class JobUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category_id: int | None = None


class AudioUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category_id: int | None = None


class LocalAudioImportRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, description="本机音频文件绝对路径列表（自动清洗首尾引号）")
    category_id: int | None = None


class RecoverRequest(BaseModel):
    restart_render_container: bool = False
    requeue_stuck: bool = True


class TtsModeRequest(BaseModel):
    mode: str = Field(..., pattern="^(index-tts2|fish-speech)$")


class OpenPathRequest(BaseModel):
    path: str


class PickFileRequest(BaseModel):
    kind: str = Field(default="video", pattern="^(video|audio)$")
    multi: bool = False
    initial_dir: str | None = None


# ==================== System & Health Endpoints ====================

@app.get("/api/v1/ping", tags=["系统状态"])
def api_ping() -> dict[str, Any]:
    return {"status": "ok", "service": "heygem-studio", "ts": int(time.time())}


@app.get("/api/v1/health", tags=["系统状态"], dependencies=[Depends(_verify_api_key)])
def api_health(request: Request, force: bool = Query(False)) -> dict[str, Any]:
    raw = system_ctrl.get_system_health(force=force)
    res = dict(raw)
    server_tuple = request.scope.get("server")
    scope_port = int(server_tuple[1]) if (isinstance(server_tuple, tuple) and len(server_tuple) >= 2 and server_tuple[1]) else None
    base_access = raw.get("access")
    if scope_port and scope_port != 80 and base_access and int(base_access.get("port") or 0) != scope_port and not os.getenv("HEYGEM_STUDIO_PORT"):
        access = dict(system_ctrl.get_network_access_info(port=scope_port))
    else:
        access = dict(base_access or system_ctrl.get_network_access_info())
    is_local = _is_local_client(request)
    access["client_is_local"] = is_local
    access["client_mode"] = "local" if is_local else "remote"
    access["desktop_tools_supported"] = (os.name == "nt")
    res["access"] = access
    return res


@app.get("/api/v1/logs", tags=["系统状态"], dependencies=[Depends(_verify_api_key)])
def api_logs(source: str = Query("queue", pattern="^(queue|tts|docker|guard)$"), tail: int = Query(120, ge=10, le=500)):
    return system_ctrl.read_service_logs(source=source, tail=tail)


@app.post("/api/v1/system/start", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_system_start() -> dict[str, Any]:
    return system_ctrl.self_heal_recover(restart_render_container=False, requeue_stuck=False)


@app.post("/api/v1/system/stop", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_system_stop() -> dict[str, Any]:
    return system_ctrl.stop_background_services()


@app.post("/api/v1/system/recover", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_system_recover(req: RecoverRequest | None = Body(default=None)) -> dict[str, Any]:
    effective = req or RecoverRequest()
    return system_ctrl.self_heal_recover(
        restart_render_container=effective.restart_render_container,
        requeue_stuck=effective.requeue_stuck,
    )


@app.post("/api/v1/queue/pause", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_queue_pause() -> dict[str, Any]:
    paused = system_ctrl.set_queue_paused(True)
    return {"ok": True, "paused": paused, "message": "队列已暂停（正在渲染的任务会继续完成，暂不派发新任务）"}


@app.post("/api/v1/queue/resume", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_queue_resume() -> dict[str, Any]:
    paused = system_ctrl.set_queue_paused(False)
    return {"ok": True, "paused": paused, "message": "队列已恢复运行"}


@app.post("/api/v1/tts/mode", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_tts_mode(req: TtsModeRequest) -> dict[str, Any]:
    mode = system_ctrl.set_tts_mode(req.mode)
    return {"ok": True, "mode": mode}


@app.post("/api/v1/system/open-path", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_open_path(request: Request, req: OpenPathRequest) -> dict[str, Any]:
    _require_local_client(request, "打开文件夹")
    ok, msg = system_ctrl.open_path_in_explorer(req.path)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/v1/system/open-media", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_open_media(request: Request, req: OpenPathRequest) -> dict[str, Any]:
    """用 Windows 本机默认播放器打开视频/音频文件（仅限本机访问且严格白名单后缀，用于播放浏览器不支持解码的 HEVC .mov）。"""
    _require_local_client(request, "调用本机播放器")
    cleaned = clean_local_path(req.path)
    if not cleaned:
        raise HTTPException(status_code=400, detail="媒体路径为空")
    p = Path(cleaned)
    if p.suffix.lower() not in (system_ctrl.VIDEO_EXTS | system_ctrl.AUDIO_EXTS):
        raise HTTPException(status_code=400, detail=f"仅支持打开音视频文件，不支持后缀：{p.suffix or '(无后缀)'}")
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"媒体文件不存在：{p}")
    ok, msg = system_ctrl.open_media_in_player(str(p))
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/v1/system/pick-file", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_pick_file(request: Request, req: PickFileRequest | None = Body(default=None)) -> dict[str, Any]:
    """呼出 Windows 本机文件选择弹窗，直接返回选中文件的本机路径（仅限本机访问，大视频无需经浏览器上传）。"""
    _require_local_client(request, "弹出文件选择窗口")
    effective = req or PickFileRequest()
    res = system_ctrl.pick_local_files(
        kind=effective.kind,
        multi=effective.multi,
        initial_dir=effective.initial_dir,
    )
    if not res.get("ok"):
        code = 409 if res.get("busy") else 500
        raise HTTPException(status_code=code, detail=res.get("detail") or "无法打开文件选择窗口")
    return res


# ==================== Categories & Model Groups ====================

@app.get("/api/v1/categories", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_list_categories() -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM studio_job_meta m JOIN video v ON v.id = m.video_id WHERE m.category_id = c.id) AS video_count,
                   (SELECT COUNT(*) FROM studio_audio_assets a WHERE a.category_id = c.id) AS audio_count
            FROM studio_categories c
            ORDER BY c.sort_order ASC, c.id ASC
            """
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/categories", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_create_category(req: CategoryCreate) -> dict[str, Any]:
    clean_name = req.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")
    conn = get_conn()
    try:
        now_ms = int(time.time() * 1000)
        cur = conn.execute(
            "INSERT INTO studio_categories (name, color, sort_order, created_at) VALUES (?, ?, 0, ?)",
            (clean_name, req.color.strip() or "#3b82f6", now_ms),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM studio_categories WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"item": dict(row)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"创建分类失败（可能已存在同名分类）：{exc}")
    finally:
        conn.close()


@app.patch("/api/v1/categories/{cat_id}", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_update_category(cat_id: int, req: CategoryUpdate) -> dict[str, Any]:
    conn = get_conn()
    try:
        fields = {}
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="分类名称不能为空")
            fields["name"] = clean_name
        if req.color is not None:
            fields["color"] = req.color.strip()
        if not fields:
            raise HTTPException(status_code=400, detail="无修改字段")
        sql = f"UPDATE studio_categories SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?"
        try:
            conn.execute(sql, list(fields.values()) + [cat_id])
            conn.commit()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"修改分类失败（可能存在同名分类）：{exc}")
        row = conn.execute("SELECT * FROM studio_categories WHERE id=?", (cat_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分类不存在")
        return {"item": dict(row)}
    finally:
        conn.close()


@app.delete("/api/v1/categories/{cat_id}", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_delete_category(cat_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("UPDATE studio_job_meta SET category_id=NULL WHERE category_id=?", (cat_id,))
        conn.execute("UPDATE studio_audio_assets SET category_id=NULL WHERE category_id=?", (cat_id,))
        conn.execute("DELETE FROM studio_categories WHERE id=?", (cat_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/model-groups", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_list_model_groups() -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT g.*, (SELECT COUNT(*) FROM f2f_model m WHERE m.group_id = g.id) AS model_count
            FROM model_group g
            ORDER BY g.sort_order ASC, g.id ASC
            """
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/model-groups", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_create_model_group(req: CategoryCreate) -> dict[str, Any]:
    clean_name = req.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="模特分组名称不能为空")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO model_group (name, sort_order, created_at) VALUES (?, 0, ?)",
            (clean_name, int(time.time() * 1000)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM model_group WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"item": dict(row)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"创建模特分组失败：{exc}")
    finally:
        conn.close()


@app.delete("/api/v1/model-groups/{group_id}", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_delete_model_group(group_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("UPDATE f2f_model SET group_id=NULL WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM model_group WHERE id=?", (group_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ==================== Models & Voices ====================

@app.get("/api/v1/models", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_list_models(group_id: str | None = None, q: str | None = None) -> dict[str, Any]:
    conn = get_conn()
    try:
        sql = """
            SELECT m.*, g.name AS group_name
            FROM f2f_model m
            LEFT JOIN model_group g ON g.id = m.group_id
            WHERE 1=1
        """
        params: list[Any] = []
        if group_id == "ungrouped":
            sql += " AND m.group_id IS NULL"
        elif group_id and group_id.isdigit():
            sql += " AND m.group_id = ?"
            params.append(int(group_id))
        if q and q.strip():
            sql += " AND m.name LIKE ?"
            params.append(f"%{q.strip()}%")
        sql += " ORDER BY (SELECT COUNT(*) FROM video v WHERE v.model_id = m.id) DESC, m.id DESC"
        rows = conn.execute(sql, params).fetchall()
        items = [_resolve_model_files(dict(r)) for r in rows]
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@app.get("/api/v1/models/discover-local", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_discover_local_models(directory: str | None = None) -> dict[str, Any]:
    """轻量扫描本机预设拍摄目录中的视频文件，方便新手一键导入为数字人。"""
    discovered = system_ctrl.discover_local_model_videos(directory=directory)
    existing_valid_names: set[str] = set()
    conn = get_conn()
    try:
        output_root = resolve_output_root()
        for r in conn.execute("SELECT name, video_path FROM f2f_model").fetchall():
            vp = str(r["video_path"] or "").strip()
            if vp and (output_root / vp.lstrip("/\\")).is_file():
                existing_valid_names.add(str(r["name"] or "").strip().lower())
    finally:
        conn.close()

    for it in discovered["items"]:
        it["already_imported"] = str(it.get("name") or "").strip().lower() in existing_valid_names
    return discovered


@app.post("/api/v1/models/cleanup-missing", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_cleanup_missing_models() -> dict[str, Any]:
    """一键清理所有底模视频文件已丢失的历史失效模特记录（保留历史成片与声音库，不删任何活跃任务对应模特）。"""
    output_root = resolve_output_root()
    deleted_ids: list[int] = []
    skipped_active_ids: list[int] = []

    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, video_path FROM f2f_model").fetchall()
        for r in rows:
            mid = int(r["id"])
            vp = str(r["video_path"] or "").strip()
            if vp and (output_root / vp.lstrip("/\\")).is_file():
                continue
            active = conn.execute(
                "SELECT COUNT(*) FROM video WHERE model_id=? AND status IN ('pending', 'waiting')",
                (mid,),
            ).fetchone()[0]
            if active:
                skipped_active_ids.append(mid)
                continue
            conn.execute("DELETE FROM f2f_model WHERE id=?", (mid,))
            deleted_ids.append(mid)
        conn.commit()
    finally:
        conn.close()

    for mid in deleted_ids:
        _remove_model_thumbnail(mid)

    system_ctrl.invalidate_health_cache()
    return {
        "ok": True,
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "skipped_active_ids": skipped_active_ids,
    }


@app.post("/api/v1/models", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_create_model(
    name: str = Form(default=""),
    group_id: int | None = Form(default=None),
    local_video_path: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """导入新数字人模特（支持网页上传 MP4/MOV 或直接指定本机视频路径），自动提取参考音频并注册音色；无音轨视频也允许导入形象。"""
    cleaned_local = clean_local_path(local_video_path)
    if file is None and not cleaned_local:
        raise HTTPException(status_code=400, detail="请上传底模视频文件或填写本机视频路径")

    output_root = resolve_output_root()
    voice_root = resolve_voice_root()
    origin_dir = voice_root / "origin_audio"
    output_root.mkdir(parents=True, exist_ok=True)
    origin_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d%H%M%S") + f"{int((time.time() % 1) * 1000):03d}"
    fallback_stem = ""
    probed_local: tuple[bool, bool] | None = None

    try:
        if file is not None:
            orig_p = Path(file.filename or "model.mp4")
            fallback_stem = orig_p.stem
            ext = orig_p.suffix.lower() or ".mp4"
            if ext not in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
                raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
            video_filename = f"{stamp}{ext}"
            target_video = output_root / video_filename
            with open(target_video, "wb") as out_fh:
                shutil.copyfileobj(file.file, out_fh)
        else:
            src = Path(cleaned_local)
            if not src.is_file():
                raise HTTPException(status_code=404, detail=f"本机视频文件不存在：{src}")
            fallback_stem = src.stem
            ext = src.suffix.lower() or ".mp4"
            if ext not in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
                raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
            # 本机文件先探测再复制，避免把数 GB 损坏文件白拷一遍再报错
            probed_local = _probe_media_streams(src)
            if not probed_local[0]:
                raise HTTPException(status_code=400, detail="所选文件不是有效的视频或文件已损坏，请换一个清晰的 MP4/MOV 视频")
            video_filename = f"{stamp}{ext}"
            target_video = output_root / video_filename
            shutil.copy2(src, target_video)
    except HTTPException:
        raise
    except Exception as exc:
        if "target_video" in locals():
            target_video.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入视频文件失败：{exc}") from exc

    final_name = (name or "").strip() or sanitize_filename(fallback_stem, f"人物_{stamp[-6:]}")
    if not final_name:
        target_video.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="人物名称不能为空")

    # 上传文件落盘后探测视频有效性及是否含音轨
    has_video, has_audio = probed_local if probed_local is not None else _probe_media_streams(target_video)
    if not has_video:
        target_video.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="所选文件不是有效的视频或文件已损坏，请换一个清晰的 MP4/MOV 视频")

    now_ms = int(time.time() * 1000)
    voice_id: int | None = None
    audio_rel_db = ""
    raw_wav = origin_dir / f"{stamp}.wav"
    fmt_wav = origin_dir / f"format_{stamp}.wav"

    if has_audio:
        try:
            _convert_to_wav(target_video, raw_wav, sample_rate=22050)
            shutil.copy2(raw_wav, fmt_wav)
            audio_rel_db = f"origin_audio\\{stamp}.wav"
        except Exception as exc:
            target_video.unlink(missing_ok=True)
            raw_wav.unlink(missing_ok=True)
            fmt_wav.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"从底模视频提取参考音色失败：{exc}")

    conn = get_conn()
    try:
        if has_audio and audio_rel_db:
            origin_rel = f"origin_audio/{stamp}.wav"
            fmt_abs_str = str(fmt_wav).replace("\\", "/")
            v_cur = conn.execute(
                """
                INSERT INTO voice (origin_audio_path, lang, asr_format_audio_url, reference_audio_text, created_at)
                VALUES (?, 'zh', ?, '', ?)
                """,
                (origin_rel, fmt_abs_str, now_ms),
            )
            voice_id = int(v_cur.lastrowid)

        m_cur = conn.execute(
            """
            INSERT INTO f2f_model (name, video_path, audio_path, voice_id, created_at, group_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (final_name, video_filename, audio_rel_db, voice_id, now_ms, group_id),
        )
        model_id = int(m_cur.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT m.*, g.name AS group_name FROM f2f_model m LEFT JOIN model_group g ON g.id = m.group_id WHERE m.id=?",
            (model_id,),
        ).fetchone()
        system_ctrl.invalidate_health_cache()
        return {"item": _resolve_model_files(dict(row))}
    except Exception:
        conn.rollback()
        target_video.unlink(missing_ok=True)
        raw_wav.unlink(missing_ok=True)
        fmt_wav.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


@app.post("/api/v1/models/{model_id}/relink", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_relink_model_video(
    model_id: int,
    local_video_path: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """重新关联或上传缺失的模特底模视频文件，修复底模缺失问题。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM f2f_model WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模特不存在")
        model = dict(row)
    finally:
        conn.close()

    cleaned_local = clean_local_path(local_video_path)
    if file is None and not cleaned_local:
        raise HTTPException(status_code=400, detail="请上传底模视频或填写本机路径")

    output_root = resolve_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    old_name = Path(str(model.get("video_path") or "").strip()).name
    allowed_exts = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    has_audio = False

    if file is not None:
        ext = Path(file.filename or "model.mp4").suffix.lower() or ".mp4"
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
        new_name = old_name if (old_name and Path(old_name).suffix.lower() == ext) else f"{int(time.time()*1000)}{ext}"
        target = output_root / new_name
        tmp_target = output_root / f"_relink_tmp_{uuid.uuid4().hex[:8]}{ext}"
        try:
            with open(tmp_target, "wb") as out_fh:
                shutil.copyfileobj(file.file, out_fh)
            has_video, has_audio = _probe_media_streams(tmp_target)
            if not has_video:
                raise HTTPException(status_code=400, detail="所选文件不是有效的视频或文件已损坏")
            os.replace(tmp_target, target)
        except Exception:
            tmp_target.unlink(missing_ok=True)
            raise
    else:
        src = Path(cleaned_local)
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"本机文件不存在：{src}")
        ext = src.suffix.lower() or ".mp4"
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
        # 本机文件先探测再复制，避免损坏大文件白拷贝
        has_video, has_audio = _probe_media_streams(src)
        if not has_video:
            raise HTTPException(status_code=400, detail="所选文件不是有效的视频或文件已损坏")
        new_name = old_name if (old_name and Path(old_name).suffix.lower() == ext) else f"{int(time.time()*1000)}{ext}"
        target = output_root / new_name
        tmp_target = output_root / f"_relink_tmp_{uuid.uuid4().hex[:8]}{ext}"
        try:
            shutil.copy2(src, tmp_target)
            os.replace(tmp_target, target)
        except Exception:
            tmp_target.unlink(missing_ok=True)
            raise

    _remove_model_thumbnail(model_id)

    # 补提参考音色：1) 原已绑定 audio_path 但文件缺失 -> 补提；2) 原为静音无音色 -> 新视频有声时自动建音色并绑定
    voice_root = resolve_voice_root()
    origin_dir = voice_root / "origin_audio"
    origin_dir.mkdir(parents=True, exist_ok=True)
    audio_rel = str(model.get("audio_path") or "").strip()
    new_audio_rel_db: str | None = None
    new_voice_origin_rel: str | None = None
    new_voice_fmt_abs: str | None = None

    if has_audio:
        if audio_rel:
            audio_abs = Path(audio_rel) if Path(audio_rel).is_absolute() else (voice_root / audio_rel.lstrip("/\\"))
            if not audio_abs.is_file():
                try:
                    _convert_to_wav(target, audio_abs, sample_rate=22050)
                    fmt_sibling = audio_abs.with_name(f"format_{audio_abs.name}")
                    if not fmt_sibling.is_file():
                        shutil.copy2(audio_abs, fmt_sibling)
                except Exception:
                    pass
        if not model.get("voice_id") or not audio_rel:
            stamp = time.strftime("%Y%m%d%H%M%S") + f"{int((time.time() % 1) * 1000):03d}"
            raw_wav = origin_dir / f"{stamp}.wav"
            fmt_wav = origin_dir / f"format_{stamp}.wav"
            try:
                _convert_to_wav(target, raw_wav, sample_rate=22050)
                shutil.copy2(raw_wav, fmt_wav)
                new_audio_rel_db = f"origin_audio\\{stamp}.wav"
                new_voice_origin_rel = f"origin_audio/{stamp}.wav"
                new_voice_fmt_abs = str(fmt_wav).replace("\\", "/")
            except Exception:
                raw_wav.unlink(missing_ok=True)
                fmt_wav.unlink(missing_ok=True)

    conn = get_conn()
    try:
        if new_audio_rel_db and new_voice_origin_rel and new_voice_fmt_abs:
            v_cur = conn.execute(
                """
                INSERT INTO voice (origin_audio_path, lang, asr_format_audio_url, reference_audio_text, created_at)
                VALUES (?, 'zh', ?, '', ?)
                """,
                (new_voice_origin_rel, new_voice_fmt_abs, int(time.time() * 1000)),
            )
            new_vid = int(v_cur.lastrowid)
            conn.execute(
                "UPDATE f2f_model SET video_path=?, audio_path=?, voice_id=? WHERE id=?",
                (new_name, new_audio_rel_db, new_vid, model_id),
            )
        else:
            conn.execute("UPDATE f2f_model SET video_path=? WHERE id=?", (new_name, model_id))

        # 若更换了不同后缀的新文件，且没有其他模特引用旧视频文件，则安全清理旧孤儿文件
        if old_name and old_name != new_name:
            other_refs = conn.execute(
                "SELECT COUNT(*) FROM f2f_model WHERE id<>? AND video_path=?",
                (model_id, old_name),
            ).fetchone()[0]
            if not other_refs:
                try:
                    (output_root / old_name).unlink(missing_ok=True)
                except OSError:
                    pass

        conn.commit()
        refreshed = conn.execute(
            "SELECT m.*, g.name AS group_name FROM f2f_model m LEFT JOIN model_group g ON g.id = m.group_id WHERE m.id=?",
            (model_id,),
        ).fetchone()
        system_ctrl.invalidate_health_cache()
        return {"item": _resolve_model_files(dict(refreshed))}
    finally:
        conn.close()


@app.patch("/api/v1/models/{model_id}", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_update_model(model_id: int, req: ModelUpdate) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM f2f_model WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模特不存在")
        updates: dict[str, Any] = {}
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="模特名称不能为空")
            updates["name"] = clean_name
        if "group_id" in req.model_fields_set:
            updates["group_id"] = None if (req.group_id is None or req.group_id <= 0) else req.group_id
        if updates:
            sql = f"UPDATE f2f_model SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?"
            conn.execute(sql, list(updates.values()) + [model_id])
            conn.commit()
        refreshed = conn.execute(
            "SELECT m.*, g.name AS group_name FROM f2f_model m LEFT JOIN model_group g ON g.id = m.group_id WHERE m.id=?",
            (model_id,),
        ).fetchone()
        return {"item": _resolve_model_files(dict(refreshed))}
    finally:
        conn.close()


@app.delete("/api/v1/models/{model_id}", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_delete_model(model_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM video WHERE model_id=? AND status IN ('pending', 'waiting')",
            (model_id,),
        ).fetchone()[0]
        if active:
            raise HTTPException(status_code=409, detail=f"该模特仍有 {active} 个正在排队/渲染中的任务，请先中断后再删除")
        conn.execute("DELETE FROM f2f_model WHERE id=?", (model_id,))
        conn.commit()
        _remove_model_thumbnail(model_id)
        system_ctrl.invalidate_health_cache()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/voices", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_list_voices() -> dict[str, Any]:
    voice_root = resolve_voice_root()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT v.*, m.id AS model_id, m.name AS model_name
            FROM voice v
            LEFT JOIN f2f_model m ON m.voice_id = v.id
            ORDER BY v.id DESC
            """
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            abs_p = _resolve_voice_file(d, voice_root)
            d["abs_path"] = str(abs_p) if abs_p else ""
            d["exists"] = bool(abs_p and abs_p.is_file())
            d["display_name"] = d["model_name"] or (Path(d["origin_audio_path"] or f"voice_{d['id']}").stem)
            d["preview_url"] = f"/api/v1/media/voice/{d['id']}" if d["exists"] else ""
            items.append(d)
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


def _resolve_voice_file(voice_row: dict[str, Any], voice_root: Path | None = None) -> Path | None:
    vr = voice_root or resolve_voice_root()
    candidates: list[Path] = []
    origin_dir = vr / "origin_audio"
    for raw in [voice_row.get("asr_format_audio_url"), voice_row.get("origin_audio_path")]:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        first = text.split("|||")[0].strip()
        if first.lower().startswith("/code/data/"):
            first = str(vr / first[len("/code/data/"):])
        p = Path(first)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(vr / first.lstrip("/"))
            candidates.append(origin_dir / p.name)
        base = p.name
        if base:
            candidates.append(origin_dir / base)
            if base.startswith("format_denoise_"):
                candidates.append(origin_dir / base.replace("format_denoise_", "format_", 1))
            elif base.startswith("format_"):
                candidates.append(origin_dir / base.replace("format_", "format_denoise_", 1))
            else:
                candidates.append(origin_dir / f"format_{base}")
                candidates.append(origin_dir / f"format_denoise_{base}")
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0] if candidates else None


def _load_adapter_config_fallback() -> dict[str, Any]:
    cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _resolve_emo_audio_file(raw_path: str | None) -> Path | None:
    """解析情感参考音频路径（自动清洗 Windows 复制路径自带引号，兼容共享音色目录相对路径）。"""
    cleaned = clean_local_path(raw_path).replace("\\", "/")
    if not cleaned:
        return None
    vr = resolve_voice_root()
    mapped = str(vr / cleaned[len("/code/data/"):]) if cleaned.lower().startswith("/code/data/") else cleaned
    p = Path(mapped)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append((Path.cwd() / p).resolve())
        candidates.append((vr / cleaned.lstrip("/")).resolve())
    if p.name:
        candidates.append((vr / "origin_audio" / p.name).resolve())
    for c in candidates:
        if c.is_file():
            return c
    return None


@app.post("/api/v1/voices", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_upload_voice(
    name: str = Form(default=""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """上传新的自定义参考音色音频，自动格式化为 22050Hz 单声道 WAV 并入库。"""
    voice_root = resolve_voice_root()
    origin_dir = voice_root / "origin_audio"
    origin_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S") + f"{int((time.time() % 1) * 1000):03d}"
    safe_tag = sanitize_filename(name or Path(file.filename or "voice").stem, "voice")[:30]
    tmp_src = origin_dir / f"_tmp_{stamp}{Path(file.filename or '.wav').suffix or '.wav'}"
    fmt_wav = origin_dir / f"format_{stamp}_{safe_tag}.wav"
    try:
        with open(tmp_src, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        _convert_to_wav(tmp_src, fmt_wav, sample_rate=22050)
    finally:
        tmp_src.unlink(missing_ok=True)

    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO voice (origin_audio_path, lang, asr_format_audio_url, reference_audio_text, created_at)
            VALUES (?, 'zh', ?, ?, ?)
            """,
            (f"origin_audio/{fmt_wav.name}", str(fmt_wav).replace("\\", "/"), safe_tag, int(time.time() * 1000)),
        )
        conn.commit()
        vid = int(cur.lastrowid)
        return {
            "item": {
                "id": vid,
                "display_name": name.strip() or safe_tag,
                "origin_audio_path": f"origin_audio/{fmt_wav.name}",
                "asr_format_audio_url": str(fmt_wav).replace("\\", "/"),
                "abs_path": str(fmt_wav),
                "exists": True,
                "preview_url": f"/api/v1/media/voice/{vid}",
            }
        }
    finally:
        conn.close()


# ==================== IndexTTS2 Config, Presets, Audition & Audio Library ====================

@app.get("/api/v1/tts/capabilities", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_capabilities() -> dict[str, Any]:
    try:
        return _proxy_tts_json("GET", "/v1/tts/capabilities", timeout=4.0)
    except HTTPException:
        # 即使 18180 尚未启动，也返回本地 config.json 以便前端仍可浏览/保存默认参数
        cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        return {
            "available": False,
            "unavailable_reason": "IndexTTS2 语音服务 (18180) 当前未启动，点击顶部「一键启动服务」即可后台拉起",
            "versions": ["IndexTTS-2", "IndexTTS-2.5"],
            "emo_vector_labels": ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"],
            "languages": ["ZH", "EN", "JA", "ES", "AR"],
            "current_config": cfg,
        }


def _load_registered_personal_voices() -> list[dict[str, Any]]:
    """只读读取 D:\\IndexTTS2-Training\\voices\\registry.json，供本地工作台列出已就绪的专属微调声音。"""
    reg_raw = (os.getenv("INDEX_TTS_VOICES_REGISTRY") or os.getenv("MYVOICE_REGISTRY_PATH") or "").strip()
    reg_path = Path(reg_raw) if reg_raw else Path(r"D:\IndexTTS2-Training\voices\registry.json")
    if not reg_path.is_file():
        return []
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for v in data.get("voices") or []:
        if not isinstance(v, dict) or str(v.get("status") or "").lower() != "ready":
            continue
        vid = str(v.get("voice_id") or "").strip()
        dname = str(v.get("display_name") or vid).strip()
        models = v.get("models") if isinstance(v.get("models"), dict) else {}
        def_mk = str(v.get("default_model_key") or "best").strip()
        prompts = v.get("prompts") if isinstance(v.get("prompts"), dict) else {}
        def_pk = str(v.get("default_prompt_key") or "natural").strip()
        def_prompt_path = str((prompts.get(def_pk) or {}).get("path") or "").strip()
        m_entry = models.get(def_mk) or next((mv for mv in models.values() if isinstance(mv, dict)), None)
        if not isinstance(m_entry, dict):
            continue
        cfg_p = str(m_entry.get("cfg_path") or "").strip()
        gpt_p = str(m_entry.get("gpt_path") or "").strip()
        if not cfg_p or not Path(cfg_p).is_file() or not gpt_p or not Path(gpt_p).is_file():
            continue
        m_label = str(m_entry.get("label") or def_mk).strip()
        out.append(
            {
                "voice_id": vid,
                "display_name": dname,
                "model_key": def_mk,
                "label": f"{dname} · {m_label}",
                "cfg_path": cfg_p.replace("\\", "/"),
                "prompt_path": def_prompt_path.replace("\\", "/"),
            }
        )
    return out


@app.get("/api/v1/tts/config", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_get_tts_config() -> dict[str, Any]:
    reg_voices = _load_registered_personal_voices()
    try:
        res = _proxy_tts_json("GET", "/v1/tts/config", timeout=4.0)
        if isinstance(res, dict):
            res["registered_voices"] = reg_voices
        return res
    except HTTPException:
        cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        return {"code": 0, "config": cfg, "registered_voices": reg_voices, "offline_fallback": True}


@app.put("/api/v1/tts/config", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_put_tts_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _proxy_tts_json("PUT", "/v1/tts/config", payload=payload, timeout=5.0)
    except HTTPException:
        cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
        current = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        for k, v in payload.items():
            if k in current or str(k).startswith("index_tts_"):
                current[k] = v
        tmp_path = cfg_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, cfg_path)
        return {"code": 0, "msg": "saved_offline", "config": current}


@app.get("/api/v1/tts/status", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_status() -> dict[str, Any]:
    try:
        return _proxy_tts_json("GET", "/v1/tts/status", timeout=3.0)
    except HTTPException:
        return {"code": 0, "data": {"state": "offline", "message": "IndexTTS2 服务未启动", "progress": 0, "recent_logs": []}}


@app.post("/api/v1/tts/cancel", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_cancel() -> dict[str, Any]:
    return _proxy_tts_json("POST", "/v1/tts/cancel", payload={}, timeout=8.0)


@app.post("/api/v1/tts/upload-emo-audio", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_upload_emo_audio(file: UploadFile = File(...)) -> dict[str, Any]:
    emo_dir = resolve_studio_assets_root() / "emo_audio"
    emo_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    tmp = emo_dir / f"_tmp_{stamp}{Path(file.filename or '.wav').suffix or '.wav'}"
    dst = emo_dir / f"emo_{stamp}.wav"
    try:
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        _convert_to_wav(tmp, dst, sample_rate=22050)
    except Exception as exc:
        dst.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"情感参考音频格式不支持或转码失败：{exc}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "path": str(dst).replace("\\", "/"), "filename": dst.name}


@app.get("/api/v1/tts/presets", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_list_presets() -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM studio_tts_presets ORDER BY is_default DESC, id ASC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json", "{}") or "{}")
            items.append(d)
        return {"items": items}
    finally:
        conn.close()


@app.post("/api/v1/tts/presets", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_save_preset(req: PresetCreate) -> dict[str, Any]:
    clean_name = req.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="预设名称不能为空")
    now_ms = int(time.time() * 1000)
    conn = get_conn()
    try:
        if req.is_default:
            conn.execute("UPDATE studio_tts_presets SET is_default=0")
        existing = conn.execute("SELECT id FROM studio_tts_presets WHERE name=?", (clean_name,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE studio_tts_presets SET description=?, params_json=?, is_default=?, updated_at=? WHERE id=?",
                (req.description, json.dumps(req.params, ensure_ascii=False), 1 if req.is_default else 0, now_ms, existing["id"]),
            )
            pid = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO studio_tts_presets (name, description, params_json, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (clean_name, req.description, json.dumps(req.params, ensure_ascii=False), 1 if req.is_default else 0, now_ms, now_ms),
            )
            pid = int(cur.lastrowid)
        conn.commit()
        row = dict(conn.execute("SELECT * FROM studio_tts_presets WHERE id=?", (pid,)).fetchone())
        row["params"] = json.loads(row.pop("params_json", "{}"))
        return {"item": row}
    finally:
        conn.close()


@app.delete("/api/v1/tts/presets/{preset_id}", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_delete_preset(preset_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM studio_tts_presets WHERE id=?", (preset_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _merge_preset_and_params(preset_id: int | None, override_params: dict[str, Any]) -> dict[str, Any]:
    if preset_id is None:
        return dict(override_params or {})
    conn = get_conn()
    try:
        return _merge_preset_and_params_conn(conn, preset_id, override_params)
    finally:
        conn.close()


@app.post("/api/v1/tts/synthesize", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_synthesize(req: TtsSynthesizeRequest) -> dict[str, Any]:
    """使用 IndexTTS2 实时合成试听音频，自动保存入音频素材库并可直接用于驱动数字人。"""
    ref_audio = clean_local_path(req.reference_audio)
    if not ref_audio and req.voice_id:
        conn = get_conn()
        try:
            vrow = conn.execute("SELECT * FROM voice WHERE id=?", (req.voice_id,)).fetchone()
            if not vrow:
                raise HTTPException(status_code=404, detail=f"音色 ID={req.voice_id} 不存在")
            resolved = _resolve_voice_file(dict(vrow))
            if not resolved or not resolved.is_file():
                raise HTTPException(status_code=404, detail=f"音色参考音频文件缺失：{resolved}")
            ref_audio = str(resolved).replace("\\", "/")
        finally:
            conn.close()

    if not ref_audio:
        raise HTTPException(status_code=400, detail="请先选择一个音色或参考音频")

    effective_params = _merge_preset_and_params(req.preset_id, req.tts_params)
    invoke_payload = {
        "text": req.text.strip(),
        "reference_audio": ref_audio,
        "tts_params": effective_params,
    }
    url = f"{system_ctrl.TTS_BASE_URL}/v1/invoke"
    body = json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8")
    http_req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(http_req, timeout=1800) as resp:
            wav_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(err_body).get("msg") or err_body
        except Exception:
            detail = err_body or str(exc)
        raise HTTPException(status_code=exc.code if 400 <= exc.code < 600 else 502, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_friendly_tts_conn_error(exc))

    audio_dir = resolve_studio_assets_root() / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    title = sanitize_filename(req.name or req.text.strip()[:24], f"tts_{stamp}")
    out_wav = audio_dir / f"{stamp}_{title}.wav"
    out_wav.write_bytes(wav_bytes)
    media = probe_media_info(out_wav)
    duration = float(media.get("duration") or 0.0)

    now_ms = int(time.time() * 1000)
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO studio_audio_assets
                (name, category_id, file_path, duration, source_type, voice_id, text_content, tts_params_json, created_at)
            VALUES (?, ?, ?, ?, 'tts', ?, ?, ?, ?)
            """,
            (
                req.name or req.text.strip()[:36],
                req.category_id,
                str(out_wav),
                duration,
                req.voice_id,
                req.text.strip(),
                json.dumps(effective_params, ensure_ascii=False),
                now_ms,
            ),
        )
        aid = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return {
        "item": {
            "id": aid,
            "name": req.name or req.text.strip()[:36],
            "category_id": req.category_id,
            "file_path": str(out_wav),
            "duration": duration,
            "size_kb": round(len(wav_bytes) / 1024, 1),
            "source_type": "tts",
            "voice_id": req.voice_id,
            "text_content": req.text.strip(),
            "tts_params": effective_params,
            "created_at": now_ms,
            "stream_url": f"/api/v1/media/audio/{aid}",
            "download_url": f"/api/v1/audios/{aid}/download",
        }
    }


@app.get("/api/v1/audios", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_list_audios(category_id: str | None = None, q: str | None = None) -> dict[str, Any]:
    conn = get_conn()
    try:
        sql = """
            SELECT a.*, c.name AS category_name, c.color AS category_color
            FROM studio_audio_assets a
            LEFT JOIN studio_categories c ON c.id = a.category_id
            WHERE 1=1
        """
        params: list[Any] = []
        if category_id == "uncategorized":
            sql += " AND a.category_id IS NULL"
        elif category_id and category_id.isdigit():
            sql += " AND a.category_id = ?"
            params.append(int(category_id))
        if q and q.strip():
            sql += " AND (a.name LIKE ? OR a.text_content LIKE ?)"
            params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
        sql += " ORDER BY a.id DESC"
        rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            p = Path(str(d.get("file_path") or ""))
            d["exists"] = p.is_file()
            d["size_kb"] = round(p.stat().st_size / 1024, 1) if d["exists"] else 0.0
            d["tts_params"] = json.loads(d.pop("tts_params_json", "{}") or "{}")
            d["stream_url"] = f"/api/v1/media/audio/{d['id']}" if d["exists"] else ""
            d["download_url"] = f"/api/v1/audios/{d['id']}/download" if d["exists"] else ""
            items.append(d)
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@app.post("/api/v1/audios/upload", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_upload_audios(
    category_id: int | None = Form(default=None),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """批量上传本地音频文件，自动转码为标准 WAV 并入音频素材库。"""
    audio_dir = resolve_studio_assets_root() / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    created: list[dict[str, Any]] = []
    written_files: list[Path] = []
    conn = get_conn()
    try:
        for upload in files:
            orig_name = upload.filename or "audio.wav"
            stem = sanitize_filename(Path(orig_name).stem, "audio")
            stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            ext = Path(orig_name).suffix.lower() or ".wav"
            tmp_in = audio_dir / f"_in_{stamp}{ext}"
            out_wav = audio_dir / f"{stamp}_{stem}.wav"
            try:
                with open(tmp_in, "wb") as fh:
                    shutil.copyfileobj(upload.file, fh)
                _convert_to_wav(tmp_in, out_wav, sample_rate=22050)
                written_files.append(out_wav)
            except Exception as exc:
                conn.rollback()
                out_wav.unlink(missing_ok=True)
                for wf in written_files:
                    wf.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"音频「{orig_name}」转码失败（请确认是有效音频文件）：{exc}")
            finally:
                tmp_in.unlink(missing_ok=True)

            media = probe_media_info(out_wav)
            dur = float(media.get("duration") or 0.0)
            now_ms = int(time.time() * 1000)
            cur = conn.execute(
                """
                INSERT INTO studio_audio_assets (name, category_id, file_path, duration, source_type, created_at)
                VALUES (?, ?, ?, ?, 'upload', ?)
                """,
                (stem, category_id, str(out_wav), dur, now_ms),
            )
            aid = int(cur.lastrowid)
            created.append(
                {
                    "id": aid,
                    "name": stem,
                    "category_id": category_id,
                    "file_path": str(out_wav),
                    "duration": dur,
                    "source_type": "upload",
                    "exists": True,
                    "stream_url": f"/api/v1/media/audio/{aid}",
                    "download_url": f"/api/v1/audios/{aid}/download",
                }
            )
        conn.commit()
    finally:
        conn.close()
    return {"items": created, "count": len(created)}


@app.post("/api/v1/audios/import-local", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_import_local_audios(req: LocalAudioImportRequest) -> dict[str, Any]:
    """通过本机路径批量快速导入音频（自动清洗引号、全量前置校验存在性后再转码入库）。"""
    cleaned_paths = [clean_local_path(p) for p in req.paths if clean_local_path(p)]
    if not cleaned_paths:
        raise HTTPException(status_code=400, detail="请至少填写 1 条有效的本机音频文件路径")

    # 全量前置校验所有文件存在性，避免部分入库残留
    source_files: list[Path] = []
    for idx, raw in enumerate(cleaned_paths):
        src = Path(raw)
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"第 {idx + 1} 条音频文件不存在：{src}")
        source_files.append(src)

    audio_dir = resolve_studio_assets_root() / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[str, Path, float]] = []
    written_files: list[Path] = []

    try:
        for src in source_files:
            stem = sanitize_filename(src.stem, "audio")
            stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            out_wav = audio_dir / f"{stamp}_{stem}.wav"
            try:
                _convert_to_wav(src, out_wav, sample_rate=22050)
                written_files.append(out_wav)
            except Exception as exc:
                out_wav.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"音频「{src.name}」转码失败（请确认是有效音频文件）：{exc}")
            media = probe_media_info(out_wav)
            dur = float(media.get("duration") or 0.0)
            prepared.append((stem, out_wav, dur))
    except Exception:
        for wf in written_files:
            wf.unlink(missing_ok=True)
        raise

    created: list[dict[str, Any]] = []
    conn = get_conn()
    try:
        now_ms = int(time.time() * 1000)
        for stem, out_wav, dur in prepared:
            cur = conn.execute(
                """
                INSERT INTO studio_audio_assets (name, category_id, file_path, duration, source_type, created_at)
                VALUES (?, ?, ?, ?, 'local_import', ?)
                """,
                (stem, req.category_id, str(out_wav), dur, now_ms),
            )
            aid = int(cur.lastrowid)
            created.append(
                {
                    "id": aid,
                    "name": stem,
                    "category_id": req.category_id,
                    "file_path": str(out_wav),
                    "duration": dur,
                    "source_type": "local_import",
                    "exists": True,
                    "stream_url": f"/api/v1/media/audio/{aid}",
                    "download_url": f"/api/v1/audios/{aid}/download",
                }
            )
        conn.commit()
    except Exception:
        conn.rollback()
        for wf in written_files:
            wf.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    return {"items": created, "count": len(created)}


@app.patch("/api/v1/audios/{audio_id}", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_update_audio(audio_id: int, req: AudioUpdateRequest) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="音频素材不存在")
        updates: dict[str, Any] = {}
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="音频名称不能为空")
            updates["name"] = clean_name
        if "category_id" in req.model_fields_set:
            updates["category_id"] = req.category_id
        if updates:
            sql = f"UPDATE studio_audio_assets SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?"
            conn.execute(sql, list(updates.values()) + [audio_id])
            conn.commit()
        refreshed = conn.execute(
            """
            SELECT a.*, c.name AS category_name, c.color AS category_color
            FROM studio_audio_assets a
            LEFT JOIN studio_categories c ON c.id = a.category_id
            WHERE a.id=?
            """,
            (audio_id,),
        ).fetchone()
        d = dict(refreshed)
        p = Path(str(d.get("file_path") or ""))
        d["exists"] = p.is_file()
        d["size_kb"] = round(p.stat().st_size / 1024, 1) if d["exists"] else 0.0
        d["tts_params"] = json.loads(d.pop("tts_params_json", "{}") or "{}")
        d["stream_url"] = f"/api/v1/media/audio/{d['id']}" if d["exists"] else ""
        d["download_url"] = f"/api/v1/audios/{d['id']}/download" if d["exists"] else ""
        return {"item": d}
    finally:
        conn.close()


@app.delete("/api/v1/audios/{audio_id}", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_delete_audio(audio_id: int, remove_file: bool = True) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT file_path FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
        if row and remove_file and row["file_path"]:
            try:
                Path(row["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass
        conn.execute("DELETE FROM studio_audio_assets WHERE id=?", (audio_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/audios/{audio_id}/download", tags=["音频素材库"])
def api_download_audio(audio_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT name, file_path FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["file_path"] or not Path(row["file_path"]).is_file():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    p = Path(row["file_path"])
    dl_name = f"{sanitize_filename(row['name'], p.stem)}.wav"
    return FileResponse(
        path=str(p),
        media_type="audio/wav",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(dl_name)}"},
    )


# ==================== Video Jobs & AI Automation API ====================

def _resolve_or_create_category_id(conn, category_id: int | None, category_name: str | None) -> int | None:
    if category_id is not None:
        return category_id
    if category_name and category_name.strip():
        cname = category_name.strip()
        row = conn.execute("SELECT id FROM studio_categories WHERE name=?", (cname,)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO studio_categories (name, color, sort_order, created_at) VALUES (?, '#3b82f6', 0, ?)",
            (cname, int(time.time() * 1000)),
        )
        return int(cur.lastrowid)
    return None


def _merge_preset_and_params_conn(conn, preset_id: int | None, override_params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if preset_id is not None:
        row = conn.execute("SELECT params_json FROM studio_tts_presets WHERE id=?", (preset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"{prefix}参数预设 ID={preset_id} 不存在")
        if row["params_json"]:
            merged.update(json.loads(row["params_json"]))
    if override_params:
        merged.update(override_params)
    return merged


def _validate_and_prepare_job_spec(conn, item: JobCreateItem, index: int | None = None) -> dict[str, Any]:
    """入队前纯只读全量校验：检查模特存在及底模文件、驱动音频或文本模式下的音色与情感参考音文件。"""
    prefix = f"第 {index + 1} 条任务：" if index is not None else ""

    model_row = conn.execute("SELECT * FROM f2f_model WHERE id=?", (item.model_id,)).fetchone()
    if not model_row:
        raise HTTPException(status_code=404, detail=f"{prefix}数字人模特 ID={item.model_id} 不存在")
    model = dict(model_row)

    output_root = resolve_output_root()
    vp = str(model.get("video_path") or "").strip()
    video_abs = output_root / vp.lstrip("/\\") if vp else None
    if not video_abs or not video_abs.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"{prefix}数字人模特「{model['name']}」(ID={item.model_id}) 的底模视频文件缺失：{video_abs or vp or '(未配置)'}，请先在「人物与声音」页面补回或重新导入底模",
        )

    text_content = (item.text or "").strip()
    audio_mode = "tts"
    audio_src_path: Path | None = None
    voice_id: int | None = item.voice_id or model.get("voice_id")
    effective_tts_params = _merge_preset_and_params_conn(conn, item.preset_id, item.tts_params, prefix=prefix)

    if item.audio_asset_id is not None:
        arow = conn.execute("SELECT * FROM studio_audio_assets WHERE id=?", (item.audio_asset_id,)).fetchone()
        if not arow or not arow["file_path"] or not Path(arow["file_path"]).is_file():
            raise HTTPException(status_code=404, detail=f"{prefix}音频库资产 ID={item.audio_asset_id} 不存在或文件丢失")
        audio_mode = "audio_asset"
        audio_src_path = Path(arow["file_path"])
        if not text_content and arow["text_content"]:
            text_content = str(arow["text_content"])
    elif item.audio_path and clean_local_path(item.audio_path):
        src_p = Path(clean_local_path(item.audio_path))
        if not src_p.is_file():
            raise HTTPException(status_code=404, detail=f"{prefix}指定音频文件不存在：{src_p}")
        audio_mode = "audio_path"
        audio_src_path = src_p
    else:
        if not text_content:
            raise HTTPException(status_code=400, detail=f"{prefix}请提供口播文案 text，或提供 audio_asset_id / audio_path")

        # 文本配音模式前置校验：参考音色记录与本地音色文件必须真实存在
        if not voice_id:
            raise HTTPException(status_code=400, detail=f"{prefix}数字人模特「{model['name']}」未绑定默认音色，请指定 voice_id")
        vrow = conn.execute("SELECT * FROM voice WHERE id=?", (voice_id,)).fetchone()
        if not vrow:
            raise HTTPException(status_code=404, detail=f"{prefix}参考音色 ID={voice_id} 不存在")
        voice_file = _resolve_voice_file(dict(vrow))
        if not voice_file or not voice_file.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"{prefix}参考音色 ID={voice_id} 的音频文件缺失：{voice_file or '(未配置)'}，请先在「人物与声音」页面检查或重新上传声音",
            )

        # 情感音频模式前置校验：若请求未显式传 emo_mode，同步参考全局 adapter/config.json 默认值
        adapter_cfg = _load_adapter_config_fallback()
        emo_mode = str(effective_tts_params.get("emo_mode") or "").strip().lower()
        if not emo_mode:
            emo_mode = str(adapter_cfg.get("index_tts_emo_mode") or "").strip().lower()
        if emo_mode == "emo_audio":
            if "emo_audio_prompt" in effective_tts_params and effective_tts_params["emo_audio_prompt"] is not None:
                emo_raw = str(effective_tts_params["emo_audio_prompt"])
            else:
                emo_raw = str(adapter_cfg.get("index_tts_emo_audio_prompt") or "")
            emo_file = _resolve_emo_audio_file(emo_raw)
            if not emo_file or not emo_file.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"{prefix}情感模式为 emo_audio 时必须提供存在的情感参考音频文件：{clean_local_path(emo_raw) or '(未提供)'}",
                )
            effective_tts_params["emo_audio_prompt"] = str(emo_file).replace("\\", "/")

    return {
        "item": item,
        "model": model,
        "audio_mode": audio_mode,
        "audio_src_path": audio_src_path,
        "text_content": text_content,
        "voice_id": voice_id,
        "effective_tts_params": effective_tts_params,
    }


def _create_jobs_atomically(items: list[JobCreateItem]) -> list[dict[str, Any]]:
    """原子化创建单条或批量任务：先全量前置校验，再统一物化音频并在单事务内入库，任意条目失败零残留。"""
    output_root = resolve_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    written_temp_files: list[Path] = []
    created_ids: list[int] = []

    conn = get_conn()
    try:
        is_batch = len(items) > 1
        specs = [_validate_and_prepare_job_spec(conn, it, index=idx if is_batch else None) for idx, it in enumerate(items)]

        # 阶段 2：准备需要落盘到 temp 目录的驱动音频（若任一条转码失败，清理前面已写入的临时音频）
        materialized: list[tuple[dict[str, Any], str | None, str]] = []
        for idx, spec in enumerate(specs):
            audio_name_for_video: str | None = None
            source_audio_abs = ""
            if spec["audio_mode"] == "audio_asset":
                src_p = spec["audio_src_path"]
                assert src_p is not None
                audio_name_for_video = f"studio_{uuid.uuid4().hex[:12]}.wav"
                dst_p = output_root / audio_name_for_video
                shutil.copy2(src_p, dst_p)
                written_temp_files.append(dst_p)
                source_audio_abs = str(src_p)
            elif spec["audio_mode"] == "audio_path":
                src_p = spec["audio_src_path"]
                assert src_p is not None
                audio_name_for_video = f"studio_{uuid.uuid4().hex[:12]}.wav"
                dst_p = output_root / audio_name_for_video
                if src_p.suffix.lower() == ".wav":
                    shutil.copy2(src_p, dst_p)
                else:
                    try:
                        _convert_to_wav(src_p, dst_p, sample_rate=22050)
                    except Exception as exc:
                        prefix = f"第 {idx + 1} 条任务：" if is_batch else ""
                        raise HTTPException(status_code=400, detail=f"{prefix}驱动音频转码失败：{exc}")
                written_temp_files.append(dst_p)
                source_audio_abs = str(src_p)
            materialized.append((spec, audio_name_for_video, source_audio_abs))

        # 阶段 3：单事务批量入库
        now_ms = int(time.time() * 1000)
        for spec, audio_name_for_video, source_audio_abs in materialized:
            item: JobCreateItem = spec["item"]
            model = spec["model"]
            text_content = spec["text_content"]
            cat_id = _resolve_or_create_category_id(conn, item.category_id, item.category_name)

            job_name = (item.name or "").strip()
            if not job_name:
                mname = sanitize_filename(str(model["name"] or ""), "")[:16]
                if spec["audio_mode"] in ("audio_asset", "audio_path") and source_audio_abs:
                    stem = Path(source_audio_abs).stem
                    stem = re.sub(r"^(?:20\d{6}[_-]?\d{6}|\d{10,18})[_-]?", "", stem)
                    stem = re.sub(r"[_-]?20\d{6}(?:[_-]?\d{6})?$", "", stem)
                    clean_a = sanitize_filename(stem, "")[:24]
                    job_name = f"{clean_a}_{mname}" if (clean_a and mname) else (clean_a or f"{mname}_{time.strftime('%m%d_%H%M')}")
                elif text_content:
                    plain = re.sub(r"[\s\r\n，。！？、；：“”‘’（）()【】\[\]《》<>—…·.,!?;:'\"`~@#$%^&*+=|/\\-]+", "", text_content.strip())
                    snippet = sanitize_filename(plain[:10], "")
                    job_name = f"{snippet}_{mname}" if (snippet and mname) else (snippet or f"{mname}_{time.strftime('%m%d_%H%M')}")
                else:
                    job_name = f"{mname}_{time.strftime('%m%d_%H%M')}" if mname else f"视频_{time.strftime('%m%d_%H%M')}"

            cur = conn.execute(
                """
                INSERT INTO video (file_path, status, message, model_id, audio_path, param, code, created_at, progress, name, duration, text_content, voice_id)
                VALUES (NULL, 'waiting', '已加入制作队列，等待调度执行', ?, ?, NULL, NULL, ?, 0, ?, NULL, ?, ?)
                """,
                (item.model_id, audio_name_for_video, now_ms, job_name, text_content, spec["voice_id"]),
            )
            vid = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO studio_job_meta
                    (video_id, category_id, phase, stage_label, progress_float, tts_params_json, source_audio_path, max_retries, webhook_url, updated_at)
                VALUES (?, ?, 'queued', '已加入制作队列，等待调度执行', 0.0, ?, ?, ?, ?, ?)
                """,
                (
                    vid,
                    cat_id,
                    json.dumps(spec["effective_tts_params"], ensure_ascii=False),
                    source_audio_abs,
                    item.max_retries,
                    (item.webhook_url or "").strip(),
                    now_ms,
                ),
            )
            created_ids.append(vid)

        conn.commit()
    except Exception:
        conn.rollback()
        for wf in written_temp_files:
            wf.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    jobs: list[dict[str, Any]] = []
    for vid in created_ids:
        job = _fetch_job_by_id(vid)
        assert job is not None
        jobs.append(job)
    return jobs


def _create_single_job_in_db(item: JobCreateItem) -> dict[str, Any]:
    return _create_jobs_atomically([item])[0]


@app.get("/api/v1/jobs", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_list_jobs(
    status: str | None = None,
    category_id: str | None = None,
    model_id: int | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conn = get_conn()
    try:
        sql = """
            SELECT v.*,
                   fm.name AS model_name,
                   fm.video_path AS model_video_path,
                   m.category_id,
                   c.name AS category_name,
                   c.color AS category_color,
                   m.phase, m.stage_label, m.progress_float,
                   m.rendered_frames, m.total_frames, m.render_fps, m.eta_seconds,
                   m.heartbeat_at, m.tts_params_json, m.source_audio_path, m.delivered_path,
                   m.retry_count, m.max_retries, m.error_code, m.error_detail,
                   m.webhook_url, m.recent_logs_json, m.updated_at AS meta_updated_at
            FROM video v
            LEFT JOIN f2f_model fm ON fm.id = v.model_id
            LEFT JOIN studio_job_meta m ON m.video_id = v.id
            LEFT JOIN studio_categories c ON c.id = m.category_id
            WHERE 1=1
        """
        params: list[Any] = []
        if status and status != "all":
            if status == "active":
                sql += " AND v.status IN ('pending', 'waiting')"
            else:
                sql += " AND v.status = ?"
                params.append(status)
        if category_id == "uncategorized":
            sql += " AND m.category_id IS NULL"
        elif category_id and category_id.isdigit():
            sql += " AND m.category_id = ?"
            params.append(int(category_id))
        if model_id is not None:
            sql += " AND v.model_id = ?"
            params.append(model_id)
        if q and q.strip():
            sql += " AND (v.name LIKE ? OR v.text_content LIKE ? OR fm.name LIKE ?)"
            like = f"%{q.strip()}%"
            params.extend([like, like, like])

        sql += " ORDER BY CASE v.status WHEN 'pending' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END, v.id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        items = [_enrich_job_row(dict(r)) for r in rows]
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@app.get("/api/v1/jobs/{job_id}", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_get_job(job_id: int) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    return {"item": job}


@app.post("/api/v1/jobs", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_create_job(item: JobCreateItem) -> dict[str, Any]:
    """创建单条数字人视频任务（支持文本+IndexTTS2参数，或直接传入本地音频/音频库资产）。"""
    job = _create_single_job_in_db(item)
    return {"item": job, "job_id": job["id"]}


@app.post("/api/v1/jobs/batch", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_create_jobs_batch(req: BatchJobsCreateRequest) -> dict[str, Any]:
    """批量创建数字人视频任务（全量前置校验 + 事务原子提交，任意一条无效则整批拒绝不留脏数据）。"""
    created = _create_jobs_atomically(req.items)
    return {"items": created, "count": len(created), "job_ids": [j["id"] for j in created]}


@app.post("/api/v1/jobs/{job_id}/cancel", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_cancel_job(job_id: int) -> dict[str, Any]:
    """实时中断任务：排队中立即取消；语音合成中立刻杀死 TTS 进程；渲染中通知调度器中止容器。保留历史记录以便随时重试。"""
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")

    status = job["status"]
    if status in {"success", "cancelled"}:
        return {"ok": True, "item": job, "message": f"任务当前状态已是 {status}"}

    now_sec = int(time.time())
    conn = get_conn()
    try:
        if status in {"waiting", "failed", "draft"}:
            # 条件更新防止刚好与队列出队抢单撞车：如果刚变成 pending，则无缝走 pending 中断分支
            cur = conn.execute(
                "UPDATE video SET status='cancelled', message='任务已被用户中断', code=NULL WHERE id=? AND status IN ('waiting', 'failed', 'draft')",
                (job_id,),
            )
            conn.commit()
            if (cur.rowcount or 0) > 0:
                upsert_job_meta(job_id, phase="cancelled", stage_label="任务已被用户中断", eta_seconds=-1)
                return {"ok": True, "item": _fetch_job_by_id(job_id), "message": "排队任务已立即中断"}
            job = _fetch_job_by_id(job_id) or job

        # pending 状态：写入取消请求表，并主动触发 TTS 中断；若队列调度器未运行则直接本地置为 cancelled
        conn.execute(
            "INSERT OR REPLACE INTO studio_cancel_requests (video_id, requested_at, reason) VALUES (?, ?, 'user_cancelled')",
            (job_id, now_sec),
        )
        conn.commit()
    finally:
        conn.close()

    # 若尚未拿到 8383 任务码（处于语音准备/合成阶段），立即向 18180 发中断信号
    if job["phase"] in {"tts_loading", "tts_synthesizing", "queued"} or not job.get("code"):
        try:
            _proxy_tts_json("POST", "/v1/tts/cancel", payload={}, timeout=4.0)
        except Exception:
            pass

    upsert_job_meta(job_id, stage_label="正在中断任务并释放显存...")

    # 如果后台队列调度器当前根本没运行，直接完成中止收尾，不让用户傻等
    queue_pids = system_ctrl._find_python_processes("heygem_queue_runner.py", use_cache=False)
    if not queue_pids:
        if job.get("code"):
            subprocess.run(
                ["docker", "restart", "duix-avatar-gen-video"],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
        conn = get_conn()
        try:
            conn.execute("UPDATE video SET status='cancelled', message='任务已被用户中断', code=NULL WHERE id=?", (job_id,))
            conn.execute("DELETE FROM studio_cancel_requests WHERE video_id=?", (job_id,))
            conn.commit()
        finally:
            conn.close()
        upsert_job_meta(job_id, phase="cancelled", stage_label="任务已被用户中断", eta_seconds=-1)

    return {"ok": True, "item": _fetch_job_by_id(job_id), "message": "已下发中断指令，任务将在数秒内停止并释放资源"}


@app.post("/api/v1/jobs/{job_id}/retry", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_retry_job(job_id: int) -> dict[str, Any]:
    """重新排队执行失败或已中断的任务（重置重试计数与错误信息）。"""
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    if job["status"] == "pending":
        raise HTTPException(status_code=409, detail="该任务正在执行中，请先点击「中断任务」后再重试")

    # 若原任务为文案配音模式且 temp 里的临时音频已丢失，清空 audio_path 让调度器重新合成语音
    clear_audio = False
    if job.get("text_content") and job.get("audio_path"):
        temp_audio = resolve_output_root() / Path(str(job["audio_path"])).name
        src_audio_str = str(job["paths"].get("source_audio_path") or "").strip()
        src_audio_ok = bool(src_audio_str and Path(src_audio_str).is_file())
        if not temp_audio.is_file() and not src_audio_ok:
            clear_audio = True

    conn = get_conn()
    try:
        conn.execute("DELETE FROM studio_cancel_requests WHERE video_id=?", (job_id,))
        conn.execute("DELETE FROM cancelled_video_tasks WHERE video_id=?", (job_id,))
        if clear_audio:
            conn.execute(
                """
                UPDATE video
                   SET status='waiting', message='用户手动重试，等待重新配音并执行', progress=0, code=NULL, param=NULL, file_path=NULL, audio_path=NULL
                 WHERE id=?
                """,
                (job_id,),
            )
        else:
            conn.execute(
                """
                UPDATE video
                   SET status='waiting', message='用户手动重试，等待执行', progress=0, code=NULL, param=NULL, file_path=NULL
                 WHERE id=?
                """,
                (job_id,),
            )
        conn.commit()
    finally:
        conn.close()
    upsert_job_meta(
        job_id,
        phase="queued",
        stage_label="已重新加入队列，等待执行",
        progress_float=0.0,
        rendered_frames=0,
        eta_seconds=-1,
        retry_count=0,
        error_code="",
        error_detail="",
        webhook_notified=0,
    )
    return {"ok": True, "item": _fetch_job_by_id(job_id)}


@app.patch("/api/v1/jobs/{job_id}", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_update_job(job_id: int, req: JobUpdateRequest) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    conn = get_conn()
    try:
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="作品标题不能为空")
            conn.execute("UPDATE video SET name=? WHERE id=?", (clean_name, job_id))
            conn.commit()
    finally:
        conn.close()

    if "category_id" in req.model_fields_set:
        upsert_job_meta(job_id, category_id=req.category_id)
        # 若已有交付成片，顺带同步移动到对应分类子目录（可选安全移动）
        delivered = job["paths"].get("delivered_path")
        if delivered and Path(delivered).is_file():
            src = Path(delivered)
            cat_folder = ""
            if req.category_id is not None:
                conn = get_conn()
                try:
                    crow = conn.execute("SELECT name FROM studio_categories WHERE id=?", (req.category_id,)).fetchone()
                    if crow and crow["name"]:
                        cat_folder = sanitize_filename(crow["name"], "")[:40]
                finally:
                    conn.close()
            dest_dir = (resolve_delivery_root() / cat_folder) if cat_folder else resolve_delivery_root()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / src.name
            if dst.resolve() != src.resolve():
                try:
                    shutil.move(str(src), str(dst))
                    upsert_job_meta(job_id, delivered_path=str(dst))
                except Exception:
                    pass

    return {"item": _fetch_job_by_id(job_id)}


@app.delete("/api/v1/jobs/{job_id}", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_delete_job(job_id: int, remove_files: bool = False) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    # 若删除的是运行中任务，先下发中止信号防止后台继续空烧显存
    if job["status"] == "pending":
        if not job.get("code"):
            try:
                _proxy_tts_json("POST", "/v1/tts/cancel", payload={}, timeout=3.0)
            except Exception:
                pass
    if remove_files:
        for key in ("delivered_path", "temp_video_path"):
            raw_p = str(job["paths"].get(key) or "").strip()
            if raw_p and Path(raw_p).is_file():
                try:
                    Path(raw_p).unlink(missing_ok=True)
                except OSError:
                    pass
    conn = get_conn()
    try:
        if job["status"] == "pending" and job.get("code"):
            conn.execute(
                "INSERT OR REPLACE INTO cancelled_video_tasks (video_id, code, cancelled_at) VALUES (?, ?, ?)",
                (job_id, job["code"], int(time.time())),
            )
        conn.execute("DELETE FROM studio_job_meta WHERE video_id=?", (job_id,))
        conn.execute("DELETE FROM studio_cancel_requests WHERE video_id=?", (job_id,))
        conn.execute("DELETE FROM video WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    _remove_video_thumbnail(job_id)
    _remove_compact_cache(job_id)
    return {"ok": True}


class FastFileResponse(FileResponse):
    """使用 4MB 大块传输替代默认的 64KB 小块，大幅减少大视频下载时的线程切换开销，同时完整保留 HTTP Range 断点续传。"""

    chunk_size = 4 * 1024 * 1024


_COMPACT_LOCK_GUARD = threading.Lock()
_COMPACT_JOB_LOCKS: dict[int, threading.Lock] = {}


def _get_compact_lock(job_id: int) -> threading.Lock:
    with _COMPACT_LOCK_GUARD:
        lock = _COMPACT_JOB_LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _COMPACT_JOB_LOCKS[job_id] = lock
        return lock


def _pick_fastest_video_source(job: dict[str, Any]) -> Path | None:
    """下载/流式读取时，若固态盘 D: 临时区存有同大小原件，则优先从固态盘读取避开机械硬盘慢速冷读。"""
    paths = job.get("paths") or {}
    delivered_raw = str(paths.get("delivered_path") or "").strip()
    temp_raw = str(paths.get("temp_video_path") or "").strip()
    playable_raw = str(paths.get("playable_path") or "").strip()

    delivered = Path(delivered_raw) if delivered_raw else None
    temp = Path(temp_raw) if temp_raw else None
    if delivered and delivered.is_file() and temp and temp.is_file():
        try:
            if temp.stat().st_size == delivered.stat().st_size and temp.stat().st_size > 0:
                return temp
        except OSError:
            pass
    if playable_raw:
        p = Path(playable_raw)
        if p.is_file():
            return p
    return None


def _remove_compact_cache(job_id: int) -> None:
    compact_dir = resolve_studio_assets_root() / "compact"
    if not compact_dir.is_dir():
        return
    for item in compact_dir.glob(f"job_{job_id}_*.mp4"):
        try:
            item.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_compact_video(job_id: int, source: Path) -> tuple[Path, bool]:
    """用 RTX 4070 NVENC 硬件加速（失败自动回退 libx264）生成清晰 1080p 小体积版并缓存，绝不改动原 4K 成片。"""
    st = source.stat()
    orig_mb = st.st_size / (1024 * 1024)
    meta = probe_media_info(source)
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    short_edge = min(w, h) if (w > 0 and h > 0) else 0
    if orig_mb <= 80.0 and (short_edge == 0 or short_edge <= 1080):
        return source, True

    compact_dir = resolve_studio_assets_root() / "compact"
    compact_dir.mkdir(parents=True, exist_ok=True)
    target = compact_dir / f"job_{job_id}_{int(st.st_mtime)}_{st.st_size}.mp4"

    lock = _get_compact_lock(job_id)
    with lock:
        if target.is_file() and target.stat().st_size > 0:
            return target, False

        # 清理该任务旧缓存
        for old in compact_dir.glob(f"job_{job_id}_*.mp4"):
            if old != target:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass

        ffmpeg_exe = resolve_ffmpeg_exe()
        # 无论横屏还是竖屏，统一按最长边 <= 1920、短边 <= 1080 等比缩放并保持偶数分辨率
        scale_filter = "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))'"
        tmp_out = compact_dir / f"_tmp_{job_id}_{uuid.uuid4().hex[:6]}.mp4"

        encoder_attempts = [
            [
                ffmpeg_exe, "-y", "-i", str(source),
                "-vf", scale_filter,
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", "0", "-maxrate", "8M", "-bufsize", "16M",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(tmp_out),
            ],
            [
                ffmpeg_exe, "-y", "-i", str(source),
                "-vf", scale_filter,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "8M", "-bufsize", "16M",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(tmp_out),
            ],
        ]

        last_err = ""
        for cmd in encoder_attempts:
            tmp_out.unlink(missing_ok=True)
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 0:
                os.replace(tmp_out, target)
                return target, False
            last_err = (res.stderr or res.stdout or "").strip()[-300:]

        tmp_out.unlink(missing_ok=True)
        raise RuntimeError(f"压缩轻量版视频失败：{last_err or 'FFmpeg 退出异常'}")


@app.get("/api/v1/jobs/{job_id}/download", tags=["数字人任务与自动化 API"])
def api_download_job(
    job_id: int,
    quality: str = Query("original", pattern="^(original|compact)$"),
) -> FileResponse:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    source = _pick_fastest_video_source(job)
    if not source or not source.is_file():
        raise HTTPException(status_code=404, detail="成片视频文件尚未生成或已移动")

    base_name = f"{job_id:04d}_{sanitize_filename(job['name'], 'video')}"
    if quality == "compact":
        try:
            target, reused = _ensure_compact_video(job_id, source)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        dl_name = f"{base_name}.mp4" if reused else f"{base_name}_轻量1080p.mp4"
    else:
        target = source
        dl_name = f"{base_name}.mp4"

    return FastFileResponse(
        path=str(target),
        media_type="video/mp4",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(dl_name)}"},
    )


@app.post("/api/v1/jobs/{job_id}/copy-file", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_copy_job_file(
    request: Request,
    job_id: int,
    quality: str = Query("original", pattern="^(original|compact)$"),
) -> dict[str, Any]:
    _require_local_client(request, "复制视频文件到剪贴板")
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    raw_target = str((job.get("paths") or {}).get("playable_path") or "").strip()
    source = Path(raw_target) if raw_target else None
    if not source or not source.is_file():
        raise HTTPException(status_code=404, detail="成片视频文件尚未生成或已移动")

    target = source
    if quality == "compact":
        try:
            target, _ = _ensure_compact_video(job_id, source)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    ok, msg = system_ctrl.copy_file_to_clipboard(str(target))
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "path": str(target), "message": msg}


@app.post("/api/v1/jobs/{job_id}/compact", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_compact_job_video(request: Request, job_id: int) -> dict[str, Any]:
    """用显卡高速生成清晰 1080p 小体积版（保留原 4K 大片），本机访问时顺带自动复制到剪贴板方便直接粘贴发微信。"""
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    source = _pick_fastest_video_source(job)
    if not source or not source.is_file():
        raise HTTPException(status_code=404, detail="成片视频文件尚未生成或已移动")

    orig_mb = round(source.stat().st_size / (1024 * 1024), 1)
    t0 = time.time()
    try:
        compact_path, reused = _ensure_compact_video(job_id, source)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    elapsed = round(time.time() - t0, 1)
    compact_mb = round(compact_path.stat().st_size / (1024 * 1024), 1)

    copied = False
    if _is_local_client(request) and os.name == "nt":
        copied, _ = system_ctrl.copy_file_to_clipboard(str(compact_path))

    return {
        "ok": True,
        "path": str(compact_path),
        "orig_size_mb": orig_mb,
        "compact_size_mb": compact_mb,
        "elapsed_seconds": elapsed,
        "reused_original": reused,
        "copied_to_clipboard": copied,
        "download_url": f"/api/v1/jobs/{job_id}/download?quality=compact",
    }


@app.post("/api/v1/jobs/{job_id}/open-folder", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_open_job_folder(request: Request, job_id: int) -> dict[str, Any]:
    _require_local_client(request, "打开文件夹")
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = job["paths"].get("playable_path") or str(resolve_delivery_root())
    ok, msg = system_ctrl.open_path_in_explorer(target)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg}


# ==================== Media Streaming Endpoints (Supports HTTP Range) ====================

@app.get("/api/v1/media/video/{job_id}/thumb", tags=["媒体预览"])
def media_video_thumbnail(job_id: int) -> FileResponse:
    """返回成品视频的轻量 JPEG 封面缩略图（若成片已移走则自动回退人物底模封面）。"""
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    raw_playable = str(job["paths"].get("playable_path") or "").strip()
    if raw_playable:
        target = Path(raw_playable)
        if target.is_file():
            thumb = _ensure_video_thumbnail(job_id, target)
            if thumb and thumb.is_file():
                return FileResponse(path=str(thumb), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    model_id = job.get("model_id")
    raw_model = str(job["paths"].get("model_video_path") or "").strip()
    if model_id and raw_model:
        model_path = Path(raw_model)
        if model_path.is_file():
            thumb = _ensure_model_thumbnail(int(model_id), model_path)
            if thumb and thumb.is_file():
                return FileResponse(path=str(thumb), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="无法提取视频封面")


@app.get("/api/v1/media/video/{job_id}", tags=["媒体预览"])
def media_stream_video(job_id: int) -> FileResponse:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = _pick_fastest_video_source(job)
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FastFileResponse(path=str(target), media_type="video/mp4")


@app.get("/api/v1/media/model/{model_id}/thumb", tags=["媒体预览"])
def media_model_thumbnail(model_id: int) -> FileResponse:
    """返回人物底模视频的轻量 JPEG 封面缩略图（自动缓存，解决 HEVC 10-bit .mov 在浏览器中黑屏问题）。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT video_path FROM f2f_model WHERE id=?", (model_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["video_path"]:
        raise HTTPException(status_code=404, detail="模特不存在")
    target = resolve_output_root() / str(row["video_path"]).lstrip("/\\")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="底模视频文件不存在")
    thumb = _ensure_model_thumbnail(model_id, target)
    if not thumb or not thumb.is_file():
        raise HTTPException(status_code=404, detail="无法提取视频缩略图")
    return FileResponse(path=str(thumb), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/v1/media/model/{model_id}", tags=["媒体预览"])
def media_stream_model(model_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT video_path FROM f2f_model WHERE id=?", (model_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["video_path"]:
        raise HTTPException(status_code=404, detail="模特不存在")
    target = resolve_output_root() / str(row["video_path"]).lstrip("/\\")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="底模视频文件不存在")
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(path=str(target), media_type=mime or "video/mp4")


@app.get("/api/v1/media/audio/{audio_id}", tags=["媒体预览"])
def media_stream_audio(audio_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT file_path FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["file_path"] or not Path(row["file_path"]).is_file():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return FileResponse(path=row["file_path"], media_type="audio/wav")


@app.get("/api/v1/media/voice/{voice_id}", tags=["媒体预览"])
def media_stream_voice(voice_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM voice WHERE id=?", (voice_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="音色不存在")
    target = _resolve_voice_file(dict(row))
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="音色文件不存在")
    return FileResponse(path=str(target), media_type="audio/wav")


# ==================== 云端数字人成片自动拉回本地 ====================


class CloudSyncConfigRequest(BaseModel):
    enabled: bool | None = None
    sync_audio: bool | None = None
    cloud_url: str | None = None
    cloud_token: str | None = None
    save_dir: str | None = None
    quality: str | None = None
    poll_interval: int | None = None
    notify_desktop: bool | None = None


class CloudSyncOpenFolderRequest(BaseModel):
    file_path: str | None = None


@app.get("/api/v1/cloud-sync/status", tags=["云端自动同步"], dependencies=[Depends(_verify_api_key)])
def get_cloud_sync_status() -> dict[str, Any]:
    return {"ok": True, "status": cloud_sync_manager.get_status()}


@app.post("/api/v1/cloud-sync/config", tags=["云端自动同步"], dependencies=[Depends(_verify_api_key)])
def update_cloud_sync_config(req: CloudSyncConfigRequest) -> dict[str, Any]:
    try:
        st = cloud_sync_manager.update_config(
            enabled=req.enabled,
            sync_audio=req.sync_audio,
            cloud_url=req.cloud_url,
            cloud_token=req.cloud_token,
            save_dir=req.save_dir,
            quality=req.quality,
            poll_interval=req.poll_interval,
            notify_desktop=req.notify_desktop,
        )
        return {"ok": True, "status": st}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"保存云端同步配置失败：{exc}") from exc


@app.post("/api/v1/cloud-sync/sync-now", tags=["云端自动同步"], dependencies=[Depends(_verify_api_key)])
def trigger_cloud_sync_now() -> dict[str, Any]:
    st = cloud_sync_manager.get_status()
    if not st.get("cloud_url"):
        raise HTTPException(status_code=400, detail="请先填写并保存 Colab 云端创作台网址")
    threading.Thread(target=cloud_sync_manager.sync_once, name="CloudSyncManual", daemon=True).start()
    return {"ok": True, "message": "已立即在后台检查云端新成片"}


@app.post("/api/v1/cloud-sync/pull/{video_id}", tags=["云端自动同步"], dependencies=[Depends(_verify_api_key)])
def manual_pull_cloud_video(video_id: int) -> dict[str, Any]:
    st = cloud_sync_manager.get_status()
    if not st.get("cloud_url"):
        raise HTTPException(status_code=400, detail="请先填写并保存 Colab 云端创作台网址")
    if st.get("active_download"):
        raise HTTPException(status_code=409, detail="当前正有一条视频正在拉取中，请等它拉完再点")

    def _run() -> None:
        try:
            cloud_sync_manager.pull_video_now(video_id, force=True)
        except Exception as exc:
            cloud_sync_manager._log_event(f"手动拉取 #{video_id} 失败：{exc}")

    threading.Thread(target=_run, name=f"CloudSyncPull-{video_id}", daemon=True).start()
    return {"ok": True, "message": f"已开始 8 线程高速拉回云端 #{video_id} 号视频"}


@app.post("/api/v1/cloud-sync/pull-audio/{audio_id}", tags=["云端自动同步"], dependencies=[Depends(_verify_api_key)])
def manual_pull_cloud_audio(audio_id: int) -> dict[str, Any]:
    st = cloud_sync_manager.get_status()
    if not st.get("cloud_url"):
        raise HTTPException(status_code=400, detail="请先填写并保存云端创作台网址")
    if st.get("active_download"):
        raise HTTPException(status_code=409, detail="当前正有文件在拉取中，请稍候再点")

    def _run() -> None:
        try:
            cloud_sync_manager.pull_audio_now(audio_id, force=True)
        except Exception as exc:
            cloud_sync_manager._log_event(f"手动拉取音频 #{audio_id} 失败：{exc}")

    threading.Thread(target=_run, name=f"CloudSyncPullAudio-{audio_id}", daemon=True).start()
    return {"ok": True, "message": f"已开始拉回云端 #{audio_id} 号配音音频"}


@app.post("/api/v1/cloud-sync/open-folder", tags=["云端自动同步"], dependencies=[Depends(_verify_api_key)])
def open_cloud_sync_folder(req: CloudSyncOpenFolderRequest = Body(default=CloudSyncOpenFolderRequest())) -> dict[str, Any]:
    st = cloud_sync_manager.get_status()
    target = Path(req.file_path) if (req.file_path and Path(req.file_path).exists()) else Path(st.get("save_dir") or r"E:\数字人视频文件")
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    ok, msg = system_ctrl.open_path_in_explorer(str(target))
    return {"ok": ok, "message": msg, "path": str(target)}


# ==================== Static Web Studio UI ====================

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon() -> Response:
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>HeyGem Studio UI 尚未初始化</h1>", status_code=503)
    return HTMLResponse(index_file.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


def run_server() -> None:
    import uvicorn

    host = os.getenv("HEYGEM_STUDIO_HOST", "0.0.0.0")
    port = int(os.getenv("HEYGEM_STUDIO_PORT", "8390"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
