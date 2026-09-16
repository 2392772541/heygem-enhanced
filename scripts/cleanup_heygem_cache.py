from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
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


DB_PATH = _resolve_db_path()
OUTPUT_ROOT = _resolve_output_root()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

LOG_MAX = 2 * 1024 * 1024
LOG_KEEP = 1 * 1024 * 1024
RENDER_TEMP_RETENTION_SECONDS = 12 * 3600
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_name(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return Path(text).name if text else ""


def dir_size_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except Exception:
            pass
    return total


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    print("HeyGem 缓存与临时文件清理")
    print("-" * 40)
    print(f"1) 正在扫描孤儿中间 WAV 音频: {OUTPUT_ROOT}")
    print(f"2) 正在扫描超过 12 小时的渲染临时目录: {OUTPUT_ROOT}")
    print(f"3) 正在扫描并修剪超大日志文件: {LOG_DIR}")
    print()

    live_audio: set[str] = set()
    success_audio_rows: list[tuple[int, str]] = []

    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            for (audio_path,) in conn.execute(
                "SELECT audio_path FROM video WHERE status IN ('waiting','pending','failed')"
            ):
                text = normalize_name(audio_path)
                if text:
                    live_audio.add(text)

            for row_id, audio_path in conn.execute(
                "SELECT id, audio_path FROM video WHERE status='success' AND audio_path IS NOT NULL AND TRIM(audio_path) != ''"
            ):
                text = normalize_name(audio_path)
                if text:
                    success_audio_rows.append((int(row_id), text))
        finally:
            conn.close()

    removed_wavs: list[str] = []
    if OUTPUT_ROOT.exists():
        for wav in OUTPUT_ROOT.glob("*.wav"):
            if wav.name in live_audio:
                continue
            try:
                wav.unlink()
                removed_wavs.append(wav.name)
            except Exception:
                pass

    removed_names = set(removed_wavs)
    stale_success_ids = [
        row_id
        for row_id, audio_name in success_audio_rows
        if audio_name in removed_names or not (OUTPUT_ROOT / audio_name).exists()
    ]

    cleared_success_audio_path = 0
    if stale_success_ids and DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.executemany(
                "UPDATE video SET audio_path=NULL WHERE id=?",
                [(row_id,) for row_id in stale_success_ids],
            )
            conn.commit()
            cleared_success_audio_path = len(stale_success_ids)
        finally:
            conn.close()

    removed_render_dir_count = 0
    removed_render_dir_bytes = 0
    now = time.time()
    if OUTPUT_ROOT.exists():
        for child in OUTPUT_ROOT.iterdir():
            if not child.is_dir() or not UUID_RE.fullmatch(child.name):
                continue
            try:
                age = now - child.stat().st_mtime
            except Exception:
                continue
            if age < RENDER_TEMP_RETENTION_SECONDS:
                continue
            removed_render_dir_bytes += dir_size_bytes(child)
            try:
                shutil.rmtree(child)
                removed_render_dir_count += 1
            except Exception:
                pass

    trimmed_logs: list[str] = []
    scan_log_dirs = [LOG_DIR, PROJECT_ROOT]
    for target_dir in scan_log_dirs:
        if not target_dir.exists():
            continue
        for log_file in target_dir.glob("*.log"):
            try:
                if log_file.stat().st_size <= LOG_MAX:
                    continue
                data = log_file.read_bytes()[-LOG_KEEP:]
                nl = data.find(b"\n")
                if nl != -1 and nl + 1 < len(data):
                    data = data[nl + 1 :]
                log_file.write_bytes(data)
                trimmed_logs.append(log_file.name)
            except Exception:
                pass

    freed_gb = removed_render_dir_bytes / (1024 ** 3)
    print(f"清理完成结果:")
    print(f"  - 清除孤儿 WAV 音频: {len(removed_wavs)} 个")
    print(f"  - 重置已删除音频的完成记录关联: {cleared_success_audio_path} 条")
    print(f"  - 删除过期临时渲染目录: {removed_render_dir_count} 个 (释放空间约 {freed_gb:.3f} GB)")
    print(f"  - 修剪超大日志文件: {len(trimmed_logs)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
