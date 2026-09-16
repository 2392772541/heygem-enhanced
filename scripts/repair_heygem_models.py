from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path


def _resolve_db_path() -> Path:
    override = os.getenv("HEYGEM_DB_OVERRIDE")
    if override:
        return Path(override)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "biz.db"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "biz.db"


def _resolve_model_root() -> Path:
    override = os.getenv("HEYGEM_MODEL_ROOT_OVERRIDE") or os.getenv("HEYGEM_OUTPUT_ROOT")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/face2face/temp"), Path("C:/duix_avatar_data/face2face/temp")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/face2face/temp")


def _resolve_voice_root() -> Path:
    override = os.getenv("HEYGEM_VOICE_ROOT_OVERRIDE") or os.getenv("HEYGEM_VOICE_ROOT")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/voice/data"), Path("C:/duix_avatar_data/voice/data")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/voice/data")


DB_PATH = _resolve_db_path()
MODEL_ROOT = _resolve_model_root()
VOICE_ROOT = _resolve_voice_root()


def normalize_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


def find_replacement_origin_audio(origin_rel_path: str) -> Path | None:
    origin_rel_path = normalize_path(origin_rel_path)
    if not origin_rel_path:
        return None

    origin_path = VOICE_ROOT / origin_rel_path
    stem = origin_path.stem
    suffix = origin_path.suffix or ".wav"
    candidates = [
        origin_path.parent / f"format_denoise_{stem}{suffix}",
        origin_path.parent / f"format_{stem}{suffix}",
        origin_path.parent / f"{stem}{suffix}",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not DB_PATH.exists():
        print(f"[错误] 找不到数据库: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        model_rows = conn.execute(
            """
            SELECT
              m.id,
              m.name,
              m.video_path,
              m.audio_path,
              m.voice_id,
              v.origin_audio_path
            FROM f2f_model m
            LEFT JOIN voice v ON v.id = m.voice_id
            ORDER BY m.id
            """
        ).fetchall()
    finally:
        conn.close()

    restored_audio = []
    missing_videos = []
    missing_audio = []

    for row in model_rows:
        model_video_name = Path(normalize_path(row["video_path"])).name
        if model_video_name and not (MODEL_ROOT / model_video_name).exists():
            missing_videos.append((int(row["id"]), str(row["name"] or ""), model_video_name))

        raw_audio_rel = normalize_path(row["audio_path"] or row["origin_audio_path"])
        if not raw_audio_rel:
            continue
        raw_audio_path = VOICE_ROOT / raw_audio_rel
        if raw_audio_path.exists():
            continue

        replacement = find_replacement_origin_audio(raw_audio_rel)
        if replacement is None:
            missing_audio.append((int(row["id"]), str(row["name"] or ""), raw_audio_rel))
            continue

        raw_audio_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(replacement, raw_audio_path)
        restored_audio.append((int(row["id"]), str(row["name"] or ""), raw_audio_path.name, replacement.name))

    print(f"模特总数: {len(model_rows)}")
    print(f"已恢复原始音频: {len(restored_audio)}")
    for item in restored_audio[:20]:
        print(f"  model_id={item[0]} name={item[1]} -> {item[2]} (from {item[3]})")

    print(f"缺失模特视频: {len(missing_videos)}")
    for item in missing_videos[:50]:
        print(f"  model_id={item[0]} name={item[1]} missing_video={item[2]}")

    if missing_audio:
        print(f"仍缺失原始音频: {len(missing_audio)}")
        for item in missing_audio[:50]:
            print(f"  model_id={item[0]} name={item[1]} missing_audio={item[2]}")

    if missing_videos:
        print("提示: 模特视频文件不在当前磁盘路径里，如需恢复请使用 restore_heygem_model_videos.py 配置源文件进行还原。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
