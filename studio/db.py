from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resolve_db_path() -> Path:
    override = os.getenv("HEYGEM_DB_OVERRIDE")
    if override:
        return Path(override)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "biz.db"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "biz.db"


def resolve_duix_data_dir() -> Path:
    override = os.getenv("DUIX_DATA_DIR")
    if override:
        return Path(override)
    for cand in [Path("D:/duix_avatar_data"), Path("C:/duix_avatar_data"), Path("E:/duix_avatar_data")]:
        if cand.exists():
            return cand
    return Path("D:/duix_avatar_data")


def resolve_output_root() -> Path:
    override = os.getenv("HEYGEM_OUTPUT_ROOT") or os.getenv("HEYGEM_OUT_OVERRIDE")
    if override:
        return Path(override)
    return resolve_duix_data_dir() / "face2face" / "temp"


def resolve_voice_root() -> Path:
    override = os.getenv("HEYGEM_VOICE_ROOT") or os.getenv("HEYGEM_SHARED_VOICE_ROOT")
    if override:
        return Path(override)
    return resolve_duix_data_dir() / "voice" / "data"


def resolve_delivery_root() -> Path:
    override = os.getenv("HEYGEM_DELIVERY_ROOT")
    if override:
        return Path(override)
    for cand in [Path("E:/数字人视频文件"), Path("D:/数字人视频文件")]:
        if cand.exists() or cand.parent.exists():
            return cand
    return Path.home() / "Desktop" / "数字人视频文件"


def resolve_studio_assets_root() -> Path:
    override = os.getenv("HEYGEM_STUDIO_ASSETS_ROOT")
    if override:
        root = Path(override)
    else:
        root = resolve_duix_data_dir() / "studio_assets"
    (root / "audio").mkdir(parents=True, exist_ok=True)
    (root / "emo_audio").mkdir(parents=True, exist_ok=True)
    return root


def resolve_pause_file() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "heygem_queue_runner.paused"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "heygem_queue_runner.paused"


def resolve_mode_file() -> Path:
    override = os.getenv("HEYGEM_MODE_FILE")
    if override:
        return Path(override)
    return PROJECT_ROOT / "tts-mode.txt"


def resolve_ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    cand = Path(r"C:\Program Files\Duix.Avatar\resources\app.asar.unpacked\resources\ffmpeg\win-amd64\bin\ffmpeg.exe")
    return str(cand) if cand.exists() else "ffmpeg"


def resolve_ffprobe_exe() -> str:
    override = os.getenv("HEYGEM_FFPROBE_EXE")
    if override and Path(override).exists():
        return override
    found = shutil.which("ffprobe")
    if found:
        return found
    cand = Path(r"C:\Program Files\Duix.Avatar\resources\app.asar.unpacked\resources\ffmpeg\win-amd64\bin\ffprobe.exe")
    return str(cand) if cand.exists() else "ffprobe"


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
    except sqlite3.OperationalError:
        pass
    return conn


DEFAULT_TTS_PRESETS = [
    (
        "标准口播（高清晰）",
        "适合常规知识讲解、产品介绍，音质饱满、咬字清晰",
        {
            "model_version": "IndexTTS-2",
            "num_beams": 3,
            "diffusion_steps": 25,
            "temperature": 0.8,
            "top_p": 0.8,
            "top_k": 30,
            "repetition_penalty": 10.0,
            "chunk_size": 180,
            "interval_silence": 200,
            "speed": 1.0,
            "emo_mode": "same_as_speaker",
            "emo_alpha": 0.65,
            "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0],
            "auto_fast_on_long_text": False,
        },
        1,
    ),
    (
        "极速批量（长文案提速）",
        "长口播批量生产专用，大幅缩短语音合成耗时",
        {
            "model_version": "IndexTTS-2",
            "num_beams": 1,
            "diffusion_steps": 12,
            "temperature": 0.75,
            "top_p": 0.8,
            "top_k": 30,
            "repetition_penalty": 10.0,
            "chunk_size": 200,
            "interval_silence": 180,
            "speed": 1.0,
            "emo_mode": "same_as_speaker",
            "emo_alpha": 0.65,
            "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0],
            "auto_fast_on_long_text": True,
        },
        0,
    ),
    (
        "热情带货（轻快昂扬）",
        "语速略快、带欢快惊喜情绪，适合短视频开场与带货口播",
        {
            "model_version": "IndexTTS-2",
            "num_beams": 2,
            "diffusion_steps": 20,
            "temperature": 0.85,
            "top_p": 0.85,
            "top_k": 35,
            "repetition_penalty": 10.0,
            "chunk_size": 160,
            "interval_silence": 150,
            "speed": 1.06,
            "emo_mode": "emo_vector",
            "emo_alpha": 0.7,
            "emo_vector": [0.55, 0, 0, 0, 0, 0, 0.3, 0],
            "auto_fast_on_long_text": False,
        },
        0,
    ),
    (
        "沉稳专业（新闻科普）",
        "情绪克制平稳，适合政策解读、技术评测、企业宣传",
        {
            "model_version": "IndexTTS-2",
            "num_beams": 3,
            "diffusion_steps": 25,
            "temperature": 0.7,
            "top_p": 0.75,
            "top_k": 25,
            "repetition_penalty": 10.5,
            "chunk_size": 180,
            "interval_silence": 240,
            "speed": 0.98,
            "emo_mode": "emo_vector",
            "emo_alpha": 0.6,
            "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0.75],
            "auto_fast_on_long_text": False,
        },
        0,
    ),
]


