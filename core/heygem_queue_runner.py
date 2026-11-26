from __future__ import annotations

import argparse
import io
import json
import os
import msvcrt
import re
import sqlite3
import subprocess
import shutil
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import wave
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

try:
    from studio.db import (
        init_studio_schema,
        probe_media_info,
        resolve_pause_file,
        sanitize_filename,
        upsert_job_meta,
    )
except Exception:
    init_studio_schema = None  # type: ignore[assignment]
    probe_media_info = None  # type: ignore[assignment]
    resolve_pause_file = None  # type: ignore[assignment]
    sanitize_filename = None  # type: ignore[assignment]
    upsert_job_meta = None  # type: ignore[assignment]


def _resolve_db_path() -> Path:
    override = os.getenv("HEYGEM_DB_OVERRIDE")
    if override:
        return Path(override)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "biz.db"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "biz.db"

def _resolve_output_root() -> Path:
    override = os.getenv("HEYGEM_OUTPUT_ROOT") or os.getenv("HEYGEM_OUT_OVERRIDE")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/face2face/temp"), Path("C:/duix_avatar_data/face2face/temp")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/face2face/temp")

def _resolve_delivery_root() -> Path:
    override = os.getenv("HEYGEM_DELIVERY_ROOT")
    if override:
        return Path(override)
    for cand in [Path("E:/数字人视频文件"), Path("D:/数字人视频文件")]:
        if cand.exists() or cand.parent.exists():
            return cand
    return Path.home() / "Desktop" / "数字人视频文件"

DB_PATH = _resolve_db_path()
OUTPUT_ROOT = _resolve_output_root()
DELIVERY_ROOT = _resolve_delivery_root()
_MODE_FILE_CACHE: Path | None = None

def _mode_file_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_value = str(os.getenv("HEYGEM_MODE_FILE") or "").strip()
    if env_value:
        candidates.append(Path(env_value))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "tts-mode.txt")
    candidates.append(repo_root / "scripts" / "tts-mode.txt")
    candidates.append(repo_root / "launchers" / "tts-mode.txt")
    desktop = Path.home() / "Desktop"
    candidates.append(desktop / "HeyGem-OneClick" / "tts-mode.txt")
    candidates.append(desktop / "HeyGem-常用启动器" / "tts-mode.txt")
    if desktop.exists():
        try:
            candidates.extend(found for found in desktop.rglob("tts-mode.txt") if found.parent.name.lower() in ("heygem-oneclick", "heygem-常用启动器"))
        except OSError:
            pass
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result

def resolve_mode_file() -> Path | None:
    global _MODE_FILE_CACHE
    if _MODE_FILE_CACHE is not None and _MODE_FILE_CACHE.exists():
        return _MODE_FILE_CACHE
    candidates = _mode_file_candidates()
    for candidate in candidates:
        if candidate.exists():
            _MODE_FILE_CACHE = candidate
            return candidate
    return candidates[0] if candidates else None
TTS_INVOKE_URL = "http://127.0.0.1:18180/v1/invoke"
TTS_STATUS_URL = "http://127.0.0.1:18180/v1/tts/status"
TTS_CANCEL_URL = "http://127.0.0.1:18180/v1/tts/cancel"
F2F_SUBMIT_URL = "http://127.0.0.1:8383/easy/submit"
F2F_QUERY_URL = "http://127.0.0.1:8383/easy/query?code={code}"
DEFAULT_MAX_RETRIES = 3
TTS_SINGLE_BATCH_TIMEOUT_SECONDS = 1800
def _resolve_ffprobe_exe() -> Path:
    override = os.getenv("HEYGEM_FFPROBE_EXE")
    if override and Path(override).exists():
        return Path(override)
    which_ffprobe = shutil.which("ffprobe")
    if which_ffprobe:
        return Path(which_ffprobe)
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        Path(prog_files) / "Duix.Avatar" / "resources" / "app.asar.unpacked" / "resources" / "ffmpeg" / "win-amd64" / "bin" / "ffprobe.exe",
        Path(r"C:\Program Files\Duix.Avatar\resources\app.asar.unpacked\resources\ffmpeg\win-amd64\bin\ffprobe.exe"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0]

def _resolve_host_shared_voice_root() -> Path:
    override = os.getenv("HEYGEM_VOICE_ROOT") or os.getenv("HEYGEM_VOICE_ROOT_OVERRIDE")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/voice/data"), Path("C:/duix_avatar_data/voice/data")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/voice/data")

def _resolve_adapter_script() -> Path:
    override = os.getenv("HEYGEM_ADAPTER_SCRIPT")
    if override and Path(override).exists():
        return Path(override)
    repo_root = Path(__file__).resolve().parent.parent
    local_adapter = repo_root / "adapter" / "heygem_index_tts2_adapter.py"
    if local_adapter.exists():
        return local_adapter
    cand = Path(r"D:\HeyGem\tts-story-f5\heygem_index_tts2_adapter.py")
    if cand.exists():
        return cand
    return local_adapter

def _resolve_adapter_python() -> Path:
    override = os.getenv("HEYGEM_ADAPTER_PYTHON")
    if override and Path(override).exists():
        return Path(override)
    repo_root = Path(__file__).resolve().parent.parent
    local_venv = repo_root / "adapter" / "venv" / "Scripts" / "python.exe"
    if local_venv.exists():
        return local_venv
    cand_venv = Path(r"D:\HeyGem\tts-story-f5\venv\Scripts\python.exe")
    if cand_venv.exists():
        return cand_venv
    which_py = shutil.which("python")
    if which_py:
        return Path(which_py)
    return Path(sys.executable)

def _resolve_runner_lock_file() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "heygem_queue_runner.lock"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "heygem_queue_runner.lock"

FFPROBE_EXE = _resolve_ffprobe_exe()
POLL_SECONDS = 2
DAEMON_IDLE_SECONDS = 6
PENDING_WITHOUT_CODE_STUCK_SECONDS = 180
PENDING_WITHOUT_CODE_CLIENT_STUCK_SECONDS = 60
PENDING_PREPARING_AUDIO_STUCK_SECONDS = 900
RUNNER_NO_CODE_SLOT_STALE_SECONDS = 45
PENDING_WITH_CODE_TIMEOUT_SECONDS = 2700
INVALID_CODE_VALUES = {"", "null", "none", "undefined", "nan"}
HOST_SHARED_VOICE_ROOT = _resolve_host_shared_voice_root()
HOST_SHARED_VOICE_ROOTS = (
    str(HOST_SHARED_VOICE_ROOT).replace("\\", "/").lower(),
    "d:/duix_avatar_data/voice/data",
    "c:/duix_avatar_data/voice/data",
)
CONTAINER_SHARED_VOICE_ROOT = "/code/data"
TTS_REQUEST_TIMEOUT_SECONDS = 300
TTS_MAX_RETRIES = 4
TTS_FIXED_SEED = 1
TTS_TEXT_SPLIT_THRESHOLD_CHARS = 240
TTS_TEXT_SEGMENT_MAX_CHARS = 160
TTS_TEXT_SEGMENT_SILENCE_SECONDS = 0.08
TTS_REFERENCE_MAX_SECONDS = 45.0
TTS_REFERENCE_CLIP_SECONDS = 18.0
FISH_REFERENCE_MIN_COUNT = 2
FISH_REFERENCE_MAX_COUNT = 3
FISH_REFERENCE_THREE_MIN_FREE_MB = 4500
SUBMIT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3
CONTAINER_RESTART_COOLDOWN_SECONDS = 120
SUBMIT_BUSY_RETRY_DELAY_SECONDS = 12
BACKEND_BUSY_BASE_COOLDOWN_SECONDS = 30
BACKEND_BUSY_MAX_COOLDOWN_SECONDS = 300
BACKEND_BUSY_FORCE_RESTART_STREAK = 5  # retained for compatibility; busy alone no longer restarts rendering
CLIENT_SUBMIT_GRACE_SECONDS = 8
BACKEND_10004_GRACE_SECONDS = 180
BACKEND_PROGRESS_STALL_CHECK_SECONDS = 180
BACKEND_PROGRESS_STALL_REQUEUE_SECONDS = 1800
GEN_VIDEO_ACTIVITY_LOOKBACK_SECONDS = 240
GEN_VIDEO_ACTIVITY_STALE_SECONDS = 180
GEN_VIDEO_ACTIVITY_CACHE_TTL_SECONDS = 20
TTS_CONTAINER_NAME = "duix-avatar-tts"
GEN_VIDEO_CONTAINER_NAME = "duix-avatar-gen-video"
INDEX_TTS_HEALTH_URL = "http://127.0.0.1:18180/healthz"
INDEX_TTS_ADAPTER_PYTHON = _resolve_adapter_python()
INDEX_TTS_ADAPTER_SCRIPT = _resolve_adapter_script()
INDEX_TTS_ADAPTER_START_COOLDOWN_SECONDS = 45
INDEX_TTS_READY_TIMEOUT_SECONDS = 45
QUEUE_OWNER = "runner"
RUNNER_LOCK_FILE = _resolve_runner_lock_file()
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LAST_CONTAINER_RESTART_AT: dict[str, float] = {}
_LAST_INDEX_TTS_START_AT = 0.0
_BACKEND_BUSY_UNTIL = 0.0
_BACKEND_BUSY_STREAK = 0
_LAST_BUSY_WAIT_LOG_AT = 0.0
_LAST_GEN_VIDEO_ACTIVITY_FETCH_AT = 0.0
_LAST_GEN_VIDEO_ACTIVITY: dict[str, float] = {}
_RUNNER_LOCK_HANDLE: io.TextIOWrapper | None = None


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_text()}] {msg}", flush=True)


def acquire_runner_lock() -> bool:
    global _RUNNER_LOCK_HANDLE

    try:
        RUNNER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    fh = open(RUNNER_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        owner = ""
        try:
            fh.seek(0)
            owner = fh.read().strip()
        except Exception:
            owner = ""
        fh.close()
        suffix = f" ({owner})" if owner else ""
        log(f"Another queue runner is already active{suffix}, exit current process.")
        return False

    lock_meta = f"pid={os.getpid()} at={now_text()}"
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(lock_meta)
        fh.flush()
    except Exception:
        pass

    _RUNNER_LOCK_HANDLE = fh
    return True


def release_runner_lock() -> None:
    global _RUNNER_LOCK_HANDLE
    fh = _RUNNER_LOCK_HANDLE
    _RUNNER_LOCK_HANDLE = None
    if fh is None:
        return
    try:
        fh.seek(0)
        fh.truncate()
        fh.write("")
        fh.flush()
    except Exception:
        pass
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def split_tts_text_for_safety(text: object) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return [""]

    split_threshold = max(1, TTS_TEXT_SPLIT_THRESHOLD_CHARS)
    max_chars = max(40, TTS_TEXT_SEGMENT_MAX_CHARS)
    if len(raw) < split_threshold:
        return [raw]

    pieces = re.split(r"(?<=[。！？!?；;，,\n])", raw)
    chunks: list[str] = []
    current = ""

    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue

        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(piece), max_chars):
                chunks.append(piece[i : i + max_chars])
            continue

        if len(current) + len(piece) > max_chars:
            if current:
                chunks.append(current)
            current = piece
        else:
            current += piece

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk.strip()] or [raw]


def _read_wav_payload(payload: bytes) -> tuple[tuple[int, int, int], bytes]:
    with wave.open(io.BytesIO(payload), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        frame_rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())
    return (channels, sample_width, frame_rate), frames


def merge_wav_payloads(payloads: list[bytes], video_id: int) -> bytes:
    if not payloads:
        return b""
    if len(payloads) == 1:
        return payloads[0]

    try:
        base_fmt, _ = _read_wav_payload(payloads[0])
    except Exception as exc:
        log(f"video {video_id} wav merge fallback (first chunk not wav): {exc}")
        return payloads[0]

    channels, sample_width, frame_rate = base_fmt
    silence_frames = max(0, int(frame_rate * max(0.0, TTS_TEXT_SEGMENT_SILENCE_SECONDS)))
    silence_bytes = b"\x00" * (silence_frames * channels * sample_width)

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(frame_rate)

        for idx, payload in enumerate(payloads):
            try:
                fmt, frames = _read_wav_payload(payload)
            except Exception as exc:
                log(f"video {video_id} wav merge skipped chunk#{idx + 1}: {exc}")
                continue

            if fmt != base_fmt:
                log(f"video {video_id} wav merge skipped chunk#{idx + 1}: format mismatch {fmt} != {base_fmt}")
                continue

            if idx > 0 and silence_bytes:
                writer.writeframes(silence_bytes)
            writer.writeframes(frames)

    merged = output.getvalue()
    if not merged:
        return payloads[0]
    return merged


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
    except Exception:
        pass
    return conn


def get_pause_file() -> Path:
    if resolve_pause_file is not None:
        return resolve_pause_file()
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "heygem_queue_runner.paused"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "heygem_queue_runner.paused"


def is_queue_paused() -> bool:
    try:
        return get_pause_file().exists()
    except OSError:
        return False


def meta_update(video_id: int, **fields: object) -> None:
    if upsert_job_meta is None:
        return
    try:
        upsert_job_meta(video_id, db_path=DB_PATH, **fields)
    except Exception as exc:
        log(f"meta_update failed for video {video_id}: {exc}")


