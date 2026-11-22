from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from flask import Flask, Response, jsonify, request

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
CONFIG_PATH = APP_ROOT / "config.json"
STUDIO_WORKER_SCRIPT = APP_ROOT / "indextts2_studio_worker.py"

INDEX_TTS_SAMPLE_RATE = 22050
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EMO_VECTOR_LABELS = ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"]
SUPPORTED_LANGUAGES = ["ZH", "EN", "JA", "ES", "AR"]

DEFAULT_CONFIG: dict[str, Any] = {
    "index_tts_model_version": "IndexTTS-2",
    "index_tts_use_fp16": True,
    "index_tts_use_cuda_kernel": False,
    "index_tts_use_deepspeed": False,
    "index_tts_use_torch_compile": False,
    "index_tts_use_accel": False,
    "index_tts_num_beams": 3,
    "index_tts_diffusion_steps": 25,
    "index_tts_temperature": 0.8,
    "index_tts_top_p": 0.8,
    "index_tts_top_k": 30,
    "index_tts_repetition_penalty": 10.0,
    "index_tts_max_mel_tokens": 1500,
    "index_tts_max_text_tokens_per_segment": 120,
    "index_tts_device": "auto",
    "index_tts_default_prompt": "",
    "index_tts_chunk_size": 180,
    "index_tts_interval_silence": 200,
    "index_tts_speed": 1.0,
    "index_tts_lang": "ZH",
    "index_tts_text_normalization": True,
    "index_tts_emo_mode": "same_as_speaker",
    "index_tts_emo_alpha": 0.65,
    "index_tts_emo_vector": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "index_tts_emo_text": "",
    "index_tts_emo_audio_prompt": "",
    "index_tts_use_random": False,
    "index_tts_auto_fast_on_long_text": True,
    "index_tts_fast_threshold_chars": 450,
    "index_tts_fast_threshold_segments": 3,
    "index_tts_fast_num_beams": 1,
    "index_tts_fast_diffusion_steps": 12,
    "index_tts_fast_max_text_tokens_per_segment": 120,
}


def _resolve_tts_engine_root() -> Path | None:
    env_root = os.getenv("HEYGEM_INDEXTTS2_ROOT")
    if env_root and Path(env_root).exists():
        p = Path(env_root)
        if (p / "engines" / "index-tts").exists():
            return p / "engines" / "index-tts"
        if (p / "indextts").exists():
            return p
    candidates = [
        Path(r"D:\HeyGem\tts-story-f5\engines\index-tts"),
        Path(r"C:\HeyGem\tts-story-f5\engines\index-tts"),
        PROJECT_ROOT / "tts-story-f5" / "engines" / "index-tts",
        Path(r"D:\index-tts-official"),
    ]
    for cand in candidates:
        if cand.exists() and (cand / "indextts").exists():
            return cand
    return None


def _resolve_worker_python(engine_root: Path | None) -> Path | None:
    if engine_root is None:
        return None
    for cand in [
        engine_root / ".venv" / "Scripts" / "python.exe",
        engine_root / "venv" / "Scripts" / "python.exe",
        engine_root / ".venv" / "bin" / "python",
    ]:
        if cand.exists():
            return cand
    return None


def _resolve_shared_voice_root() -> Path:
    override = os.getenv("HEYGEM_SHARED_VOICE_ROOT") or os.getenv("HEYGEM_VOICE_ROOT")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/voice/data"), Path("C:/duix_avatar_data/voice/data")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/voice/data")


def _resolve_ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    cand = Path(r"C:\Program Files\Duix.Avatar\resources\app.asar.unpacked\resources\ffmpeg\win-amd64\bin\ffmpeg.exe")
    if cand.exists():
        return str(cand)
    return "ffmpeg"


ENGINE_ROOT = _resolve_tts_engine_root()
WORKER_PYTHON = _resolve_worker_python(ENGINE_ROOT)
SHARED_ROOT = _resolve_shared_voice_root()
ORIGIN_AUDIO_DIR = SHARED_ROOT / "origin_audio"
FFMPEG_EXE = _resolve_ffmpeg_exe()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("heygem-index-tts2-adapter")

app = Flask(__name__)

_GENERATE_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_PROC_LOCK = threading.Lock()
_ACTIVE_PROC: subprocess.Popen[str] | None = None
_SYNTH_ACTIVE = False
_SYNTH_GEN = 0
_CANCEL_EVENT = threading.Event()
WORKER_TIMEOUT_SECONDS = max(120, int(os.getenv("HEYGEM_TTS_WORKER_TIMEOUT", "1800")))

_STATUS: dict[str, Any] = {
    "state": "idle",
    "job_id": None,
    "model_version": None,
    "stage": "idle",
    "message": "空闲待命",
    "chunk_current": 0,
    "chunk_total": 0,
    "progress": 0,
    "started_at": None,
    "updated_at": time.time(),
    "last_error": None,
    "text_preview": "",
    "recent_logs": deque(maxlen=60),
}


