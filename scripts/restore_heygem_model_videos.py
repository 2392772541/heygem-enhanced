from __future__ import annotations

import json
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


def _resolve_target_root() -> Path:
    override = os.getenv("HEYGEM_MODEL_ROOT_OVERRIDE") or os.getenv("HEYGEM_OUTPUT_ROOT")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data/face2face/temp"), Path("C:/duix_avatar_data/face2face/temp")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data/face2face/temp")


DB_PATH = _resolve_db_path()
TARGET_ROOT = _resolve_target_root()
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


def load_source_map() -> dict[int, str]:
    candidates = [
        SCRIPT_DIR / "model_sources.local.json",
        PROJECT_ROOT / "model_sources.local.json",
        SCRIPT_DIR / "model_sources.json",
        PROJECT_ROOT / "model_sources.json",
    ]
    env_cfg = os.getenv("HEYGEM_MODEL_SOURCES_JSON")
    if env_cfg:
        candidates.insert(0, Path(env_cfg))

    for cand in candidates:
        if cand.exists():
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    mappings = data.get("mappings", {})
                    print(f"[配置] 已从 {cand.name} 加载 {len(mappings)} 条模特映射。")
                    return {int(k): str(v) for k, v in mappings.items()}
            except Exception as exc:
                print(f"[警告] 读取配置文件 {cand} 失败: {exc}")

    print("[提示] 未找到 model_sources.local.json 配置文件。")
    print("      请复制 scripts/model_sources.example.json 为 scripts/model_sources.local.json 并配置源路径。")
    return {}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not DB_PATH.exists():
        print(f"[错误] 找不到数据库: {DB_PATH}")
        return 1

    model_source_map = load_source_map()
    if not model_source_map:
        return 0

    TARGET_ROOT.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, name, video_path FROM f2f_model ORDER BY id").fetchall()
    finally:
        conn.close()

    copied = []
    missing_source = []
    missing_mapping = []

    for row in rows:
        model_id = int(row["id"])
        target_name = Path(str(row["video_path"] or "").strip().replace("\\", "/")).name
        if not target_name:
            missing_mapping.append((model_id, str(row["name"] or ""), "<empty video_path>"))
            continue

        source_text = model_source_map.get(model_id)
        if not source_text:
            missing_mapping.append((model_id, str(row["name"] or ""), target_name))
            continue

        source = Path(source_text)
        if not source.exists():
            missing_source.append((model_id, str(row["name"] or ""), str(source)))
            continue

        target = TARGET_ROOT / target_name
        shutil.copy2(source, target)
        copied.append((model_id, str(row["name"] or ""), target.name, source.name))

    print(f"已恢复模特视频: {len(copied)}")
    for model_id, name, target_name, source_name in copied:
        print(f"  model_id={model_id} name={name} -> {target_name} (from {source_name})")

    if missing_source:
        print(f"缺失源文件: {len(missing_source)}")
        for item in missing_source:
            print(f"  model_id={item[0]} name={item[1]} source_missing={item[2]}")

    if missing_mapping:
        print(f"缺失映射: {len(missing_mapping)}")
        for item in missing_mapping:
            print(f"  model_id={item[0]} name={item[1]} target={item[2]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
