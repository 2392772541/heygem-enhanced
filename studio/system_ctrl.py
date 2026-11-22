from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .db import (
    CREATE_NO_WINDOW,
    PROJECT_ROOT,
    clean_local_path,
    get_conn,
    init_studio_schema,
    resolve_db_path,
    resolve_delivery_root,
    resolve_duix_data_dir,
    resolve_mode_file,
    resolve_output_root,
    resolve_pause_file,
    resolve_voice_root,
)

ADAPTER_SCRIPT = PROJECT_ROOT / "adapter" / "heygem_index_tts2_adapter.py"
QUEUE_SCRIPT = PROJECT_ROOT / "core" / "heygem_queue_runner.py"
QUEUE_GUARD_SCRIPT = PROJECT_ROOT / "core" / "heygem-queue-guard.ps1"
LOG_DIR = PROJECT_ROOT / "logs"
COMPOSE_DIR = PROJECT_ROOT / "deploy"

TTS_BASE_URL = os.getenv("HEYGEM_TTS_BASE_URL", "http://127.0.0.1:18180").rstrip("/")
F2F_BASE_URL = os.getenv("HEYGEM_F2F_BASE_URL", "http://127.0.0.1:8383").rstrip("/")

_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE_DATA: dict[str, Any] | None = None
_HEALTH_CACHE_TS = 0.0
_HEALTH_CACHE_TTL = 4.0

_PS_PROC_CACHE: dict[str, tuple[float, list[int]]] = {}
_PS_PROC_CACHE_TTL = 12.0


def invalidate_health_cache() -> None:
    global _HEALTH_CACHE_TS
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE_TS = 0.0
    _PS_PROC_CACHE.clear()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_live_pid_file(pid_file: Path) -> int | None:
    if not pid_file.is_file():
        return None
    try:
        raw = pid_file.read_text(encoding="ascii", errors="ignore").strip()
        if raw.isdigit():
            pid = int(raw)
            if _is_pid_alive(pid):
                return pid
    except OSError:
        pass
    return None


