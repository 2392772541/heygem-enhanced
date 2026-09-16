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
import time
import uuid
import urllib.error
import urllib.request
import wave
from pathlib import Path


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
F2F_SUBMIT_URL = "http://127.0.0.1:8383/easy/submit"
F2F_QUERY_URL = "http://127.0.0.1:8383/easy/query?code={code}"
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        )
    except Exception as exc:
        log(f"gen-video activity check failed: {exc}")
        return dict(_LAST_GEN_VIDEO_ACTIVITY)

    blob = "\n".join(part for part in [(result.stdout or ""), (result.stderr or "")] if part)
    activity: dict[str, float] = {}
    activity_tokens = ("avi write success", "av_transfer", "get_run_netg end", "preprocess_ end")
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


def ensure_retry_triggers() -> None:
    conn = db()
    try:
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


def deliver_video_copy(video_id: int, video_name: object, code: str, source: Path) -> Path | None:
    """Copy a completed backend video to the user's stable delivery folder.

    The backend keeps using D:/duix_avatar_data for its mounted working data;
    this copy is the user-facing output and does not change the DB path expected
    by the HeyGem client.
    """
    if not source.exists() or source.stat().st_size <= 0:
        return None
    name = str(video_name or f"video_{video_id}").strip() or f"video_{video_id}"
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")[:80] or f"video_{video_id}"
    short_code = (str(code or "")[:8] or "result")
    DELIVERY_ROOT.mkdir(parents=True, exist_ok=True)
    target = DELIVERY_ROOT / f"{video_id:04d}_{name}_{short_code}.mp4"
    if target.resolve() != source.resolve():
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
    return target


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
    # The desktop client may submit a newly-created row itself. Give it a short
    # window first so the client and queue runner do not submit the same job.
    cutoff_ms = int(time.time() * 1000) - CLIENT_SUBMIT_GRACE_SECONDS * 1000
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
            check=False, timeout=25,
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


