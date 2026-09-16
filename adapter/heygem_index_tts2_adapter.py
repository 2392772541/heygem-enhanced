from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from flask import Flask, Response, jsonify, request

import sys

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent

def _resolve_tts_engine_root() -> Path | None:
    env_root = os.getenv("HEYGEM_INDEXTTS2_ROOT")
    if env_root and Path(env_root).exists():
        return Path(env_root)
    candidates = [
        Path(r"D:\HeyGem\tts-story-f5"),
        Path(r"C:\HeyGem\tts-story-f5"),
        PROJECT_ROOT / "tts-story-f5",
        APP_ROOT / "tts-story-f5",
    ]
    for c in candidates:
        if (c / "src" / "engines").exists():
            return c
    return None

_tts_engine_root = _resolve_tts_engine_root()
if _tts_engine_root and str(_tts_engine_root) not in sys.path:
    sys.path.insert(0, str(_tts_engine_root))

try:
    from src.engines.base import VoiceAssignment
    from src.engines.index_tts_engine import (
        INDEX_TTS_AVAILABLE,
        INDEX_TTS_SAMPLE_RATE,
        INDEX_TTS_UNAVAILABLE_REASON,
        IndexTTSEngine,
    )
except ImportError as exc:
    VoiceAssignment = None
    INDEX_TTS_AVAILABLE = False
    INDEX_TTS_SAMPLE_RATE = 24000
    INDEX_TTS_UNAVAILABLE_REASON = f"IndexTTS2 engine import unavailable: {exc}. Set HEYGEM_INDEXTTS2_ROOT to engine root."
    IndexTTSEngine = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("heygem-index-tts2-adapter")

CONFIG_PATH = APP_ROOT / "config.json"

def _resolve_shared_voice_root() -> Path:
    override = os.getenv("HEYGEM_SHARED_VOICE_ROOT") or os.getenv("HEYGEM_VOICE_ROOT")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/voice/data"), Path("C:/duix_avatar_data/voice/data")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/voice/data")

SHARED_ROOT = _resolve_shared_voice_root()
ORIGIN_AUDIO_DIR = SHARED_ROOT / "origin_audio"

app = Flask(__name__)

_ENGINE: IndexTTSEngine | None = None
_ENGINE_LOCK = threading.Lock()
_GENERATE_LOCK = threading.Lock()


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _build_engine() -> IndexTTSEngine:
    if not INDEX_TTS_AVAILABLE:
        raise RuntimeError(INDEX_TTS_UNAVAILABLE_REASON)

    config = _load_config()
    return IndexTTSEngine(
        model_version=config.get("index_tts_model_version", "IndexTTS-2"),
        use_fp16=bool(config.get("index_tts_use_fp16", True)),
        use_cuda_kernel=bool(config.get("index_tts_use_cuda_kernel", False)),
        use_deepspeed=bool(config.get("index_tts_use_deepspeed", False)),
        use_torch_compile=bool(config.get("index_tts_use_torch_compile", False)),
        use_accel=bool(config.get("index_tts_use_accel", False)),
        num_beams=int(config.get("index_tts_num_beams", 3)),
        diffusion_steps=int(config.get("index_tts_diffusion_steps", 25)),
        temperature=float(config.get("index_tts_temperature", 0.8)),
        top_p=float(config.get("index_tts_top_p", 0.8)),
        top_k=int(config.get("index_tts_top_k", 30)),
        repetition_penalty=float(config.get("index_tts_repetition_penalty", 10.0)),
        max_mel_tokens=int(config.get("index_tts_max_mel_tokens", 1500)),
        max_text_tokens_per_segment=int(config.get("index_tts_max_text_tokens_per_segment", 120)),
        device=(config.get("index_tts_device") or None),
        default_prompt=(config.get("index_tts_default_prompt") or None),
    )


def _get_engine() -> IndexTTSEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            logger.info("Loading IndexTTS2 engine for HeyGem compatibility service")
            _ENGINE = _build_engine()
        return _ENGINE


def _normalize_path_string(value: str) -> str:
    return (value or "").strip().replace("\\", "/")


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
        raise FileNotFoundError("Reference audio path is empty.")

    candidates: list[Path] = []

    pieces = [piece.strip() for piece in normalized.split("|||") if piece.strip()]
    if not pieces:
        pieces = [normalized]

    for piece in pieces:
        candidate = Path(piece)
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.append((Path.cwd() / candidate).resolve())
            candidates.append((SHARED_ROOT / piece.lstrip("/")).resolve())

    for alt_name in _candidate_basenames(raw_path):
        candidates.append((ORIGIN_AUDIO_DIR / alt_name).resolve())

    for resolved in _unique_paths(candidates):
        if resolved.exists():
            return resolved

    checked = "\n".join(str(path) for path in _unique_paths(candidates))
    raise FileNotFoundError(f"Unable to resolve audio path from '{raw_path}'. Checked:\n{checked}")


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
        "ffmpeg",
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
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not target_path.exists():
        raise RuntimeError(
            "ffmpeg failed while preparing the voice prompt.\n"
            f"command: {' '.join(command)}\n"
            f"stderr:\n{result.stderr.strip()}"
        )
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