def _http_get_json(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def get_tts_mode() -> str:
    mf = resolve_mode_file()
    if mf.exists():
        val = mf.read_text(encoding="utf-8", errors="ignore").strip().lower()
        if val in {"index-tts2", "fish-speech"}:
            return val
    return "index-tts2"


def set_tts_mode(mode: str) -> str:
    norm = (mode or "").strip().lower()
    if norm not in {"index-tts2", "fish-speech"}:
        raise ValueError("TTS 模式必须是 index-tts2 或 fish-speech")
    mf = resolve_mode_file()
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(norm, encoding="utf-8")
    invalidate_health_cache()
    return norm


def is_queue_paused() -> bool:
    return resolve_pause_file().exists()


def set_queue_paused(paused: bool) -> bool:
    pf = resolve_pause_file()
    if paused:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(f"paused_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
    else:
        pf.unlink(missing_ok=True)
    invalidate_health_cache()
    return pf.exists()


def _resolve_adapter_python() -> Path:
    override = os.getenv("HEYGEM_ADAPTER_PYTHON")
    if override and Path(override).exists():
        return Path(override)
    for cand in [
        PROJECT_ROOT / "adapter" / "venv" / "Scripts" / "python.exe",
        Path(r"D:\HeyGem\tts-story-f5\venv\Scripts\python.exe"),
    ]:
        if cand.exists():
            return cand
    return Path(sys.executable)


def _find_processes_by_cmdline(proc_names: list[str], keyword: str, use_cache: bool = False) -> list[int]:
    """用轻量级 PowerShell/CIM 查询匹配命令行的进程 PID（通过环境变量传关键字并排除 $PID，防止自匹配）。"""
    cache_key = f"{','.join(proc_names)}::{keyword}"
    now = time.time()
    if use_cache:
        cached = _PS_PROC_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _PS_PROC_CACHE_TTL:
            return [p for p in cached[1] if _is_pid_alive(p)]

    name_filter = " OR ".join(f"Name='{n}'" for n in proc_names)
    cmd = (
        "$kw = $env:HEYGEM_PROC_KEYWORD; "
        f"$p = Get-CimInstance Win32_Process -Filter \"{name_filter}\" -ErrorAction SilentlyContinue "
        "| Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like ('*' + $kw + '*') } "
        "| Select-Object -ExpandProperty ProcessId; "
        "if ($p) { $p -join ',' }"
    )
    env = os.environ.copy()
    env["HEYGEM_PROC_KEYWORD"] = keyword
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = (res.stdout or "").strip()
        self_pid = os.getpid()
        pids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        pids = [p for p in pids if p != self_pid and _is_pid_alive(p)]
        _PS_PROC_CACHE[cache_key] = (now, pids)
        return pids
    except Exception:
        return []


def _find_python_processes(keyword: str, use_cache: bool = False) -> list[int]:
    # 对队列调度器优先检查 PID 文件（0 子进程、0.1ms），避免每轮健康检查都拉起 powershell.exe
    if use_cache and keyword == "heygem_queue_runner.py":
        pid = _read_live_pid_file(PROJECT_ROOT / "heygem-queue-daemon.pid")
        if pid is not None:
            return [pid]
    return _find_processes_by_cmdline(["python.exe", "pythonw.exe"], keyword, use_cache=use_cache)


def _find_queue_guard_processes(use_cache: bool = False) -> list[int]:
    """查询队列守护脚本进程（优先走 PID 文件避免高频子进程）。"""
    pid = _read_live_pid_file(PROJECT_ROOT / "heygem-queue-guard.pid")
    if use_cache and pid is not None:
        return [pid]
    pids = _find_processes_by_cmdline(["powershell.exe", "pwsh.exe"], "heygem-queue-guard.ps1", use_cache=use_cache)
    if pid is not None and pid not in pids:
        pids.append(pid)
    return pids


def _resolve_docker_desktop_exe() -> Path | None:
    """定位本机 Docker Desktop 可执行文件路径。"""
    override = clean_local_path(os.getenv("DOCKER_DESKTOP_PATH"))
    if override and Path(override).is_file():
        return Path(override)
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for cand in [
        Path(prog_files) / "Docker" / "Docker" / "Docker Desktop.exe",
        Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe"),
    ]:
        if cand.is_file():
            return cand
    return None


def _is_docker_daemon_ready(timeout: float = 5.0) -> bool:
    """探测 Docker 后台守护引擎是否已响应。"""
    try:
        res = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0
    except Exception:
        return False


def _ensure_docker_desktop_ready(wait_seconds: int | None = None) -> tuple[bool, str]:
    """若 Docker 引擎未运行，则自动拉起 Docker Desktop 并轮询等待就绪。"""
    if _is_docker_daemon_ready():
        return True, "Docker 引擎已在线"

    exe = _resolve_docker_desktop_exe()
    if exe is None:
        return False, "Docker 引擎未运行且未找到 Docker Desktop 可执行文件（可通过环境变量 DOCKER_DESKTOP_PATH 指定）"

    try:
        subprocess.Popen([str(exe)])
    except Exception as exc:
        return False, f"拉起 Docker Desktop 失败：{exc}"

    if wait_seconds is not None:
        timeout_s = max(5, int(wait_seconds))
    else:
        raw_timeout = str(os.getenv("HEYGEM_DOCKER_READY_TIMEOUT", "120") or "").strip()
        try:
            timeout_s = max(5, int(raw_timeout))
        except ValueError:
            timeout_s = 120
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _is_docker_daemon_ready():
            return True, "已自动拉起 Docker Desktop 且引擎就绪"
        time.sleep(2.0)
    return False, f"已尝试拉起 Docker Desktop，但在 {timeout_s} 秒内引擎仍未就绪，请检查 Docker Desktop 状态"


def get_gpu_info() -> dict[str, Any]:
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
            if len(parts) >= 5:
                used = int(float(parts[1]))
                total = int(float(parts[2]))
                return {
                    "available": True,
                    "name": parts[0],
                    "memory_used_mb": used,
                    "memory_total_mb": total,
                    "memory_free_mb": max(0, total - used),
                    "memory_percent": round((used / total) * 100, 1) if total > 0 else 0,
                    "utilization_percent": int(float(parts[3])),
                    "temperature_c": int(float(parts[4])),
                }
    except Exception:
        pass
    return {"available": False, "name": "未知 GPU"}


def get_docker_status() -> dict[str, Any]:
    info: dict[str, Any] = {"daemon_ok": False, "containers": {}, "f2f_api_ok": False}
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0:
            info["daemon_ok"] = True
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and "duix" in parts[0].lower():
                    info["containers"][parts[0]] = {
                        "state": parts[1].strip().lower(),
                        "status": parts[2].strip(),
                        "running": parts[1].strip().lower() == "running",
                    }
    except Exception as exc:
        info["error"] = str(exc)

    probe = _http_get_json(f"{F2F_BASE_URL}/easy/query?code=__health_probe__", timeout=2.5)
    if probe is not None and int(probe.get("code", -1)) in (10000, 10004):
        info["f2f_api_ok"] = True
    return info


def get_tts_service_status() -> dict[str, Any]:
    mode = get_tts_mode()
    health = _http_get_json(f"{TTS_BASE_URL}/healthz", timeout=1.2)
    status = _http_get_json(f"{TTS_BASE_URL}/v1/tts/status", timeout=1.2) if health is not None else None
    ok = health is not None and str(health.get("status") or "").lower() == "ok"
    return {
        "mode": mode,
        "reachable": health is not None,
        "ok": ok,
        "health": health or {},
        "live_status": (status or {}).get("data") or {},
    }


def get_disk_status() -> list[dict[str, Any]]:
    checked: set[str] = set()
    items: list[dict[str, Any]] = []
    for label, path in [("渲染缓存盘", resolve_duix_data_dir()), ("成片交付盘", resolve_delivery_root())]:
        anchor = str(path.anchor or path)[:3].upper()
        if anchor in checked:
            continue
        checked.add(anchor)
        try:
            target = path if path.exists() else Path(anchor)
            usage = shutil.disk_usage(target)
            items.append(
                {
                    "label": label,
                    "drive": anchor,
                    "free_gb": round(usage.free / (1024**3), 1),
                    "total_gb": round(usage.total / (1024**3), 1),
                    "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
                }
            )
        except Exception:
            pass
    return items


def get_system_health(force: bool = False) -> dict[str, Any]:
    global _HEALTH_CACHE_DATA, _HEALTH_CACHE_TS
    now = time.time()
    if not force:
        with _HEALTH_CACHE_LOCK:
            if _HEALTH_CACHE_DATA is not None and (now - _HEALTH_CACHE_TS) < _HEALTH_CACHE_TTL:
                return _HEALTH_CACHE_DATA

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_docker = pool.submit(get_docker_status)
        f_tts = pool.submit(get_tts_service_status)
        f_gpu = pool.submit(get_gpu_info)
        f_queue = pool.submit(_find_python_processes, "heygem_queue_runner.py", not force)
        f_guard = pool.submit(_find_queue_guard_processes, not force)
        docker_info = f_docker.result()
        tts_info = f_tts.result()
        gpu_info = f_gpu.result()
        queue_pids = f_queue.result()
        guard_pids = f_guard.result()
    disks = get_disk_status()
    queue_paused = is_queue_paused()

    # 统计数据库状态与底模完整性
    job_counts: dict[str, int] = {}
    missing_models: list[dict[str, Any]] = []
    total_models = 0
    output_root = resolve_output_root()
    try:
        conn = get_conn()
        try:
            for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM video GROUP BY status").fetchall():
                job_counts[str(row["status"] or "unknown")] = int(row["cnt"])
            models = conn.execute("SELECT id, name, video_path FROM f2f_model ORDER BY id DESC").fetchall()
            total_models = len(models)
            for m in models:
                vp = str(m["video_path"] or "").strip()
                if not vp or not (output_root / vp.lstrip("/\\")).exists():
                    missing_models.append({"id": int(m["id"]), "name": m["name"], "video_path": vp})
        finally:
            conn.close()
    except Exception:
        pass

    alerts: list[dict[str, str]] = []
    if not docker_info.get("daemon_ok"):
        alerts.append(
            {
                "severity": "error",
                "code": "DOCKER_DAEMON_DOWN",
                "message": "Docker 引擎未启动，无法进行数字人视频渲染。请点击「一键启动服务」或先启动 Docker Desktop。",
            }
        )
    elif not docker_info.get("f2f_api_ok"):
        alerts.append(
            {
                "severity": "error",
                "code": "GEN_VIDEO_DOWN",
                "message": "数字人视频渲染引擎 (duix-avatar-gen-video / 8383) 未运行，点击「一键启动服务」即可拉起。",
            }
        )

    if tts_info["mode"] == "index-tts2" and not tts_info["ok"]:
        alerts.append(
            {
                "severity": "error",
                "code": "INDEXTTS2_DOWN",
                "message": "IndexTTS2 本地语音服务 (18180) 未就绪，无法进行文本配音；点击「一键自愈恢复」即可后台无框拉起。",
            }
        )

    if not queue_pids:
        alerts.append(
            {
                "severity": "warning",
                "code": "QUEUE_DAEMON_DOWN",
                "message": "后台任务队列调度器未运行，新提交的视频任务将停留在排队中；点击「一键自愈恢复」可立即拉起。",
            }
        )
    elif queue_paused:
        alerts.append(
            {
                "severity": "info",
                "code": "QUEUE_PAUSED",
                "message": "任务队列当前处于【暂停派发】状态，点击顶部「恢复队列」即可继续处理排队任务。",
            }
        )

    failed_cnt = job_counts.get("failed", 0)
    if failed_cnt > 0:
        alerts.append(
            {
                "severity": "warning",
                "code": "FAILED_JOBS",
                "message": f"当前有 {failed_cnt} 个失败任务，请打开「任务监控」查看具体报错原因或一键重试。",
            }
        )

    if missing_models:
        alerts.append(
            {
                "severity": "warning",
                "code": "MODEL_FILES_MISSING",
                "message": f"检测到 {len(missing_models)}/{total_models} 个数字人模特的底模视频文件在 {output_root} 中缺失，请在「模特与音色」页面重新关联或导入。",
            }
        )

    for d in disks:
        if d["free_gb"] < 15.0:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "LOW_DISK",
                    "message": f"{d['label']} ({d['drive']}) 仅剩 {d['free_gb']} GB，建议及时清理临时缓存。",
                }
            )

    overall = "healthy"
    if any(a["severity"] == "error" for a in alerts):
        overall = "error"
    elif any(a["severity"] == "warning" for a in alerts):
        overall = "warning"

    result = {
        "overall": overall,
        "timestamp": int(time.time()),
        "docker": docker_info,
        "tts": tts_info,
        "queue": {
            "running": bool(queue_pids),
            "pids": queue_pids,
            "paused": queue_paused,
            "guard_running": bool(guard_pids),
            "guard_pids": guard_pids,
        },
        "gpu": gpu_info,
        "disks": disks,
        "job_counts": job_counts,
        "models_integrity": {
            "total": total_models,
            "missing_count": len(missing_models),
            "missing": missing_models[:20],
        },
        "paths": {
            "db": str(resolve_db_path()),
            "output_root": str(output_root),
            "voice_root": str(resolve_voice_root()),
            "delivery_root": str(resolve_delivery_root()),
        },
        "alerts": alerts,
    }
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE_DATA = result
        _HEALTH_CACHE_TS = time.time()
    return result


