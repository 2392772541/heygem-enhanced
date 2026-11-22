from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Tuple

# 离线与静默环境变量设置，避免加载阶段卡在 HuggingFace 联网检查
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit_progress(event: Dict[str, Any]) -> None:
    payload = dict(event)
    payload.setdefault("ts", round(time.time(), 3))
    print(f"[PROGRESS] {json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _coerce_device(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return None if normalized in {"", "auto"} else normalized


def _normalize_model_version(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"indextts-2.5", "index tts-2.5", "indextts2.5", "index tts2.5", "2.5"}:
        return "IndexTTS-2.5"
    if normalized in {"", "indextts-2", "index tts-2", "indextts2", "index tts2", "2.0", "2"}:
        return "IndexTTS-2"
    return "IndexTTS-2"


def _required_paths(model_version: str) -> tuple[str, ...]:
    if model_version == "IndexTTS-2.5":
        return (
            "config.yaml",
            "codec.pth",
            "gpt.pth",
            "s2mel.pth",
            "feat1.pt",
            "feat2.pt",
            "wav2vec2bert_stats.pt",
            "multilingual_zh_ja_yue_char_del.tiktoken",
        )
    return (
        "config.yaml",
        "pinyin.vocab",
        "bpe.model",
        "gpt.pth",
        "s2mel.pth",
        "feat1.pt",
        "feat2.pt",
        "wav2vec2bert_stats.pt",
    )


def _validate_model_files(model_dir: Path, model_version: str, cfg_path: Path, need_qwen_emo: bool) -> None:
    if not cfg_path.exists():
        raise FileNotFoundError(f"未找到 IndexTTS 配置文件: {cfg_path}")
    missing = [rel for rel in _required_paths(model_version) if not (model_dir / rel).exists()]
    if need_qwen_emo and not (model_dir / "qwen0.6bemo4-merge").exists():
        missing.append("qwen0.6bemo4-merge")
    if missing:
        raise FileNotFoundError(f"{model_version} 权重目录不完整 ({model_dir})，缺少: {', '.join(missing)}")


def _load_engine(job: Dict[str, Any]) -> Tuple[Any, str]:
    model_version = _normalize_model_version(job.get("model_version"))
    model_dir = Path(job.get("model_dir") or ("checkpoints_2_5" if model_version == "IndexTTS-2.5" else "checkpoints"))
    cfg_path = Path(job.get("cfg_path") or (model_dir / "config.yaml"))
    emo_mode = str(job.get("emo_mode") or "same_as_speaker").strip().lower()
    need_qwen_emo = emo_mode == "emo_text" or bool(job.get("use_emo_text", False))

    _validate_model_files(model_dir, model_version, cfg_path, need_qwen_emo)

    device = _coerce_device(job.get("device"))
    use_fp16 = bool(job.get("use_fp16", True))
    use_bf16 = bool(job.get("use_bf16", use_fp16))
    use_cuda_kernel = bool(job.get("use_cuda_kernel", False))

    emit_progress(
        {
            "stage": "loading_model",
            "model_version": model_version,
            "use_qwen_emo": need_qwen_emo,
            "message": f"正在加载 {model_version} 语音模型...",
        }
    )

    if model_version == "IndexTTS-2.5":
        with contextlib.redirect_stdout(sys.stderr):
            from indextts.infer_v2_5 import IndexTTS2

        runtime_flags = {
            "use_deepspeed": bool(job.get("use_deepspeed", False)),
            "use_accel": bool(job.get("use_accel", False)),
            "use_torch_compile": bool(job.get("use_torch_compile", False)),
        }
        log(f"Loading IndexTTS-2.5: bf16={use_bf16}, qwen_emo={need_qwen_emo}, flags={runtime_flags}")
        with contextlib.redirect_stdout(sys.stderr):
            engine = IndexTTS2(
                cfg_path=str(cfg_path),
                model_dir=str(model_dir),
                use_bf16=use_bf16,
                device=device,
                use_cuda_kernel=use_cuda_kernel,
                use_deepspeed=runtime_flags["use_deepspeed"],
                use_accel=runtime_flags["use_accel"],
                use_torch_compile=runtime_flags["use_torch_compile"],
                use_qwen_emo=need_qwen_emo,
            )
        return engine, model_version

    with contextlib.redirect_stdout(sys.stderr):
        from indextts.infer_v2 import IndexTTS2

    attempts = [
        {
            "use_deepspeed": bool(job.get("use_deepspeed", False)),
            "use_accel": bool(job.get("use_accel", False)),
            "use_torch_compile": bool(job.get("use_torch_compile", False)),
        }
    ]
    safe_attempt = {"use_deepspeed": False, "use_accel": False, "use_torch_compile": False}
    if attempts[0] != safe_attempt:
        attempts.append(safe_attempt)

    last_error: Exception | None = None
    for index, runtime_flags in enumerate(attempts, start=1):
        try:
            log(f"Loading IndexTTS-2 (attempt {index}/{len(attempts)}): fp16={use_fp16}, qwen_emo={need_qwen_emo}")
            with contextlib.redirect_stdout(sys.stderr):
                engine = IndexTTS2(
                    cfg_path=str(cfg_path),
                    model_dir=str(model_dir),
                    use_fp16=use_fp16,
                    device=device,
                    use_cuda_kernel=use_cuda_kernel,
                    use_deepspeed=runtime_flags["use_deepspeed"],
                    use_accel=runtime_flags["use_accel"],
                    use_torch_compile=runtime_flags["use_torch_compile"],
                    use_qwen_emo=need_qwen_emo,
                )
            return engine, model_version
        except Exception as exc:
            last_error = exc
            log(f"Load attempt {index} failed for IndexTTS-2: {exc}")
    assert last_error is not None
    raise last_error


def _build_generation_kwargs(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "num_beams": max(1, min(8, int(job.get("num_beams", 3)))),
        "diffusion_steps": max(5, min(60, int(job.get("diffusion_steps", 25)))),
        "temperature": max(0.05, min(2.0, float(job.get("temperature", 0.8)))),
        "top_p": max(0.05, min(1.0, float(job.get("top_p", 0.8)))),
        "top_k": max(1, min(200, int(job.get("top_k", 30)))),
        "repetition_penalty": max(1.0, min(20.0, float(job.get("repetition_penalty", 10.0)))),
        "max_mel_tokens": max(200, min(4000, int(job.get("max_mel_tokens", 1500)))),
        "max_text_tokens_per_segment": max(20, min(300, int(job.get("max_text_tokens_per_segment", 120)))),
    }


def _normalize_emo_vector(vec: list[float], engine: Any | None = None) -> list[float]:
    if engine is not None and hasattr(engine, "normalize_emo_vec"):
        try:
            normalized = engine.normalize_emo_vec(list(vec), apply_bias=True)
            return [round(float(v), 4) for v in normalized]
        except Exception:
            pass
    # 兜底归一化：保证 8 维情感总权重不超过 0.8，避免 (1 - sum) 变负引发音质失真
    total = sum(vec)
    if total > 0.8:
        scale = 0.8 / total
        return [round(v * scale, 4) for v in vec]
    return [round(v, 4) for v in vec]


def _build_emotion_kwargs(job: Dict[str, Any], engine: Any | None = None) -> Dict[str, Any]:
    emo_mode = str(job.get("emo_mode") or "same_as_speaker").strip().lower()
    emo_alpha = max(0.0, min(1.0, float(job.get("emo_alpha", 0.65))))
    use_random = bool(job.get("use_random", False))
    interval_silence = max(0, min(2000, int(job.get("interval_silence", 200))))

    kwargs: Dict[str, Any] = {
        "emo_audio_prompt": None,
        "emo_alpha": emo_alpha,
        "emo_vector": None,
        "use_emo_text": False,
        "emo_text": None,
        "use_random": use_random,
        "interval_silence": interval_silence,
    }

    if emo_mode == "emo_audio":
        emo_audio = str(job.get("emo_audio_prompt") or "").strip().strip('"').strip("'")
        if not emo_audio or not Path(emo_audio).is_file():
            raise FileNotFoundError(f"情感参考音频文件不存在: {emo_audio or '(未指定)'}")
        kwargs["emo_audio_prompt"] = emo_audio
    elif emo_mode == "emo_vector":
        raw_vec = job.get("emo_vector")
        if isinstance(raw_vec, list) and len(raw_vec) == 8:
            vec = [max(0.0, min(1.2, float(v))) for v in raw_vec]
            if any(v > 0.0 for v in vec):
                kwargs["emo_vector"] = _normalize_emo_vector(vec, engine)
    elif emo_mode == "emo_text":
        emo_text = str(job.get("emo_text") or "").strip()
        kwargs["use_emo_text"] = True
        kwargs["emo_text"] = emo_text or None

    return kwargs


def _empty_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(description="HeyGem Studio IndexTTS2 Worker")
    parser.add_argument("--job-file", required=True, help="Path to JSON job file")
    args = parser.parse_args()

    try:
        with open(args.job_file, "r", encoding="utf-8-sig") as fh:
            job = json.load(fh)

        t0 = time.time()
        engine, model_version = _load_engine(job)
        load_cost = round(time.time() - t0, 2)

        generation_kwargs = _build_generation_kwargs(job)
        emotion_kwargs = _build_emotion_kwargs(job, engine)
        chunks = job.get("chunks") or []
        total = len(chunks)

        emit_progress(
            {
                "stage": "model_ready",
                "model_version": model_version,
                "load_seconds": load_cost,
                "chunk_current": 0,
                "chunk_total": total,
                "message": f"{model_version} 模型就绪（耗时 {load_cost}s），开始分段合成（共 {total} 段）",
            }
        )

        files: list[str] = []
        duration_factor = max(0.5, min(2.0, float(job.get("duration_factor", 1.0))))
        text_normalization = bool(job.get("text_normalization", True))

        for index, chunk in enumerate(chunks, start=1):
            text = (chunk.get("text") or "").strip()
            prompt_value = (chunk.get("spk_audio_prompt") or "").strip()
            prompt_path = Path(prompt_value) if prompt_value else None
            output_path = Path(chunk["output_path"]).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)

            if not text:
                raise ValueError(f"第 {index} 段文本为空。")
            if prompt_path is None or not prompt_path.exists():
                raise FileNotFoundError(f"参考音色文件不存在: {prompt_value}")

            emit_progress(
                {
                    "stage": "synthesizing",
                    "chunk_current": index,
                    "chunk_total": total,
                    "text_preview": text[:40],
                    "message": f"正在合成第 {index}/{total} 段语音...",
                }
            )
            log(f"Generating chunk {index}/{total}: {output_path.name} ({len(text)} chars)")

            with contextlib.redirect_stdout(sys.stderr):
                if model_version == "IndexTTS-2.5":
                    language = str(chunk.get("lang") or job.get("lang") or "ZH").strip().upper()
                    if language not in {"ZH", "EN", "JA", "ES", "AR"}:
                        language = "ZH"
                    engine.infer(
                        spk_audio_prompt=str(prompt_path),
                        text=text,
                        lang=language,
                        output_path=str(output_path),
                        verbose=False,
                        duration_factor=duration_factor,
                        text_normalization=text_normalization,
                        **emotion_kwargs,
                        **generation_kwargs,
                    )
                else:
                    engine.infer(
                        spk_audio_prompt=str(prompt_path),
                        text=text,
                        output_path=str(output_path),
                        verbose=False,
                        **emotion_kwargs,
                        **generation_kwargs,
                    )

            if not output_path.exists() or output_path.stat().st_size <= 0:
                raise FileNotFoundError(f"IndexTTS 未产出有效音频文件: {output_path}")

            files.append(str(output_path))
            emit_progress(
                {
                    "stage": "chunk_done",
                    "chunk_current": index,
                    "chunk_total": total,
                    "message": f"已完成第 {index}/{total} 段语音合成",
                }
            )
            log(f"[CHUNK_DONE] {output_path}")
            _empty_cache()

        emit_progress(
            {
                "stage": "done",
                "chunk_current": total,
                "chunk_total": total,
                "message": f"全部 {total} 段语音合成完成",
            }
        )
        print(json.dumps({"success": True, "files": files, "model_version": model_version}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        detail = traceback.format_exc()
        log(detail.rstrip())
        emit_progress({"stage": "error", "message": str(exc)})
        print(
            json.dumps(
                {"success": False, "error": str(exc), "traceback": detail},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