def reset_stuck_rows() -> None:
    retryable_failed = fetch_all("SELECT id, message FROM video WHERE status='failed'")
    for row in retryable_failed:
        if is_retryable_failure_message(row["message"]):
            update_video(
                int(row["id"]),
                status="waiting",
                message="Auto-retry after transient failure",
                progress=0,
                code=None,
                param=None,
                file_path=None,
            )
            log(f"Recovered retryable failed -> waiting: {row['id']}")

    now_ms = int(time.time() * 1000)
    invalid_cutoff_ms = now_ms - PENDING_WITHOUT_CODE_STUCK_SECONDS * 1000
    timeout_cutoff_ms = now_ms - PENDING_WITH_CODE_TIMEOUT_SECONDS * 1000

    drafts = fetch_all("SELECT id FROM video WHERE status='draft'")
    for row in drafts:
        update_video(int(row["id"]), status="waiting", message=None, code=None, progress=0, file_path=None)
        log(f"Recovered draft -> waiting: {row['id']}")

    pending_rows = fetch_all("SELECT id, code, created_at, param, message FROM video WHERE status='pending'")
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
                delivered = deliver_video_copy(int(row["id"]), row["name"], code, abs_result)
                if delivered:
                    log(f"Delivered video {row['id']} -> {delivered}")
                update_video(
                    int(row["id"]),
                    status="success",
                    message="Recovered from timeout by existing output file",
                    file_path=rel_result,
                    progress=100,
                    duration=duration,
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
        if "preparing audio" in message_lower:
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
    changed = False
    pending = fetch_all("SELECT * FROM video WHERE status='pending'")
    activity_ages: dict[str, float] | None = None
    for row in pending:
        video_id = int(row["id"])
        code = normalize_code(row["code"])
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
                update_video(
                    video_id,
                    status="waiting",
                    message="Auto-retry after transient backend failure",
                    progress=0,
                    code=None,
                    param=None,
                    file_path=None,
                )
                log(f"video {video_id} backend code {status_code} is retryable -> waiting")
            else:
                update_video(video_id, status="failed", message=fail_msg, progress=0)
                log(f"video {video_id} marked failed by backend code {status_code}")
            changed = True
            continue

        if status_code == 10004:
            rel_result, out_file = final_mp4_for_code(code)
            duration = get_duration_seconds(out_file) if out_file.exists() else None
            if out_file.exists() and duration is not None and duration > 0:
                delivered = deliver_video_copy(video_id, row["name"], code, out_file)
                if delivered:
                    log(f"Delivered video {video_id} -> {delivered}")
                update_video(
                    video_id,
                    status="success",
                    message="Recovered by output file while backend returned 10004",
                    file_path=rel_result,
                    progress=100,
                    duration=duration,
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
                    delivered = deliver_video_copy(video_id, row["name"], code, out_file)
                    if delivered:
                        log(f"Delivered video {video_id} -> {delivered}")
                    update_video(
                        video_id,
                        status="success",
                        message=msg or "视频处理完成",
                        file_path=rel_result,
                        progress=100,
                        duration=duration,
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
            result = str(data.get("result") or "")
            msg = str(data.get("msg") or "Task finished")
            video_file = OUTPUT_ROOT / result.lstrip("/\\")
            duration = get_duration_seconds(video_file)
            delivered = deliver_video_copy(video_id, row["name"], code, video_file)
            if delivered:
                log(f"Delivered video {video_id} -> {delivered}")
            update_video(
                video_id,
                status="success",
                message=msg,
                file_path=result,
                progress=int(data.get("progress") or 100),
                duration=duration,
            )
            log(f"video {video_id} success -> {result}")
            changed = True
            continue

        if status_code == 10000 and task_status == 3:
            fail_msg = msg or "backend status=3"
            if is_retryable_failure_message(fail_msg):
                update_video(
                    video_id,
                    status="waiting",
                    message="Auto-retry after transient backend failure",
                    progress=0,
                    code=None,
                    param=None,
                    file_path=None,
                )
                log(f"video {video_id} backend status 3 is retryable -> waiting")
            else:
                update_video(video_id, status="failed", message=fail_msg, progress=0)
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


def make_audio_for_video(video: sqlite3.Row, voice: sqlite3.Row) -> str:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    speaker = str(uuid.uuid4())
    tts_mode = get_tts_mode()

    if tts_mode == "index-tts2" and not is_index_tts2_ready(timeout=4):
        maybe_start_index_tts2_adapter("preflight health check")
        if not is_index_tts2_ready(timeout=4):
            raise RuntimeError("index-tts2 adapter is not ready on 127.0.0.1:18180")

    selected_voice, reference_audio = pick_voice_for_tts(voice, tts_mode)
    reference_audio = maybe_shorten_reference_audio(reference_audio, tts_mode, int(video["id"]))
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
                f"video {video['id']} fish refs={len(ref_parts)} "
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
        log(f"video {video['id']} mapped reference audio for {tts_mode}: {reference_audio}")
    if not reference_audio_exists(reference_audio, tts_mode):
        raise FileNotFoundError(
            f"video {video['id']} reference audio is missing for mode={tts_mode}: {reference_audio}"
        )
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
    text_chunks = split_tts_text_for_safety(video["text_content"] or "")
    if len(text_chunks) > 1:
        log(
            f"video {video['id']} auto-split text: chars={len(str(video['text_content'] or ''))} "
            f"-> chunks={len(text_chunks)}"
        )

    chunk_audios: list[bytes] = []
    for idx, chunk_text in enumerate(text_chunks, start=1):
        payload = dict(base_payload)
        payload["text"] = chunk_text
        if len(text_chunks) > 1:
            try:
                chunk_len = int(payload.get("chunk_length") or 100)
            except Exception:
                chunk_len = 100
            payload["chunk_length"] = min(chunk_len, 80)

        for attempt in range(1, TTS_MAX_RETRIES + 1):
            try:
                if len(text_chunks) > 1:
                    log(
                        f"video {video['id']} generating audio chunk {idx}/{len(text_chunks)} "
                        f"(attempt {attempt}/{TTS_MAX_RETRIES})"
                    )
                else:
                    log(f"video {video['id']} generating audio... (attempt {attempt}/{TTS_MAX_RETRIES})")
                audio_bytes = http_post_binary(TTS_INVOKE_URL, payload, timeout=TTS_REQUEST_TIMEOUT_SECONDS)
                chunk_audios.append(audio_bytes)
                break
            except Exception as exc:
                log(
                    f"video {video['id']} audio generation failed "
                    f"(chunk {idx}/{len(text_chunks)}) attempt {attempt}: {exc}"
                )
                if tts_mode == "fish-speech" and attempt >= 2:
                    maybe_restart_container(TTS_CONTAINER_NAME, "tts invoke errors")
                if tts_mode == "index-tts2" and attempt >= 2:
                    maybe_start_index_tts2_adapter("tts invoke errors")
                if attempt >= TTS_MAX_RETRIES:
                    raise RuntimeError(
                        f"video {video['id']} audio generation failed after retries "
                        f"(chunk {idx}/{len(text_chunks)}): {exc}"
                    ) from exc
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    audio_bytes = merge_wav_payloads(chunk_audios, int(video["id"]))

    filename = f"{speaker}.wav"
    (OUTPUT_ROOT / filename).write_bytes(audio_bytes)
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


def poll_until_done(video_id: int, code: str) -> tuple[bool, dict]:
    consecutive_query_errors = 0
    while True:
        time.sleep(POLL_SECONDS)
        try:
            res = http_get_json(F2F_QUERY_URL.format(code=code), timeout=30)
            consecutive_query_errors = 0
        except Exception as exc:
            consecutive_query_errors += 1
            log(f"video {video_id} query network error: {exc}")
            if consecutive_query_errors >= 10:
                maybe_restart_container(GEN_VIDEO_CONTAINER_NAME, "query errors")
                consecutive_query_errors = 0
            continue

        status_code = int(res.get("code", -1))
        data = res.get("data") or {}
        task_status = data.get("status")
        progress = int(data.get("progress") or 0)
        msg = str(data.get("msg") or res.get("msg") or "")

        if status_code in (9999, 10002, 10003):
            return False, {"msg": msg or f"backend code {status_code}"}

        if status_code == 10000 and task_status == 1:
            update_video(video_id, status="pending", message=msg, progress=progress)
            continue
        if status_code == 10000 and task_status == 2:
            return True, data
        if status_code == 10000 and task_status == 3:
            return False, {"msg": msg or "backend status=3"}


def run_one(video: sqlite3.Row) -> None:
    video_id = int(video["id"])
    model = fetch_model(int(video["model_id"]))
    voice_id = int(video["voice_id"] or model["voice_id"])
    voice = fetch_voice(voice_id)
    run_started_at_ms = int(time.time() * 1000)
    cleanup_other_runner_no_code_pending(video_id)

    update_video(
        video_id,
        status="pending",
        message="Preparing audio",
        file_path=None,
        progress=0,
        code=None,
        param=json.dumps({"_run_started_at_ms": run_started_at_ms, "_queue_owner": QUEUE_OWNER}, ensure_ascii=False),
    )

    audio_path = video["audio_path"]
    if audio_path:
        audio_name = str(audio_path)
    else:
        audio_name = make_audio_for_video(video, voice)

    if has_pending_with_code(exclude_video_id=video_id):
        update_video(
            video_id,
            status="waiting",
            message="已有任务在渲染，自动顺延重试",
            audio_path=audio_name,
            code=None,
            param=None,
            progress=0,
            file_path=None,
        )
        log(f"video {video_id} deferred before submit because another pending task is active")
        return

    update_video(video_id, status="pending", message="Submitting task", audio_path=audio_name)

    submit_result, submit_param = submit_video_task(audio_name, str(model["video_path"]))
    submit_code = _coerce_int(submit_result.get("code"))
    submit_msg = str(submit_result.get("msg") or submit_result)
    submit_param_with_meta = dict(submit_param)
    submit_param_with_meta["_submitted_at_ms"] = int(time.time() * 1000)
    submit_param_with_meta["_queue_owner"] = QUEUE_OWNER

    if submit_code != 10000:
        if is_busy_submit_response(submit_result) or is_retryable_failure_message(submit_msg):
            active_code = find_matching_active_backend_code(audio_name, str(model["video_path"]))
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
                    progress=0,
                    file_path=None,
                )
                log(f"video {video_id} adopted active backend code={active_code}")
                return

            cooldown = mark_backend_busy(f"submit rejected ({submit_msg})")
            update_video(
                video_id,
                status="waiting",
                message=f"后端忙碌，{cooldown}s后自动重试",
                audio_path=audio_name,
                code=None,
                param=None,
                progress=0,
                file_path=None,
            )
            log(f"video {video_id} submit busy -> deferred retry ({submit_msg})")
            return

        update_video(
            video_id,
            status="failed",
            message=submit_msg,
            audio_path=audio_name,
            code=str(submit_param["code"]),
            param=json.dumps(submit_param_with_meta, ensure_ascii=False),
            progress=0,
        )
        raise RuntimeError(f"video {video_id} submit failed: {submit_result}")

    clear_backend_busy_lock()
    update_video(
        video_id,
        status="pending",
        message=json.dumps(submit_result, ensure_ascii=False),
        audio_path=audio_name,
        code=str(submit_param["code"]),
        param=json.dumps(submit_param_with_meta, ensure_ascii=False),
        progress=0,
    )
    task_code = str(submit_param["code"])
    log(f"video {video_id} submitted, code={task_code}")

    ok, data = poll_until_done(video_id, task_code)
    if not ok:
        fail_msg = str(data.get("msg", "unknown error"))
        if is_retryable_failure_message(fail_msg):
            update_video(
                video_id,
                status="waiting",
                message="Auto-retry after transient backend failure",
                progress=0,
                code=None,
                param=None,
                file_path=None,
            )
            log(f"video {video_id} retryable backend failure -> waiting ({fail_msg})")
            return
        update_video(video_id, status="failed", message=fail_msg, progress=0)
        raise RuntimeError(f"video {video_id} failed: {data}")

    result = str(data.get("result") or "")
    msg = str(data.get("msg") or "Task finished")
    progress = int(data.get("progress") or 100)
    video_file = OUTPUT_ROOT / result.lstrip("/\\")
    duration = get_duration_seconds(video_file)
    delivered = deliver_video_copy(video_id, video["name"], task_code, video_file)
    if delivered:
        log(f"Delivered video {video_id} -> {delivered}")
    update_video(
        video_id,
        status="success",
        message=msg,
        file_path=result,
        progress=progress,
        duration=duration,
    )
    log(f"video {video_id} success -> {result}")


def maybe_recover_retryable_video_error(video_id: int, exc: Exception) -> bool:
    msg = str(exc or "")
    if not is_retryable_failure_message(msg):
        return False
    update_video(
        video_id,
        status="waiting",
        message="Auto-retry after transient runtime failure",
        progress=0,
        code=None,
        param=None,
        file_path=None,
    )
    log(f"video {video_id} runtime retryable error -> waiting ({msg})")
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
    sync_voice_reference_paths_for_mode()
    reset_stuck_rows()
    sync_pending_from_backend()
    sync_success_deliveries()
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
        if not maybe_recover_retryable_video_error(int(next_video["id"]), exc):
            raise
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
                if backend_busy_wait_seconds() > 0:
                    maybe_log_backend_busy_wait()
                elif not has_pending_with_code() and has_waiting():
                    next_video = fetch_next_waiting()
                    if next_video is not None:
                        try:
                            run_one(next_video)
                            changed = True
                        except Exception as exc:
                            if maybe_recover_retryable_video_error(int(next_video["id"]), exc):
                                changed = True
                            else:
                                raise
                if not changed:
                    time.sleep(DAEMON_IDLE_SECONDS)
            except Exception as exc:
                log(f"daemon loop error: {exc}")
                time.sleep(DAEMON_IDLE_SECONDS)
    finally:
        release_runner_lock()


if __name__ == "__main__":
    raise SystemExit(main())