def start_tts_adapter_silent(wait_seconds: int = 30) -> tuple[bool, str]:
    existing = _http_get_json(f"{TTS_BASE_URL}/healthz", timeout=2.0)
    if existing and str(existing.get("status") or "").lower() == "ok":
        return True, "IndexTTS2 语音服务已在运行中"

    if not ADAPTER_SCRIPT.exists():
        return False, f"未找到适配器脚本: {ADAPTER_SCRIPT}"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_log = LOG_DIR / "heygem-index-tts2.out.log"
    err_log = LOG_DIR / "heygem-index-tts2.err.log"
    py_exe = _resolve_adapter_python()

    with open(out_log, "a", encoding="utf-8") as fout, open(err_log, "a", encoding="utf-8") as ferr:
        proc = subprocess.Popen(
            [str(py_exe), str(ADAPTER_SCRIPT)],
            cwd=str(ADAPTER_SCRIPT.parent),
            stdout=fout,
            stderr=ferr,
            creationflags=CREATE_NO_WINDOW,
        )
    (PROJECT_ROOT / "heygem-index-tts2.pid").write_text(str(proc.pid), encoding="ascii")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        h = _http_get_json(f"{TTS_BASE_URL}/healthz", timeout=2.0)
        if h is not None:
            ok = str(h.get("status") or "").lower() == "ok"
            reason = str(h.get("unavailable_reason") or "")
            return ok, f"IndexTTS2 语音服务已启动 (PID={proc.pid}, state={h.get('status')})" + (f" - {reason}" if reason else "")
        time.sleep(1.0)
    return False, f"IndexTTS2 语音服务在 {wait_seconds} 秒内未响应，请查看 logs/heygem-index-tts2.err.log"