def _update_status(**kwargs: Any) -> None:
    with _STATE_LOCK:
        for key, value in kwargs.items():
            if key == "append_log":
                _STATUS["recent_logs"].append(f"[{time.strftime('%H:%M:%S')}] {value}")
            else:
                _STATUS[key] = value
        _STATUS["updated_at"] = time.time()


def _snapshot_status() -> dict[str, Any]:
    with _STATE_LOCK:
        started_at = _STATUS["started_at"]
        elapsed = round(time.time() - started_at, 1) if (started_at and _STATUS["state"] not in ("idle", "error", "cancelled")) else 0.0
        return {
            "state": _STATUS["state"],
            "job_id": _STATUS["job_id"],
            "model_version": _STATUS["model_version"],
            "stage": _STATUS["stage"],
            "message": _STATUS["message"],
            "chunk_current": _STATUS["chunk_current"],
            "chunk_total": _STATUS["chunk_total"],
            "progress": _STATUS["progress"],
            "elapsed_seconds": elapsed,
            "updated_at": _STATUS["updated_at"],
            "last_error": _STATUS["last_error"],
            "text_preview": _STATUS["text_preview"],
            "recent_logs": list(_STATUS["recent_logs"]),
        }


def _detect_available_versions() -> list[str]:
    if ENGINE_ROOT is None:
        return []
    versions: list[str] = []
    if (ENGINE_ROOT / "checkpoints" / "gpt.pth").exists() and (ENGINE_ROOT / "checkpoints" / "config.yaml").exists():
        versions.append("IndexTTS-2")
    if (ENGINE_ROOT / "checkpoints_2_5" / "gpt.pth").exists() and (ENGINE_ROOT / "checkpoints_2_5" / "config.yaml").exists():
        versions.append("IndexTTS-2.5")
    return versions


def _engine_availability() -> tuple[bool, str, list[str]]:
    if ENGINE_ROOT is None:
        return False, "未找到 IndexTTS 引擎目录（可通过 HEYGEM_INDEXTTS2_ROOT 指定）", []
    if WORKER_PYTHON is None:
        return False, f"未找到 IndexTTS 虚拟环境 Python: {ENGINE_ROOT}\\.venv", []
    if not STUDIO_WORKER_SCRIPT.exists():
        return False, f"未找到 Worker 脚本: {STUDIO_WORKER_SCRIPT}", []
    versions = _detect_available_versions()
    if not versions:
        return False, f"未在 {ENGINE_ROOT} 检测到完整权重（checkpoints 或 checkpoints_2_5）", []
    return True, "", versions


def _load_config() -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                merged.update(raw)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", CONFIG_PATH, exc)
    return merged


def _save_config(patch: dict[str, Any]) -> dict[str, Any]:
    current = _load_config()
    for key, value in patch.items():
        if key in DEFAULT_CONFIG:
            current[key] = value
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(current, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)
    return current