def _generate_chunked_audio(
    engine: IndexTTSEngine,
    prompt_path: Path,
    text: str,
    max_chars: int,
    generation_overrides: dict[str, Any] | None = None,
) -> np.ndarray:
    segments = _split_text(text, max_chars)
    if not segments:
        raise ValueError("Text is empty after normalization.")

    with tempfile.TemporaryDirectory(prefix="heygem-index-tts2-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        assignment = VoiceAssignment(audio_prompt_path=str(prompt_path))
        worker_chunks: list[dict[str, Any]] = []
        chunk_meta: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            output_path = tmp_root / f"chunk_{index:04d}.wav"
            worker_chunks.append(
                {
                    "text": segment,
                    "spk_audio_prompt": str(prompt_path),
                    "output_path": str(output_path),
                    "_order_index": index,
                }
            )
            chunk_meta.append(
                {
                    "speaker": "heygem",
                    "text": segment,
                    "segment_index": 0,
                    "chunk_index": index,
                    "output_path": str(output_path),
                    "assignment": assignment,
                    "_order_index": index,
                }
            )

        files = engine.generate_batch_prebuilt(
            worker_chunks=worker_chunks,
            chunk_meta=chunk_meta,
            group_by_speaker=True,
            generation_overrides=generation_overrides,
        )

        audio_parts: list[np.ndarray] = []
        for file_path in files:
            audio, _ = sf.read(file_path, dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            audio_parts.append(audio)

        if not audio_parts:
            raise RuntimeError("IndexTTS2 did not produce audio chunks.")

        return np.concatenate(audio_parts)


def _build_generation_profile(
    config: dict[str, Any],
    text: str,
    chunk_length: int,
) -> tuple[str, dict[str, Any]]:
    estimated_segments = len(_split_text(text, chunk_length))
    auto_fast = bool(config.get("index_tts_auto_fast_on_long_text", True))
    fast_threshold_chars = max(1, int(config.get("index_tts_fast_threshold_chars", 260)))
    fast_threshold_segments = max(2, int(config.get("index_tts_fast_threshold_segments", 3)))

    use_fast = auto_fast and (
        len(text) >= fast_threshold_chars or estimated_segments >= fast_threshold_segments
    )

    if use_fast:
        return (
            "fast",
            {
                "num_beams": int(config.get("index_tts_fast_num_beams", 1)),
                "diffusion_steps": int(config.get("index_tts_fast_diffusion_steps", 25)),
                "max_text_tokens_per_segment": int(
                    config.get("index_tts_fast_max_text_tokens_per_segment", 120)
                ),
            },
        )

    return (
        "quality",
        {
            "num_beams": int(config.get("index_tts_num_beams", 3)),
            "diffusion_steps": int(config.get("index_tts_diffusion_steps", 25)),
            "max_text_tokens_per_segment": int(
                config.get("index_tts_max_text_tokens_per_segment", 120)
            ),
        },
    )


@app.get("/healthz")
def healthz() -> Response:
    return jsonify(
        {
            "status": "ok",
            "backend": "index-tts2",
            "engine_state": "loaded" if _ENGINE is not None else "cold",
            "index_tts_available": INDEX_TTS_AVAILABLE,
            "shared_root": str(SHARED_ROOT),
        }
    )


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
    try:
        config = _load_config()
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"code": 1, "msg": "text is required"}), 400

        reference_audio = payload.get("reference_audio") or ""
        prompt_path = _resolve_audio_path(reference_audio)
        chunk_length = int(payload.get("chunk_length") or config.get("index_tts_chunk_size") or 180)
        profile_name, generation_overrides = _build_generation_profile(config, text, chunk_length)

        with _GENERATE_LOCK:
            engine = _get_engine()
            logger.info(
                "Generating audio with IndexTTS2 using prompt %s (%s chars, chunk_length=%s, profile=%s, beams=%s, steps=%s)",
                prompt_path,
                len(text),
                chunk_length,
                profile_name,
                generation_overrides.get("num_beams"),
                generation_overrides.get("diffusion_steps"),
            )
            audio = _generate_chunked_audio(
                engine=engine,
                prompt_path=prompt_path,
                text=text,
                max_chars=chunk_length,
                generation_overrides=generation_overrides,
            )

        buffer = io.BytesIO()
        sf.write(buffer, audio, INDEX_TTS_SAMPLE_RATE, format="wav")
        return Response(buffer.getvalue(), mimetype="audio/wav")
    except Exception as exc:
        logger.exception("IndexTTS2 invoke failed")
        return jsonify({"code": 1, "msg": str(exc)}), 500


if __name__ == "__main__":
    logger.info("Starting HeyGem IndexTTS2 compatibility service on 127.0.0.1:18180")
    app.run(host="127.0.0.1", port=18180, debug=False, threaded=True)