def get_job_meta(video_id: int) -> dict[str, object]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM studio_job_meta WHERE video_id=?", (video_id,)).fetchone()
        return dict(row) if row is not None else {}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def notify_webhook_if_needed(video_id: int, event: str, payload: dict[str, object]) -> None:
    meta = get_job_meta(video_id)
    url = str(meta.get("webhook_url") or "").strip()
    if not url or int(meta.get("webhook_notified") or 0) == 1:
        return
    body = json.dumps(
        {
            "event": event,
            "video_id": video_id,
            "timestamp": int(time.time()),
            "data": payload,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read(256)
        meta_update(video_id, webhook_notified=1)
        log(f"video {video_id} webhook notified ({event}) -> {url}")
    except Exception as exc:
        log(f"video {video_id} webhook failed ({url}): {exc}")


def get_primary_gpu_memory_mb() -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    line = next((ln.strip() for ln in result.stdout.splitlines() if ln.strip()), "")
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None
    try:
        total_mb = int(float(parts[0]))
        used_mb = int(float(parts[1]))
    except Exception:
        return None
    return total_mb, used_mb


def choose_fish_reference_count() -> int:
    info = get_primary_gpu_memory_mb()
    if info is None:
        return FISH_REFERENCE_MIN_COUNT
    total_mb, used_mb = info
    free_mb = max(0, total_mb - used_mb)
    if total_mb <= 13000 or free_mb < FISH_REFERENCE_THREE_MIN_FREE_MB:
        return FISH_REFERENCE_MIN_COUNT
    return FISH_REFERENCE_MAX_COUNT


def http_post_json(url: str, payload: dict, timeout: int = 3600) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8", errors="replace"))


def http_post_binary(url: str, payload: dict, timeout: int = 3600) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def http_get_json(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8", errors="replace"))


def normalize_code(value: object) -> str:
    return str(value or "").strip()


def is_valid_task_code(value: object) -> bool:
    code = normalize_code(value)
    return bool(code) and code.lower() not in INVALID_CODE_VALUES


def get_tts_mode() -> str:
    mode_file = resolve_mode_file()
    if mode_file is None:
        return "fish-speech"
    try:
        mode = mode_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
    except OSError:
        return "fish-speech"
    return mode if mode in {"fish-speech", "index-tts2"} else "fish-speech"


def _path_exists_for_mode(path: str, tts_mode: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False

    host_candidate = _container_voice_to_host_path(normalized)
    if host_candidate is not None and host_candidate.exists():
        return True

    if tts_mode != "fish-speech":
        local_candidate = Path(normalized)
        if local_candidate.is_absolute() and local_candidate.exists():
            return True

    return False


def _pick_best_existing_path(candidates: list[str], tts_mode: str) -> str:
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip().replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)

    for candidate in deduped:
        if _path_exists_for_mode(candidate, tts_mode):
            return candidate

    return deduped[0] if deduped else ""


def _session_piece_to_origin_candidates(piece: str, tts_mode: str) -> list[str]:
    normalized = str(piece or "").strip().replace("\\", "/")
    if not normalized:
        return []

    base_name = Path(normalized).name
    if not base_name:
        return []

    base_name = re.sub(r"_part\d+(\.[A-Za-z0-9]+)$", r"\1", base_name)
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix or ".wav"

    container_candidates: list[str] = [f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/{base_name}"]
    if stem.startswith("format_denoise_"):
        raw = stem[len("format_denoise_") :]
        container_candidates.append(f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/format_{raw}{suffix}")
        container_candidates.append(f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/{raw}{suffix}")
    elif stem.startswith("format_"):
        raw = stem[len("format_") :]
        container_candidates.append(f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/format_denoise_{raw}{suffix}")
        container_candidates.append(f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/{raw}{suffix}")
    else:
        container_candidates.append(f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/format_denoise_{stem}{suffix}")
        container_candidates.append(f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/format_{stem}{suffix}")

    if tts_mode == "fish-speech":
        return container_candidates

    # index-tts2 prefers local absolute paths.
    local_candidates: list[str] = []
    host_voice_root = str(HOST_SHARED_VOICE_ROOT).replace("\\", "/")
    for candidate in container_candidates:
        suffix_path = candidate[len(CONTAINER_SHARED_VOICE_ROOT) :]
        local_candidates.append(f"{host_voice_root}{suffix_path}")
    return local_candidates


def normalize_reference_audio_path(raw_path: object, tts_mode: str) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""

    pieces = [piece.strip() for piece in value.split("|||") if piece.strip()]
    if not pieces:
        pieces = [value]

    normalized: list[str] = []
    for piece in pieces:
        mapped = piece.replace("\\", "/")
        mapped_lower = mapped.lower()
        if tts_mode == "fish-speech":
            for prefix in HOST_SHARED_VOICE_ROOTS:
                if mapped_lower.startswith(prefix):
                    mapped = CONTAINER_SHARED_VOICE_ROOT + mapped[len(prefix) :]
                    break
            if mapped.lower().startswith("/code/sessions/"):
                repaired = _pick_best_existing_path(_session_piece_to_origin_candidates(mapped, tts_mode), tts_mode)
                if repaired:
                    mapped = repaired
        elif tts_mode == "index-tts2":
            if mapped_lower.startswith(CONTAINER_SHARED_VOICE_ROOT + "/"):
                suffix = mapped[len(CONTAINER_SHARED_VOICE_ROOT) :]
                host_voice_root = str(HOST_SHARED_VOICE_ROOT).replace("\\", "/")
                mapped = f"{host_voice_root}{suffix}"
        normalized.append(mapped)

    return "|||".join(normalized)


def build_reference_audio_from_origin(origin_audio_path: object, tts_mode: str) -> str:
    origin = str(origin_audio_path or "").strip().replace("\\", "/")
    if not origin:
        return ""

    if tts_mode == "fish-speech":
        relative = origin.lstrip("/")
        base_name = Path(relative).name
        stem = Path(base_name).stem
        suffix = Path(base_name).suffix or ".wav"
        container_candidates = [
            f"{CONTAINER_SHARED_VOICE_ROOT}/{relative}",
            f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/format_denoise_{stem}{suffix}",
            f"{CONTAINER_SHARED_VOICE_ROOT}/origin_audio/format_{stem}{suffix}",
        ]
        return _pick_best_existing_path(container_candidates, tts_mode)
    if tts_mode == "index-tts2":
        relative = origin.lstrip("/")
        base_name = Path(relative).name
        stem = Path(base_name).stem
        suffix = Path(base_name).suffix or ".wav"
        host_voice_root = str(HOST_SHARED_VOICE_ROOT).replace("\\", "/")
        local_candidates = [
            f"{host_voice_root}/{relative}",
            f"{host_voice_root}/origin_audio/format_denoise_{stem}{suffix}",
            f"{host_voice_root}/origin_audio/format_{stem}{suffix}",
        ]
        return _pick_best_existing_path(local_candidates, tts_mode)
    return origin


def sync_voice_reference_paths_for_mode() -> int:
    tts_mode = get_tts_mode()
    conn = db()
    changed = 0
    try:
        rows = conn.execute("SELECT id, asr_format_audio_url, origin_audio_path FROM voice").fetchall()
        for row in rows:
            current = str(row["asr_format_audio_url"] or "").strip().replace("\\", "/")
            target = normalize_reference_audio_path(current, tts_mode)
            if not target:
                target = build_reference_audio_from_origin(row["origin_audio_path"], tts_mode)
            if target and target != current:
                conn.execute("UPDATE voice SET asr_format_audio_url=? WHERE id=?", (target, int(row["id"])))
                changed += 1
        if changed:
            conn.commit()
            log(f"synced {changed} voice reference paths for mode={tts_mode}")
    finally:
        conn.close()
    return changed


def resolve_reference_audio_for_mode(voice: sqlite3.Row, tts_mode: str) -> str:
    original = str(voice["asr_format_audio_url"] or "")
    reference_audio = normalize_reference_audio_path(original, tts_mode)

    if tts_mode == "fish-speech":
        if not reference_audio.lower().startswith(CONTAINER_SHARED_VOICE_ROOT + "/"):
            origin_audio_path = str(voice["origin_audio_path"] or "").strip().replace("\\", "/")
            if origin_audio_path:
                reference_audio = f"{CONTAINER_SHARED_VOICE_ROOT}/{origin_audio_path.lstrip('/')}"
                log(f"voice {voice['id']} fallback reference audio -> {reference_audio}")
    elif tts_mode == "index-tts2":
        lowered = reference_audio.lower()
        if lowered.startswith(CONTAINER_SHARED_VOICE_ROOT + "/"):
            suffix = reference_audio[len(CONTAINER_SHARED_VOICE_ROOT) :]
            host_voice_root = str(HOST_SHARED_VOICE_ROOT).replace("\\", "/")
            reference_audio = f"{host_voice_root}{suffix}"

    return reference_audio


def _reference_candidates(reference_audio: str) -> list[str]:
    pieces = [piece.strip() for piece in str(reference_audio or "").split("|||") if piece.strip()]
    return pieces or [str(reference_audio or "").strip()]


def _container_voice_to_host_path(path: str) -> Path | None:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered.startswith(CONTAINER_SHARED_VOICE_ROOT + "/"):
        suffix = normalized[len(CONTAINER_SHARED_VOICE_ROOT) :].lstrip("/\\")
        return HOST_SHARED_VOICE_ROOT / suffix
    return None


def _host_voice_to_container_path(path: Path) -> str | None:
    normalized = str(path).strip().replace("\\", "/")
    lowered = normalized.lower()
    for host_root in HOST_SHARED_VOICE_ROOTS:
        base = host_root.rstrip("/")
        if lowered.startswith(base + "/"):
            suffix = normalized[len(base) :].lstrip("/\\")
            return f"{CONTAINER_SHARED_VOICE_ROOT}/{suffix}"
    return None


def _ensure_clipped_reference_audio(
    ffmpeg_exe: Path,
    source_path: Path,
    output_path: Path,
    clip_seconds: float,
    offset_seconds: float,
    video_id: int,
) -> bool:
    if output_path.exists():
        return True
    args = [str(ffmpeg_exe), "-y"]
    if offset_seconds > 0:
        args.extend(["-ss", f"{offset_seconds:.3f}"])
    args.extend(
        [
            "-i",
            str(source_path),
            "-t",
            str(clip_seconds),
            "-acodec",
            "pcm_s16le",
            str(output_path),
        ]
    )
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode == 0:
        return True
    log(
        f"video {video_id} reference trim failed ({source_path.name}): "
        f"{result.stderr.strip()[:200]}"
    )
    return False


def maybe_shorten_reference_audio(reference_audio: str, tts_mode: str, video_id: int) -> str:
    if tts_mode != "fish-speech":
        return reference_audio

    ffmpeg_exe = FFPROBE_EXE.with_name("ffmpeg.exe")
    if not ffmpeg_exe.exists():
        return reference_audio

    candidates = [c.replace("\\", "/").strip() for c in _reference_candidates(reference_audio) if c.strip()]
    if not candidates:
        return reference_audio

    rewritten: list[str] = []
    changed = False

    host_candidates = [_container_voice_to_host_path(c) for c in candidates]
    same_host_source = None
    if host_candidates and all(h is not None for h in host_candidates):
        unique_hosts = {str(h).lower() for h in host_candidates if h is not None}
        if len(unique_hosts) == 1:
            same_host_source = host_candidates[0]

    # If multiple references point to the same long file, split into multiple clips.
    if same_host_source is not None and same_host_source.exists():
        duration = get_duration_seconds(same_host_source)
        if duration is not None and duration > TTS_REFERENCE_MAX_SECONDS:
            clip_count = max(len(candidates), FISH_REFERENCE_MIN_COUNT)
            clip_count = min(clip_count, FISH_REFERENCE_MAX_COUNT)
            max_start = max(0.0, duration - TTS_REFERENCE_CLIP_SECONDS)
            if max_start <= 0 or clip_count == 1:
                offsets = [0.0 for _ in range(clip_count)]
            else:
                offsets = [(max_start * i) / (clip_count - 1) for i in range(clip_count)]

            for idx, offset in enumerate(offsets, start=1):
                original = candidates[min(idx - 1, len(candidates) - 1)]
                clipped_host = same_host_source.with_name(
                    f"{same_host_source.stem}_ref{idx}_{int(TTS_REFERENCE_CLIP_SECONDS)}s{same_host_source.suffix}"
                )
                ok = _ensure_clipped_reference_audio(
                    ffmpeg_exe, same_host_source, clipped_host, TTS_REFERENCE_CLIP_SECONDS, offset, video_id
                )
                if not ok:
                    rewritten.append(original)
                    continue
                clipped_container = _host_voice_to_container_path(clipped_host)
                if clipped_container:
                    rewritten.append(clipped_container)
                    changed = True
                    log(
                        f"video {video_id} trimmed reference audio#{idx}: {same_host_source.name} "
                        f"({duration:.1f}s -> {TTS_REFERENCE_CLIP_SECONDS:.0f}s, offset={offset:.1f}s)"
                    )
                else:
                    rewritten.append(original)

            if changed:
                return "|||".join(rewritten)
            return reference_audio

    for normalized in candidates:
        host_candidate = _container_voice_to_host_path(normalized)
        if host_candidate is None or not host_candidate.exists():
            rewritten.append(normalized)
            continue

        duration = get_duration_seconds(host_candidate)
        if duration is None or duration <= TTS_REFERENCE_MAX_SECONDS:
            rewritten.append(normalized)
            continue

        clipped_host = host_candidate.with_name(
            f"{host_candidate.stem}_ref{int(TTS_REFERENCE_CLIP_SECONDS)}s{host_candidate.suffix}"
        )
        ok = _ensure_clipped_reference_audio(
            ffmpeg_exe, host_candidate, clipped_host, TTS_REFERENCE_CLIP_SECONDS, 0.0, video_id
        )
        if not ok:
            rewritten.append(normalized)
            continue

        clipped_container = _host_voice_to_container_path(clipped_host)
        if clipped_container:
            rewritten.append(clipped_container)
            changed = True
            log(
                f"video {video_id} trimmed reference audio: {host_candidate.name} "
                f"({duration:.1f}s -> {TTS_REFERENCE_CLIP_SECONDS:.0f}s)"
            )
        else:
            rewritten.append(normalized)

    if not rewritten:
        return reference_audio
    if changed:
        return "|||".join(rewritten)
    return reference_audio


def reference_audio_exists(reference_audio: str, tts_mode: str) -> bool:
    for candidate in _reference_candidates(reference_audio):
        normalized = candidate.replace("\\", "/")
        if tts_mode == "fish-speech":
            host_candidate = _container_voice_to_host_path(normalized)
            if host_candidate is not None and host_candidate.exists():
                return True
            continue

        # index-tts2 and other local modes: prefer direct absolute path checks.
        host_candidate = _container_voice_to_host_path(normalized)
        if host_candidate is not None and host_candidate.exists():
            return True
        candidate_path = Path(normalized)
        if candidate_path.is_absolute() and candidate_path.exists():
            return True
    return False


def pick_voice_for_tts(preferred_voice: sqlite3.Row, tts_mode: str) -> tuple[sqlite3.Row, str]:
    preferred_ref = resolve_reference_audio_for_mode(preferred_voice, tts_mode)
    if reference_audio_exists(preferred_ref, tts_mode):
        return preferred_voice, preferred_ref

    # Do not silently switch to another speaker; this causes unexpected timbre drift.
    log(f"voice {preferred_voice['id']} reference missing; keep original speaker and fail fast.")
    return preferred_voice, preferred_ref


def maybe_restart_container(container_name: str, reason: str) -> None:
    now = time.time()
    last = _LAST_CONTAINER_RESTART_AT.get(container_name, 0.0)
    if now - last < CONTAINER_RESTART_COOLDOWN_SECONDS:
        return

    try:
        result = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log(f"self-heal restart {container_name} failed: {exc}")
        return

    if result.returncode == 0:
        _LAST_CONTAINER_RESTART_AT[container_name] = now
        log(f"self-heal restarted {container_name} ({reason})")
        time.sleep(4)
    else:
        err = (result.stderr or "").strip() or (result.stdout or "").strip()
        log(f"self-heal restart {container_name} failed: {err}")


def is_index_tts2_ready(timeout: int = 5) -> bool:
    try:
        res = http_get_json(INDEX_TTS_HEALTH_URL, timeout=timeout)
    except Exception:
        return False
    return str(res.get("status") or "").lower() == "ok"


def maybe_start_index_tts2_adapter(reason: str) -> bool:
    global _LAST_INDEX_TTS_START_AT

    if is_index_tts2_ready(timeout=4):
        return True

    now = time.time()
    if now - _LAST_INDEX_TTS_START_AT < INDEX_TTS_ADAPTER_START_COOLDOWN_SECONDS:
        return False

    if not INDEX_TTS_ADAPTER_PYTHON.exists():
        log(f"index-tts2 auto-start skipped: python not found ({INDEX_TTS_ADAPTER_PYTHON})")
        return False
    if not INDEX_TTS_ADAPTER_SCRIPT.exists():
        log(f"index-tts2 auto-start skipped: adapter script not found ({INDEX_TTS_ADAPTER_SCRIPT})")
        return False

    try:
        subprocess.Popen(
            [str(INDEX_TTS_ADAPTER_PYTHON), str(INDEX_TTS_ADAPTER_SCRIPT)],
            cwd=str(INDEX_TTS_ADAPTER_SCRIPT.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        _LAST_INDEX_TTS_START_AT = now
        log(f"self-heal started index-tts2 adapter ({reason})")
    except Exception as exc:
        log(f"index-tts2 auto-start failed: {exc}")
        return False

    deadline = time.time() + INDEX_TTS_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if is_index_tts2_ready(timeout=4):
            return True
        time.sleep(2)

    log("index-tts2 adapter still unhealthy after auto-start wait")
    return False


def _parse_log_timestamp_seconds(line: str) -> float | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[,\.](\d{1,3}))?", line)
    if not match:
        return None
    try:
        base_seconds = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None
    millis = match.group(2) or "0"
    try:
        ms = int(millis[:3].ljust(3, "0")) / 1000.0
    except Exception:
        ms = 0.0
    return base_seconds + ms


def fetch_gen_video_activity_ages(force: bool = False) -> dict[str, float]:
    global _LAST_GEN_VIDEO_ACTIVITY_FETCH_AT, _LAST_GEN_VIDEO_ACTIVITY

    now = time.time()
    if (
        not force
        and _LAST_GEN_VIDEO_ACTIVITY
        and (now - _LAST_GEN_VIDEO_ACTIVITY_FETCH_AT) < GEN_VIDEO_ACTIVITY_CACHE_TTL_SECONDS
    ):
        return dict(_LAST_GEN_VIDEO_ACTIVITY)

    try:
        result = subprocess.run(
            [
                "docker",
                "logs",
                "--since",
                f"{GEN_VIDEO_ACTIVITY_LOOKBACK_SECONDS}s",
                "--tail",
                "2000",
                GEN_VIDEO_CONTAINER_NAME,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=25,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log(f"gen-video activity check failed: {exc}")
        return dict(_LAST_GEN_VIDEO_ACTIVITY)

    blob = "\n".join(part for part in [(result.stdout or ""), (result.stderr or "")] if part)
    activity: dict[str, float] = {}
    activity_tokens = ("avi write success", "av_transfer", "get_run_netg end", "preprocess_ end", "command:ffmpeg", "result info")
    now = time.time()

    for line in blob.splitlines():
        lowered = line.lower()
        if not any(token in lowered for token in activity_tokens):
            continue
        code_match = UUID_PATTERN.search(line)
        if not code_match:
            continue
        code = code_match.group(0).lower()
        timestamp = _parse_log_timestamp_seconds(line)
        age = 0.0 if timestamp is None else max(0.0, now - timestamp)
        previous = activity.get(code)
        if previous is None or age < previous:
            activity[code] = age

    _LAST_GEN_VIDEO_ACTIVITY = activity
    _LAST_GEN_VIDEO_ACTIVITY_FETCH_AT = now
    return dict(activity)


_FRAME_IDX_RE = re.compile(r"current_idx\s*:\s*(\d+)")
_VIDEO_AUDIO_INFO_RE = re.compile(r"video_info:\[([0-9.]+)/\d+/\d+/[0-9.]+\],\s*audio_info:\[([0-9.]+)\]")
_PHASE_RANK = {
    "queued": 0,
    "tts_loading": 1,
    "tts_synthesizing": 2,
    "submitting": 3,
    "face2face_rendering": 4,
    "ffmpeg_muxing": 5,
    "delivering": 6,
    "success": 7,
}


def parse_gen_video_render_telemetry(
    code: str,
    estimated_total_frames: int = 0,
    render_started_at: float | None = None,
    log_text: str | None = None,
) -> dict[str, object]:
    """从 duix-avatar-gen-video 容器日志中提取某任务 UUID 的真实帧级渲染进度与健康状态。"""
    code_lower = normalize_code(code).lower()
    telemetry: dict[str, object] = {
        "phase": "face2face_rendering",
        "stage_label": "正在准备视频渲染...",
        "rendered_frames": 0,
        "total_frames": max(0, int(estimated_total_frames or 0)),
        "render_fps": 0.0,
        "eta_seconds": -1,
        "progress_float": 25.0,
        "heartbeat_at": 0.0,
        "first_avi_ts": 0.0,
        "container_error": "",
        "recent_logs": [],
    }
    if not code_lower:
        return telemetry

    if log_text is None:
        since_sec = 300
        if render_started_at and render_started_at > 0:
            since_sec = min(3600, max(300, int(time.time() - render_started_at) + 60))
        try:
            result = subprocess.run(
                [
                    "docker",
                    "logs",
                    "--since",
                    f"{since_sec}s",
                    "--tail",
                    "2500",
                    GEN_VIDEO_CONTAINER_NAME,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            log_text = "\n".join(p for p in ((result.stdout or ""), (result.stderr or "")) if p)
        except Exception as exc:
            telemetry["recent_logs"] = [f"读取容器日志失败: {exc}"]
            return telemetry

    matched_lines: list[str] = []
    seen_code = False
    max_avi_frame = 0
    max_queued_frame = 0
    log_total_frames = 0
    latest_ts = 0.0
    first_avi_ts = 0.0
    latest_avi_ts = 0.0
    phase = "face2face_rendering"
    stage_label = "正在初始化唇形渲染引擎..."
    container_error = ""
    has_feature_done = False

    for line in log_text.splitlines():
        if "/easy/query" in line:
            continue
        lowered = line.lower()
        if code_lower not in lowered:
            # 仅在当前任务 UUID 已经出现在日志之后，才捕获相邻的 CUDA OOM / 严重异常，避免串扰旧任务报错
            if seen_code and ("out of memory" in lowered or "cuda error" in lowered):
                container_error = line.strip()[-260:]
            continue

        seen_code = True
        clean = line.strip()
        matched_lines.append(clean[-220:])
        ts = _parse_log_timestamp_seconds(clean)
        if ts and ts > latest_ts:
            latest_ts = ts

        # 解析引擎打印的精确视频帧率与音频时长，拿到真实总帧数（兼容 25/30/60fps 底模）
        info_match = _VIDEO_AUDIO_INFO_RE.search(clean)
        if info_match:
            try:
                v_fps = float(info_match.group(1))
                a_dur = float(info_match.group(2))
                if v_fps > 0 and a_dur > 0:
                    log_total_frames = max(1, int(round(v_fps * a_dur)))
            except Exception:
                pass

        if "get_aud_feat1 success" in lowered or "init_wh_process end" in lowered:
            has_feature_done = True

        # 严格区分「输入帧预读取 av_transfer」与「真实 GPU 唇形渲染写入 avi write success」
        idx_match = _FRAME_IDX_RE.search(clean)
        if idx_match:
            frame_idx = int(idx_match.group(1))
            if "avi write success" in lowered:
                if frame_idx > max_avi_frame:
                    max_avi_frame = frame_idx
                if first_avi_ts == 0.0 and ts:
                    first_avi_ts = ts
                if ts and ts > latest_avi_ts:
                    latest_avi_ts = ts
                phase = "face2face_rendering"
                stage_label = f"唇形渲染中（已写出 {max_avi_frame} 帧）"
            elif "av_transfer" in lowered:
                if frame_idx > max_queued_frame:
                    max_queued_frame = frame_idx

        if "handler netg end" in lowered or "av_transfer success" in lowered:
            phase = "ffmpeg_muxing"
            stage_label = "唇形推理完毕，正在拼接视频轨..."
        elif "command:ffmpeg" in lowered:
            phase = "ffmpeg_muxing"
            stage_label = "FFmpeg 正在封装高清成片（请耐心等待，勿重启）"
        elif "任务最终合成结果" in clean:
            phase = "delivering"
            stage_label = "成片封装完成，正在校验并归档交付..."
        elif any(tok in lowered for tok in ("traceback", "error:", "exception", "out of memory", "failed")):
            if "loglevel warning" not in lowered:
                container_error = clean[-260:]

    total_frames = log_total_frames if log_total_frames > 0 else max(0, int(estimated_total_frames or 0))
    if max_avi_frame > total_frames and total_frames > 0:
        total_frames = max_avi_frame

    # 用真实 AVI 渲染写帧时间窗口计算渲染速度，避免被前置特征提取时间拉偏 ETA
    elapsed = 0.0
    if first_avi_ts > 0 and latest_avi_ts > first_avi_ts:
        elapsed = max(0.5, latest_avi_ts - first_avi_ts)
    elif first_avi_ts > 0:
        elapsed = max(1.0, time.time() - first_avi_ts)
    elif render_started_at and render_started_at > 0 and max_avi_frame > 0:
        elapsed = max(1.0, time.time() - render_started_at)

    render_fps = round(max_avi_frame / elapsed, 2) if (max_avi_frame > 0 and elapsed > 0) else 0.0
    eta_seconds = -1
    if phase == "face2face_rendering" and total_frames > 0 and max_avi_frame > 0:
        ratio = min(1.0, max_avi_frame / float(total_frames))
        progress_float = round(28.0 + ratio * 62.0, 1)  # 28% -> 90%
        if render_fps > 0.05:
            remaining_frames = max(0, total_frames - max_avi_frame)
            eta_seconds = int(remaining_frames / render_fps) + max(8, int((total_frames / 30.0) * 0.25))
        stage_label = f"唇形渲染 {max_avi_frame}/{total_frames} 帧 ({ratio * 100:.1f}%)"
    elif phase == "face2face_rendering" and max_queued_frame > 0:
        q_ratio = min(1.0, max_queued_frame / float(total_frames)) if total_frames > 0 else 0.5
        progress_float = round(26.0 + q_ratio * 2.0, 1)
        stage_label = (
            f"正在预处理底模与音画特征（已送入 {max_queued_frame}/{total_frames} 帧）..."
            if total_frames > 0
            else f"正在预处理底模与音画特征（已送入 {max_queued_frame} 帧）..."
        )
    elif phase == "face2face_rendering" and has_feature_done:
        progress_float = 26.5
        stage_label = "音视频特征提取完成，正在启动 GPU 唇形推理..."
    elif phase == "ffmpeg_muxing":
        progress_float = 93.0 if "FFmpeg" in stage_label else 91.5
        eta_seconds = 20
    elif phase == "delivering":
        progress_float = 98.0
        eta_seconds = 5
    else:
        progress_float = 25.5 if matched_lines else 25.0

    telemetry.update(
        {
            "phase": phase,
            "stage_label": stage_label,
            "rendered_frames": max_avi_frame,
            "total_frames": total_frames,
            "render_fps": render_fps,
            "eta_seconds": eta_seconds,
            "progress_float": progress_float,
            "heartbeat_at": latest_ts or (time.time() if matched_lines else 0.0),
            "first_avi_ts": first_avi_ts,
            "container_error": container_error,
            "recent_logs": matched_lines[-15:],
        }
    )
    return telemetry


def update_video_if_status(video_id: int, expected_status: str, **fields: object) -> bool:
    """条件更新 video 表，避免后台进度线程把已被用户中断的任务覆盖回 pending。"""
    if not fields:
        return False
    conn = db()
    try:
        keys = list(fields.keys())
        sql = f"UPDATE video SET {', '.join(f'{k}=?' for k in keys)} WHERE id=? AND status=?"
        cur = conn.execute(sql, [fields[k] for k in keys] + [video_id, expected_status])
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def update_video(video_id: int, **fields: object) -> None:
    if not fields:
        return
    conn = db()
    try:
        keys = list(fields.keys())
        sql = f"UPDATE video SET {', '.join(f'{k}=?' for k in keys)} WHERE id=?"
        conn.execute(sql, [fields[k] for k in keys] + [video_id])
        conn.commit()
    finally:
        conn.close()


def ensure_cancel_triggers(conn: sqlite3.Connection | None = None) -> None:
    close_after = False
    if conn is None:
        conn = db()
        close_after = True
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cancelled_video_tasks (
                video_id INTEGER PRIMARY KEY,
                code TEXT,
                cancelled_at INTEGER
            );
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_video_record_cancelled")
        conn.execute(
            """
            CREATE TRIGGER trg_video_record_cancelled
            AFTER DELETE ON video
            WHEN OLD.status IN ('pending', 'waiting')
              OR (
                OLD.status NOT IN ('success', 'failed', 'draft')
                AND OLD.code IS NOT NULL
                AND trim(OLD.code) <> ''
              )
            BEGIN
                INSERT OR REPLACE INTO cancelled_video_tasks (video_id, code, cancelled_at)
                VALUES (OLD.id, OLD.code, CAST(strftime('%s', 'now') AS INTEGER));
            END;
            """
        )
        conn.commit()
    finally:
        if close_after:
            conn.close()


def is_studio_cancel_requested(video_id: int) -> bool:
    conn = db()
    try:
        cur = conn.execute("SELECT 1 FROM studio_cancel_requests WHERE video_id = ?", [video_id])
        if cur.fetchone() is not None:
            return True
        row = conn.execute("SELECT status FROM video WHERE id = ?", [video_id]).fetchone()
        return row is not None and str(row["status"] or "").lower() == "cancelled"
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def abort_active_tts_if_needed() -> None:
    try:
        http_post_json(TTS_CANCEL_URL, {}, timeout=5)
    except Exception:
        pass


def mark_video_cancelled(video_id: int, reason: str = "任务已被用户中断") -> None:
    conn = db()
    try:
        row = conn.execute("SELECT 1 FROM video WHERE id = ?", [video_id]).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE video SET status='cancelled', message=?, code=NULL WHERE id=?",
                (reason, video_id),
            )
            conn.commit()
    finally:
        conn.close()
    meta_update(video_id, phase="cancelled", stage_label=reason, eta_seconds=-1)


def is_video_cancelled(video_id: int, task_code: str | None = None) -> bool:
    conn = db()
    try:
        try:
            cur = conn.execute("SELECT 1 FROM studio_cancel_requests WHERE video_id = ?", [video_id])
            if cur.fetchone() is not None:
                return True
        except sqlite3.OperationalError:
            pass
        cur = conn.execute(
            "SELECT 1 FROM cancelled_video_tasks WHERE video_id = ?",
            [video_id],
        )
        if cur.fetchone() is not None:
            return True
        norm_code = normalize_code(task_code)
        if is_valid_task_code(norm_code):
            cur = conn.execute(
                "SELECT 1 FROM cancelled_video_tasks WHERE code = ? AND trim(code) <> ''",
                [norm_code],
            )
            if cur.fetchone() is not None:
                return True
        row = conn.execute("SELECT status FROM video WHERE id = ?", [video_id]).fetchone()
        if row is None:
            return True
        if str(row["status"] or "").lower() == "cancelled":
            return True
        return False
    except sqlite3.OperationalError:
        try:
            cur = conn.execute("SELECT 1 FROM video WHERE id = ?", [video_id])
            return cur.fetchone() is None
        except Exception:
            return False
    finally:
        conn.close()


def clear_cancelled_video_task(video_id: int, task_code: str | None = None) -> None:
    conn = db()
    try:
        norm_code = normalize_code(task_code)
        if is_valid_task_code(norm_code):
            conn.execute(
                "DELETE FROM cancelled_video_tasks WHERE video_id = ? OR code = ?",
                [video_id, norm_code],
            )
        else:
            conn.execute(
                "DELETE FROM cancelled_video_tasks WHERE video_id = ?",
                [video_id],
            )
        try:
            conn.execute("DELETE FROM studio_cancel_requests WHERE video_id = ?", [video_id])
        except sqlite3.OperationalError:
            pass
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def is_gen_video_ready(timeout: int = 2) -> bool:
    try:
        res = http_get_json(F2F_QUERY_URL.format(code="__health_probe__"), timeout=timeout)
        return int(res.get("code", -1)) in (10000, 10004)
    except Exception:
        return False


def wait_for_gen_video_ready(timeout: int = 35) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_gen_video_ready(timeout=2):
            return True
        time.sleep(2)
    return False


def force_restart_gen_video_container(reason: str, wait_ready: bool = True) -> bool:
    container_name = GEN_VIDEO_CONTAINER_NAME
    log(f"[CANCEL] Force restarting {container_name} ({reason})")
    try:
        result = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log(f"[CANCEL] Force restart {container_name} failed: {exc}")
        return False

    now = time.time()
    if result.returncode == 0:
        _LAST_CONTAINER_RESTART_AT[container_name] = now
        log(f"[CANCEL] Force restarted {container_name} successfully ({reason})")
        clear_backend_busy_lock()
        if wait_ready:
            ready = wait_for_gen_video_ready(timeout=35)
            if ready:
                log(f"[CANCEL] {container_name} is back online and ready for new tasks")
            else:
                log(f"[CANCEL] {container_name} restart completed, probe timed out (will retry on next cycle)")
        return True
    else:
        err = (result.stderr or "").strip() or (result.stdout or "").strip()
        log(f"[CANCEL] Force restart {container_name} failed: {err}")
        return False


def process_cancelled_tasks() -> bool:
    conn = db()
    rows: list[tuple[int, str, bool]] = []
    try:
        for r in conn.execute("SELECT video_id, code FROM cancelled_video_tasks").fetchall():
            rows.append((int(r[0]), normalize_code(r[1]), False))
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            """
            SELECT c.video_id, v.code
            FROM studio_cancel_requests c
            LEFT JOIN video v ON v.id = c.video_id
            """
        ).fetchall():
            rows.append((int(r[0]), normalize_code(r[1]), True))
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    if not rows:
        return False

    changed = False
    for vid, code, is_studio_cancel in rows:
        log(f"[CANCEL] Found pending cancelled task: video_id={vid}, code={code}, studio={is_studio_cancel}")
        if is_valid_task_code(code):
            try:
                res = http_get_json(F2F_QUERY_URL.format(code=code), timeout=10)
                status_code = int(res.get("code", -1))
                task_status = (res.get("data") or {}).get("status")
            except Exception:
                status_code = -1
                task_status = None

            if (status_code == 10000 and task_status == 1) or status_code == 10001:
                log(f"[CANCEL] Task {vid} (code={code}) is active in backend; aborting container...")
                force_restart_gen_video_container(f"daemon aborting cancelled task {vid} code={code}", wait_ready=True)

            try:
                _, out_file = final_mp4_for_code(code)
                if out_file.exists():
                    out_file.unlink(missing_ok=True)
            except Exception:
                pass

        if is_studio_cancel:
            mark_video_cancelled(vid, "任务已被用户中断")
        clear_cancelled_video_task(vid, code)
        changed = True

    return changed


def ensure_retry_triggers() -> None:
    if init_studio_schema is not None:
        try:
            init_studio_schema(DB_PATH)
        except Exception as exc:
            log(f"init_studio_schema warning: {exc}")
    conn = db()
    try:
        ensure_cancel_triggers(conn)
        conn.execute("DROP TRIGGER IF EXISTS trg_video_retryable_failed_update")
        conn.execute("DROP TRIGGER IF EXISTS trg_video_retryable_failed_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_video_protect_runner_pending")
        retryable_message_sql = """
                NEW.code IS NOT NULL OR
                instr(lower(COALESCE(NEW.message, '')), 'request failed with status code 500') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'internal server error') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'socket hang up') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'remote end closed connection') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'connection refused') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'econnrefused') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'winerror 10053') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'winerror 10054') > 0 OR
                instr(lower(COALESCE(NEW.message, '')), 'busy') > 0 OR
                instr(COALESCE(NEW.message, ''), '忙碌') > 0 OR
                instr(COALESCE(NEW.message, ''), '繁忙') > 0 OR
                instr(COALESCE(NEW.message, ''), '排队') > 0 OR
                instr(COALESCE(NEW.message, ''), '任务不存在') > 0 OR
                instr(COALESCE(NEW.message, ''), '蹇欑') > 0
        """
        conn.execute(
            f"""
            CREATE TRIGGER trg_video_retryable_failed_update
            AFTER UPDATE OF status, message ON video
            WHEN NEW.status='failed'
              AND COALESCE(NEW.progress, 0)=0
              AND COALESCE(trim(NEW.file_path), '')=''
              AND instr(COALESCE(NEW.message, ''), '[已达重试上限]') = 0
              AND instr(COALESCE(NEW.message, ''), '[不可重试]') = 0
              AND NOT (
                    OLD.status='pending'
                    AND COALESCE(trim(OLD.code), '') <> ''
                    AND instr(COALESCE(OLD.param, ''), '_queue_owner') > 0
              )
              AND ({retryable_message_sql})
            BEGIN
                UPDATE video
                   SET status='waiting',
                       message='Auto-retry after transient failure',
                       progress=0,
                       code=NULL,
                       param=NULL,
                       file_path=NULL
                 WHERE id=NEW.id;
            END;
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER trg_video_retryable_failed_insert
            AFTER INSERT ON video
            WHEN NEW.status='failed'
              AND COALESCE(NEW.progress, 0)=0
              AND COALESCE(trim(NEW.file_path), '')=''
              AND instr(COALESCE(NEW.message, ''), '[已达重试上限]') = 0
              AND instr(COALESCE(NEW.message, ''), '[不可重试]') = 0
              AND ({retryable_message_sql})
            BEGIN
                UPDATE video
                   SET status='waiting',
                       message='Auto-retry after transient failure',
                       progress=0,
                       code=NULL,
                       param=NULL,
                       file_path=NULL
                 WHERE id=NEW.id;
                END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_video_protect_runner_pending
            AFTER UPDATE OF status, message, code, param, progress ON video
            WHEN OLD.status='pending'
              AND instr(COALESCE(OLD.param, ''), '_queue_owner') > 0
              AND (
                    (
                        COALESCE(trim(OLD.code), '') <> ''
                        AND NEW.status='failed'
                        AND COALESCE(NEW.progress, 0)=0
                        AND COALESCE(trim(NEW.file_path), '')=''
                        AND COALESCE(trim(NEW.code), '') <> ''
                    )
                    OR
                    (
                        COALESCE(trim(OLD.code), '') <> ''
                        AND NEW.status='pending'
                        AND COALESCE(trim(NEW.code), '') = ''
                    )
                  )
            BEGIN
                UPDATE video
                   SET status=OLD.status,
                       message=OLD.message,
                       progress=OLD.progress,
                       code=OLD.code,
                       param=OLD.param,
                       file_path=OLD.file_path,
                       audio_path=OLD.audio_path
                 WHERE id=OLD.id;
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_duration_seconds(video_path: Path) -> float | None:
    if not video_path.exists() or not FFPROBE_EXE.exists():
        return None
    result = subprocess.run(
        [
            str(FFPROBE_EXE),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def final_mp4_for_code(code: str) -> tuple[str, Path]:
    rel = f"/{code}-r.mp4"
    return rel, OUTPUT_ROOT / f"{code}-r.mp4"


def _resolve_category_subfolder_name(video_id: int) -> str:
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT c.name
            FROM studio_job_meta m
            JOIN studio_categories c ON c.id = m.category_id
            WHERE m.video_id = ?
            """,
            (video_id,),
        ).fetchone()
        if row and row["name"]:
            cleaned = re.sub(r'[<>:"/\\|?*]', "_", str(row["name"]).strip()).strip(" .")
            return cleaned[:40]
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return ""


def deliver_video_copy(video_id: int, video_name: object, code: str, source: Path) -> Path | None:
    """Copy a completed backend video to the user's stable delivery folder (with optional category subfolder)."""
    if not source.exists() or source.stat().st_size <= 0:
        return None
    name = str(video_name or f"video_{video_id}").strip() or f"video_{video_id}"
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")[:80] or f"video_{video_id}"
    short_code = (str(code or "")[:8] or "result")
    cat_folder = _resolve_category_subfolder_name(video_id)
    dest_dir = (DELIVERY_ROOT / cat_folder) if cat_folder else DELIVERY_ROOT
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{video_id:04d}_{name}_{short_code}.mp4"
    if target.resolve() != source.resolve():
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
    meta_update(video_id, delivered_path=str(target))
    return target


def _finalize_video_success(
    video_id: int,
    video_name: object,
    code: str,
    rel_result: str,
    video_file: Path,
    message: str,
    duration: float | None = None,
    stage_label: str = "合成并交付完成",
) -> Path | None:
    """统一成片完成与文件自愈恢复收尾：归档交付、更新 video 与 studio_job_meta、并触发 job.completed Webhook。"""
    if duration is None:
        duration = get_duration_seconds(video_file)
    delivered = deliver_video_copy(video_id, video_name, code, video_file)
    if delivered:
        log(f"Delivered video {video_id} -> {delivered}")
    update_video(
        video_id,
        status="success",
        message=message,
        file_path=rel_result,
        progress=100,
        duration=duration,
    )
    meta_update(
        video_id,
        phase="success",
        stage_label=stage_label,
        progress_float=100.0,
        eta_seconds=0,
        delivered_path=str(delivered) if delivered else "",
        error_code="",
        error_detail="",
        heartbeat_at=time.time(),
    )
    notify_webhook_if_needed(
        video_id,
        "job.completed",
        {
            "status": "success",
            "name": video_name,
            "file_path": rel_result,
            "delivered_path": str(delivered) if delivered else "",
            "duration": duration,
        },
    )
    return delivered


def fetch_row(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    conn = db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def fetch_next_waiting() -> sqlite3.Row | None:
    # Studio 提交的任务可直接执行；旧版 Electron 客户端新建任务仍保留 8 秒避让窗口防止并发抢单。
    cutoff_ms = int(time.time() * 1000) - CLIENT_SUBMIT_GRACE_SECONDS * 1000
    try:
        return fetch_row(
            """
            SELECT v.*
            FROM video v
            LEFT JOIN studio_job_meta m ON m.video_id = v.id
            WHERE v.status='waiting'
              AND (m.video_id IS NOT NULL OR v.created_at <= ?)
            ORDER BY v.id ASC
            LIMIT 1
            """,
            (cutoff_ms,),
        )
    except sqlite3.OperationalError:
        return fetch_row(
            "SELECT * FROM video WHERE status='waiting' AND created_at<=? ORDER BY id ASC LIMIT 1",
            (cutoff_ms,),
        )


def fetch_model(model_id: int) -> sqlite3.Row:
    row = fetch_row("SELECT * FROM f2f_model WHERE id=?", (model_id,))
    if row is None:
        raise RuntimeError(f"f2f_model not found: id={model_id}")
    return row


def fetch_voice(voice_id: int) -> sqlite3.Row:
    row = fetch_row("SELECT * FROM voice WHERE id=?", (voice_id,))
    if row is None:
        raise RuntimeError(f"voice not found: id={voice_id}")
    return row


def _coerce_int(value: object) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return 0


def get_row_queue_owner(row: sqlite3.Row) -> str:
    raw_param = str(row["param"] or "").strip()
    if not raw_param:
        return ""
    try:
        parsed = json.loads(raw_param)
        if isinstance(parsed, dict):
            return str(parsed.get("_queue_owner") or "").strip()
    except Exception:
        return ""
    return ""


def extract_submit_started_ms(row: sqlite3.Row) -> int:
    raw_param = str(row["param"] or "").strip()
    if raw_param:
        try:
            param_obj = json.loads(raw_param)
            submitted_at = (
                param_obj.get("_run_started_at_ms")
                or
                param_obj.get("_submitted_at_ms")
                or param_obj.get("submitted_at_ms")
                or param_obj.get("submit_at_ms")
            )
            submitted_at_int = _coerce_int(submitted_at)
            if submitted_at_int > 0:
                return submitted_at_int
        except Exception:
            pass

    # Client-side submissions often do not include timestamp metadata in `param`.
    # Falling back to old `created_at` would incorrectly mark active tasks as timeout.
    if "code" in row.keys() and is_valid_task_code(row["code"]):
        return 0

    return _coerce_int(row["created_at"])


def ensure_queue_owner_marker(row: sqlite3.Row) -> bool:
    raw_param = str(row["param"] or "").strip()
    param_obj: dict[str, object] = {}

    if raw_param:
        try:
            parsed = json.loads(raw_param)
            if isinstance(parsed, dict):
                param_obj = dict(parsed)
        except Exception:
            pass

    needs_update = False
    if str(param_obj.get("_queue_owner") or "").strip() != QUEUE_OWNER:
        param_obj["_queue_owner"] = QUEUE_OWNER
        needs_update = True

    if _coerce_int(param_obj.get("_submitted_at_ms")) <= 0:
        submitted_ms = extract_submit_started_ms(row)
        if submitted_ms <= 0:
            created_ms = _coerce_int(row["created_at"])
            submitted_ms = created_ms if created_ms > 0 else int(time.time() * 1000)
        param_obj["_submitted_at_ms"] = submitted_ms
        needs_update = True

    if not needs_update:
        return False

    update_video(int(row["id"]), param=json.dumps(param_obj, ensure_ascii=False))
    return True


def is_busy_submit_response(result: dict) -> bool:
    code = _coerce_int(result.get("code"))
    if code == 10001:
        return True
    msg = str(result.get("msg") or "")
    msg_lower = msg.lower()
    if "busy" in msg_lower:
        return True
    return ("忙碌" in msg) or ("繁忙" in msg) or ("排队" in msg) or ("蹇欑" in msg)


def is_retryable_failure_message(message: object) -> bool:
    msg = str(message or "")
    msg_lower = msg.lower()
    return (
        "request failed with status code 500" in msg_lower
        or "internal server error" in msg_lower
        or "socket hang up" in msg_lower
        or "remote end closed connection without response" in msg_lower
        or "connection refused" in msg_lower
        or "econnrefused" in msg_lower
        or "connection reset by peer" in msg_lower
        or "winerror 10053" in msg_lower
        or "winerror 10054" in msg_lower
        or "timed out" in msg_lower
        or "timeout" in msg_lower
        or "read timed out" in msg_lower
        or "read operation timed out" in msg_lower
        or "busy" in msg_lower
        or "忙碌中" in msg
        or "忙碌" in msg
        or "繁忙" in msg
        or "排队" in msg
        or "任务不存在" in msg
        or "蹇欑" in msg
    )


def find_matching_active_backend_code(audio_name: str, model_video_path: str) -> str | None:
    """Recover a client-submitted code when a simultaneous runner submit gets busy."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", "90s", "--tail", "5000", GEN_VIDEO_CONTAINER_NAME],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=25, creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log(f"active submission discovery failed: {exc}")
        return None

    blob = "\n".join(part for part in ((result.stdout or ""), (result.stderr or "")) if part)
    audio_key = Path(str(audio_name)).name.lower()
    video_key = Path(str(model_video_path).replace("/", "\\")).name.lower()
    candidates: list[str] = []
    for line in blob.splitlines():
        lowered = line.lower()
        if "audio_url:" not in lowered or "video_url:" not in lowered:
            continue
        if audio_key not in lowered or video_key not in lowered:
            continue
        match = UUID_PATTERN.search(line)
        if match:
            candidates.append(match.group(0))
    if not candidates:
        return None

    activity = fetch_gen_video_activity_ages(force=True)
    for code in reversed(candidates):
        age = activity.get(code.lower())
        if age is not None and age <= 30:
            return code
    return None


def mark_backend_busy(reason: str) -> int:
    global _BACKEND_BUSY_UNTIL, _BACKEND_BUSY_STREAK
    _BACKEND_BUSY_STREAK += 1
    # A busy response normally means a real render is active. Restarting the
    # container here destroys that render, so busy only enables backoff.
    cooldown = min(
        BACKEND_BUSY_MAX_COOLDOWN_SECONDS,
        BACKEND_BUSY_BASE_COOLDOWN_SECONDS * (2 ** (_BACKEND_BUSY_STREAK - 1)),
    )
    until = time.time() + cooldown
    if until > _BACKEND_BUSY_UNTIL:
        _BACKEND_BUSY_UNTIL = until
    log(f"backend busy lock enabled for {cooldown}s ({reason}, streak={_BACKEND_BUSY_STREAK})")
    return int(max(1, round(_BACKEND_BUSY_UNTIL - time.time())))


def clear_backend_busy_lock() -> None:
    global _BACKEND_BUSY_UNTIL, _BACKEND_BUSY_STREAK
    _BACKEND_BUSY_UNTIL = 0.0
    _BACKEND_BUSY_STREAK = 0


def backend_busy_wait_seconds() -> int:
    remaining = int(round(_BACKEND_BUSY_UNTIL - time.time()))
    return remaining if remaining > 0 else 0


def maybe_log_backend_busy_wait() -> None:
    global _LAST_BUSY_WAIT_LOG_AT
    remaining = backend_busy_wait_seconds()
    if remaining <= 0:
        return
    now = time.time()
    if now - _LAST_BUSY_WAIT_LOG_AT < 20:
        return
    _LAST_BUSY_WAIT_LOG_AT = now
    log(f"backend busy lock active, skip submit for {remaining}s")


def _resolve_max_retries(meta: dict[str, object]) -> int:
    raw = meta.get("max_retries")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MAX_RETRIES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES


def reset_stuck_rows() -> None:
    process_cancelled_tasks()
    retryable_failed = fetch_all("SELECT id, message FROM video WHERE status='failed'")
    for row in retryable_failed:
        vid = int(row["id"])
        msg_str = str(row["message"] or "")
        if "[已达重试上限]" in msg_str or "[不可重试]" in msg_str:
            continue
        if is_retryable_failure_message(msg_str):
            meta = get_job_meta(vid)
            retries = int(meta.get("retry_count") or 0)
            max_retries = _resolve_max_retries(meta)
            if retries >= max_retries:
                final_msg = f"[已达重试上限] 自动重试 {retries}/{max_retries} 次仍失败：{msg_str[:180]}"
                _fail_video_permanently(vid, "MAX_RETRIES_EXCEEDED", final_msg, msg_str)
                continue
            next_retry = retries + 1
            meta_update(
                vid,
                retry_count=next_retry,
                phase="queued",
                stage_label=f"瞬时故障自动重试 ({next_retry}/{max_retries})",
                progress_float=0.0,
                rendered_frames=0,
                eta_seconds=-1,
            )
            update_video(
                vid,
                status="waiting",
                message=f"瞬时故障自动重试 ({next_retry}/{max_retries})",
                progress=0,
                code=None,
                param=None,
                file_path=None,
            )
            log(f"Recovered retryable failed -> waiting: {vid} ({next_retry}/{max_retries})")

    now_ms = int(time.time() * 1000)
    invalid_cutoff_ms = now_ms - PENDING_WITHOUT_CODE_STUCK_SECONDS * 1000
    timeout_cutoff_ms = now_ms - PENDING_WITH_CODE_TIMEOUT_SECONDS * 1000

    drafts = fetch_all("SELECT id FROM video WHERE status='draft'")
    for row in drafts:
        update_video(int(row["id"]), status="waiting", message=None, code=None, progress=0, file_path=None)
        log(f"Recovered draft -> waiting: {row['id']}")

    pending_rows = fetch_all("SELECT id, name, code, created_at, param, message FROM video WHERE status='pending'")
    active_runner_no_code_id: int | None = None
    active_runner_no_code_started_ms = 0

    for row in pending_rows:
        if is_valid_task_code(row["code"]):
            continue
        if get_row_queue_owner(row) != QUEUE_OWNER:
            continue
        started_ms = extract_submit_started_ms(row)
        if started_ms > active_runner_no_code_started_ms:
            active_runner_no_code_started_ms = started_ms
            active_runner_no_code_id = int(row["id"])

    for row in pending_rows:
        pending_since_ms = extract_submit_started_ms(row)
        if pending_since_ms <= 0:
            continue
        if is_valid_task_code(row["code"]):
            if pending_since_ms > timeout_cutoff_ms:
                continue
            code = normalize_code(row["code"])
            rel_result, abs_result = final_mp4_for_code(code)
            duration = get_duration_seconds(abs_result) if abs_result.exists() else None
            if abs_result.exists() and duration is not None and duration > 0:
                _finalize_video_success(
                    int(row["id"]),
                    row["name"],
                    code,
                    rel_result,
                    abs_result,
                    message="Recovered from timeout by existing output file",
                    duration=duration,
                    stage_label="检测到已有成片，自动恢复并交付完成",
                )
                log(f"Recovered timeout pending -> success by file: {row['id']} ({rel_result})")
            else:
                update_video(
                    int(row["id"]),
                    status="waiting",
                    message="Auto-retry after pending timeout",
                    code=None,
                    param=None,
                    progress=0,
                    file_path=None,
                )
                meta_update(int(row["id"]), phase="queued", stage_label="渲染超时，已自动重排队列", progress_float=0.0)
                log(f"Recovered timeout pending -> waiting: {row['id']}")
            continue

        is_runner_owned = get_row_queue_owner(row) == QUEUE_OWNER
        if not is_runner_owned:
            client_invalid_cutoff_ms = now_ms - PENDING_WITHOUT_CODE_CLIENT_STUCK_SECONDS * 1000
            if pending_since_ms > client_invalid_cutoff_ms:
                continue
            update_video(
                int(row["id"]),
                status="waiting",
                message="Auto-recovered ghost pending (no backend task code)",
                code=None,
                progress=0,
                file_path=None,
            )
            log(f"Recovered ghost pending(no owner/no code) -> waiting: {row['id']}")
            continue

        if active_runner_no_code_id is not None and int(row["id"]) != active_runner_no_code_id:
            stale_slot_cutoff_ms = now_ms - RUNNER_NO_CODE_SLOT_STALE_SECONDS * 1000
            if pending_since_ms <= stale_slot_cutoff_ms:
                update_video(
                    int(row["id"]),
                    status="waiting",
                    message="Auto-recovered stale runner pending slot",
                    code=None,
                    param=None,
                    progress=0,
                    file_path=None,
                )
                log(f"Recovered stale runner no-code pending slot -> waiting: {row['id']}")
                continue

        message_lower = str(row["message"] or "").strip().lower()
        meta_phase = str(get_job_meta(int(row["id"])).get("phase") or "").lower()
        is_tts_phase = (
            "preparing audio" in message_lower
            or "正在准备音频" in message_lower
            or "正在合成" in message_lower
            or "正在加载" in message_lower
            or "语音" in message_lower
            or meta_phase in ("tts_loading", "tts_synthesizing")
        )
        if is_tts_phase:
            preparing_audio_cutoff_ms = now_ms - PENDING_PREPARING_AUDIO_STUCK_SECONDS * 1000
            if pending_since_ms > preparing_audio_cutoff_ms:
                continue
            update_video(
                int(row["id"]),
                status="waiting",
                message="Auto-retry after preparing audio timeout",
                code=None,
                param=None,
                progress=0,
                file_path=None,
            )
            meta_update(int(row["id"]), phase="queued", stage_label="语音准备超时，已自动重排队列", progress_float=0.0)
            log(f"Recovered stuck pending(preparing audio timeout) -> waiting: {row['id']}")
            continue

        if pending_since_ms > invalid_cutoff_ms:
            continue
        update_video(
            int(row["id"]),
            status="waiting",
            message="Auto-retry after pending(no-code) timeout",
            code=None,
            param=None,
            progress=0,
            file_path=None,
        )
        log(f"Recovered stuck pending(invalid code) -> waiting: {row['id']}")


def cleanup_other_runner_no_code_pending(current_video_id: int) -> None:
    rows = fetch_all(
        "SELECT id, code, created_at, param, message FROM video WHERE status='pending' AND id<>?",
        (current_video_id,),
    )
    now_ms = int(time.time() * 1000)
    stale_cutoff_ms = now_ms - RUNNER_NO_CODE_SLOT_STALE_SECONDS * 1000
    for row in rows:
        if is_valid_task_code(row["code"]):
            continue
        if get_row_queue_owner(row) != QUEUE_OWNER:
            continue
        started_ms = extract_submit_started_ms(row)
        if started_ms <= 0 or started_ms > stale_cutoff_ms:
            continue
        update_video(
            int(row["id"]),
            status="waiting",
            message="Auto-recovered stale runner pending slot",
            code=None,
            param=None,
            progress=0,
            file_path=None,
        )
        log(f"Recovered stale runner no-code pending slot before run -> waiting: {row['id']}")


def sync_pending_from_backend() -> bool:
    changed = process_cancelled_tasks()
    pending = fetch_all("SELECT * FROM video WHERE status='pending'")
    activity_ages: dict[str, float] | None = None
    for row in pending:
        video_id = int(row["id"])
        code = normalize_code(row["code"])
        if is_video_cancelled(video_id, code):
            studio_cancel = is_studio_cancel_requested(video_id)
            log(f"[CANCEL] video {video_id} code={code} in sync_pending is cancelled, aborting backend")
            force_restart_gen_video_container(f"cancelled pending video {video_id} code={code}", wait_ready=True)
            if studio_cancel:
                mark_video_cancelled(video_id, "任务已被用户中断")
            clear_cancelled_video_task(video_id, code)
            changed = True
            continue
        code_key = code.lower()
        if not is_valid_task_code(code):
            continue
        if ensure_queue_owner_marker(row):
            changed = True
            refreshed = fetch_row("SELECT * FROM video WHERE id=?", (video_id,))
            if refreshed is not None:
                row = refreshed
        try:
            res = http_get_json(F2F_QUERY_URL.format(code=code), timeout=20)
        except Exception:
            continue

        status_code = int(res.get("code", -1))
        data = res.get("data") or {}
        task_status = data.get("status")
        progress = int(data.get("progress") or 0)
        msg = str(data.get("msg") or res.get("msg") or "")

        if status_code in (9999, 10002, 10003):
            fail_msg = msg or f"backend code {status_code}"
            if is_retryable_failure_message(fail_msg):
                maybe_recover_retryable_video_error(video_id, RuntimeError(fail_msg))
                log(f"video {video_id} backend code {status_code} handled via retry policy")
            else:
                _fail_video_permanently(
                    video_id,
                    f"BACKEND_{status_code}",
                    f"数字人渲染引擎返回错误 ({status_code})：{fail_msg}",
                    json.dumps(res, ensure_ascii=False),
                )
                log(f"video {video_id} marked failed permanently by backend code {status_code}")
            changed = True
            continue

        if status_code == 10004:
            rel_result, out_file = final_mp4_for_code(code)
            duration = get_duration_seconds(out_file) if out_file.exists() else None
            if out_file.exists() and duration is not None and duration > 0:
                _finalize_video_success(
                    video_id,
                    row["name"],
                    code,
                    rel_result,
                    out_file,
                    message="Recovered by output file while backend returned 10004",
                    duration=duration,
                    stage_label="检测到成片已生成，自动恢复并交付完成",
                )
                log(f"video {video_id} recovered from backend 10004 by file -> {rel_result}")
                changed = True
                continue

            now_ms = int(time.time() * 1000)
            pending_since_ms = extract_submit_started_ms(row)
            if pending_since_ms <= 0:
                created_ms = _coerce_int(row["created_at"])
                pending_since_ms = created_ms if created_ms > 0 else now_ms

            if activity_ages is None:
                activity_ages = fetch_gen_video_activity_ages()
            activity_age = activity_ages.get(code_key)
            if activity_age is not None and activity_age <= GEN_VIDEO_ACTIVITY_STALE_SECONDS:
                active_msg = "后端10004但渲染仍在进行"
                if (row["message"] or "") != active_msg:
                    update_video(video_id, status="pending", message=active_msg)
                    changed = True
                continue

            if now_ms - pending_since_ms >= BACKEND_10004_GRACE_SECONDS * 1000:
                update_video(
                    video_id,
                    status="waiting",
                    message="Auto-retry after backend 10004",
                    code=None,
                    param=None,
                    progress=0,
                    file_path=None,
                )
                meta_update(video_id, phase="queued", stage_label="后端任务记录丢失，已自动重排队列", progress_float=0.0)
                log(f"video {video_id} moved back to waiting after backend 10004 grace period")
                changed = True
            elif (row["message"] or "") != (msg or "backend code 10004"):
                update_video(video_id, status="pending", message=msg or "backend code 10004")
                changed = True
            continue

        if status_code == 10000 and task_status == 1:
            rel_result, out_file = final_mp4_for_code(code)
            current_progress = int(row["progress"] or 0)
            if out_file.exists():
                duration = get_duration_seconds(out_file)
                if duration is not None and duration > 0:
                    _finalize_video_success(
                        video_id,
                        row["name"],
                        code,
                        rel_result,
                        out_file,
                        message=msg or "视频处理完成",
                        duration=duration,
                        stage_label="合成并交付完成",
                    )
                    log(f"video {video_id} finalized from existing output -> {rel_result}")
                    changed = True
                    continue
            pending_since_ms = extract_submit_started_ms(row)
            pending_age_seconds = 0
            if pending_since_ms > 0:
                pending_age_seconds = max(0, int((time.time() * 1000 - pending_since_ms) / 1000))

            is_progress_stalled = progress <= current_progress
            if pending_age_seconds >= BACKEND_PROGRESS_STALL_CHECK_SECONDS and is_progress_stalled:
                if activity_ages is None:
                    activity_ages = fetch_gen_video_activity_ages()
                activity_age = activity_ages.get(code_key)
                if activity_age is not None and activity_age <= GEN_VIDEO_ACTIVITY_STALE_SECONDS:
                    active_msg = "渲染进行中（后端进度暂未刷新）"
                    if (row["message"] or "") != active_msg:
                        update_video(video_id, status="pending", message=active_msg, progress=max(current_progress, progress))
                        changed = True
                    continue
                if pending_age_seconds >= BACKEND_PROGRESS_STALL_REQUEUE_SECONDS:
                    maybe_restart_container(GEN_VIDEO_CONTAINER_NAME, f"stalled pending code={code}")
                    update_video(
                        video_id,
                        status="waiting",
                        message="渲染疑似卡住，自动重试",
                        progress=0,
                        code=None,
                        param=None,
                        file_path=None,
                    )
                    meta_update(video_id, phase="queued", stage_label="渲染疑似卡住，已自动重排队列", progress_float=0.0)
                    log(f"video {video_id} recovered from render stall -> waiting")
                    changed = True
                    continue
                monitor_msg = "渲染进度停滞，正在自动监测"
                if (row["message"] or "") != monitor_msg:
                    update_video(video_id, status="pending", message=monitor_msg, progress=max(current_progress, progress))
                    changed = True
                continue

            if current_progress != progress or (row["message"] or "") != msg:
                update_video(video_id, status="pending", message=msg, progress=progress)
                changed = True
            continue

        if status_code == 10000 and task_status == 2:
            result = str(data.get("result") or "").strip() or f"/{code}-r.mp4"
            msg = str(data.get("msg") or "视频合成完成")
            video_file = OUTPUT_ROOT / result.lstrip("/\\")
            _finalize_video_success(
                video_id,
                row["name"],
                code,
                result,
                video_file,
                message=msg,
                stage_label="合成并交付完成",
            )
            log(f"video {video_id} success -> {result}")
            changed = True
            continue

        if status_code == 10000 and task_status == 3:
            fail_msg = msg or "backend status=3"
            if is_retryable_failure_message(fail_msg):
                maybe_recover_retryable_video_error(video_id, RuntimeError(fail_msg))
                log(f"video {video_id} backend status 3 handled via retry policy")
            else:
                _fail_video_permanently(video_id, "RENDER_FAILED", f"数字人渲染失败：{fail_msg}", json.dumps(data, ensure_ascii=False))
                log(f"video {video_id} marked failed by backend status 3")
            changed = True
            continue

    return changed


def sync_success_deliveries(limit: int = 10) -> None:
    """Ensure recently completed videos in biz.db are copied to DELIVERY_ROOT."""
    rows = fetch_all(
        "SELECT id, name, code, file_path, duration FROM video WHERE status='success' AND code IS NOT NULL ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    for row in rows:
        video_id = int(row["id"])
        code_val = normalize_code(row["code"])
        if not code_val:
            continue
        rel_result, out_file = final_mp4_for_code(code_val)
        if not out_file.exists():
            file_path = str(row["file_path"] or "").lstrip("/\\")
            if file_path:
                candidate = OUTPUT_ROOT / file_path
                if candidate.exists():
                    out_file = candidate
        if out_file.exists():
            duration = get_duration_seconds(out_file)
            if duration is not None and duration > 0:
                deliver_video_copy(video_id, row["name"], code_val, out_file)
                if row["duration"] is None or row["duration"] <= 0:
                    update_video(video_id, duration=duration)


def _load_job_tts_params(video_id: int) -> dict[str, object]:
    meta = get_job_meta(video_id)
    raw = str(meta.get("tts_params_json") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _fail_video_permanently(video_id: int, error_code: str, message: str, detail: str = "") -> None:
    clean_msg = message if ("[不可重试]" in message or "[已达重试上限]" in message) else f"[不可重试] {message}"
    update_video(
        video_id,
        status="failed",
        message=clean_msg[:300],
        progress=0,
        code=None,
    )
    meta_update(
        video_id,
        phase="failed",
        stage_label=clean_msg[:180],
        progress_float=0.0,
        eta_seconds=-1,
        error_code=error_code,
        error_detail=(detail or message)[:2000],
    )
    notify_webhook_if_needed(
        video_id,
        "job.failed",
        {"status": "failed", "error_code": error_code, "message": clean_msg, "detail": (detail or message)[:1000]},
    )


def make_audio_for_video(video: sqlite3.Row, voice: sqlite3.Row | None) -> str:
    video_id = int(video["id"])
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    speaker = str(uuid.uuid4())
    tts_mode = get_tts_mode()

    if tts_mode == "index-tts2" and not is_index_tts2_ready(timeout=4):
        meta_update(video_id, phase="tts_loading", stage_label="正在拉起本地 IndexTTS2 语音服务...", progress_float=2.0)
        maybe_start_index_tts2_adapter("preflight health check")
        if not is_index_tts2_ready(timeout=4):
            raise RuntimeError("index-tts2 adapter is not ready on 127.0.0.1:18180")

    tts_params = _load_job_tts_params(video_id)
    explicit_ref = str(tts_params.get("reference_audio") or "").strip().strip('"').strip("'")
    if tts_mode == "index-tts2" and explicit_ref and Path(explicit_ref).is_file():
        reference_audio = explicit_ref.replace("\\", "/")
        reference_text = ""
        log(f"video {video_id} using explicit tone reference audio: {reference_audio}")
    else:
        if voice is None:
            raise ValueError("未找到可用的参考音色，且未指定自定义语气参考音频。")
        selected_voice, reference_audio = pick_voice_for_tts(voice, tts_mode)
        reference_audio = maybe_shorten_reference_audio(reference_audio, tts_mode, video_id)
        reference_text = str(selected_voice["reference_audio_text"] or "")

        if tts_mode == "fish-speech":
            target_count = choose_fish_reference_count()
            gpu_mem = get_primary_gpu_memory_mb()
            ref_parts = _reference_candidates(reference_audio)
            if len(ref_parts) < target_count and ref_parts:
                ref_parts.extend([ref_parts[-1]] * (target_count - len(ref_parts)))
            if len(ref_parts) > target_count:
                ref_parts = ref_parts[:target_count]
            reference_audio = "|||".join(ref_parts)
            if gpu_mem is not None:
                total_mb, used_mb = gpu_mem
                free_mb = max(0, total_mb - used_mb)
                log(
                    f"video {video_id} fish refs={len(ref_parts)} "
                    f"(target={target_count}, gpu_free={free_mb}MB/{total_mb}MB)"
                )

            text_parts = [part.strip() for part in reference_text.split("|||") if part.strip()]
            if text_parts:
                if len(text_parts) < len(ref_parts):
                    text_parts.extend([text_parts[-1]] * (len(ref_parts) - len(text_parts)))
                elif len(text_parts) > len(ref_parts):
                    text_parts = text_parts[: len(ref_parts)]
                reference_text = "|||".join(text_parts)

        original_reference_audio = str(voice["asr_format_audio_url"] or "").replace("\\", "/")
        if reference_audio != original_reference_audio.replace("\\", "/"):
            log(f"video {video_id} mapped reference audio for {tts_mode}: {reference_audio}")

    if not reference_audio_exists(reference_audio, tts_mode):
        raise FileNotFoundError(
            f"参考音色文件缺失（mode={tts_mode}）：{reference_audio}。请在「模特与音色」页面检查该音色文件。"
        )

    full_text = str(video["text_content"] or "").strip()
    if not full_text:
        raise ValueError("口播文案为空，无法生成语音。")

    # IndexTTS2 模式：整段文案单次发给适配器（内部按句分段、只加载一次模型、不重复冷启动），同时实时轮询进度与中断信号
    if tts_mode == "index-tts2":
        payload: dict[str, object] = {
            "speaker": speaker,
            "format": "wav",
            "text": full_text,
            "reference_audio": reference_audio,
            "reference_text": reference_text,
            "tts_params": tts_params,
        }
        stop_watch = threading.Event()
        tts_started_at = time.time() - 0.5

        def _watch_tts() -> None:
            while not stop_watch.is_set():
                if is_video_cancelled(video_id):
                    log(f"[CANCEL] video {video_id} cancel detected during IndexTTS2 synthesis, aborting adapter...")
                    abort_active_tts_if_needed()
                    return
                try:
                    st = http_get_json(TTS_STATUS_URL, timeout=2)
                    if stop_watch.is_set() or is_video_cancelled(video_id):
                        if is_video_cancelled(video_id):
                            abort_active_tts_if_needed()
                        return
                    data = st.get("data") or {}
                    tts_state = str(data.get("state") or "")
                    updated_at = float(data.get("updated_at") or 0.0)
                    # 仅采信本次任务启动后的真实运行状态，避免把上一条任务的 100% 残留状态错写进当前任务
                    if tts_state in ("loading_model", "synthesizing", "postprocessing") and updated_at >= tts_started_at:
                        tts_pct = int(data.get("progress") or 0)
                        stage_msg = str(data.get("message") or "IndexTTS2 语音合成中...")
                        overall = round(2.0 + min(100, max(0, tts_pct)) * 0.20, 1)  # 2% -> 22%
                        phase_name = "tts_loading" if tts_state == "loading_model" else "tts_synthesizing"
                        logs = data.get("recent_logs") or []
                        if not stop_watch.is_set() and not is_video_cancelled(video_id):
                            if update_video_if_status(video_id, "pending", message=stage_msg[:180], progress=int(overall)):
                                meta_update(
                                    video_id,
                                    phase=phase_name,
                                    stage_label=stage_msg,
                                    progress_float=overall,
                                    heartbeat_at=time.time(),
                                    recent_logs_json=json.dumps(logs[-15:], ensure_ascii=False),
                                )
                except Exception:
                    pass
                stop_watch.wait(1.0)

        watcher = threading.Thread(target=_watch_tts, daemon=True)
        watcher.start()
        try:
            audio_bytes = b""
            for attempt in range(1, TTS_MAX_RETRIES + 1):
                if is_video_cancelled(video_id):
                    abort_active_tts_if_needed()
                    raise InterruptedError(f"video {video_id} cancelled by user")
                try:
                    log(
                        f"video {video_id} IndexTTS2 single-batch invoke (chars={len(full_text)}, attempt={attempt}/{TTS_MAX_RETRIES})"
                    )
                    audio_bytes = http_post_binary(
                        TTS_INVOKE_URL,
                        payload,
                        timeout=TTS_SINGLE_BATCH_TIMEOUT_SECONDS,
                    )
                    break
                except urllib.error.HTTPError as http_exc:
                    body_text = ""
                    try:
                        body_text = http_exc.read().decode("utf-8", errors="replace")[:400]
                    except Exception:
                        pass
                    if http_exc.code == 499 or is_video_cancelled(video_id):
                        raise InterruptedError(f"video {video_id} cancelled by user") from http_exc
                    if http_exc.code == 400:
                        raise ValueError(f"IndexTTS2 参数或音色错误 (HTTP 400): {body_text or http_exc}") from http_exc
                    log(f"video {video_id} IndexTTS2 HTTP {http_exc.code} attempt {attempt}: {body_text}")
                    if attempt >= 2:
                        maybe_start_index_tts2_adapter("tts invoke http errors")
                    if attempt >= TTS_MAX_RETRIES:
                        raise RuntimeError(f"IndexTTS2 语音合成连续失败 (HTTP {http_exc.code}): {body_text}") from http_exc
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                except Exception as exc:
                    if is_video_cancelled(video_id):
                        raise InterruptedError(f"video {video_id} cancelled by user") from exc
                    log(f"video {video_id} IndexTTS2 invoke failed attempt {attempt}: {exc}")
                    if attempt >= 2:
                        maybe_start_index_tts2_adapter("tts invoke errors")
                    if attempt >= TTS_MAX_RETRIES:
                        raise RuntimeError(f"IndexTTS2 语音合成重试失败: {exc}") from exc
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        finally:
            stop_watch.set()
            watcher.join(timeout=4)

        filename = f"{speaker}.wav"
        target_wav = OUTPUT_ROOT / filename
        target_wav.write_bytes(audio_bytes)
        meta_update(
            video_id,
            source_audio_path=str(target_wav),
            phase="submitting",
            stage_label="语音合成完成，准备提交数字人渲染...",
            progress_float=23.0,
            heartbeat_at=time.time(),
        )
        return filename

    # Fish-Speech 兼容分段逻辑（保持原样）
    base_payload = {
        "speaker": speaker,
        "format": "wav",
        "topP": 0.7,
        "max_new_tokens": 1024,
        "chunk_length": 100,
        "repetition_penalty": 1.2,
        "temperature": 0.7,
        "need_asr": False,
        "streaming": False,
        "is_fixed_seed": TTS_FIXED_SEED,
        "is_norm": 1,
        "reference_audio": reference_audio,
        "reference_text": reference_text,
    }
    text_chunks = split_tts_text_for_safety(full_text)
    if len(text_chunks) > 1:
        log(f"video {video_id} auto-split text: chars={len(full_text)} -> chunks={len(text_chunks)}")

    chunk_audios: list[bytes] = []
    for idx, chunk_text in enumerate(text_chunks, start=1):
        if is_video_cancelled(video_id):
            log(f"[CANCEL] video {video_id} was cancelled during audio generation")
            raise InterruptedError(f"video {video_id} cancelled by user")
        payload = dict(base_payload)
        payload["text"] = chunk_text
        if len(text_chunks) > 1:
            try:
                chunk_len = int(payload.get("chunk_length") or 100)
            except Exception:
                chunk_len = 100
            payload["chunk_length"] = min(chunk_len, 80)

        meta_update(
            video_id,
            phase="tts_synthesizing",
            stage_label=f"Fish-Speech 正在合成第 {idx}/{len(text_chunks)} 段语音...",
            progress_float=round(2.0 + ((idx - 1) / max(1, len(text_chunks))) * 20.0, 1),
            heartbeat_at=time.time(),
        )

        for attempt in range(1, TTS_MAX_RETRIES + 1):
            try:
                audio_bytes = http_post_binary(TTS_INVOKE_URL, payload, timeout=TTS_REQUEST_TIMEOUT_SECONDS)
                chunk_audios.append(audio_bytes)
                break
            except Exception as exc:
                log(f"video {video_id} fish audio failed (chunk {idx}/{len(text_chunks)}) attempt {attempt}: {exc}")
                if attempt >= 2:
                    maybe_restart_container(TTS_CONTAINER_NAME, "tts invoke errors")
                if attempt >= TTS_MAX_RETRIES:
                    raise RuntimeError(
                        f"video {video_id} audio generation failed after retries (chunk {idx}/{len(text_chunks)}): {exc}"
                    ) from exc
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    audio_bytes = merge_wav_payloads(chunk_audios, video_id)
    filename = f"{speaker}.wav"
    target_wav = OUTPUT_ROOT / filename
    target_wav.write_bytes(audio_bytes)
    meta_update(video_id, source_audio_path=str(target_wav), progress_float=23.0, heartbeat_at=time.time())
    return filename


def submit_video_task(audio_name: str, model_video_path: str) -> tuple[dict, dict]:
    for attempt in range(1, SUBMIT_MAX_RETRIES + 1):
        task_code = str(uuid.uuid4())
        param = {
            "audio_url": audio_name,
            "video_url": model_video_path,
            "code": task_code,
            "chaofen": 0,
            "watermark_switch": 0,
            "pn": 1,
        }
        try:
            result = http_post_json(F2F_SUBMIT_URL, param, timeout=120)
            return result, param
        except Exception as exc:
            log(f"submit failed attempt {attempt}/{SUBMIT_MAX_RETRIES}: {exc}")
            if attempt >= 2:
                maybe_restart_container(GEN_VIDEO_CONTAINER_NAME, "submit errors")
            if attempt >= SUBMIT_MAX_RETRIES:
                raise RuntimeError(f"submit failed after retries: {exc}") from exc
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError("submit failed unexpectedly")


def poll_until_done(
    video_id: int,
    code: str,
    estimated_total_frames: int = 0,
    render_started_at: float | None = None,
) -> tuple[bool, dict]:
    consecutive_query_errors = 0
    consecutive_unknown_states = 0
    started_at = render_started_at or time.time()
    first_10004_at = 0.0
    last_telemetry_at = 0.0
    last_progress_float = 25.0
    best_rendered_frames = 0
    known_total_frames = max(0, int(estimated_total_frames or 0))
    best_phase = "face2face_rendering"
    last_active_hb = started_at

    while True:
        time.sleep(POLL_SECONDS)

        if is_video_cancelled(video_id, code):
            studio_cancel = is_studio_cancel_requested(video_id)
            log(f"[CANCEL] video {video_id} (code={code}) was cancelled by user! Aborting render...")
            force_restart_gen_video_container(f"user cancelled video {video_id} code={code}", wait_ready=True)
            if studio_cancel:
                mark_video_cancelled(video_id, "任务已被用户中断")
            clear_cancelled_video_task(video_id, code)
            try:
                _, out_file = final_mp4_for_code(code)
                if out_file.exists():
                    out_file.unlink(missing_ok=True)
            except Exception:
                pass
            return False, {"msg": "Task cancelled by user", "cancelled": True}

        try:
            res = http_get_json(F2F_QUERY_URL.format(code=code), timeout=30)
            consecutive_query_errors = 0
        except Exception as exc:
            if is_video_cancelled(video_id, code):
                studio_cancel = is_studio_cancel_requested(video_id)
                log(f"[CANCEL] video {video_id} (code={code}) cancelled during query retry")
                force_restart_gen_video_container(f"user cancelled video {video_id} code={code}", wait_ready=True)
                if studio_cancel:
                    mark_video_cancelled(video_id, "任务已被用户中断")
                clear_cancelled_video_task(video_id, code)
                return False, {"msg": "Task cancelled by user", "cancelled": True}
            consecutive_query_errors += 1
            log(f"video {video_id} query network error ({consecutive_query_errors}/10): {exc}")
            meta_update(
                video_id,
                stage_label=f"渲染引擎连接抖动，正在重连 ({consecutive_query_errors}/10)...",
                error_detail=str(exc),
            )
            if consecutive_query_errors >= 10:
                maybe_restart_container(GEN_VIDEO_CONTAINER_NAME, "query errors")
                consecutive_query_errors = 0
            continue

        status_code = int(res.get("code", -1))
        data = res.get("data") or {}
        task_status = data.get("status")
        backend_progress = int(data.get("progress") or 0)
        msg = str(data.get("msg") or res.get("msg") or "")

        if status_code in (9999, 10002, 10003):
            return False, {"msg": msg or f"backend code {status_code}"}

        # 修复原版死循环：显式处理 10004（任务不存在）
        if status_code == 10004:
            _, out_file = final_mp4_for_code(code)
            if out_file.exists():
                dur = get_duration_seconds(out_file)
                if dur is not None and dur > 0:
                    return True, {"result": f"/{code}-r.mp4", "msg": "检测到成片已生成，自动恢复完成", "progress": 100}

            telemetry = parse_gen_video_render_telemetry(code, known_total_frames, started_at)
            hb = float(telemetry.get("heartbeat_at") or 0.0)
            if hb > 0 and (time.time() - hb) <= GEN_VIDEO_ACTIVITY_STALE_SECONDS:
                first_10004_at = 0.0
                last_active_hb = max(last_active_hb, hb)
                tel_frames = int(telemetry.get("rendered_frames") or 0)
                if tel_frames > best_rendered_frames:
                    best_rendered_frames = tel_frames
                tel_total = int(telemetry.get("total_frames") or 0)
                if tel_total > known_total_frames:
                    known_total_frames = tel_total
                stage_lbl = str(telemetry.get("stage_label") or "渲染仍在进行中（后端状态暂未同步）")
                meta_update(
                    video_id,
                    phase=str(telemetry.get("phase") or best_phase),
                    stage_label=stage_lbl,
                    rendered_frames=best_rendered_frames,
                    total_frames=known_total_frames,
                    render_fps=float(telemetry.get("render_fps") or 0.0),
                    eta_seconds=int(telemetry.get("eta_seconds") or -1),
                    progress_float=max(last_progress_float, float(telemetry.get("progress_float") or last_progress_float)),
                    heartbeat_at=hb,
                    recent_logs_json=json.dumps(telemetry.get("recent_logs") or [], ensure_ascii=False),
                )
                continue

            if first_10004_at == 0.0:
                first_10004_at = time.time()
            elapsed_10004 = time.time() - first_10004_at
            meta_update(
                video_id,
                stage_label=f"后端任务记录丢失 (10004)，等待确认 ({int(elapsed_10004)}s/{BACKEND_10004_GRACE_SECONDS}s)...",
            )
            if elapsed_10004 >= BACKEND_10004_GRACE_SECONDS:
                return False, {"msg": "任务不存在 (backend 10004 timeout，可能容器已重启)"}
            continue

        first_10004_at = 0.0

        if status_code == 10000 and task_status == 1:
            consecutive_unknown_states = 0
            now_t = time.time()
            if now_t - last_telemetry_at >= 3.0:
                last_telemetry_at = now_t
                telemetry = parse_gen_video_render_telemetry(code, known_total_frames, started_at)
                tel_frames = int(telemetry.get("rendered_frames") or 0)
                if tel_frames > best_rendered_frames:
                    best_rendered_frames = tel_frames
                tel_total = int(telemetry.get("total_frames") or 0)
                if tel_total > known_total_frames:
                    known_total_frames = tel_total
                tel_phase = str(telemetry.get("phase") or "face2face_rendering")
                if _PHASE_RANK.get(tel_phase, 0) >= _PHASE_RANK.get(best_phase, 0):
                    best_phase = tel_phase
                tel_hb = float(telemetry.get("heartbeat_at") or 0.0)
                if tel_hb > last_active_hb:
                    last_active_hb = tel_hb

                # 后端 /easy/query 在封装完成前最多到 80%，结合真实帧级遥测保证进度单调平滑
                tel_progress = float(telemetry.get("progress_float") or 25.0)
                combined_pct = max(last_progress_float, tel_progress, float(backend_progress))
                combined_pct = min(99.0, combined_pct)
                last_progress_float = round(combined_pct, 1)
                stage_lbl = str(telemetry.get("stage_label") or msg or "视频渲染中")

                if update_video_if_status(video_id, "pending", message=stage_lbl[:180], progress=int(last_progress_float)):
                    meta_update(
                        video_id,
                        phase=best_phase,
                        stage_label=stage_lbl,
                        rendered_frames=best_rendered_frames,
                        total_frames=known_total_frames,
                        render_fps=float(telemetry.get("render_fps") or 0.0),
                        eta_seconds=int(telemetry.get("eta_seconds") or -1),
                        progress_float=last_progress_float,
                        heartbeat_at=last_active_hb,
                        recent_logs_json=json.dumps(telemetry.get("recent_logs") or [], ensure_ascii=False),
                    )
                    if telemetry.get("container_error"):
                        meta_update(video_id, error_detail=str(telemetry["container_error"]))

                # 死锁/长时失活守卫：仅当连续超时且容器日志长期无新心跳时主动介入，避免假死永久挂起
                render_age = now_t - started_at
                hb_stale_sec = now_t - last_active_hb
                if render_age >= BACKEND_PROGRESS_STALL_REQUEUE_SECONDS and hb_stale_sec >= GEN_VIDEO_ACTIVITY_STALE_SECONDS * 2:
                    maybe_restart_container(GEN_VIDEO_CONTAINER_NAME, f"render stalled >{int(hb_stale_sec)}s code={code}")
                    return False, {"msg": f"渲染进程超过 {int(hb_stale_sec)} 秒无日志心跳，疑似卡死，已触发容器恢复"}
                if render_age >= PENDING_WITH_CODE_TIMEOUT_SECONDS and hb_stale_sec >= GEN_VIDEO_ACTIVITY_STALE_SECONDS:
                    maybe_restart_container(GEN_VIDEO_CONTAINER_NAME, f"render timeout code={code}")
                    return False, {"msg": f"视频渲染超时（>{PENDING_WITH_CODE_TIMEOUT_SECONDS}s 且无活跃日志）"}
            else:
                if float(backend_progress) > last_progress_float:
                    last_progress_float = round(min(99.0, float(backend_progress)), 1)
                    update_video_if_status(video_id, "pending", message=msg or "视频渲染中", progress=int(last_progress_float))
            continue

        if status_code == 10000 and task_status == 2:
            if is_video_cancelled(video_id, code):
                studio_cancel = is_studio_cancel_requested(video_id)
                log(f"[CANCEL] video {video_id} was cancelled right before completion, discarding output")
                if studio_cancel:
                    mark_video_cancelled(video_id, "任务已被用户中断")
                clear_cancelled_video_task(video_id, code)
                return False, {"msg": "Task cancelled by user", "cancelled": True}
            return True, data

        if status_code == 10000 and task_status == 3:
            telemetry = parse_gen_video_render_telemetry(code, known_total_frames, started_at)
            err_hint = str(telemetry.get("container_error") or "")
            combined_msg = f"{msg or 'backend status=3'}" + (f" | {err_hint}" if err_hint else "")
            return False, {"msg": combined_msg}

        # 防止未知状态码/task_status 引发静默死循环
        consecutive_unknown_states += 1
        log(f"video {video_id} unexpected query reply ({consecutive_unknown_states}/15): code={status_code} status={task_status} msg={msg}")
        if consecutive_unknown_states >= 15:
            return False, {"msg": f"渲染引擎返回异常状态 (code={status_code}, status={task_status}): {msg}"}


def _estimate_total_frames(model_video_path: Path, audio_file: Path) -> int:
    try:
        fps = 25.0
        audio_dur = 0.0
        if probe_media_info is not None:
            m_info = probe_media_info(model_video_path)
            if m_info.get("fps") and float(m_info["fps"]) > 1.0:
                fps = float(m_info["fps"])
            a_info = probe_media_info(audio_file)
            if a_info.get("duration") and float(a_info["duration"]) > 0:
                audio_dur = float(a_info["duration"])
        else:
            d = get_duration_seconds(audio_file)
            audio_dur = float(d or 0.0)
        if audio_dur > 0:
            # 实机验证：duix.avatar 按底模原始帧率（包括 60fps）完整渲染，不降帧
            return max(1, int(round(audio_dur * fps)))
    except Exception:
        pass
    return 0


def run_one(video: sqlite3.Row) -> None:
    video_id = int(video["id"])
    try:
        model = fetch_model(int(video["model_id"]))
    except Exception as exc:
        _fail_video_permanently(video_id, "MODEL_NOT_FOUND", f"找不到指定的数字人模特 (id={video['model_id']})", str(exc))
        return

    model_video_rel = str(model["video_path"] or "").strip()
    model_video_abs = OUTPUT_ROOT / model_video_rel.lstrip("/\\")
    if not model_video_rel or not model_video_abs.exists():
        _fail_video_permanently(
            video_id,
            "MODEL_VIDEO_MISSING",
            f"数字人「{model['name']}」的底模视频文件缺失（{model_video_abs}），请前往「模特与音色」页面重新关联或上传底模视频。",
        )
        return

    audio_path = str(video["audio_path"] or "").strip()
    voice: sqlite3.Row | None = None
    if not audio_path:
        job_tts_params = _load_job_tts_params(video_id)
        custom_ref = str(job_tts_params.get("reference_audio") or "").strip().strip('"').strip("'")
        has_custom_ref = bool(custom_ref and Path(custom_ref).is_file())
        raw_vid = video["voice_id"] or model["voice_id"]
        if raw_vid:
            try:
                voice = fetch_voice(int(raw_vid))
            except Exception as exc:
                if not has_custom_ref:
                    _fail_video_permanently(video_id, "VOICE_NOT_FOUND", f"找不到可用音色配置：{exc}", str(exc))
                    return
        elif not has_custom_ref:
            _fail_video_permanently(video_id, "VOICE_NOT_FOUND", "找不到可用音色配置，且未指定专属声音语气")
            return

    run_started_at_ms = int(time.time() * 1000)
    cleanup_other_runner_no_code_pending(video_id)

    update_video(
        video_id,
        status="pending",
        message="正在准备音频...",
        file_path=None,
        progress=1,
        code=None,
        param=json.dumps({"_run_started_at_ms": run_started_at_ms, "_queue_owner": QUEUE_OWNER}, ensure_ascii=False),
    )
    meta_update(
        video_id,
        phase="tts_loading" if not audio_path else "submitting",
        stage_label="正在准备音频..." if not audio_path else "正在校验已导入音频...",
        progress_float=1.0 if not audio_path else 20.0,
        rendered_frames=0,
        total_frames=0,
        eta_seconds=-1,
        error_code="",
        error_detail="",
        heartbeat_at=time.time(),
    )

    if is_video_cancelled(video_id):
        if is_studio_cancel_requested(video_id):
            mark_video_cancelled(video_id, "任务已被用户中断")
        clear_cancelled_video_task(video_id)
        return

    try:
        if audio_path:
            audio_name = Path(audio_path).name
            target_audio = OUTPUT_ROOT / audio_name
            if not target_audio.exists():
                meta = get_job_meta(video_id)
                src_audio = Path(str(meta.get("source_audio_path") or ""))
                if src_audio.is_file():
                    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_audio, target_audio)
            if not target_audio.exists():
                _fail_video_permanently(
                    video_id,
                    "AUDIO_FILE_MISSING",
                    f"指定的驱动音频文件不存在：{target_audio}",
                )
                return
        else:
            audio_name = make_audio_for_video(video, voice)
    except InterruptedError:
        log(f"[CANCEL] video {video_id} cancelled cleanly during audio prep")
        if is_studio_cancel_requested(video_id):
            mark_video_cancelled(video_id, "任务在语音合成阶段被用户中断")
        clear_cancelled_video_task(video_id)
        return
    except (FileNotFoundError, ValueError) as exc:
        _fail_video_permanently(video_id, "AUDIO_PREP_ERROR", str(exc), str(exc))
        return
    except Exception as exc:
        if is_video_cancelled(video_id):
            if is_studio_cancel_requested(video_id):
                mark_video_cancelled(video_id, "任务在语音合成阶段被用户中断")
            clear_cancelled_video_task(video_id)
            return
        raise

    if is_video_cancelled(video_id):
        log(f"[CANCEL] video {video_id} was cancelled after audio prep, skipping submit")
        if is_studio_cancel_requested(video_id):
            mark_video_cancelled(video_id, "任务已被用户中断")
        clear_cancelled_video_task(video_id)
        return

    if has_pending_with_code(exclude_video_id=video_id):
        update_video(
            video_id,
            status="waiting",
            message="已有任务在渲染，自动顺延排队",
            audio_path=audio_name,
            code=None,
            param=None,
            progress=0,
            file_path=None,
        )
        meta_update(video_id, phase="queued", stage_label="已有任务在渲染，音频已就绪等待渲染槽位", progress_float=22.0)
        log(f"video {video_id} deferred before submit because another pending task is active")
        return

    estimated_total_frames = _estimate_total_frames(model_video_abs, OUTPUT_ROOT / audio_name)
    update_video(video_id, status="pending", message="正在提交至数字人渲染引擎...", audio_path=audio_name, progress=24)
    meta_update(
        video_id,
        phase="submitting",
        stage_label="正在提交至数字人渲染引擎 (8383)...",
        total_frames=estimated_total_frames,
        progress_float=24.0,
        heartbeat_at=time.time(),
    )

    if is_video_cancelled(video_id):
        if is_studio_cancel_requested(video_id):
            mark_video_cancelled(video_id, "任务已被用户中断")
        clear_cancelled_video_task(video_id)
        return

    submit_result, submit_param = submit_video_task(audio_name, model_video_rel)
    submit_code = _coerce_int(submit_result.get("code"))
    submit_msg = str(submit_result.get("msg") or submit_result)
    submit_param_with_meta = dict(submit_param)
    submit_param_with_meta["_submitted_at_ms"] = int(time.time() * 1000)
    submit_param_with_meta["_queue_owner"] = QUEUE_OWNER

    if submit_code != 10000:
        if is_video_cancelled(video_id):
            if is_studio_cancel_requested(video_id):
                mark_video_cancelled(video_id, "任务已被用户中断")
            clear_cancelled_video_task(video_id)
            return
        if is_busy_submit_response(submit_result) or is_retryable_failure_message(submit_msg):
            active_code = find_matching_active_backend_code(audio_name, model_video_rel)
            if active_code:
                recovered_param = dict(submit_param_with_meta)
                recovered_param["code"] = active_code
                recovered_param["_submitted_at_ms"] = int(time.time() * 1000)
                recovered_param["_recovered_client_submit"] = True
                clear_backend_busy_lock()
                update_video(
                    video_id,
                    status="pending",
                    message="检测到同一任务已在后端渲染，已自动接管跟踪",
                    audio_path=audio_name,
                    code=active_code,
                    param=json.dumps(recovered_param, ensure_ascii=False),
                    progress=25,
                    file_path=None,
                )
                meta_update(video_id, phase="face2face_rendering", stage_label="已接管后台渲染任务", progress_float=25.0)
                log(f"video {video_id} adopted active backend code={active_code}")
                return

            cooldown = mark_backend_busy(f"submit rejected ({submit_msg})")
            update_video(
                video_id,
                status="waiting",
                message=f"后端忙碌，{cooldown}s 后自动重试",
                audio_path=audio_name,
                code=None,
                param=None,
                progress=0,
                file_path=None,
            )
            meta_update(video_id, phase="queued", stage_label=f"渲染引擎忙碌，{cooldown}s 后自动重试")
            log(f"video {video_id} submit busy -> deferred retry ({submit_msg})")
            return

        _fail_video_permanently(video_id, "SUBMIT_REJECTED", f"渲染引擎拒绝任务：{submit_msg}", json.dumps(submit_result, ensure_ascii=False))
        return

    clear_backend_busy_lock()
    task_code = str(submit_param["code"])
    render_started_at = time.time()
    update_video(
        video_id,
        status="pending",
        message="已进入数字人唇形渲染...",
        audio_path=audio_name,
        code=task_code,
        param=json.dumps(submit_param_with_meta, ensure_ascii=False),
        progress=25,
    )
    meta_update(
        video_id,
        phase="face2face_rendering",
        stage_label="已提交渲染引擎，等待首批帧输出...",
        total_frames=estimated_total_frames,
        progress_float=25.0,
        heartbeat_at=render_started_at,
    )
    log(f"video {video_id} submitted, code={task_code}, est_frames={estimated_total_frames}")

    ok, data = poll_until_done(
        video_id,
        task_code,
        estimated_total_frames=estimated_total_frames,
        render_started_at=render_started_at,
    )
    if not ok:
        if data.get("cancelled"):
            log(f"[CANCEL] video {video_id} aborted cleanly (task cancelled by user)")
            return
        fail_msg = str(data.get("msg", "unknown error"))
        if is_retryable_failure_message(fail_msg):
            if maybe_recover_retryable_video_error(video_id, RuntimeError(fail_msg)):
                return
        _fail_video_permanently(video_id, "RENDER_FAILED", f"数字人渲染失败：{fail_msg}", json.dumps(data, ensure_ascii=False))
        return

    result = str(data.get("result") or "")
    msg = str(data.get("msg") or "视频合成完成")
    video_file = OUTPUT_ROOT / result.lstrip("/\\")
    meta_update(video_id, phase="delivering", stage_label="渲染完成，正在校验并归档成片...", progress_float=98.0)
    _finalize_video_success(
        video_id,
        video["name"],
        task_code,
        result,
        video_file,
        message=msg,
        stage_label="合成并交付完成",
    )
    log(f"video {video_id} success -> {result}")


def maybe_recover_retryable_video_error(video_id: int, exc: Exception) -> bool:
    if is_video_cancelled(video_id):
        log(f"[CANCEL] Task {video_id} was cancelled, skipping retry recovery")
        if is_studio_cancel_requested(video_id):
            mark_video_cancelled(video_id, "任务已被用户中断")
        clear_cancelled_video_task(video_id)
        return True
    msg = str(exc or "")
    if not is_retryable_failure_message(msg):
        _fail_video_permanently(video_id, "RUNTIME_ERROR", f"任务执行异常：{msg[:200]}", msg)
        return True

    meta = get_job_meta(video_id)
    retries = int(meta.get("retry_count") or 0)
    max_retries = _resolve_max_retries(meta)
    if retries >= max_retries:
        _fail_video_permanently(
            video_id,
            "MAX_RETRIES_EXCEEDED",
            f"[已达重试上限] 自动重试 {retries}/{max_retries} 次后仍失败：{msg[:160]}",
            msg,
        )
        return True

    next_retry = retries + 1
    meta_update(
        video_id,
        retry_count=next_retry,
        phase="queued",
        stage_label=f"遇到瞬时异常，正在准备第 {next_retry}/{max_retries} 次自动重试...",
        progress_float=0.0,
        rendered_frames=0,
        eta_seconds=-1,
        error_detail=msg[:1500],
    )
    update_video(
        video_id,
        status="waiting",
        message=f"瞬时异常自动重试 ({next_retry}/{max_retries}): {msg[:80]}",
        progress=0,
        code=None,
        param=None,
        file_path=None,
    )
    log(f"video {video_id} runtime retryable error -> waiting ({next_retry}/{max_retries}): {msg}")
    return True


def has_pending_with_code(exclude_video_id: int | None = None) -> bool:
    rows = fetch_all("SELECT id, code FROM video WHERE status='pending'")
    for row in rows:
        if exclude_video_id is not None and int(row["id"]) == int(exclude_video_id):
            continue
        if is_valid_task_code(row["code"]):
            return True
    return False


def has_waiting() -> bool:
    row = fetch_row("SELECT id FROM video WHERE status='waiting' LIMIT 1")
    return row is not None


def run_once() -> int:
    ensure_retry_triggers()
    process_cancelled_tasks()
    sync_voice_reference_paths_for_mode()
    reset_stuck_rows()
    sync_pending_from_backend()
    sync_success_deliveries()
    if is_queue_paused():
        log("Queue is paused; skipping new task dequeue.")
        return 0
    if backend_busy_wait_seconds() > 0:
        maybe_log_backend_busy_wait()
        return 0
    if has_pending_with_code():
        log("Pending backend task exists; wait for next sync cycle.")
        return 0

    next_video = fetch_next_waiting()
    if next_video is None:
        log("No waiting tasks left.")
        return 0

    try:
        run_one(next_video)
    except Exception as exc:
        maybe_recover_retryable_video_error(int(next_video["id"]), exc)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HeyGem queue runner")
    parser.add_argument("--daemon", action="store_true", help="Run continuously and auto-heal queue")
    parser.add_argument("--init-triggers", action="store_true", help="Initialize queue SQLite triggers and exit")
    args = parser.parse_args()

    if args.init_triggers:
        ensure_retry_triggers()
        return 0

    if not acquire_runner_lock():
        return 0

    try:
        if not args.daemon:
            return run_once()

        log("Queue daemon started.")
        ensure_retry_triggers()
        while True:
            try:
                sync_voice_reference_paths_for_mode()
                reset_stuck_rows()
                changed = sync_pending_from_backend()
                sync_success_deliveries()
                if is_queue_paused():
                    pass
                elif backend_busy_wait_seconds() > 0:
                    maybe_log_backend_busy_wait()
                elif not has_pending_with_code() and has_waiting():
                    next_video = fetch_next_waiting()
                    if next_video is not None:
                        try:
                            run_one(next_video)
                            changed = True
                        except Exception as exc:
                            maybe_recover_retryable_video_error(int(next_video["id"]), exc)
                            changed = True
                if not changed:
                    time.sleep(DAEMON_IDLE_SECONDS)
            except Exception as exc:
                log(f"daemon loop error: {exc}")
                time.sleep(DAEMON_IDLE_SECONDS)
    finally:
        release_runner_lock()


if __name__ == "__main__":
    raise SystemExit(main())