def _normalize_path_string(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("音频路径参数必须为字符串")
    return value.strip().strip('"').strip("'").replace("\\", "/")


def _unique_paths(candidates: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _alternate_basename(name: str) -> list[str]:
    names = [name]
    if name.startswith("format_denoise_"):
        names.append(name.replace("format_denoise_", "format_", 1))
    elif name.startswith("format_"):
        names.append(name.replace("format_", "format_denoise_", 1))
    else:
        names.append(f"format_{name}")
        names.append(f"format_denoise_{name}")
    return names


def _partless_basename(name: str) -> str:
    path = Path(name)
    stem = re.sub(r"_part\d+$", "", path.stem, flags=re.IGNORECASE)
    return f"{stem}{path.suffix}"


def _candidate_basenames(raw_path: str) -> list[str]:
    pieces = [piece.strip() for piece in _normalize_path_string(raw_path).split("|||") if piece.strip()]
    if not pieces:
        pieces = [_normalize_path_string(raw_path)]
    names: list[str] = []
    for piece in pieces:
        basename = Path(piece).name
        if not basename:
            continue
        names.extend(_alternate_basename(basename))
        partless = _partless_basename(basename)
        if partless != basename:
            names.extend(_alternate_basename(partless))
    return names


def _resolve_audio_path(raw_path: str) -> Path:
    normalized = _normalize_path_string(raw_path)
    if not normalized:
        raise FileNotFoundError("参考音色音频路径为空。")

    candidates: list[Path] = []
    pieces = [piece.strip() for piece in normalized.split("|||") if piece.strip()]
    if not pieces:
        pieces = [normalized]

    for piece in pieces:
        mapped = piece
        if mapped.lower().startswith("/code/data/"):
            mapped = str(SHARED_ROOT / mapped[len("/code/data/"):])
        candidate = Path(mapped)
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.append((Path.cwd() / candidate).resolve())
            candidates.append((SHARED_ROOT / piece.lstrip("/")).resolve())

    for alt_name in _candidate_basenames(raw_path):
        candidates.append((ORIGIN_AUDIO_DIR / alt_name).resolve())

    for resolved in _unique_paths(candidates):
        if resolved.is_file():
            return resolved

    checked = "\n".join(str(path) for path in _unique_paths(candidates))
    raise FileNotFoundError(f"找不到参考音频 '{raw_path}'，已检查路径:\n{checked}")


def _timestamp_from_name(name: str) -> str:
    match = re.search(r"(\d{17})", name)
    if match:
        return match.group(1)
    now = time.localtime()
    millis = int((time.time() % 1) * 1000)
    return time.strftime("%Y%m%d%H%M%S", now) + f"{millis:03d}"


def _formatted_prompt_path(source_path: Path) -> Path:
    stamp = _timestamp_from_name(source_path.stem)
    ORIGIN_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return (ORIGIN_AUDIO_DIR / f"format_{stamp}.wav").resolve()


def _convert_prompt_to_wav(source_path: Path, target_path: Path) -> Path:
    if source_path.resolve() == target_path.resolve():
        return target_path

    command = [
        FFMPEG_EXE,
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(INDEX_TTS_SAMPLE_RATE),
        str(target_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or not target_path.exists():
        raise RuntimeError(f"ffmpeg 预处理参考音频失败:\n{result.stderr.strip()}")
    return target_path


def _prepare_prompt(raw_path: str) -> Path:
    source_path = _resolve_audio_path(raw_path)
    if source_path.suffix.lower() == ".wav" and source_path.name.startswith("format_"):
        return source_path.resolve()
    target_path = _formatted_prompt_path(source_path)
    return _convert_prompt_to_wav(source_path, target_path)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _split_oversized_piece(piece: str, max_chars: int) -> list[str]:
    text = piece.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    secondary = re.split(r"(?<=[，,、；;：:])", text)
    if len(secondary) > 1:
        chunks: list[str] = []
        current = ""
        for item in secondary:
            item = item.strip()
            if not item:
                continue
            candidate = f"{current}{item}".strip()
            if current and len(candidate) > max_chars:
                chunks.extend(_split_oversized_piece(current, max_chars))
                current = item
            else:
                current = candidate
        if current:
            chunks.extend(_split_oversized_piece(current, max_chars))
        return chunks

    hard_chunks: list[str] = []
    total = math.ceil(len(text) / max_chars)
    for index in range(total):
        start = index * max_chars
        hard_chunks.append(text[start : start + max_chars])
    return hard_chunks


def _split_text(text: str, max_chars: int) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    max_chars = max(40, int(max_chars))
    primary = re.split(r"(?<=[。！？!?；;\n])", normalized)
    chunks: list[str] = []
    current = ""

    for item in primary:
        item = item.strip()
        if not item:
            continue

        if len(item) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_oversized_piece(item, max_chars))
            continue

        candidate = f"{current}{item}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = item
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks or [normalized]


def _resolve_effective_params(config: dict[str, Any], payload: dict[str, Any], text: str) -> dict[str, Any]:
    """合并默认配置与单次请求覆盖参数（支持前端独立调参及 AI API 覆盖）。"""
    tts_params = payload.get("tts_params")
    if not isinstance(tts_params, dict):
        tts_params = {}

    def pick(param_key: str, cfg_key: str, default: Any) -> Any:
        if param_key in tts_params and tts_params[param_key] is not None:
            return tts_params[param_key]
        if param_key in payload and payload[param_key] is not None:
            return payload[param_key]
        if cfg_key in config and config[cfg_key] is not None:
            return config[cfg_key]
        return default

    model_version = str(pick("model_version", "index_tts_model_version", "IndexTTS-2")).strip()
    if model_version.lower() in {"indextts-2.5", "2.5", "indextts2.5"}:
        model_version = "IndexTTS-2.5"
    else:
        model_version = "IndexTTS-2"

    # 若指定版本权重缺失，自动回退到本机存在的版本
    avail = _detect_available_versions()
    if avail and model_version not in avail:
        model_version = avail[0]

    chunk_size = max(40, min(400, int(pick("chunk_size", "index_tts_chunk_size", 180))))
    # 兼容旧客户端传入过小的 chunk_length，仅当显式传 tts_params.chunk_size 才强覆盖
    if "chunk_size" not in tts_params and "chunk_length" in payload:
        try:
            raw_cl = int(payload.get("chunk_length") or 0)
            if raw_cl >= 120:
                chunk_size = max(40, min(400, raw_cl))
        except (TypeError, ValueError):
            pass

    segments = _split_text(text, chunk_size)
    auto_fast = bool(pick("auto_fast_on_long_text", "index_tts_auto_fast_on_long_text", True))
    fast_threshold_chars = max(1, int(config.get("index_tts_fast_threshold_chars", 450)))
    fast_threshold_segments = max(2, int(config.get("index_tts_fast_threshold_segments", 3)))

    explicit_beams = "num_beams" in tts_params and tts_params["num_beams"] is not None
    explicit_steps = "diffusion_steps" in tts_params and tts_params["diffusion_steps"] is not None

    use_fast = (
        auto_fast
        and not explicit_beams
        and not explicit_steps
        and (len(text) >= fast_threshold_chars or len(segments) >= fast_threshold_segments)
    )

    if use_fast:
        num_beams = int(config.get("index_tts_fast_num_beams", 1))
        diffusion_steps = int(config.get("index_tts_fast_diffusion_steps", 12))
        max_text_tokens = int(config.get("index_tts_fast_max_text_tokens_per_segment", 120))
        profile_name = "fast"
    else:
        num_beams = int(pick("num_beams", "index_tts_num_beams", 3))
        diffusion_steps = int(pick("diffusion_steps", "index_tts_diffusion_steps", 25))
        max_text_tokens = int(pick("max_text_tokens_per_segment", "index_tts_max_text_tokens_per_segment", 120))
        profile_name = "custom" if (explicit_beams or explicit_steps) else "quality"

    speed = max(0.5, min(2.0, float(pick("speed", "index_tts_speed", 1.0))))
    duration_factor = round(1.0 / speed, 4) if speed > 0 else 1.0
    if "duration_factor" in tts_params and tts_params["duration_factor"] is not None:
        duration_factor = max(0.5, min(2.0, float(tts_params["duration_factor"])))

    emo_mode = str(pick("emo_mode", "index_tts_emo_mode", "same_as_speaker")).strip().lower()
    if emo_mode not in {"same_as_speaker", "emo_vector", "emo_audio", "emo_text"}:
        emo_mode = "same_as_speaker"

    raw_vec = pick("emo_vector", "index_tts_emo_vector", [0.0] * 8)
    if isinstance(raw_vec, list) and len(raw_vec) == 8:
        emo_vector = [max(0.0, min(1.2, float(v))) for v in raw_vec]
    else:
        emo_vector = [0.0] * 8

    emo_audio_raw = str(pick("emo_audio_prompt", "index_tts_emo_audio_prompt", "") or "").strip().strip('"').strip("'")
    emo_audio_resolved = ""
    if emo_mode == "emo_audio":
        if not emo_audio_raw:
            raise FileNotFoundError("当前情感模式为「情感参考音频」，请先上传情感参考音或填写有效本地音频路径。")
        emo_audio_resolved = str(_resolve_audio_path(emo_audio_raw))

    return {
        "profile_name": profile_name,
        "model_version": model_version,
        "chunk_size": chunk_size,
        "segments": segments,
        "use_fp16": bool(pick("use_fp16", "index_tts_use_fp16", True)),
        "use_cuda_kernel": bool(pick("use_cuda_kernel", "index_tts_use_cuda_kernel", False)),
        "use_deepspeed": bool(pick("use_deepspeed", "index_tts_use_deepspeed", False)),
        "use_torch_compile": bool(pick("use_torch_compile", "index_tts_use_torch_compile", False)),
        "use_accel": bool(pick("use_accel", "index_tts_use_accel", False)),
        "device": str(pick("device", "index_tts_device", "auto") or "auto"),
        "num_beams": max(1, min(8, num_beams)),
        "diffusion_steps": max(5, min(60, diffusion_steps)),
        "temperature": max(0.05, min(2.0, float(pick("temperature", "index_tts_temperature", 0.8)))),
        "top_p": max(0.05, min(1.0, float(pick("top_p", "index_tts_top_p", 0.8)))),
        "top_k": max(1, min(200, int(pick("top_k", "index_tts_top_k", 30)))),
        "repetition_penalty": max(1.0, min(20.0, float(pick("repetition_penalty", "index_tts_repetition_penalty", 10.0)))),
        "max_mel_tokens": max(200, min(4000, int(pick("max_mel_tokens", "index_tts_max_mel_tokens", 1500)))),
        "max_text_tokens_per_segment": max(20, min(300, max_text_tokens)),
        "interval_silence": max(0, min(2000, int(pick("interval_silence", "index_tts_interval_silence", 200)))),
        "speed": speed,
        "duration_factor": duration_factor,
        "lang": str(pick("lang", "index_tts_lang", "ZH") or "ZH").upper(),
        "text_normalization": bool(pick("text_normalization", "index_tts_text_normalization", True)),
        "emo_mode": emo_mode,
        "emo_alpha": max(0.0, min(1.0, float(pick("emo_alpha", "index_tts_emo_alpha", 0.65)))),
        "emo_vector": emo_vector,
        "emo_text": str(pick("emo_text", "index_tts_emo_text", "") or "").strip(),
        "emo_audio_prompt": emo_audio_resolved,
        "use_random": bool(pick("use_random", "index_tts_use_random", False)),
    }


def _apply_speed_atempo(audio: np.ndarray, sample_rate: int, speed: float) -> np.ndarray:
    """对 IndexTTS-2.0 输出应用高质量 ffmpeg atempo 调速。"""
    if abs(speed - 1.0) <= 0.02:
        return audio

    # ffmpeg atempo 单级支持 0.5~2.0
    clamped = max(0.5, min(2.0, speed))
    with tempfile.TemporaryDirectory(prefix="heygem-atempo-") as td:
        src = Path(td) / "in.wav"
        dst = Path(td) / "out.wav"
        sf.write(src, audio, sample_rate, format="WAV")
        cmd = [
            FFMPEG_EXE,
            "-y",
            "-i",
            str(src),
            "-filter:a",
            f"atempo={clamped:.4f}",
            "-acodec",
            "pcm_s16le",
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
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0 or not dst.exists():
            logger.warning("atempo failed (speed=%.2f), returning original audio: %s", speed, res.stderr[:200])
            return audio
        out_audio, _ = sf.read(dst, dtype="float32")
        if out_audio.ndim > 1:
            out_audio = out_audio.mean(axis=1)
        return out_audio


def _terminate_active_proc() -> bool:
    with _PROC_LOCK:
        proc = _ACTIVE_PROC
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Windows 下连同子进程树一起强杀，防止残留显存占用
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=8,
                    creationflags=CREATE_NO_WINDOW,
                )
            else:
                proc.kill()
        return True
    except Exception as exc:
        logger.warning("Failed to terminate active TTS worker: %s", exc)
        return False


def _run_studio_worker(prompt_path: Path, params: dict[str, Any], job_id: str) -> tuple[np.ndarray, int]:
    global _ACTIVE_PROC

    available, reason, _ = _engine_availability()
    if not available or ENGINE_ROOT is None or WORKER_PYTHON is None:
        raise RuntimeError(reason or "IndexTTS2 引擎不可用")

    segments: list[str] = params["segments"]
    if not segments:
        raise ValueError("待合成文本为空。")

    model_version = params["model_version"]
    checkpoint_dir = ENGINE_ROOT / ("checkpoints_2_5" if model_version == "IndexTTS-2.5" else "checkpoints")

    with _PROC_LOCK:
        if _SYNTH_ACTIVE and _CANCEL_EVENT.is_set():
            _update_status(state="cancelled", stage="cancelled", message="语音合成已被用户中断", progress=0, append_log="任务在模型启动前已中断")
            raise InterruptedError("语音合成已被用户中断")
        if not _SYNTH_ACTIVE:
            _CANCEL_EVENT.clear()
    _update_status(
        state="loading_model",
        job_id=job_id,
        model_version=model_version,
        stage="loading_model",
        message=f"正在启动 {model_version} 引擎（共 {len(segments)} 段）...",
        chunk_current=0,
        chunk_total=len(segments),
        progress=5,
        started_at=time.time(),
        last_error=None,
        text_preview=segments[0][:40],
        append_log=f"开始任务 {job_id}: {model_version}, {len(segments)} 段, beams={params['num_beams']}, steps={params['diffusion_steps']}, emo={params['emo_mode']}",
    )

    with tempfile.TemporaryDirectory(prefix="heygem-index-tts2-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        worker_chunks = [
            {
                "text": seg,
                "spk_audio_prompt": str(prompt_path),
                "output_path": str((tmp_root / f"chunk_{idx:04d}.wav").resolve()),
                "lang": params["lang"],
            }
            for idx, seg in enumerate(segments)
        ]

        job_payload = {
            "model_dir": str(checkpoint_dir),
            "cfg_path": str(checkpoint_dir / "config.yaml"),
            "model_version": model_version,
            "use_fp16": params["use_fp16"],
            "use_bf16": params["use_fp16"],
            "use_cuda_kernel": params["use_cuda_kernel"],
            "use_deepspeed": params["use_deepspeed"],
            "use_torch_compile": params["use_torch_compile"],
            "use_accel": params["use_accel"],
            "device": params["device"],
            "num_beams": params["num_beams"],
            "diffusion_steps": params["diffusion_steps"],
            "temperature": params["temperature"],
            "top_p": params["top_p"],
            "top_k": params["top_k"],
            "repetition_penalty": params["repetition_penalty"],
            "max_mel_tokens": params["max_mel_tokens"],
            "max_text_tokens_per_segment": params["max_text_tokens_per_segment"],
            "interval_silence": params["interval_silence"],
            "lang": params["lang"],
            "duration_factor": params["duration_factor"],
            "text_normalization": params["text_normalization"],
            "emo_mode": params["emo_mode"],
            "emo_alpha": params["emo_alpha"],
            "emo_vector": params["emo_vector"],
            "emo_text": params["emo_text"],
            "emo_audio_prompt": params["emo_audio_prompt"],
            "use_random": params["use_random"],
            "chunks": worker_chunks,
        }

        job_file = tmp_root / "job.json"
        job_file.write_text(json.dumps(job_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ENGINE_ROOT)
        env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")

        with _PROC_LOCK:
            if _CANCEL_EVENT.is_set():
                _update_status(state="cancelled", stage="cancelled", message="语音合成已被用户中断", progress=0, append_log="任务在启动前已中断")
                raise InterruptedError("语音合成已被用户中断")
            proc = subprocess.Popen(
                [str(WORKER_PYTHON), str(STUDIO_WORKER_SCRIPT), "--job-file", str(job_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ENGINE_ROOT),
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            _ACTIVE_PROC = proc

        stderr_lines: list[str] = []
        stdout_chunks: list[str] = []
        total_chunks = len(segments)

        def _stream_stderr() -> None:
            assert proc.stderr is not None
            for raw_line in proc.stderr:
                line = raw_line.rstrip()
                if not line:
                    continue
                stderr_lines.append(line)
                if line.startswith("[PROGRESS] "):
                    try:
                        evt = json.loads(line[len("[PROGRESS] ") :])
                        stage = str(evt.get("stage") or "")
                        cur = int(evt.get("chunk_current") or 0)
                        tot = int(evt.get("chunk_total") or total_chunks) or 1
                        msg = str(evt.get("message") or "")
                        if stage == "loading_model":
                            pct = 8
                            st = "loading_model"
                        elif stage == "model_ready":
                            pct = 18
                            st = "synthesizing"
                        elif stage == "synthesizing":
                            pct = min(92, 18 + int(((max(0, cur - 1)) / tot) * 74))
                            st = "synthesizing"
                        elif stage == "chunk_done":
                            pct = min(94, 18 + int((cur / tot) * 76))
                            st = "synthesizing"
                        elif stage == "done":
                            pct = 96
                            st = "postprocessing"
                        else:
                            with _STATE_LOCK:
                                pct = _STATUS.get("progress", 0)
                                st = _STATUS.get("state", "synthesizing")
                        _update_status(
                            state=st,
                            stage=stage,
                            chunk_current=cur,
                            chunk_total=tot,
                            progress=pct,
                            message=msg,
                            append_log=msg or line,
                        )
                    except Exception:
                        _update_status(append_log=line[:200])
                else:
                    logger.info("[worker] %s", line)
                    if not line.startswith("Loading"):
                        _update_status(append_log=line[:200])

        def _read_stdout() -> None:
            assert proc.stdout is not None
            stdout_chunks.append(proc.stdout.read())

        t_err = threading.Thread(target=_stream_stderr, daemon=True)
        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err.start()
        t_out.start()

        timed_out = False
        deadline = time.time() + WORKER_TIMEOUT_SECONDS
        try:
            while proc.poll() is None:
                if _CANCEL_EVENT.is_set():
                    _terminate_active_proc()
                    break
                if time.time() > deadline:
                    timed_out = True
                    _terminate_active_proc()
                    break
                time.sleep(0.25)
            t_err.join(timeout=5)
            t_out.join(timeout=5)
        finally:
            with _PROC_LOCK:
                if _ACTIVE_PROC is proc:
                    _ACTIVE_PROC = None

        if timed_out:
            raise RuntimeError(f"IndexTTS Worker 运行超过 {WORKER_TIMEOUT_SECONDS} 秒已自动终止，请尝试拆分更短文本或降低扩散步数。")

        if _CANCEL_EVENT.is_set():
            _update_status(state="cancelled", stage="cancelled", message="语音合成已被用户中断", progress=0, append_log="任务已手动中断")
            raise InterruptedError("语音合成已被用户中断")

        stdout_data = "".join(stdout_chunks).strip()
        if not stdout_data:
            err_tail = "\n".join(stderr_lines[-20:]) or "(无 stderr 输出)"
            raise RuntimeError(f"IndexTTS Worker 异常退出 (exit={proc.returncode}):\n{err_tail}")

        # stdout 可能带额外日志，优先解析最后一行 JSON
        parsed: dict[str, Any] | None = None
        for candidate_line in reversed(stdout_data.splitlines()):
            candidate_line = candidate_line.strip()
            if candidate_line.startswith("{") and candidate_line.endswith("}"):
                try:
                    parsed = json.loads(candidate_line)
                    break
                except Exception:
                    pass
        if parsed is None:
            parsed = json.loads(stdout_data)

        if not parsed.get("success"):
            raise RuntimeError(str(parsed.get("error") or "IndexTTS Worker 返回未知错误"))

        files = parsed.get("files") or []
        if not files:
            raise RuntimeError("IndexTTS 未生成任何分段音频。")

        _update_status(state="postprocessing", stage="merging", progress=97, message="正在拼接并优化输出音频...")

        audio_parts: list[np.ndarray] = []
        actual_sr = INDEX_TTS_SAMPLE_RATE
        silence_samples = 0

        for idx, fpath in enumerate(files):
            part, sr = sf.read(fpath, dtype="float32")
            actual_sr = int(sr or INDEX_TTS_SAMPLE_RATE)
            if part.ndim > 1:
                part = part.mean(axis=1)
            if idx == 0:
                silence_samples = int(actual_sr * (max(0, params["interval_silence"]) / 1000.0))
            elif silence_samples > 0:
                audio_parts.append(np.zeros(silence_samples, dtype="float32"))
            audio_parts.append(part)

        merged = np.concatenate(audio_parts)

        # IndexTTS-2.0 通过 ffmpeg atempo 支持语速调节；2.5 已由 duration_factor 原生处理
        if model_version == "IndexTTS-2" and abs(params["speed"] - 1.0) > 0.02:
            merged = _apply_speed_atempo(merged, actual_sr, params["speed"])

        _update_status(
            state="idle",
            stage="done",
            progress=100,
            message=f"语音合成完成（{len(merged) / actual_sr:.1f}秒）",
            append_log=f"任务 {job_id} 完成，音频时长 {len(merged) / actual_sr:.2f}s",
        )
        return merged, actual_sr


@app.get("/healthz")
def healthz() -> Response:
    available, reason, versions = _engine_availability()
    status = _snapshot_status()
    return jsonify(
        {
            "status": "ok" if available else "degraded",
            "backend": "index-tts2",
            "engine_state": status["state"],
            "index_tts_available": available,
            "unavailable_reason": reason,
            "available_versions": versions,
            "shared_root": str(SHARED_ROOT),
            "engine_root": str(ENGINE_ROOT) if ENGINE_ROOT else None,
        }
    )


@app.get("/v1/tts/capabilities")
def tts_capabilities() -> Response:
    available, reason, versions = _engine_availability()
    return jsonify(
        {
            "available": available,
            "unavailable_reason": reason,
            "versions": versions,
            "emo_vector_labels": EMO_VECTOR_LABELS,
            "languages": SUPPORTED_LANGUAGES,
            "default_config": DEFAULT_CONFIG,
            "current_config": _load_config(),
        }
    )


@app.get("/v1/tts/config")
def get_tts_config() -> Response:
    return jsonify({"code": 0, "config": _load_config()})


@app.put("/v1/tts/config")
def put_tts_config() -> Response:
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"code": 1, "msg": "JSON body must be an object"}), 400
        saved = _save_config(payload)
        return jsonify({"code": 0, "msg": "saved", "config": saved})
    except Exception as exc:
        logger.exception("Failed to save config")
        return jsonify({"code": 1, "msg": str(exc)}), 400


@app.get("/v1/tts/status")
def tts_status() -> Response:
    return jsonify({"code": 0, "data": _snapshot_status()})


@app.post("/v1/tts/cancel")
def tts_cancel() -> Response:
    with _PROC_LOCK:
        cancel_gen = _SYNTH_GEN
        in_flight = (
            _SYNTH_ACTIVE
            or _GENERATE_LOCK.locked()
            or (_ACTIVE_PROC is not None and _ACTIVE_PROC.poll() is None)
        )
        if in_flight:
            _CANCEL_EVENT.set()
        else:
            _CANCEL_EVENT.clear()

    killed = _terminate_active_proc() if in_flight else False
    if in_flight or killed:
        msg = "已中断当前语音合成任务" if killed else "已下发中断指令，正在停止准备中的语音任务"
        # 仅当本次目标合成仍处于活跃且尚未落入终态时，才标记 cancelling，防止竞态倒退覆写 cancelled/idle
        with _PROC_LOCK:
            still_same_flight = (
                _SYNTH_GEN == cancel_gen
                and (_SYNTH_ACTIVE or _GENERATE_LOCK.locked() or (_ACTIVE_PROC is not None and _ACTIVE_PROC.poll() is None))
            )
            if still_same_flight:
                with _STATE_LOCK:
                    cur_state = str(_STATUS.get("state") or "")
                if cur_state not in ("cancelled", "idle", "error"):
                    _update_status(
                        state="cancelling",
                        stage="cancelling",
                        message=msg,
                        append_log=msg,
                    )
        return jsonify({"code": 0, "cancelled": True, "killed_process": killed, "msg": msg})

    _update_status(state="idle", message="当前无运行中的语音任务")
    return jsonify({"code": 0, "cancelled": False, "killed_process": False, "msg": "当前没有正在执行的语音任务"})


@app.post("/v1/preprocess_and_tran")
def preprocess_and_tran() -> Response:
    try:
        payload = request.get_json(silent=True) or {}
        reference_audio = payload.get("reference_audio") or ""
        prepared_path = _prepare_prompt(reference_audio)
        response = {
            "code": 0,
            "msg": "success",
            "asr_format_audio_url": str(prepared_path).replace("\\", "/"),
            "reference_audio_text": "",
        }
        logger.info("Prepared voice prompt: %s -> %s", reference_audio, prepared_path)
        return jsonify(response)
    except Exception as exc:
        logger.exception("Failed to preprocess voice prompt")
        return jsonify({"code": 1, "msg": str(exc)}), 400


@app.post("/v1/invoke")
def invoke() -> Response:
    global _SYNTH_ACTIVE, _SYNTH_GEN
    job_id = uuid.uuid4().hex[:8]
    # 阶段 1：在获取 _GENERATE_LOCK 前完成参数与文件路径校验，失败直接返回 400，绝不覆盖正在合成中的任务状态
    try:
        config = _load_config()
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"code": 1, "msg": "请求体必须为 JSON 对象"}), 400
        raw_text = payload.get("text")
        if raw_text is not None and not isinstance(raw_text, str):
            return jsonify({"code": 1, "msg": "待合成文本 text 必须为字符串"}), 400
        text = (raw_text or "").strip()
        if not text:
            return jsonify({"code": 1, "msg": "待合成文本 text 不能为空"}), 400

        reference_audio = payload.get("reference_audio") or config.get("index_tts_default_prompt") or ""
        prompt_path = _resolve_audio_path(reference_audio)
        params = _resolve_effective_params(config, payload, text)
    except (FileNotFoundError, ValueError, TypeError) as exc:
        logger.warning("IndexTTS2 pre-validation rejected (job=%s): %s", job_id, exc)
        return jsonify({"code": 1, "msg": str(exc)}), 400
    except Exception as exc:
        logger.exception("IndexTTS2 pre-validation failed")
        return jsonify({"code": 1, "msg": str(exc)}), 500

    # 阶段 2：持锁执行实际合成，保证全局 _STATUS 只由当前持锁任务更新
    try:
        with _GENERATE_LOCK:
            with _PROC_LOCK:
                _CANCEL_EVENT.clear()
                _SYNTH_GEN += 1
                _SYNTH_ACTIVE = True
            try:
                logger.info(
                    "Synthesizing job=%s prompt=%s chars=%d segs=%d model=%s profile=%s beams=%d steps=%d emo=%s speed=%.2f",
                    job_id,
                    prompt_path.name,
                    len(text),
                    len(params["segments"]),
                    params["model_version"],
                    params["profile_name"],
                    params["num_beams"],
                    params["diffusion_steps"],
                    params["emo_mode"],
                    params["speed"],
                )
                audio, sample_rate = _run_studio_worker(prompt_path, params, job_id)
            except InterruptedError:
                raise
            except (FileNotFoundError, ValueError, TypeError) as exc:
                _update_status(state="error", stage="error", message=str(exc), last_error=str(exc), append_log=f"参数/文件错误: {exc}")
                raise
            except Exception as exc:
                _update_status(state="error", stage="error", message=str(exc), last_error=str(exc), append_log=f"错误: {exc}")
                raise
            finally:
                with _PROC_LOCK:
                    _SYNTH_ACTIVE = False
                    _CANCEL_EVENT.clear()

        buffer = io.BytesIO()
        sf.write(buffer, audio, sample_rate, format="WAV")
        resp = Response(buffer.getvalue(), mimetype="audio/wav")
        resp.headers["X-TTS-Job-Id"] = job_id
        resp.headers["X-TTS-Model-Version"] = params["model_version"]
        resp.headers["X-TTS-Duration-Sec"] = f"{len(audio) / sample_rate:.3f}"
        return resp
    except InterruptedError as exc:
        return jsonify({"code": 499, "cancelled": True, "msg": str(exc)}), 499
    except (FileNotFoundError, ValueError, TypeError) as exc:
        return jsonify({"code": 1, "msg": str(exc)}), 400
    except Exception as exc:
        logger.exception("IndexTTS2 invoke failed")
        return jsonify({"code": 1, "msg": str(exc)}), 500


if __name__ == "__main__":
    host = os.getenv("HEYGEM_TTS_HOST", "127.0.0.1")
    port = int(os.getenv("HEYGEM_TTS_PORT", "18180"))
    logger.info("Starting HeyGem Studio IndexTTS2 adapter on %s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