def init_studio_schema(db_path: Path | None = None) -> None:
    """增量创建 Studio 所需扩展表，完全兼容原版 Duix.Avatar 表结构。"""
    conn = get_conn(db_path)
    try:
        # 兼容全新的空数据库（例如单元测试临时库）
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS context (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                key TEXT,
                val TEXT
            );
            CREATE TABLE IF NOT EXISTS model_group (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                sort_order INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                origin_audio_path TEXT,
                lang TEXT,
                asr_format_audio_url TEXT,
                reference_audio_text TEXT,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS f2f_model (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                video_path TEXT,
                audio_path TEXT,
                voice_id INTEGER,
                created_at INTEGER,
                group_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS video (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT,
                status TEXT,
                message TEXT,
                model_id INTEGER,
                audio_path TEXT,
                param TEXT,
                code TEXT,
                created_at INTEGER,
                progress INTEGER DEFAULT 0,
                name TEXT,
                duration REAL,
                text_content TEXT,
                voice_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS cancelled_video_tasks (
                video_id INTEGER PRIMARY KEY,
                code TEXT,
                cancelled_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS studio_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                color TEXT DEFAULT '#3b82f6',
                sort_order INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS studio_job_meta (
                video_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                phase TEXT DEFAULT 'queued',
                stage_label TEXT DEFAULT '',
                progress_float REAL DEFAULT 0.0,
                rendered_frames INTEGER DEFAULT 0,
                total_frames INTEGER DEFAULT 0,
                render_fps REAL DEFAULT 0.0,
                eta_seconds INTEGER DEFAULT -1,
                heartbeat_at REAL DEFAULT 0.0,
                tts_params_json TEXT DEFAULT '{}',
                source_audio_path TEXT DEFAULT '',
                delivered_path TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                error_code TEXT DEFAULT '',
                error_detail TEXT DEFAULT '',
                webhook_url TEXT DEFAULT '',
                webhook_notified INTEGER DEFAULT 0,
                recent_logs_json TEXT DEFAULT '[]',
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS studio_tts_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT DEFAULT '',
                params_json TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS studio_audio_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category_id INTEGER,
                file_path TEXT NOT NULL,
                duration REAL DEFAULT 0.0,
                source_type TEXT DEFAULT 'tts',
                voice_id INTEGER,
                text_content TEXT DEFAULT '',
                tts_params_json TEXT DEFAULT '{}',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS studio_cancel_requests (
                video_id INTEGER PRIMARY KEY,
                requested_at INTEGER NOT NULL,
                reason TEXT DEFAULT 'user_cancelled'
            );
            """
        )

        # 如果老库 f2f_model 没有 group_id，平滑补充
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(f2f_model)").fetchall()}
        if "group_id" not in cols:
            conn.execute("ALTER TABLE f2f_model ADD COLUMN group_id INTEGER")

        # 首次初始化默认预设（仅当表为空）
        cnt = conn.execute("SELECT COUNT(*) FROM studio_tts_presets").fetchone()[0]
        if cnt == 0:
            now_ms = int(time.time() * 1000)
            for name, desc, params, is_default in DEFAULT_TTS_PRESETS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO studio_tts_presets (name, description, params_json, is_default, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, desc, json.dumps(params, ensure_ascii=False), is_default, now_ms, now_ms),
                )

        # 首次初始化常用作品分类（仅当表为空）
        cat_cnt = conn.execute("SELECT COUNT(*) FROM studio_categories").fetchone()[0]
        if cat_cnt == 0:
            now_ms = int(time.time() * 1000)
            for idx, (cname, color) in enumerate(
                [("日常口播", "#3b82f6"), ("产品推广", "#10b981"), ("测试样片", "#f59e0b")]
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO studio_categories (name, color, sort_order, created_at) VALUES (?, ?, ?, ?)",
                    (cname, color, idx, now_ms),
                )

        conn.commit()
    finally:
        conn.close()


def upsert_job_meta(video_id: int, db_path: Path | None = None, **fields: Any) -> None:
    if not fields:
        return
    now_ms = int(time.time() * 1000)
    fields["updated_at"] = now_ms
    conn = get_conn(db_path)
    try:
        exists = conn.execute("SELECT 1 FROM studio_job_meta WHERE video_id=?", (video_id,)).fetchone()
        if exists:
            cols = list(fields.keys())
            sql = f"UPDATE studio_job_meta SET {', '.join(f'{c}=?' for c in cols)} WHERE video_id=?"
            conn.execute(sql, [fields[c] for c in cols] + [video_id])
        else:
            cols = ["video_id"] + list(fields.keys())
            placeholders = ", ".join(["?"] * len(cols))
            sql = f"INSERT INTO studio_job_meta ({', '.join(cols)}) VALUES ({placeholders})"
            conn.execute(sql, [video_id] + [fields[c] for c in fields.keys()])
        conn.commit()
    finally:
        conn.close()


def probe_media_info(path: Path) -> dict[str, Any]:
    """用 ffprobe 获取音视频时长、分辨率、帧率与文件大小。"""
    info: dict[str, Any] = {
        "exists": False,
        "size_bytes": 0,
        "duration": None,
        "fps": None,
        "width": None,
        "height": None,
    }
    if not path.exists() or not path.is_file():
        return info
    info["exists"] = True
    try:
        info["size_bytes"] = path.stat().st_size
    except OSError:
        pass
    try:
        res = subprocess.run(
            [
                resolve_ffprobe_exe(),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            fmt = data.get("format") or {}
            if fmt.get("duration"):
                info["duration"] = round(float(fmt["duration"]), 3)
            for stream in data.get("streams") or []:
                if stream.get("codec_type") == "video":
                    info["width"] = stream.get("width")
                    info["height"] = stream.get("height")
                    rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
                    if "/" in rate:
                        num, den = rate.split("/", 1)
                        if float(den) > 0:
                            info["fps"] = round(float(num) / float(den), 3)
                    break
    except Exception:
        pass
    return info


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", str(name or "").strip()).strip(" .")
    return cleaned[:80] or fallback


def clean_local_path(raw: str | None) -> str:
    """清洗 Windows 资源管理器“复制文件地址”自带的首尾双引号/单引号。"""
    return str(raw or "").strip().strip('"').strip("'").strip()


def find_delivered_video(video_id: int, code: str | None, name: str | None) -> Path | None:
    """在交付目录中查找已归档的成片文件（支持带分类子目录或根目录）。"""
    delivery_root = resolve_delivery_root()
    if not delivery_root.exists():
        return None
    short_code = (str(code or "")[:8]).lower()
    prefix = f"{video_id:04d}_"
    id_prefix = f"{video_id}_"
    # 先扫描根目录和一级分类子目录（不做全盘深扫描）
    search_dirs = [delivery_root]
    try:
        for sub in delivery_root.iterdir():
            if sub.is_dir() and sub.name not in ("拍摄的视频",):
                search_dirs.append(sub)
    except OSError:
        pass

    fallback_hit: Path | None = None
    for folder in search_dirs:
        try:
            for item in folder.iterdir():
                if not item.is_file() or item.suffix.lower() != ".mp4":
                    continue
                fname = item.name
                if not (fname.startswith(prefix) or fname.startswith(id_prefix)):
                    continue
                if short_code and short_code in fname.lower():
                    return item
                if fallback_hit is None:
                    fallback_hit = item
        except OSError:
            pass
    return fallback_hit
