from __future__ import annotations

import os
import re
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
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RENDER_OUTPUT_RE = re.compile(r"^[0-9a-fA-F-]{36}-r\.mp4$", re.IGNORECASE)
INTERMEDIATE_AUDIO_RE = re.compile(r"^[0-9a-fA-F-]{36}\.wav$", re.IGNORECASE)


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
        except OSError:
            continue
    return total


def print_plan() -> None:
    print()
    print("即将执行以下操作:")
    print("1) 删除 video 表中的全部作品记录")
    print(f"2) 删除作品生成出来的成片与中间音频: {OUTPUT_ROOT}")
    print(f"3) 删除渲染产生的 UUID 临时目录: {OUTPUT_ROOT}")
    print("4) 自动保留模特源视频，不会误删 f2f_model 里的模特文件")
    print()


def get_protected_model_video_names(conn: sqlite3.Connection) -> set[str]:
    names: set[str] = set()
    try:
        rows = conn.execute("SELECT video_path FROM f2f_model").fetchall()
    except sqlite3.DatabaseError:
        return names

    for (video_path,) in rows:
        name = normalize_name(video_path)
        if name:
            names.add(name.lower())
    return names


def collect_video_rows(conn: sqlite3.Connection) -> list[tuple[object, object, object]]:
    return conn.execute("SELECT file_path, audio_path, code FROM video").fetchall()


def collect_targets(rows: list[tuple[object, object, object]]) -> set[Path]:
    targets: set[Path] = set()
    for file_path, audio_path, code in rows:
        file_name = normalize_name(file_path)
        if file_name:
            targets.add(OUTPUT_ROOT / file_name)

        audio_name = normalize_name(audio_path)
        if audio_name:
            targets.add(OUTPUT_ROOT / audio_name)

        code_text = str(code or "").strip()
        if code_text:
            targets.add(OUTPUT_ROOT / f"{code_text}-r.mp4")
    return targets


def remove_targets(targets: set[Path], protected_names: set[str]) -> tuple[int, int]:
    removed_files = 0
    removed_bytes = 0
    for fp in targets:
        try:
            if not fp.exists() or not fp.is_file():
                continue
            if fp.name.lower() in protected_names:
                continue
            removed_bytes += fp.stat().st_size
            fp.unlink()
            removed_files += 1
        except OSError:
            continue
    return removed_files, removed_bytes


def remove_orphan_render_outputs(protected_names: set[str]) -> tuple[int, int]:
    removed_files = 0
    removed_bytes = 0
    if not OUTPUT_ROOT.exists():
        return removed_files, removed_bytes

    for child in OUTPUT_ROOT.iterdir():
        if not child.is_file():
            continue
        lower_name = child.name.lower()
        if lower_name in protected_names:
            continue
        should_remove = RENDER_OUTPUT_RE.fullmatch(child.name) or INTERMEDIATE_AUDIO_RE.fullmatch(child.name)
        if not should_remove:
            continue
        try:
            removed_bytes += child.stat().st_size
            child.unlink()
            removed_files += 1
        except OSError:
            continue

    return removed_files, removed_bytes


def remove_render_temp_dirs() -> tuple[int, int]:
    removed_dirs = 0
    removed_bytes = 0
    if not OUTPUT_ROOT.exists():
        return removed_dirs, removed_bytes

    for child in OUTPUT_ROOT.iterdir():
        if not child.is_dir() or not UUID_RE.fullmatch(child.name):
            continue
        try:
            removed_bytes += dir_size_bytes(child)
            shutil.rmtree(child)
            removed_dirs += 1
        except OSError:
            continue
    return removed_dirs, removed_bytes


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not DB_PATH.exists():
        print(f"[错误] 找不到数据库: {DB_PATH}")
        return 1

    print_plan()
    confirm = input("输入 YES 后继续执行: ").strip()
    if confirm.upper() != "YES":
        print("已取消。")
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        protected_names = get_protected_model_video_names(conn)
        rows = collect_video_rows(conn)
        targets = collect_targets(rows)
        conn.execute("DELETE FROM video")
        conn.commit()
    finally:
        conn.close()

    removed_files, removed_file_bytes = remove_targets(targets, protected_names)
    extra_files, extra_file_bytes = remove_orphan_render_outputs(protected_names)
    removed_dirs, removed_dir_bytes = remove_render_temp_dirs()

    total_removed_files = removed_files + extra_files
    total_removed_bytes = removed_file_bytes + extra_file_bytes + removed_dir_bytes
    freed_gb = total_removed_bytes / (1024 ** 3)

    print(
        "成功: 已删除作品记录 {rows} 条, 已删除文件 {files} 个, 已删除临时目录 {dirs} 个, 释放空间约 {gb:.3f} GB".format(
            rows=len(rows),
            files=total_removed_files,
            dirs=removed_dirs,
            gb=freed_gb,
        )
    )
    print("已自动保留模特源视频文件。")
    print("[完成] 批量删除已执行完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