def start_queue_daemon_silent() -> tuple[bool, str]:
    pids = _find_python_processes("heygem_queue_runner.py")
    if pids:
        return True, f"后台队列调度器已在运行中 (PID={pids[0]})"

    if not QUEUE_SCRIPT.exists():
        return False, f"未找到队列脚本: {QUEUE_SCRIPT}"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_log = LOG_DIR / "heygem-queue-daemon.out.log"
    err_log = LOG_DIR / "heygem-queue-daemon.err.log"

    env = os.environ.copy()
    env["HEYGEM_MODE_FILE"] = str(resolve_mode_file())
    with open(out_log, "a", encoding="utf-8") as fout, open(err_log, "a", encoding="utf-8") as ferr:
        proc = subprocess.Popen(
            [sys.executable, str(QUEUE_SCRIPT), "--daemon"],
            cwd=str(QUEUE_SCRIPT.parent),
            stdout=fout,
            stderr=ferr,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    pid_file = PROJECT_ROOT / "heygem-queue-daemon.pid"
    pid_file.write_text(str(proc.pid), encoding="ascii")
    time.sleep(1.0)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        return False, "队列调度器启动后立刻退出，请查看 logs/heygem-queue-daemon.err.log"
    return True, f"后台队列调度器已无框启动 (PID={proc.pid})"


def start_queue_guard_silent() -> tuple[bool, str]:
    """后台无框拉起 PowerShell 队列守护进程，确保停服再启后仍保留自动拉活自愈能力。"""
    pids = _find_queue_guard_processes(use_cache=False)
    if pids:
        return True, f"后台队列守护进程已在运行中 (PID={pids[0]})"

    if not QUEUE_GUARD_SCRIPT.exists():
        return False, f"未找到队列守护脚本: {QUEUE_GUARD_SCRIPT}"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HEYGEM_MODE_FILE"] = str(resolve_mode_file())
    try:
        proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(QUEUE_GUARD_SCRIPT),
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return False, f"启动队列守护进程失败：{exc}"

    pid_file = PROJECT_ROOT / "heygem-queue-guard.pid"
    pid_file.write_text(str(proc.pid), encoding="ascii")
    time.sleep(0.6)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        return False, "后台队列守护进程启动后立即退出，请查看 logs/heygem-queue-guard.log"
    return True, f"后台队列守护进程已无框启动 (PID={proc.pid})"


def start_docker_services(restart_gen_video: bool = False, auto_start_docker: bool = True) -> tuple[bool, str]:
    if not _is_docker_daemon_ready():
        if not auto_start_docker:
            return False, "Docker 引擎未运行，请先启动 Docker Desktop"
        ok_dk, detail_dk = _ensure_docker_desktop_ready()
        if not ok_dk:
            return False, detail_dk

    env = os.environ.copy()
    env["DUIX_DATA_DIR"] = str(resolve_duix_data_dir()).replace("\\", "/")
    tts_mode = get_tts_mode()
    services = ["duix-avatar-asr", "duix-avatar-gen-video"] if tts_mode == "index-tts2" else ["duix-avatar-tts", "duix-avatar-asr", "duix-avatar-gen-video"]

    if restart_gen_video:
        # 先确保全部必要容器存在且启动，再重启渲染容器释放显存与卡死线程
        up_res = subprocess.run(
            ["docker", "compose", "up", "-d", *services],
            cwd=str(COMPOSE_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if up_res.returncode != 0:
            return False, f"拉起 Docker 容器失败: {(up_res.stderr or up_res.stdout).strip()[:240]}"
        res = subprocess.run(
            ["docker", "restart", "duix-avatar-gen-video"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0:
            return False, f"重启渲染容器失败: {(res.stderr or res.stdout).strip()[:240]}"
    else:
        res = subprocess.run(
            ["docker", "compose", "up", "-d", *services],
            cwd=str(COMPOSE_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0:
            return False, f"启动 Docker 容器失败（请先确认 Docker Desktop 已启动）: {(res.stderr or res.stdout).strip()[:240]}"

    # 在 index-tts2 模式下停掉可能残留的 duix-avatar-tts 容器，避免占用 18180 端口冲突
    if tts_mode == "index-tts2":
        try:
            subprocess.run(
                ["docker", "compose", "stop", "duix-avatar-tts"],
                cwd=str(COMPOSE_DIR),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass

    deadline = time.time() + 45
    while time.time() < deadline:
        probe = _http_get_json(f"{F2F_BASE_URL}/easy/query?code=__health_probe__", timeout=2.5)
        if probe is not None and int(probe.get("code", -1)) in (10000, 10004):
            return True, "数字人渲染容器 (8383) 已就绪"
        time.sleep(2.0)
    return False, "容器已启动但 8383 接口在 45 秒内未就绪，可能仍在加载模型，请稍后刷新状态"


def self_heal_recover(restart_render_container: bool = False, requeue_stuck: bool = True) -> dict[str, Any]:
    """一键自愈恢复：检查并修复数据库触发器、Docker 渲染容器、IndexTTS2 适配器、队列调度与守护进程以及僵尸任务。"""
    steps: list[dict[str, Any]] = []

    try:
        init_studio_schema()
        steps.append({"step": "数据库架构自检", "ok": True, "detail": "biz.db 与 Studio 扩展表正常"})
    except Exception as exc:
        steps.append({"step": "数据库架构自检", "ok": False, "detail": str(exc)})

    docker_info = get_docker_status()
    if restart_render_container or not docker_info.get("f2f_api_ok"):
        ok, detail = start_docker_services(restart_gen_video=restart_render_container and docker_info.get("daemon_ok", False))
        steps.append({"step": "视频渲染引擎 (Docker 8383)", "ok": ok, "detail": detail})
    else:
        steps.append({"step": "视频渲染引擎 (Docker 8383)", "ok": True, "detail": "已处于健康在线状态"})

    if get_tts_mode() == "index-tts2":
        ok, detail = start_tts_adapter_silent(wait_seconds=25)
        steps.append({"step": "IndexTTS2 语音服务 (18180)", "ok": ok, "detail": detail})

    ok, detail = start_queue_daemon_silent()
    steps.append({"step": "后台任务队列调度器", "ok": ok, "detail": detail})

    ok_guard, detail_guard = start_queue_guard_silent()
    steps.append({"step": "后台队列守护进程 (Guard)", "ok": ok_guard, "detail": detail_guard})

    recovered_count = 0
    if requeue_stuck:
        tts_now = get_tts_service_status()
        tts_busy = str((tts_now.get("live_status") or {}).get("state") or "idle") in ("loading_model", "synthesizing", "postprocessing")
        now_sec = time.time()
        conn = get_conn()
        try:
            # 仅重置真正失活的无 code 僵尸 pending（避免误杀当前正在 IndexTTS2 合成中的活跃任务）
            rows = conn.execute(
                """
                SELECT v.id, v.created_at, m.heartbeat_at, m.phase
                FROM video v
                LEFT JOIN studio_job_meta m ON m.video_id = v.id
                WHERE v.status='pending' AND COALESCE(trim(v.code), '') = ''
                """
            ).fetchall()
            stale_ids: list[int] = []
            for r in rows:
                hb = float(r["heartbeat_at"] or 0.0)
                created_sec = float(r["created_at"] or 0) / 1000.0
                last_seen = hb if hb > 0 else created_sec
                age = max(0.0, now_sec - last_seen) if last_seen > 0 else 9999.0
                phase = str(r["phase"] or "")
                # 如果 TTS 正在忙碌且最近 180s 内有心跳，说明它正在正常配音，绝不误重置
                if tts_busy and phase in ("tts_loading", "tts_synthesizing") and age < 180.0:
                    continue
                if age >= 120.0 or not tts_busy:
                    stale_ids.append(int(r["id"]))

            for vid in stale_ids:
                conn.execute(
                    "UPDATE video SET status='waiting', message='一键自愈：重置异常挂起任务', progress=0, code=NULL, param=NULL WHERE id=?",
                    (vid,),
                )
                conn.execute(
                    "UPDATE studio_job_meta SET phase='queued', stage_label='一键自愈：已重排队列', progress_float=0.0, updated_at=? WHERE video_id=?",
                    (int(now_sec * 1000), vid),
                )
            recovered_count = len(stale_ids)
            conn.commit()
        finally:
            conn.close()
        steps.append(
            {
                "step": "僵尸任务扫描",
                "ok": True,
                "detail": f"已重置 {recovered_count} 个无任务码挂起任务" if recovered_count else "未发现僵尸挂起任务",
            }
        )

    invalidate_health_cache()
    return {
        "ok": all(s["ok"] for s in steps),
        "steps": steps,
        "health": get_system_health(force=True),
    }


def stop_background_services() -> dict[str, Any]:
    """优雅停止守护脚本、队列调度器、IndexTTS2 适配器与 Worker 以及 Docker 业务容器（不关闭当前 Studio 网页服务本身）。"""
    steps: list[dict[str, Any]] = []

    # 1. 先停 PowerShell 队列守护脚本，防止刚杀掉调度器/适配器就被守护脚本在 10 秒后自动复活
    guard_pids = _find_processes_by_cmdline(["powershell.exe", "pwsh.exe"], "heygem-queue-guard.ps1", use_cache=False)
    pid_guard = _read_live_pid_file(PROJECT_ROOT / "heygem-queue-guard.pid")
    if pid_guard and pid_guard not in guard_pids:
        guard_pids.append(pid_guard)
    for pid in guard_pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    (PROJECT_ROOT / "heygem-queue-guard.pid").unlink(missing_ok=True)
    steps.append({"step": "停止队列守护进程", "ok": True, "detail": f"已停止 PID: {guard_pids}" if guard_pids else "未在运行"})

    # 2. 停止队列调度器、活跃 TTS Worker 与 18180 适配器
    for label, pattern, pid_name in [
        ("队列调度器", "heygem_queue_runner.py", "heygem-queue-daemon.pid"),
        ("IndexTTS2 推理 Worker", "indextts2_studio_worker.py", ""),
        ("IndexTTS2 适配器", "heygem_index_tts2_adapter.py", "heygem-index-tts2.pid"),
    ]:
        pids = _find_python_processes(pattern, use_cache=False)
        if pid_name:
            fpid = _read_live_pid_file(PROJECT_ROOT / pid_name)
            if fpid and fpid not in pids:
                pids.append(fpid)
            (PROJECT_ROOT / pid_name).unlink(missing_ok=True)
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        steps.append({"step": f"停止 {label}", "ok": True, "detail": f"已停止 PID: {pids}" if pids else "未在运行"})

    if not _is_docker_daemon_ready():
        steps.append({"step": "停止 Docker 容器", "ok": True, "detail": "Docker 引擎未运行，已跳过容器停止"})
    else:
        env = os.environ.copy()
        env["DUIX_DATA_DIR"] = str(resolve_duix_data_dir()).replace("\\", "/")
        res = subprocess.run(
            ["docker", "compose", "stop"],
            cwd=str(COMPOSE_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
        steps.append(
            {
                "step": "停止 Docker 容器",
                "ok": res.returncode == 0,
                "detail": "容器已停止" if res.returncode == 0 else (res.stderr or res.stdout).strip()[:200],
            }
        )
    invalidate_health_cache()
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


def _tail_file(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception as exc:
        return [f"读取日志失败: {exc}"]


def read_service_logs(source: str = "queue", tail: int = 120) -> dict[str, Any]:
    tail = max(10, min(500, int(tail)))
    src = (source or "queue").strip().lower()
    if src == "queue":
        lines = _tail_file(LOG_DIR / "heygem-queue-daemon.out.log", tail)
        err = _tail_file(LOG_DIR / "heygem-queue-daemon.err.log", 40)
        if err:
            lines = lines + ["--- STDERR ---"] + err
        return {"source": "queue", "lines": lines[-tail:]}

    if src == "tts":
        status = _http_get_json(f"{TTS_BASE_URL}/v1/tts/status", timeout=2.0) or {}
        live_logs = (status.get("data") or {}).get("recent_logs") or []
        out_logs = _tail_file(LOG_DIR / "heygem-index-tts2.out.log", tail // 2)
        err_logs = _tail_file(LOG_DIR / "heygem-index-tts2.err.log", tail // 2)
        combined = out_logs + (["--- STDERR ---"] + err_logs if err_logs else []) + (["--- LIVE ---"] + live_logs if live_logs else [])
        return {"source": "tts", "lines": combined[-tail:]}

    if src == "docker":
        try:
            res = subprocess.run(
                ["docker", "logs", "--tail", str(tail * 4), "duix-avatar-gen-video"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            blob = "\n".join(p for p in ((res.stdout or ""), (res.stderr or "")) if p)
            filtered = [ln for ln in blob.splitlines() if "/easy/query" not in ln and ln.strip()]
            return {"source": "docker", "lines": filtered[-tail:]}
        except Exception as exc:
            return {"source": "docker", "lines": [f"获取 Docker 日志失败: {exc}"]}

    if src == "guard":
        return {"source": "guard", "lines": _tail_file(LOG_DIR / "heygem-queue-guard.log", tail)}

    return {"source": src, "lines": [f"未知日志源: {src}"]}


def open_path_in_explorer(target_path: str) -> tuple[bool, str]:
    cleaned = clean_local_path(target_path)
    if not cleaned:
        return False, "路径为空"
    p = Path(cleaned)
    if p.is_file():
        subprocess.Popen(["explorer.exe", f"/select,{p}"], creationflags=CREATE_NO_WINDOW)
        return True, f"已在资源管理器定位文件: {p}"
    if p.is_dir():
        subprocess.Popen(["explorer.exe", str(p)], creationflags=CREATE_NO_WINDOW)
        return True, f"已在资源管理器打开目录: {p}"
    if p.parent.exists():
        subprocess.Popen(["explorer.exe", str(p.parent)], creationflags=CREATE_NO_WINDOW)
        return True, f"文件不存在，已打开其上级目录: {p.parent}"
    return False, f"目标路径不存在: {p}"
