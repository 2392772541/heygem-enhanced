from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import system_ctrl
from .db import (
    CREATE_NO_WINDOW,
    clean_local_path,
    find_delivered_video,
    get_conn,
    init_studio_schema,
    probe_media_info,
    resolve_delivery_root,
    resolve_ffmpeg_exe,
    resolve_output_root,
    resolve_studio_assets_root,
    resolve_voice_root,
    sanitize_filename,
    upsert_job_meta,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STUDIO_API_KEY = os.getenv("HEYGEM_STUDIO_API_KEY", "").strip()


def _verify_api_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    if not STUDIO_API_KEY:
        return
    token = (x_api_key or "").strip()
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token != STUDIO_API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_studio_schema()
    resolve_studio_assets_root()
    yield


app = FastAPI(
    title="HeyGem Studio · 数字人 & IndexTTS2 统一中枢 API",
    version="2.0.0",
    description=(
        "本地数字人（Duix.Avatar）与 IndexTTS2 深度融合控制中心及自动化 AI 接口。\n\n"
        "支持单条/批量提交口播文案或本地音频生成数字人视频、全参数 IndexTTS2 试听与调参、"
        "实时帧级进度追踪、随时中断恢复、作品分类与成片下载。"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _proxy_tts_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    url = f"{system_ctrl.TTS_BASE_URL}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("msg") or body
        except Exception:
            msg = body or str(exc)
        raise HTTPException(status_code=exc.code if 400 <= exc.code < 600 else 502, detail=msg)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"IndexTTS2 语音服务 (18180) 暂不可用：{exc}")


def _convert_to_wav(src: Path, dst: Path, sample_rate: int = 22050) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_ffmpeg_exe(),
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
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
        timeout=120,
        creationflags=CREATE_NO_WINDOW,
    )
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size <= 0:
        raise RuntimeError(f"FFmpeg 音频转码失败：{res.stderr.strip()[:300]}")
    return dst


def _resolve_model_files(row: dict[str, Any]) -> dict[str, Any]:
    output_root = resolve_output_root()
    voice_root = resolve_voice_root()
    vp = str(row.get("video_path") or "").strip()
    ap = str(row.get("audio_path") or "").strip()

    video_abs = output_root / vp.lstrip("/\\") if vp else None
    audio_abs = None
    if ap:
        cand = Path(ap)
        audio_abs = cand if cand.is_absolute() else (voice_root / ap.lstrip("/\\"))

    video_exists = bool(video_abs and video_abs.is_file())
    audio_exists = bool(audio_abs and audio_abs.is_file())
    video_size_mb = round(video_abs.stat().st_size / (1024 * 1024), 2) if video_exists and video_abs else 0.0

    row["video_abs_path"] = str(video_abs) if video_abs else ""
    row["audio_abs_path"] = str(audio_abs) if audio_abs else ""
    row["video_exists"] = video_exists
    row["audio_exists"] = audio_exists
    row["video_size_mb"] = video_size_mb
    row["preview_url"] = f"/api/v1/media/model/{row['id']}" if video_exists else ""
    row["voice_preview_url"] = f"/api/v1/media/voice/{row['voice_id']}" if row.get("voice_id") and audio_exists else ""
    return row


def _enrich_job_row(row: dict[str, Any]) -> dict[str, Any]:
    vid = int(row["id"])
    output_root = resolve_output_root()
    code = str(row.get("code") or "").strip()
    file_path = str(row.get("file_path") or "").strip()

    temp_video_path: Path | None = None
    if file_path:
        cand = output_root / file_path.lstrip("/\\")
        if cand.is_file():
            temp_video_path = cand
    if temp_video_path is None and code:
        cand = output_root / f"{code}-r.mp4"
        if cand.is_file():
            temp_video_path = cand

    delivered_str = str(row.get("delivered_path") or "").strip()
    delivered_path: Path | None = Path(delivered_str) if delivered_str and Path(delivered_str).is_file() else None
    if delivered_path is None and row.get("status") == "success":
        delivered_path = find_delivered_video(vid, code, row.get("name"))
        if delivered_path:
            upsert_job_meta(vid, delivered_path=str(delivered_path))

    playable_path = delivered_path or temp_video_path
    size_mb = round(playable_path.stat().st_size / (1024 * 1024), 2) if playable_path else 0.0

    tts_params = {}
    try:
        tts_params = json.loads(row.get("tts_params_json") or "{}")
    except Exception:
        tts_params = {}

    recent_logs = []
    try:
        recent_logs = json.loads(row.get("recent_logs_json") or "[]")
    except Exception:
        recent_logs = []

    hb = float(row.get("heartbeat_at") or 0.0)
    hb_age = round(max(0.0, time.time() - hb), 1) if hb > 0 else None

    status = str(row.get("status") or "waiting")
    phase = str(row.get("phase") or "")
    if not phase:
        phase = {
            "waiting": "queued",
            "pending": "face2face_rendering",
            "success": "success",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(status, status)

    progress_float = float(row.get("progress_float") or 0.0)
    db_prog = float(row.get("progress") or 0)
    if status == "success":
        progress_float = 100.0
    elif progress_float <= 0 and db_prog > 0:
        progress_float = db_prog

    source_audio_path = str(row.get("source_audio_path") or "").strip()
    if not source_audio_path and row.get("audio_path"):
        cand_a = output_root / str(row["audio_path"]).lstrip("/\\")
        source_audio_path = str(cand_a)

    return {
        "id": vid,
        "name": row.get("name") or f"视频_{vid}",
        "status": status,
        "phase": phase,
        "stage_label": row.get("stage_label") or row.get("message") or "",
        "message": row.get("message") or "",
        "progress": int(round(progress_float)),
        "progress_float": round(progress_float, 1),
        "rendered_frames": int(row.get("rendered_frames") or 0),
        "total_frames": int(row.get("total_frames") or 0),
        "render_fps": float(row.get("render_fps") or 0.0),
        "eta_seconds": int(row.get("eta_seconds") if row.get("eta_seconds") is not None else -1),
        "heartbeat_at": hb,
        "heartbeat_age_seconds": hb_age,
        "model_id": row.get("model_id"),
        "model_name": row.get("model_name") or "",
        "voice_id": row.get("voice_id"),
        "category_id": row.get("category_id"),
        "category_name": row.get("category_name") or "未分类",
        "category_color": row.get("category_color") or "#64748b",
        "text_content": row.get("text_content") or "",
        "audio_path": row.get("audio_path") or "",
        "code": code,
        "duration": row.get("duration"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("meta_updated_at") or row.get("created_at"),
        "retry_count": int(row.get("retry_count") or 0),
        "max_retries": int(row.get("max_retries") or 3),
        "error_code": row.get("error_code") or "",
        "error_detail": row.get("error_detail") or "",
        "tts_params": tts_params,
        "recent_logs": recent_logs,
        "webhook_url": row.get("webhook_url") or "",
        "paths": {
            "delivered_path": str(delivered_path) if delivered_path else "",
            "temp_video_path": str(temp_video_path) if temp_video_path else "",
            "playable_path": str(playable_path) if playable_path else "",
            "source_audio_path": source_audio_path,
            "model_video_path": str(output_root / str(row.get("model_video_path") or "").lstrip("/\\")) if row.get("model_video_path") else "",
        },
        "file_exists": playable_path is not None,
        "file_size_mb": size_mb,
        "download_url": f"/api/v1/jobs/{vid}/download" if playable_path else "",
        "stream_url": f"/api/v1/media/video/{vid}" if playable_path else "",
    }


def _fetch_job_by_id(job_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT v.*,
                   fm.name AS model_name,
                   fm.video_path AS model_video_path,
                   m.category_id,
                   c.name AS category_name,
                   c.color AS category_color,
                   m.phase, m.stage_label, m.progress_float,
                   m.rendered_frames, m.total_frames, m.render_fps, m.eta_seconds,
                   m.heartbeat_at, m.tts_params_json, m.source_audio_path, m.delivered_path,
                   m.retry_count, m.max_retries, m.error_code, m.error_detail,
                   m.webhook_url, m.recent_logs_json, m.updated_at AS meta_updated_at
            FROM video v
            LEFT JOIN f2f_model fm ON fm.id = v.model_id
            LEFT JOIN studio_job_meta m ON m.video_id = v.id
            LEFT JOIN studio_categories c ON c.id = m.category_id
            WHERE v.id = ?
            """,
            (job_id,),
        ).fetchone()
        return _enrich_job_row(dict(row)) if row else None
    finally:
        conn.close()


# ==================== Pydantic Request Schemas ====================

class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    color: str = Field(default="#3b82f6", max_length=20)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    color: str | None = Field(default=None, max_length=20)


class ModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    group_id: int | None = None


class PresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class TtsSynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice_id: int | None = None
    reference_audio: str | None = None
    tts_params: dict[str, Any] = Field(default_factory=dict)
    preset_id: int | None = None
    name: str | None = None
    category_id: int | None = None


class JobCreateItem(BaseModel):
    model_id: int = Field(..., description="数字人模特 ID")
    name: str | None = Field(default=None, description="作品标题，留空自动生成")
    text: str | None = Field(default=None, description="口播文案（文本配音模式）")
    voice_id: int | None = Field(default=None, description="指定音色 ID，留空默认使用模特自带音色")
    tts_params: dict[str, Any] = Field(default_factory=dict, description="IndexTTS2 覆盖参数（情感/语速/步数/版本等）")
    preset_id: int | None = Field(default=None, description="引用已保存的 IndexTTS2 预设 ID")
    audio_asset_id: int | None = Field(default=None, description="引用音频库资产 ID（音频直驱模式）")
    audio_path: str | None = Field(default=None, description="直接指定本机音频文件绝对路径（音频直驱模式）")
    category_id: int | None = Field(default=None, description="作品分类 ID")
    category_name: str | None = Field(default=None, description="作品分类名称（不存在时自动创建，方便 AI 自动化调用）")
    webhook_url: str | None = Field(default=None, description="任务完成或失败时的回调 Webhook URL")
    max_retries: int = Field(default=3, ge=0, le=10)


class BatchJobsCreateRequest(BaseModel):
    items: list[JobCreateItem] = Field(..., min_length=1)


class JobUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category_id: int | None = None


class AudioUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category_id: int | None = None


class LocalAudioImportRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, description="本机音频文件绝对路径列表（自动清洗首尾引号）")
    category_id: int | None = None


class RecoverRequest(BaseModel):
    restart_render_container: bool = False
    requeue_stuck: bool = True


class TtsModeRequest(BaseModel):
    mode: str = Field(..., pattern="^(index-tts2|fish-speech)$")


class OpenPathRequest(BaseModel):
    path: str


# ==================== System & Health Endpoints ====================

@app.get("/api/v1/ping", tags=["系统状态"])
def api_ping() -> dict[str, Any]:
    return {"status": "ok", "service": "heygem-studio", "ts": int(time.time())}


@app.get("/api/v1/health", tags=["系统状态"], dependencies=[Depends(_verify_api_key)])
def api_health(force: bool = Query(False)) -> dict[str, Any]:
    return system_ctrl.get_system_health(force=force)


@app.get("/api/v1/logs", tags=["系统状态"], dependencies=[Depends(_verify_api_key)])
def api_logs(source: str = Query("queue", pattern="^(queue|tts|docker|guard)$"), tail: int = Query(120, ge=10, le=500)):
    return system_ctrl.read_service_logs(source=source, tail=tail)


@app.post("/api/v1/system/start", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_system_start() -> dict[str, Any]:
    return system_ctrl.self_heal_recover(restart_render_container=False, requeue_stuck=False)


@app.post("/api/v1/system/stop", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_system_stop() -> dict[str, Any]:
    return system_ctrl.stop_background_services()


@app.post("/api/v1/system/recover", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_system_recover(req: RecoverRequest | None = Body(default=None)) -> dict[str, Any]:
    effective = req or RecoverRequest()
    return system_ctrl.self_heal_recover(
        restart_render_container=effective.restart_render_container,
        requeue_stuck=effective.requeue_stuck,
    )


@app.post("/api/v1/queue/pause", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_queue_pause() -> dict[str, Any]:
    paused = system_ctrl.set_queue_paused(True)
    return {"ok": True, "paused": paused, "message": "队列已暂停（正在渲染的任务会继续完成，暂不派发新任务）"}


@app.post("/api/v1/queue/resume", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_queue_resume() -> dict[str, Any]:
    paused = system_ctrl.set_queue_paused(False)
    return {"ok": True, "paused": paused, "message": "队列已恢复运行"}


@app.post("/api/v1/tts/mode", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_tts_mode(req: TtsModeRequest) -> dict[str, Any]:
    mode = system_ctrl.set_tts_mode(req.mode)
    return {"ok": True, "mode": mode}


@app.post("/api/v1/system/open-path", tags=["系统控制"], dependencies=[Depends(_verify_api_key)])
def api_open_path(req: OpenPathRequest) -> dict[str, Any]:
    ok, msg = system_ctrl.open_path_in_explorer(req.path)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg}


# ==================== Categories & Model Groups ====================

@app.get("/api/v1/categories", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_list_categories() -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM studio_job_meta m JOIN video v ON v.id = m.video_id WHERE m.category_id = c.id) AS video_count,
                   (SELECT COUNT(*) FROM studio_audio_assets a WHERE a.category_id = c.id) AS audio_count
            FROM studio_categories c
            ORDER BY c.sort_order ASC, c.id ASC
            """
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/categories", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_create_category(req: CategoryCreate) -> dict[str, Any]:
    clean_name = req.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")
    conn = get_conn()
    try:
        now_ms = int(time.time() * 1000)
        cur = conn.execute(
            "INSERT INTO studio_categories (name, color, sort_order, created_at) VALUES (?, ?, 0, ?)",
            (clean_name, req.color.strip() or "#3b82f6", now_ms),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM studio_categories WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"item": dict(row)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"创建分类失败（可能已存在同名分类）：{exc}")
    finally:
        conn.close()


@app.patch("/api/v1/categories/{cat_id}", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_update_category(cat_id: int, req: CategoryUpdate) -> dict[str, Any]:
    conn = get_conn()
    try:
        fields = {}
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="分类名称不能为空")
            fields["name"] = clean_name
        if req.color is not None:
            fields["color"] = req.color.strip()
        if not fields:
            raise HTTPException(status_code=400, detail="无修改字段")
        sql = f"UPDATE studio_categories SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?"
        try:
            conn.execute(sql, list(fields.values()) + [cat_id])
            conn.commit()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"修改分类失败（可能存在同名分类）：{exc}")
        row = conn.execute("SELECT * FROM studio_categories WHERE id=?", (cat_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分类不存在")
        return {"item": dict(row)}
    finally:
        conn.close()


@app.delete("/api/v1/categories/{cat_id}", tags=["分类管理"], dependencies=[Depends(_verify_api_key)])
def api_delete_category(cat_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("UPDATE studio_job_meta SET category_id=NULL WHERE category_id=?", (cat_id,))
        conn.execute("UPDATE studio_audio_assets SET category_id=NULL WHERE category_id=?", (cat_id,))
        conn.execute("DELETE FROM studio_categories WHERE id=?", (cat_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/model-groups", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_list_model_groups() -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT g.*, (SELECT COUNT(*) FROM f2f_model m WHERE m.group_id = g.id) AS model_count
            FROM model_group g
            ORDER BY g.sort_order ASC, g.id ASC
            """
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/model-groups", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_create_model_group(req: CategoryCreate) -> dict[str, Any]:
    clean_name = req.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="模特分组名称不能为空")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO model_group (name, sort_order, created_at) VALUES (?, 0, ?)",
            (clean_name, int(time.time() * 1000)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM model_group WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"item": dict(row)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"创建模特分组失败：{exc}")
    finally:
        conn.close()


@app.delete("/api/v1/model-groups/{group_id}", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_delete_model_group(group_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("UPDATE f2f_model SET group_id=NULL WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM model_group WHERE id=?", (group_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ==================== Models & Voices ====================

@app.get("/api/v1/models", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_list_models(group_id: str | None = None, q: str | None = None) -> dict[str, Any]:
    conn = get_conn()
    try:
        sql = """
            SELECT m.*, g.name AS group_name
            FROM f2f_model m
            LEFT JOIN model_group g ON g.id = m.group_id
            WHERE 1=1
        """
        params: list[Any] = []
        if group_id == "ungrouped":
            sql += " AND m.group_id IS NULL"
        elif group_id and group_id.isdigit():
            sql += " AND m.group_id = ?"
            params.append(int(group_id))
        if q and q.strip():
            sql += " AND m.name LIKE ?"
            params.append(f"%{q.strip()}%")
        sql += " ORDER BY m.id DESC"
        rows = conn.execute(sql, params).fetchall()
        items = [_resolve_model_files(dict(r)) for r in rows]
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@app.post("/api/v1/models", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_create_model(
    name: str = Form(...),
    group_id: int | None = Form(default=None),
    local_video_path: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """导入新数字人模特（支持网页上传 MP4/MOV 或直接指定本机视频路径），自动提取参考音频并注册音色。"""
    cleaned_local = clean_local_path(local_video_path)
    if file is None and not cleaned_local:
        raise HTTPException(status_code=400, detail="请上传底模视频文件或填写本机视频路径")

    output_root = resolve_output_root()
    voice_root = resolve_voice_root()
    origin_dir = voice_root / "origin_audio"
    output_root.mkdir(parents=True, exist_ok=True)
    origin_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d%H%M%S") + f"{int((time.time() % 1) * 1000):03d}"

    if file is not None:
        ext = Path(file.filename or "model.mp4").suffix.lower() or ".mp4"
        if ext not in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
        video_filename = f"{stamp}{ext}"
        target_video = output_root / video_filename
        with open(target_video, "wb") as out_fh:
            shutil.copyfileobj(file.file, out_fh)
    else:
        src = Path(cleaned_local)
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"本机视频文件不存在：{src}")
        ext = src.suffix.lower() or ".mp4"
        if ext not in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
        video_filename = f"{stamp}{ext}"
        target_video = output_root / video_filename
        shutil.copy2(src, target_video)

    # 提取 22050Hz 单声道参考音色音频
    raw_wav = origin_dir / f"{stamp}.wav"
    fmt_wav = origin_dir / f"format_{stamp}.wav"
    try:
        _convert_to_wav(target_video, raw_wav, sample_rate=22050)
        shutil.copy2(raw_wav, fmt_wav)
    except Exception as exc:
        target_video.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"从底模视频提取参考音色失败（请确认视频包含音轨）：{exc}")

    now_ms = int(time.time() * 1000)
    origin_rel = f"origin_audio/{stamp}.wav"
    fmt_abs_str = str(fmt_wav).replace("\\", "/")

    conn = get_conn()
    try:
        v_cur = conn.execute(
            """
            INSERT INTO voice (origin_audio_path, lang, asr_format_audio_url, reference_audio_text, created_at)
            VALUES (?, 'zh', ?, '', ?)
            """,
            (origin_rel, fmt_abs_str, now_ms),
        )
        voice_id = int(v_cur.lastrowid)
        m_cur = conn.execute(
            """
            INSERT INTO f2f_model (name, video_path, audio_path, voice_id, created_at, group_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name.strip(), video_filename, f"origin_audio\\{stamp}.wav", voice_id, now_ms, group_id),
        )
        model_id = int(m_cur.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT m.*, g.name AS group_name FROM f2f_model m LEFT JOIN model_group g ON g.id = m.group_id WHERE m.id=?",
            (model_id,),
        ).fetchone()
        return {"item": _resolve_model_files(dict(row))}
    finally:
        conn.close()


@app.post("/api/v1/models/{model_id}/relink", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_relink_model_video(
    model_id: int,
    local_video_path: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """重新关联或上传缺失的模特底模视频文件，修复底模缺失问题。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM f2f_model WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模特不存在")
        model = dict(row)
    finally:
        conn.close()

    cleaned_local = clean_local_path(local_video_path)
    if file is None and not cleaned_local:
        raise HTTPException(status_code=400, detail="请上传底模视频或填写本机路径")

    output_root = resolve_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    old_name = Path(str(model.get("video_path") or "").strip()).name
    allowed_exts = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

    if file is not None:
        ext = Path(file.filename or "model.mp4").suffix.lower() or ".mp4"
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
        new_name = old_name if (old_name and Path(old_name).suffix.lower() == ext) else f"{int(time.time()*1000)}{ext}"
        target = output_root / new_name
        with open(target, "wb") as out_fh:
            shutil.copyfileobj(file.file, out_fh)
    else:
        src = Path(cleaned_local)
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"本机文件不存在：{src}")
        ext = src.suffix.lower() or ".mp4"
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式：{ext}")
        new_name = old_name if (old_name and Path(old_name).suffix.lower() == ext) else f"{int(time.time()*1000)}{ext}"
        target = output_root / new_name
        shutil.copy2(src, target)

    # 若关联音色文件不存在，尝试从新底模视频自动补提参考音色（静默容错）
    audio_rel = str(model.get("audio_path") or "").strip()
    if audio_rel:
        voice_root = resolve_voice_root()
        audio_abs = Path(audio_rel) if Path(audio_rel).is_absolute() else (voice_root / audio_rel.lstrip("/\\"))
        if not audio_abs.is_file():
            try:
                _convert_to_wav(target, audio_abs, sample_rate=22050)
            except Exception:
                pass

    conn = get_conn()
    try:
        conn.execute("UPDATE f2f_model SET video_path=? WHERE id=?", (new_name, model_id))
        conn.commit()
        refreshed = conn.execute(
            "SELECT m.*, g.name AS group_name FROM f2f_model m LEFT JOIN model_group g ON g.id = m.group_id WHERE m.id=?",
            (model_id,),
        ).fetchone()
        system_ctrl.invalidate_health_cache()
        return {"item": _resolve_model_files(dict(refreshed))}
    finally:
        conn.close()


@app.patch("/api/v1/models/{model_id}", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_update_model(model_id: int, req: ModelUpdate) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM f2f_model WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模特不存在")
        updates: dict[str, Any] = {}
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="模特名称不能为空")
            updates["name"] = clean_name
        if "group_id" in req.model_fields_set:
            updates["group_id"] = req.group_id
        if updates:
            sql = f"UPDATE f2f_model SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?"
            conn.execute(sql, list(updates.values()) + [model_id])
            conn.commit()
        refreshed = conn.execute(
            "SELECT m.*, g.name AS group_name FROM f2f_model m LEFT JOIN model_group g ON g.id = m.group_id WHERE m.id=?",
            (model_id,),
        ).fetchone()
        return {"item": _resolve_model_files(dict(refreshed))}
    finally:
        conn.close()


@app.delete("/api/v1/models/{model_id}", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_delete_model(model_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM video WHERE model_id=? AND status IN ('pending', 'waiting')",
            (model_id,),
        ).fetchone()[0]
        if active:
            raise HTTPException(status_code=409, detail=f"该模特仍有 {active} 个正在排队/渲染中的任务，请先中断后再删除")
        conn.execute("DELETE FROM f2f_model WHERE id=?", (model_id,))
        conn.commit()
        system_ctrl.invalidate_health_cache()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/voices", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_list_voices() -> dict[str, Any]:
    voice_root = resolve_voice_root()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT v.*, m.id AS model_id, m.name AS model_name
            FROM voice v
            LEFT JOIN f2f_model m ON m.voice_id = v.id
            ORDER BY v.id DESC
            """
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            abs_p = _resolve_voice_file(d, voice_root)
            d["abs_path"] = str(abs_p) if abs_p else ""
            d["exists"] = bool(abs_p and abs_p.is_file())
            d["display_name"] = d["model_name"] or (Path(d["origin_audio_path"] or f"voice_{d['id']}").stem)
            d["preview_url"] = f"/api/v1/media/voice/{d['id']}" if d["exists"] else ""
            items.append(d)
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


def _resolve_voice_file(voice_row: dict[str, Any], voice_root: Path | None = None) -> Path | None:
    vr = voice_root or resolve_voice_root()
    candidates: list[Path] = []
    origin_dir = vr / "origin_audio"
    for raw in [voice_row.get("asr_format_audio_url"), voice_row.get("origin_audio_path")]:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        first = text.split("|||")[0].strip()
        if first.lower().startswith("/code/data/"):
            first = str(vr / first[len("/code/data/"):])
        p = Path(first)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(vr / first.lstrip("/"))
            candidates.append(origin_dir / p.name)
        base = p.name
        if base:
            candidates.append(origin_dir / base)
            if base.startswith("format_denoise_"):
                candidates.append(origin_dir / base.replace("format_denoise_", "format_", 1))
            elif base.startswith("format_"):
                candidates.append(origin_dir / base.replace("format_", "format_denoise_", 1))
            else:
                candidates.append(origin_dir / f"format_{base}")
                candidates.append(origin_dir / f"format_denoise_{base}")
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0] if candidates else None


def _load_adapter_config_fallback() -> dict[str, Any]:
    cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _resolve_emo_audio_file(raw_path: str | None) -> Path | None:
    """解析情感参考音频路径（自动清洗 Windows 复制路径自带引号，兼容共享音色目录相对路径）。"""
    cleaned = clean_local_path(raw_path).replace("\\", "/")
    if not cleaned:
        return None
    vr = resolve_voice_root()
    mapped = str(vr / cleaned[len("/code/data/"):]) if cleaned.lower().startswith("/code/data/") else cleaned
    p = Path(mapped)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append((Path.cwd() / p).resolve())
        candidates.append((vr / cleaned.lstrip("/")).resolve())
    if p.name:
        candidates.append((vr / "origin_audio" / p.name).resolve())
    for c in candidates:
        if c.is_file():
            return c
    return None


@app.post("/api/v1/voices", tags=["模特与音色"], dependencies=[Depends(_verify_api_key)])
def api_upload_voice(
    name: str = Form(default=""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """上传新的自定义参考音色音频，自动格式化为 22050Hz 单声道 WAV 并入库。"""
    voice_root = resolve_voice_root()
    origin_dir = voice_root / "origin_audio"
    origin_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S") + f"{int((time.time() % 1) * 1000):03d}"
    safe_tag = sanitize_filename(name or Path(file.filename or "voice").stem, "voice")[:30]
    tmp_src = origin_dir / f"_tmp_{stamp}{Path(file.filename or '.wav').suffix or '.wav'}"
    fmt_wav = origin_dir / f"format_{stamp}_{safe_tag}.wav"
    try:
        with open(tmp_src, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        _convert_to_wav(tmp_src, fmt_wav, sample_rate=22050)
    finally:
        tmp_src.unlink(missing_ok=True)

    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO voice (origin_audio_path, lang, asr_format_audio_url, reference_audio_text, created_at)
            VALUES (?, 'zh', ?, ?, ?)
            """,
            (f"origin_audio/{fmt_wav.name}", str(fmt_wav).replace("\\", "/"), safe_tag, int(time.time() * 1000)),
        )
        conn.commit()
        vid = int(cur.lastrowid)
        return {
            "item": {
                "id": vid,
                "display_name": name.strip() or safe_tag,
                "origin_audio_path": f"origin_audio/{fmt_wav.name}",
                "asr_format_audio_url": str(fmt_wav).replace("\\", "/"),
                "abs_path": str(fmt_wav),
                "exists": True,
                "preview_url": f"/api/v1/media/voice/{vid}",
            }
        }
    finally:
        conn.close()


# ==================== IndexTTS2 Config, Presets, Audition & Audio Library ====================

@app.get("/api/v1/tts/capabilities", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_capabilities() -> dict[str, Any]:
    try:
        return _proxy_tts_json("GET", "/v1/tts/capabilities", timeout=4.0)
    except HTTPException:
        # 即使 18180 尚未启动，也返回本地 config.json 以便前端仍可浏览/保存默认参数
        cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        return {
            "available": False,
            "unavailable_reason": "IndexTTS2 适配器 (18180) 当前未启动，点击顶部「一键自愈恢复」即可拉起",
            "versions": ["IndexTTS-2", "IndexTTS-2.5"],
            "emo_vector_labels": ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"],
            "languages": ["ZH", "EN", "JA", "ES", "AR"],
            "current_config": cfg,
        }


@app.get("/api/v1/tts/config", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_get_tts_config() -> dict[str, Any]:
    try:
        return _proxy_tts_json("GET", "/v1/tts/config", timeout=4.0)
    except HTTPException:
        cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        return {"code": 0, "config": cfg, "offline_fallback": True}


@app.put("/api/v1/tts/config", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_put_tts_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _proxy_tts_json("PUT", "/v1/tts/config", payload=payload, timeout=5.0)
    except HTTPException:
        cfg_path = Path(__file__).resolve().parent.parent / "adapter" / "config.json"
        current = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        for k, v in payload.items():
            if k in current or str(k).startswith("index_tts_"):
                current[k] = v
        tmp_path = cfg_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, cfg_path)
        return {"code": 0, "msg": "saved_offline", "config": current}


@app.get("/api/v1/tts/status", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_status() -> dict[str, Any]:
    try:
        return _proxy_tts_json("GET", "/v1/tts/status", timeout=3.0)
    except HTTPException:
        return {"code": 0, "data": {"state": "offline", "message": "IndexTTS2 服务未启动", "progress": 0, "recent_logs": []}}


@app.post("/api/v1/tts/cancel", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_cancel() -> dict[str, Any]:
    return _proxy_tts_json("POST", "/v1/tts/cancel", payload={}, timeout=8.0)


@app.post("/api/v1/tts/upload-emo-audio", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_upload_emo_audio(file: UploadFile = File(...)) -> dict[str, Any]:
    emo_dir = resolve_studio_assets_root() / "emo_audio"
    emo_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    tmp = emo_dir / f"_tmp_{stamp}{Path(file.filename or '.wav').suffix or '.wav'}"
    dst = emo_dir / f"emo_{stamp}.wav"
    try:
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        _convert_to_wav(tmp, dst, sample_rate=22050)
    except Exception as exc:
        dst.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"情感参考音频格式不支持或转码失败：{exc}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "path": str(dst).replace("\\", "/"), "filename": dst.name}


@app.get("/api/v1/tts/presets", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_list_presets() -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM studio_tts_presets ORDER BY is_default DESC, id ASC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json", "{}") or "{}")
            items.append(d)
        return {"items": items}
    finally:
        conn.close()


@app.post("/api/v1/tts/presets", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_save_preset(req: PresetCreate) -> dict[str, Any]:
    clean_name = req.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="预设名称不能为空")
    now_ms = int(time.time() * 1000)
    conn = get_conn()
    try:
        if req.is_default:
            conn.execute("UPDATE studio_tts_presets SET is_default=0")
        existing = conn.execute("SELECT id FROM studio_tts_presets WHERE name=?", (clean_name,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE studio_tts_presets SET description=?, params_json=?, is_default=?, updated_at=? WHERE id=?",
                (req.description, json.dumps(req.params, ensure_ascii=False), 1 if req.is_default else 0, now_ms, existing["id"]),
            )
            pid = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO studio_tts_presets (name, description, params_json, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (clean_name, req.description, json.dumps(req.params, ensure_ascii=False), 1 if req.is_default else 0, now_ms, now_ms),
            )
            pid = int(cur.lastrowid)
        conn.commit()
        row = dict(conn.execute("SELECT * FROM studio_tts_presets WHERE id=?", (pid,)).fetchone())
        row["params"] = json.loads(row.pop("params_json", "{}"))
        return {"item": row}
    finally:
        conn.close()


@app.delete("/api/v1/tts/presets/{preset_id}", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_delete_preset(preset_id: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM studio_tts_presets WHERE id=?", (preset_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _merge_preset_and_params(preset_id: int | None, override_params: dict[str, Any]) -> dict[str, Any]:
    if preset_id is None:
        return dict(override_params or {})
    conn = get_conn()
    try:
        return _merge_preset_and_params_conn(conn, preset_id, override_params)
    finally:
        conn.close()


@app.post("/api/v1/tts/synthesize", tags=["IndexTTS2 语音工作台"], dependencies=[Depends(_verify_api_key)])
def api_tts_synthesize(req: TtsSynthesizeRequest) -> dict[str, Any]:
    """使用 IndexTTS2 实时合成试听音频，自动保存入音频素材库并可直接用于驱动数字人。"""
    ref_audio = clean_local_path(req.reference_audio)
    if not ref_audio and req.voice_id:
        conn = get_conn()
        try:
            vrow = conn.execute("SELECT * FROM voice WHERE id=?", (req.voice_id,)).fetchone()
            if not vrow:
                raise HTTPException(status_code=404, detail=f"音色 ID={req.voice_id} 不存在")
            resolved = _resolve_voice_file(dict(vrow))
            if not resolved or not resolved.is_file():
                raise HTTPException(status_code=404, detail=f"音色参考音频文件缺失：{resolved}")
            ref_audio = str(resolved).replace("\\", "/")
        finally:
            conn.close()

    if not ref_audio:
        raise HTTPException(status_code=400, detail="请先选择一个音色或参考音频")

    effective_params = _merge_preset_and_params(req.preset_id, req.tts_params)
    invoke_payload = {
        "text": req.text.strip(),
        "reference_audio": ref_audio,
        "tts_params": effective_params,
    }
    url = f"{system_ctrl.TTS_BASE_URL}/v1/invoke"
    body = json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8")
    http_req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(http_req, timeout=1800) as resp:
            wav_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(err_body).get("msg") or err_body
        except Exception:
            detail = err_body or str(exc)
        raise HTTPException(status_code=exc.code if 400 <= exc.code < 600 else 502, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"调用 IndexTTS2 合成失败：{exc}")

    audio_dir = resolve_studio_assets_root() / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    title = sanitize_filename(req.name or req.text.strip()[:24], f"tts_{stamp}")
    out_wav = audio_dir / f"{stamp}_{title}.wav"
    out_wav.write_bytes(wav_bytes)
    media = probe_media_info(out_wav)
    duration = float(media.get("duration") or 0.0)

    now_ms = int(time.time() * 1000)
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO studio_audio_assets
                (name, category_id, file_path, duration, source_type, voice_id, text_content, tts_params_json, created_at)
            VALUES (?, ?, ?, ?, 'tts', ?, ?, ?, ?)
            """,
            (
                req.name or req.text.strip()[:36],
                req.category_id,
                str(out_wav),
                duration,
                req.voice_id,
                req.text.strip(),
                json.dumps(effective_params, ensure_ascii=False),
                now_ms,
            ),
        )
        aid = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return {
        "item": {
            "id": aid,
            "name": req.name or req.text.strip()[:36],
            "category_id": req.category_id,
            "file_path": str(out_wav),
            "duration": duration,
            "size_kb": round(len(wav_bytes) / 1024, 1),
            "source_type": "tts",
            "voice_id": req.voice_id,
            "text_content": req.text.strip(),
            "tts_params": effective_params,
            "created_at": now_ms,
            "stream_url": f"/api/v1/media/audio/{aid}",
            "download_url": f"/api/v1/audios/{aid}/download",
        }
    }


@app.get("/api/v1/audios", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_list_audios(category_id: str | None = None, q: str | None = None) -> dict[str, Any]:
    conn = get_conn()
    try:
        sql = """
            SELECT a.*, c.name AS category_name, c.color AS category_color
            FROM studio_audio_assets a
            LEFT JOIN studio_categories c ON c.id = a.category_id
            WHERE 1=1
        """
        params: list[Any] = []
        if category_id == "uncategorized":
            sql += " AND a.category_id IS NULL"
        elif category_id and category_id.isdigit():
            sql += " AND a.category_id = ?"
            params.append(int(category_id))
        if q and q.strip():
            sql += " AND (a.name LIKE ? OR a.text_content LIKE ?)"
            params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
        sql += " ORDER BY a.id DESC"
        rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            p = Path(str(d.get("file_path") or ""))
            d["exists"] = p.is_file()
            d["size_kb"] = round(p.stat().st_size / 1024, 1) if d["exists"] else 0.0
            d["tts_params"] = json.loads(d.pop("tts_params_json", "{}") or "{}")
            d["stream_url"] = f"/api/v1/media/audio/{d['id']}" if d["exists"] else ""
            d["download_url"] = f"/api/v1/audios/{d['id']}/download" if d["exists"] else ""
            items.append(d)
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@app.post("/api/v1/audios/upload", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_upload_audios(
    category_id: int | None = Form(default=None),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """批量上传本地音频文件，自动转码为标准 WAV 并入音频素材库。"""
    audio_dir = resolve_studio_assets_root() / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    created: list[dict[str, Any]] = []
    written_files: list[Path] = []
    conn = get_conn()
    try:
        for upload in files:
            orig_name = upload.filename or "audio.wav"
            stem = sanitize_filename(Path(orig_name).stem, "audio")
            stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            ext = Path(orig_name).suffix.lower() or ".wav"
            tmp_in = audio_dir / f"_in_{stamp}{ext}"
            out_wav = audio_dir / f"{stamp}_{stem}.wav"
            try:
                with open(tmp_in, "wb") as fh:
                    shutil.copyfileobj(upload.file, fh)
                _convert_to_wav(tmp_in, out_wav, sample_rate=22050)
                written_files.append(out_wav)
            except Exception as exc:
                conn.rollback()
                out_wav.unlink(missing_ok=True)
                for wf in written_files:
                    wf.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"音频「{orig_name}」转码失败（请确认是有效音频文件）：{exc}")
            finally:
                tmp_in.unlink(missing_ok=True)

            media = probe_media_info(out_wav)
            dur = float(media.get("duration") or 0.0)
            now_ms = int(time.time() * 1000)
            cur = conn.execute(
                """
                INSERT INTO studio_audio_assets (name, category_id, file_path, duration, source_type, created_at)
                VALUES (?, ?, ?, ?, 'upload', ?)
                """,
                (stem, category_id, str(out_wav), dur, now_ms),
            )
            aid = int(cur.lastrowid)
            created.append(
                {
                    "id": aid,
                    "name": stem,
                    "category_id": category_id,
                    "file_path": str(out_wav),
                    "duration": dur,
                    "source_type": "upload",
                    "exists": True,
                    "stream_url": f"/api/v1/media/audio/{aid}",
                    "download_url": f"/api/v1/audios/{aid}/download",
                }
            )
        conn.commit()
    finally:
        conn.close()
    return {"items": created, "count": len(created)}


@app.post("/api/v1/audios/import-local", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_import_local_audios(req: LocalAudioImportRequest) -> dict[str, Any]:
    """通过本机路径批量快速导入音频（自动清洗引号、全量前置校验存在性后再转码入库）。"""
    cleaned_paths = [clean_local_path(p) for p in req.paths if clean_local_path(p)]
    if not cleaned_paths:
        raise HTTPException(status_code=400, detail="请至少填写 1 条有效的本机音频文件路径")

    # 全量前置校验所有文件存在性，避免部分入库残留
    source_files: list[Path] = []
    for idx, raw in enumerate(cleaned_paths):
        src = Path(raw)
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"第 {idx + 1} 条音频文件不存在：{src}")
        source_files.append(src)

    audio_dir = resolve_studio_assets_root() / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[str, Path, float]] = []
    written_files: list[Path] = []

    try:
        for src in source_files:
            stem = sanitize_filename(src.stem, "audio")
            stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            out_wav = audio_dir / f"{stamp}_{stem}.wav"
            try:
                _convert_to_wav(src, out_wav, sample_rate=22050)
                written_files.append(out_wav)
            except Exception as exc:
                out_wav.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"音频「{src.name}」转码失败（请确认是有效音频文件）：{exc}")
            media = probe_media_info(out_wav)
            dur = float(media.get("duration") or 0.0)
            prepared.append((stem, out_wav, dur))
    except Exception:
        for wf in written_files:
            wf.unlink(missing_ok=True)
        raise

    created: list[dict[str, Any]] = []
    conn = get_conn()
    try:
        now_ms = int(time.time() * 1000)
        for stem, out_wav, dur in prepared:
            cur = conn.execute(
                """
                INSERT INTO studio_audio_assets (name, category_id, file_path, duration, source_type, created_at)
                VALUES (?, ?, ?, ?, 'local_import', ?)
                """,
                (stem, req.category_id, str(out_wav), dur, now_ms),
            )
            aid = int(cur.lastrowid)
            created.append(
                {
                    "id": aid,
                    "name": stem,
                    "category_id": req.category_id,
                    "file_path": str(out_wav),
                    "duration": dur,
                    "source_type": "local_import",
                    "exists": True,
                    "stream_url": f"/api/v1/media/audio/{aid}",
                    "download_url": f"/api/v1/audios/{aid}/download",
                }
            )
        conn.commit()
    except Exception:
        conn.rollback()
        for wf in written_files:
            wf.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    return {"items": created, "count": len(created)}


@app.patch("/api/v1/audios/{audio_id}", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_update_audio(audio_id: int, req: AudioUpdateRequest) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="音频素材不存在")
        updates: dict[str, Any] = {}
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="音频名称不能为空")
            updates["name"] = clean_name
        if "category_id" in req.model_fields_set:
            updates["category_id"] = req.category_id
        if updates:
            sql = f"UPDATE studio_audio_assets SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?"
            conn.execute(sql, list(updates.values()) + [audio_id])
            conn.commit()
        refreshed = conn.execute(
            """
            SELECT a.*, c.name AS category_name, c.color AS category_color
            FROM studio_audio_assets a
            LEFT JOIN studio_categories c ON c.id = a.category_id
            WHERE a.id=?
            """,
            (audio_id,),
        ).fetchone()
        d = dict(refreshed)
        p = Path(str(d.get("file_path") or ""))
        d["exists"] = p.is_file()
        d["size_kb"] = round(p.stat().st_size / 1024, 1) if d["exists"] else 0.0
        d["tts_params"] = json.loads(d.pop("tts_params_json", "{}") or "{}")
        d["stream_url"] = f"/api/v1/media/audio/{d['id']}" if d["exists"] else ""
        d["download_url"] = f"/api/v1/audios/{d['id']}/download" if d["exists"] else ""
        return {"item": d}
    finally:
        conn.close()


@app.delete("/api/v1/audios/{audio_id}", tags=["音频素材库"], dependencies=[Depends(_verify_api_key)])
def api_delete_audio(audio_id: int, remove_file: bool = True) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT file_path FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
        if row and remove_file and row["file_path"]:
            try:
                Path(row["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass
        conn.execute("DELETE FROM studio_audio_assets WHERE id=?", (audio_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/audios/{audio_id}/download", tags=["音频素材库"])
def api_download_audio(audio_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT name, file_path FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["file_path"] or not Path(row["file_path"]).is_file():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    p = Path(row["file_path"])
    dl_name = f"{sanitize_filename(row['name'], p.stem)}.wav"
    return FileResponse(
        path=str(p),
        media_type="audio/wav",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(dl_name)}"},
    )


# ==================== Video Jobs & AI Automation API ====================

def _resolve_or_create_category_id(conn, category_id: int | None, category_name: str | None) -> int | None:
    if category_id is not None:
        return category_id
    if category_name and category_name.strip():
        cname = category_name.strip()
        row = conn.execute("SELECT id FROM studio_categories WHERE name=?", (cname,)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO studio_categories (name, color, sort_order, created_at) VALUES (?, '#3b82f6', 0, ?)",
            (cname, int(time.time() * 1000)),
        )
        return int(cur.lastrowid)
    return None


def _merge_preset_and_params_conn(conn, preset_id: int | None, override_params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if preset_id is not None:
        row = conn.execute("SELECT params_json FROM studio_tts_presets WHERE id=?", (preset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"{prefix}参数预设 ID={preset_id} 不存在")
        if row["params_json"]:
            merged.update(json.loads(row["params_json"]))
    if override_params:
        merged.update(override_params)
    return merged


def _validate_and_prepare_job_spec(conn, item: JobCreateItem, index: int | None = None) -> dict[str, Any]:
    """入队前纯只读全量校验：检查模特存在及底模文件、驱动音频或文本模式下的音色与情感参考音文件。"""
    prefix = f"第 {index + 1} 条任务：" if index is not None else ""

    model_row = conn.execute("SELECT * FROM f2f_model WHERE id=?", (item.model_id,)).fetchone()
    if not model_row:
        raise HTTPException(status_code=404, detail=f"{prefix}数字人模特 ID={item.model_id} 不存在")
    model = dict(model_row)

    output_root = resolve_output_root()
    vp = str(model.get("video_path") or "").strip()
    video_abs = output_root / vp.lstrip("/\\") if vp else None
    if not video_abs or not video_abs.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"{prefix}数字人模特「{model['name']}」(ID={item.model_id}) 的底模视频文件缺失：{video_abs or vp or '(未配置)'}，请先在「模特与音色」页面重新关联或上传底模",
        )

    text_content = (item.text or "").strip()
    audio_mode = "tts"
    audio_src_path: Path | None = None
    voice_id: int | None = item.voice_id or model.get("voice_id")
    effective_tts_params = _merge_preset_and_params_conn(conn, item.preset_id, item.tts_params, prefix=prefix)

    if item.audio_asset_id is not None:
        arow = conn.execute("SELECT * FROM studio_audio_assets WHERE id=?", (item.audio_asset_id,)).fetchone()
        if not arow or not arow["file_path"] or not Path(arow["file_path"]).is_file():
            raise HTTPException(status_code=404, detail=f"{prefix}音频库资产 ID={item.audio_asset_id} 不存在或文件丢失")
        audio_mode = "audio_asset"
        audio_src_path = Path(arow["file_path"])
        if not text_content and arow["text_content"]:
            text_content = str(arow["text_content"])
    elif item.audio_path and clean_local_path(item.audio_path):
        src_p = Path(clean_local_path(item.audio_path))
        if not src_p.is_file():
            raise HTTPException(status_code=404, detail=f"{prefix}指定音频文件不存在：{src_p}")
        audio_mode = "audio_path"
        audio_src_path = src_p
    else:
        if not text_content:
            raise HTTPException(status_code=400, detail=f"{prefix}请提供口播文案 text，或提供 audio_asset_id / audio_path")

        # 文本配音模式前置校验：参考音色记录与本地音色文件必须真实存在
        if not voice_id:
            raise HTTPException(status_code=400, detail=f"{prefix}数字人模特「{model['name']}」未绑定默认音色，请指定 voice_id")
        vrow = conn.execute("SELECT * FROM voice WHERE id=?", (voice_id,)).fetchone()
        if not vrow:
            raise HTTPException(status_code=404, detail=f"{prefix}参考音色 ID={voice_id} 不存在")
        voice_file = _resolve_voice_file(dict(vrow))
        if not voice_file or not voice_file.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"{prefix}参考音色 ID={voice_id} 的音频文件缺失：{voice_file or '(未配置)'}，请先在「模特与音色」页面检查或重新上传音色",
            )

        # 情感音频模式前置校验：若请求未显式传 emo_mode，同步参考全局 adapter/config.json 默认值
        adapter_cfg = _load_adapter_config_fallback()
        emo_mode = str(effective_tts_params.get("emo_mode") or "").strip().lower()
        if not emo_mode:
            emo_mode = str(adapter_cfg.get("index_tts_emo_mode") or "").strip().lower()
        if emo_mode == "emo_audio":
            if "emo_audio_prompt" in effective_tts_params and effective_tts_params["emo_audio_prompt"] is not None:
                emo_raw = str(effective_tts_params["emo_audio_prompt"])
            else:
                emo_raw = str(adapter_cfg.get("index_tts_emo_audio_prompt") or "")
            emo_file = _resolve_emo_audio_file(emo_raw)
            if not emo_file or not emo_file.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"{prefix}情感模式为 emo_audio 时必须提供存在的情感参考音频文件：{clean_local_path(emo_raw) or '(未提供)'}",
                )
            effective_tts_params["emo_audio_prompt"] = str(emo_file).replace("\\", "/")

    return {
        "item": item,
        "model": model,
        "audio_mode": audio_mode,
        "audio_src_path": audio_src_path,
        "text_content": text_content,
        "voice_id": voice_id,
        "effective_tts_params": effective_tts_params,
    }


def _create_jobs_atomically(items: list[JobCreateItem]) -> list[dict[str, Any]]:
    """原子化创建单条或批量任务：先全量前置校验，再统一物化音频并在单事务内入库，任意条目失败零残留。"""
    output_root = resolve_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    written_temp_files: list[Path] = []
    created_ids: list[int] = []

    conn = get_conn()
    try:
        is_batch = len(items) > 1
        specs = [_validate_and_prepare_job_spec(conn, it, index=idx if is_batch else None) for idx, it in enumerate(items)]

        # 阶段 2：准备需要落盘到 temp 目录的驱动音频（若任一条转码失败，清理前面已写入的临时音频）
        materialized: list[tuple[dict[str, Any], str | None, str]] = []
        for idx, spec in enumerate(specs):
            audio_name_for_video: str | None = None
            source_audio_abs = ""
            if spec["audio_mode"] == "audio_asset":
                src_p = spec["audio_src_path"]
                assert src_p is not None
                audio_name_for_video = f"studio_{uuid.uuid4().hex[:12]}.wav"
                dst_p = output_root / audio_name_for_video
                shutil.copy2(src_p, dst_p)
                written_temp_files.append(dst_p)
                source_audio_abs = str(src_p)
            elif spec["audio_mode"] == "audio_path":
                src_p = spec["audio_src_path"]
                assert src_p is not None
                audio_name_for_video = f"studio_{uuid.uuid4().hex[:12]}.wav"
                dst_p = output_root / audio_name_for_video
                if src_p.suffix.lower() == ".wav":
                    shutil.copy2(src_p, dst_p)
                else:
                    try:
                        _convert_to_wav(src_p, dst_p, sample_rate=22050)
                    except Exception as exc:
                        prefix = f"第 {idx + 1} 条任务：" if is_batch else ""
                        raise HTTPException(status_code=400, detail=f"{prefix}驱动音频转码失败：{exc}")
                written_temp_files.append(dst_p)
                source_audio_abs = str(src_p)
            materialized.append((spec, audio_name_for_video, source_audio_abs))

        # 阶段 3：单事务批量入库
        now_ms = int(time.time() * 1000)
        for spec, audio_name_for_video, source_audio_abs in materialized:
            item: JobCreateItem = spec["item"]
            model = spec["model"]
            text_content = spec["text_content"]
            cat_id = _resolve_or_create_category_id(conn, item.category_id, item.category_name)

            job_name = (item.name or "").strip()
            if not job_name:
                snippet = sanitize_filename(text_content[:18], "") if text_content else ""
                job_name = f"{model['name']}_{snippet or time.strftime('%m%d_%H%M%S')}"

            cur = conn.execute(
                """
                INSERT INTO video (file_path, status, message, model_id, audio_path, param, code, created_at, progress, name, duration, text_content, voice_id)
                VALUES (NULL, 'waiting', '已加入制作队列，等待调度执行', ?, ?, NULL, NULL, ?, 0, ?, NULL, ?, ?)
                """,
                (item.model_id, audio_name_for_video, now_ms, job_name, text_content, spec["voice_id"]),
            )
            vid = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO studio_job_meta
                    (video_id, category_id, phase, stage_label, progress_float, tts_params_json, source_audio_path, max_retries, webhook_url, updated_at)
                VALUES (?, ?, 'queued', '已加入制作队列，等待调度执行', 0.0, ?, ?, ?, ?, ?)
                """,
                (
                    vid,
                    cat_id,
                    json.dumps(spec["effective_tts_params"], ensure_ascii=False),
                    source_audio_abs,
                    item.max_retries,
                    (item.webhook_url or "").strip(),
                    now_ms,
                ),
            )
            created_ids.append(vid)

        conn.commit()
    except Exception:
        conn.rollback()
        for wf in written_temp_files:
            wf.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    jobs: list[dict[str, Any]] = []
    for vid in created_ids:
        job = _fetch_job_by_id(vid)
        assert job is not None
        jobs.append(job)
    return jobs


def _create_single_job_in_db(item: JobCreateItem) -> dict[str, Any]:
    return _create_jobs_atomically([item])[0]


@app.get("/api/v1/jobs", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_list_jobs(
    status: str | None = None,
    category_id: str | None = None,
    model_id: int | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conn = get_conn()
    try:
        sql = """
            SELECT v.*,
                   fm.name AS model_name,
                   fm.video_path AS model_video_path,
                   m.category_id,
                   c.name AS category_name,
                   c.color AS category_color,
                   m.phase, m.stage_label, m.progress_float,
                   m.rendered_frames, m.total_frames, m.render_fps, m.eta_seconds,
                   m.heartbeat_at, m.tts_params_json, m.source_audio_path, m.delivered_path,
                   m.retry_count, m.max_retries, m.error_code, m.error_detail,
                   m.webhook_url, m.recent_logs_json, m.updated_at AS meta_updated_at
            FROM video v
            LEFT JOIN f2f_model fm ON fm.id = v.model_id
            LEFT JOIN studio_job_meta m ON m.video_id = v.id
            LEFT JOIN studio_categories c ON c.id = m.category_id
            WHERE 1=1
        """
        params: list[Any] = []
        if status and status != "all":
            if status == "active":
                sql += " AND v.status IN ('pending', 'waiting')"
            else:
                sql += " AND v.status = ?"
                params.append(status)
        if category_id == "uncategorized":
            sql += " AND m.category_id IS NULL"
        elif category_id and category_id.isdigit():
            sql += " AND m.category_id = ?"
            params.append(int(category_id))
        if model_id is not None:
            sql += " AND v.model_id = ?"
            params.append(model_id)
        if q and q.strip():
            sql += " AND (v.name LIKE ? OR v.text_content LIKE ? OR fm.name LIKE ?)"
            like = f"%{q.strip()}%"
            params.extend([like, like, like])

        sql += " ORDER BY CASE v.status WHEN 'pending' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END, v.id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        items = [_enrich_job_row(dict(r)) for r in rows]
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@app.get("/api/v1/jobs/{job_id}", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_get_job(job_id: int) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    return {"item": job}


@app.post("/api/v1/jobs", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_create_job(item: JobCreateItem) -> dict[str, Any]:
    """创建单条数字人视频任务（支持文本+IndexTTS2参数，或直接传入本地音频/音频库资产）。"""
    job = _create_single_job_in_db(item)
    return {"item": job, "job_id": job["id"]}


@app.post("/api/v1/jobs/batch", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_create_jobs_batch(req: BatchJobsCreateRequest) -> dict[str, Any]:
    """批量创建数字人视频任务（全量前置校验 + 事务原子提交，任意一条无效则整批拒绝不留脏数据）。"""
    created = _create_jobs_atomically(req.items)
    return {"items": created, "count": len(created), "job_ids": [j["id"] for j in created]}


@app.post("/api/v1/jobs/{job_id}/cancel", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_cancel_job(job_id: int) -> dict[str, Any]:
    """实时中断任务：排队中立即取消；语音合成中立刻杀死 TTS 进程；渲染中通知调度器中止容器。保留历史记录以便随时重试。"""
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")

    status = job["status"]
    if status in {"success", "cancelled"}:
        return {"ok": True, "item": job, "message": f"任务当前状态已是 {status}"}

    now_sec = int(time.time())
    conn = get_conn()
    try:
        if status in {"waiting", "failed", "draft"}:
            # 条件更新防止刚好与队列出队抢单撞车：如果刚变成 pending，则无缝走 pending 中断分支
            cur = conn.execute(
                "UPDATE video SET status='cancelled', message='任务已被用户中断', code=NULL WHERE id=? AND status IN ('waiting', 'failed', 'draft')",
                (job_id,),
            )
            conn.commit()
            if (cur.rowcount or 0) > 0:
                upsert_job_meta(job_id, phase="cancelled", stage_label="任务已被用户中断", eta_seconds=-1)
                return {"ok": True, "item": _fetch_job_by_id(job_id), "message": "排队任务已立即中断"}
            job = _fetch_job_by_id(job_id) or job

        # pending 状态：写入取消请求表，并主动触发 TTS 中断；若队列调度器未运行则直接本地置为 cancelled
        conn.execute(
            "INSERT OR REPLACE INTO studio_cancel_requests (video_id, requested_at, reason) VALUES (?, ?, 'user_cancelled')",
            (job_id, now_sec),
        )
        conn.commit()
    finally:
        conn.close()

    # 若尚未拿到 8383 任务码（处于语音准备/合成阶段），立即向 18180 发中断信号
    if job["phase"] in {"tts_loading", "tts_synthesizing", "queued"} or not job.get("code"):
        try:
            _proxy_tts_json("POST", "/v1/tts/cancel", payload={}, timeout=4.0)
        except Exception:
            pass

    upsert_job_meta(job_id, stage_label="正在中断任务并释放显存...")

    # 如果后台队列调度器当前根本没运行，直接完成中止收尾，不让用户傻等
    queue_pids = system_ctrl._find_python_processes("heygem_queue_runner.py", use_cache=False)
    if not queue_pids:
        if job.get("code"):
            subprocess.run(
                ["docker", "restart", "duix-avatar-gen-video"],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
        conn = get_conn()
        try:
            conn.execute("UPDATE video SET status='cancelled', message='任务已被用户中断', code=NULL WHERE id=?", (job_id,))
            conn.execute("DELETE FROM studio_cancel_requests WHERE video_id=?", (job_id,))
            conn.commit()
        finally:
            conn.close()
        upsert_job_meta(job_id, phase="cancelled", stage_label="任务已被用户中断", eta_seconds=-1)

    return {"ok": True, "item": _fetch_job_by_id(job_id), "message": "已下发中断指令，任务将在数秒内停止并释放资源"}


@app.post("/api/v1/jobs/{job_id}/retry", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_retry_job(job_id: int) -> dict[str, Any]:
    """重新排队执行失败或已中断的任务（重置重试计数与错误信息）。"""
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    if job["status"] == "pending":
        raise HTTPException(status_code=409, detail="该任务正在执行中，请先点击「中断任务」后再重试")

    # 若原任务为文案配音模式且 temp 里的临时音频已丢失，清空 audio_path 让调度器重新合成语音
    clear_audio = False
    if job.get("text_content") and job.get("audio_path"):
        temp_audio = resolve_output_root() / Path(str(job["audio_path"])).name
        src_audio_str = str(job["paths"].get("source_audio_path") or "").strip()
        src_audio_ok = bool(src_audio_str and Path(src_audio_str).is_file())
        if not temp_audio.is_file() and not src_audio_ok:
            clear_audio = True

    conn = get_conn()
    try:
        conn.execute("DELETE FROM studio_cancel_requests WHERE video_id=?", (job_id,))
        conn.execute("DELETE FROM cancelled_video_tasks WHERE video_id=?", (job_id,))
        if clear_audio:
            conn.execute(
                """
                UPDATE video
                   SET status='waiting', message='用户手动重试，等待重新配音并执行', progress=0, code=NULL, param=NULL, file_path=NULL, audio_path=NULL
                 WHERE id=?
                """,
                (job_id,),
            )
        else:
            conn.execute(
                """
                UPDATE video
                   SET status='waiting', message='用户手动重试，等待执行', progress=0, code=NULL, param=NULL, file_path=NULL
                 WHERE id=?
                """,
                (job_id,),
            )
        conn.commit()
    finally:
        conn.close()
    upsert_job_meta(
        job_id,
        phase="queued",
        stage_label="已重新加入队列，等待执行",
        progress_float=0.0,
        rendered_frames=0,
        eta_seconds=-1,
        retry_count=0,
        error_code="",
        error_detail="",
        webhook_notified=0,
    )
    return {"ok": True, "item": _fetch_job_by_id(job_id)}


@app.patch("/api/v1/jobs/{job_id}", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_update_job(job_id: int, req: JobUpdateRequest) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    conn = get_conn()
    try:
        if req.name is not None:
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="作品标题不能为空")
            conn.execute("UPDATE video SET name=? WHERE id=?", (clean_name, job_id))
            conn.commit()
    finally:
        conn.close()

    if "category_id" in req.model_fields_set:
        upsert_job_meta(job_id, category_id=req.category_id)
        # 若已有交付成片，顺带同步移动到对应分类子目录（可选安全移动）
        delivered = job["paths"].get("delivered_path")
        if delivered and Path(delivered).is_file():
            src = Path(delivered)
            cat_folder = ""
            if req.category_id is not None:
                conn = get_conn()
                try:
                    crow = conn.execute("SELECT name FROM studio_categories WHERE id=?", (req.category_id,)).fetchone()
                    if crow and crow["name"]:
                        cat_folder = sanitize_filename(crow["name"], "")[:40]
                finally:
                    conn.close()
            dest_dir = (resolve_delivery_root() / cat_folder) if cat_folder else resolve_delivery_root()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / src.name
            if dst.resolve() != src.resolve():
                try:
                    shutil.move(str(src), str(dst))
                    upsert_job_meta(job_id, delivered_path=str(dst))
                except Exception:
                    pass

    return {"item": _fetch_job_by_id(job_id)}


@app.delete("/api/v1/jobs/{job_id}", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_delete_job(job_id: int, remove_files: bool = False) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 ID={job_id} 不存在")
    # 若删除的是运行中任务，先下发中止信号防止后台继续空烧显存
    if job["status"] == "pending":
        if not job.get("code"):
            try:
                _proxy_tts_json("POST", "/v1/tts/cancel", payload={}, timeout=3.0)
            except Exception:
                pass
    if remove_files:
        for key in ("delivered_path", "temp_video_path"):
            raw_p = str(job["paths"].get(key) or "").strip()
            if raw_p and Path(raw_p).is_file():
                try:
                    Path(raw_p).unlink(missing_ok=True)
                except OSError:
                    pass
    conn = get_conn()
    try:
        if job["status"] == "pending" and job.get("code"):
            conn.execute(
                "INSERT OR REPLACE INTO cancelled_video_tasks (video_id, code, cancelled_at) VALUES (?, ?, ?)",
                (job_id, job["code"], int(time.time())),
            )
        conn.execute("DELETE FROM studio_job_meta WHERE video_id=?", (job_id,))
        conn.execute("DELETE FROM studio_cancel_requests WHERE video_id=?", (job_id,))
        conn.execute("DELETE FROM video WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/v1/jobs/{job_id}/download", tags=["数字人任务与自动化 API"])
def api_download_job(job_id: int) -> FileResponse:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = Path(job["paths"].get("playable_path") or "")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="成片视频文件尚未生成或已移动")
    dl_name = f"{job_id:04d}_{sanitize_filename(job['name'], 'video')}.mp4"
    return FileResponse(
        path=str(target),
        media_type="video/mp4",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(dl_name)}"},
    )


@app.post("/api/v1/jobs/{job_id}/open-folder", tags=["数字人任务与自动化 API"], dependencies=[Depends(_verify_api_key)])
def api_open_job_folder(job_id: int) -> dict[str, Any]:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = job["paths"].get("playable_path") or str(resolve_delivery_root())
    ok, msg = system_ctrl.open_path_in_explorer(target)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg}


# ==================== Media Streaming Endpoints (Supports HTTP Range) ====================

@app.get("/api/v1/media/video/{job_id}", tags=["媒体预览"])
def media_stream_video(job_id: int) -> FileResponse:
    job = _fetch_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = Path(job["paths"].get("playable_path") or "")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(path=str(target), media_type="video/mp4")


@app.get("/api/v1/media/model/{model_id}", tags=["媒体预览"])
def media_stream_model(model_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT video_path FROM f2f_model WHERE id=?", (model_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["video_path"]:
        raise HTTPException(status_code=404, detail="模特不存在")
    target = resolve_output_root() / str(row["video_path"]).lstrip("/\\")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="底模视频文件不存在")
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(path=str(target), media_type=mime or "video/mp4")


@app.get("/api/v1/media/audio/{audio_id}", tags=["媒体预览"])
def media_stream_audio(audio_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT file_path FROM studio_audio_assets WHERE id=?", (audio_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["file_path"] or not Path(row["file_path"]).is_file():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return FileResponse(path=row["file_path"], media_type="audio/wav")


@app.get("/api/v1/media/voice/{voice_id}", tags=["媒体预览"])
def media_stream_voice(voice_id: int) -> FileResponse:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM voice WHERE id=?", (voice_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="音色不存在")
    target = _resolve_voice_file(dict(row))
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="音色文件不存在")
    return FileResponse(path=str(target), media_type="audio/wav")


# ==================== Static Web Studio UI ====================

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon() -> Response:
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>HeyGem Studio UI 尚未初始化</h1>", status_code=503)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


def run_server() -> None:
    import uvicorn

    host = os.getenv("HEYGEM_STUDIO_HOST", "127.0.0.1")
    port = int(os.getenv("HEYGEM_STUDIO_PORT", "8390"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
