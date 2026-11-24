from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _make_tiny_mp4(path: Path) -> None:
    """用本地 ffmpeg 生成 1 秒测试短视频（含 440Hz 音轨），用于真实底模导入与流媒体测试。"""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x120:d=1:r=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and path.is_file(), f"ffmpeg failed: {res.stderr}"


def _make_tiny_wav(path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=520:duration=1",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        "22050",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and path.is_file(), f"ffmpeg wav failed: {res.stderr}"


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "duix_avatar_data"
    (data_dir / "face2face" / "temp").mkdir(parents=True, exist_ok=True)
    (data_dir / "voice" / "data" / "origin_audio").mkdir(parents=True, exist_ok=True)
    delivery_dir = tmp_path / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "biz.db"
    mode_file = tmp_path / "tts-mode.txt"
    mode_file.write_text("index-tts2", encoding="utf-8")

    monkeypatch.setenv("HEYGEM_DB_OVERRIDE", str(db_file))
    monkeypatch.setenv("DUIX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HEYGEM_OUTPUT_ROOT", str(data_dir / "face2face" / "temp"))
    monkeypatch.setenv("HEYGEM_VOICE_ROOT", str(data_dir / "voice" / "data"))
    monkeypatch.setenv("HEYGEM_DELIVERY_ROOT", str(delivery_dir))
    monkeypatch.setenv("HEYGEM_STUDIO_ASSETS_ROOT", str(data_dir / "studio_assets"))
    monkeypatch.setenv("HEYGEM_MODE_FILE", str(mode_file))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    from studio import db as studio_db

    studio_db.init_studio_schema(db_file)
    return {
        "tmp": tmp_path,
        "db": db_file,
        "data_dir": data_dir,
        "output_root": data_dir / "face2face" / "temp",
        "voice_root": data_dir / "voice" / "data",
        "delivery_dir": delivery_dir,
    }


def test_queue_telemetry_parser_and_oom_detection():
    import core.heygem_queue_runner as qr

    sample_code = "33a73ac3-48c2-4821-a922-83cc1739ad48"
    sample_logs = f"""
2026-11-20 15:00:10,100-[service.self_logger]-pool.py[line:125]-INFO: {sample_code} -> [500] avi write success, current_idx: 1000.
2026-11-20 15:00:41,821-[service.self_logger]-pool.py[line:125]-INFO: {sample_code} -> [1250] avi write success, current_idx: 2500.
"""
    tel = qr.parse_gen_video_render_telemetry(sample_code, estimated_total_frames=5000, log_text=sample_logs)
    assert tel["phase"] == "face2face_rendering"
    assert tel["rendered_frames"] == 2500
    assert tel["total_frames"] == 5000
    assert 55.0 <= float(tel["progress_float"]) <= 60.0  # 25 + 0.5*65 = 57.5
    assert int(tel["eta_seconds"]) > 0

    mux_logs = sample_logs + f"\n2026-11-20 15:01:13,634-INFO: {sample_code} -> handler netG end, start to wait pool and concat avi.\n"
    mux_logs += f"2026-11-20 15:01:14,100-INFO: command:ffmpeg -i /code/data/temp/{sample_code}/result.avi /code/data/temp/{sample_code}-r.mp4\n"
    tel2 = qr.parse_gen_video_render_telemetry(sample_code, estimated_total_frames=5000, log_text=mux_logs)
    assert tel2["phase"] == "ffmpeg_muxing"
    assert float(tel2["progress_float"]) >= 90.0

    oom_logs = sample_logs + "\nRuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
    tel3 = qr.parse_gen_video_render_telemetry(sample_code, estimated_total_frames=5000, log_text=oom_logs)
    assert "out of memory" in str(tel3["container_error"]).lower()


def test_queue_preflight_missing_model_and_retry_cap_and_pause(isolated_env, monkeypatch):
    import core.heygem_queue_runner as qr

    monkeypatch.setattr(qr, "DB_PATH", isolated_env["db"])
    monkeypatch.setattr(qr, "OUTPUT_ROOT", isolated_env["output_root"])
    monkeypatch.setattr(qr, "DELIVERY_ROOT", isolated_env["delivery_dir"])
    monkeypatch.setattr(qr, "HOST_SHARED_VOICE_ROOT", isolated_env["voice_root"])
    qr.ensure_retry_triggers()

    conn = sqlite3.connect(isolated_env["db"])
    conn.execute("INSERT INTO voice (id, origin_audio_path, asr_format_audio_url, created_at) VALUES (1, 'origin_audio/a.wav', 'missing.wav', 1)")
    conn.execute("INSERT INTO f2f_model (id, name, video_path, audio_path, voice_id, created_at) VALUES (1, '缺失模特', 'not_exist.mp4', 'origin_audio/a.wav', 1, 1)")
    conn.execute("INSERT INTO video (id, status, model_id, created_at, name, text_content, voice_id) VALUES (10, 'waiting', 1, 1, '测试缺失底模', '你好世界', 1)")
    conn.execute("INSERT INTO studio_job_meta (video_id, max_retries, updated_at) VALUES (10, 2, 1)")
    conn.commit()
    conn.close()

    # 1) 暂停状态下，run_once 不应出队
    pause_file = qr.get_pause_file()
    pause_file.parent.mkdir(parents=True, exist_ok=True)
    pause_file.write_text("paused", encoding="utf-8")
    assert qr.is_queue_paused() is True
    monkeypatch.setattr(qr, "sync_pending_from_backend", lambda: False)
    qr.run_once()
    row = qr.fetch_row("SELECT status FROM video WHERE id=10")
    assert row["status"] == "waiting"

    # 2) 取消暂停后，缺失底模应在前置校验直接明确报错（不可重试），且不被 SQLite 触发器反弹回 waiting
    pause_file.unlink()
    qr.run_once()
    row = qr.fetch_row("SELECT status, message FROM video WHERE id=10")
    assert row["status"] == "failed"
    assert "[不可重试]" in row["message"]
    assert "缺失" in row["message"]
    meta = qr.get_job_meta(10)
    assert meta["error_code"] == "MODEL_VIDEO_MISSING"

    # 3) 瞬时重试上限校验（max_retries = 2）
    conn = sqlite3.connect(isolated_env["db"])
    conn.execute("INSERT INTO video (id, status, model_id, created_at, name, text_content, voice_id) VALUES (11, 'pending', 1, 1, '重试上限测试', '你好', 1)")
    conn.execute("INSERT INTO studio_job_meta (video_id, retry_count, max_retries, updated_at) VALUES (11, 0, 2, 1)")
    conn.commit()
    conn.close()

    qr.maybe_recover_retryable_video_error(11, RuntimeError("connection refused"))
    assert qr.fetch_row("SELECT status FROM video WHERE id=11")["status"] == "waiting"
    assert qr.get_job_meta(11)["retry_count"] == 1

    qr.maybe_recover_retryable_video_error(11, RuntimeError("connection refused"))
    assert qr.fetch_row("SELECT status FROM video WHERE id=11")["status"] == "waiting"
    assert qr.get_job_meta(11)["retry_count"] == 2

    # 第 3 次触发应停止重试并稳定停留在 failed（不能被 SQLite 触发器拉回 waiting）
    qr.maybe_recover_retryable_video_error(11, RuntimeError("connection refused"))
    row11 = qr.fetch_row("SELECT status, message FROM video WHERE id=11")
    assert row11["status"] == "failed"
    assert "[已达重试上限]" in row11["message"]


def test_category_delivery_subfolder(isolated_env, monkeypatch):
    import core.heygem_queue_runner as qr

    monkeypatch.setattr(qr, "DB_PATH", isolated_env["db"])
    monkeypatch.setattr(qr, "OUTPUT_ROOT", isolated_env["output_root"])
    monkeypatch.setattr(qr, "DELIVERY_ROOT", isolated_env["delivery_dir"])

    src = isolated_env["output_root"] / "abc12345-r.mp4"
    _make_tiny_mp4(src)

    conn = sqlite3.connect(isolated_env["db"])
    cur = conn.execute("INSERT INTO studio_categories (name, color, sort_order, created_at) VALUES ('农资科普', '#10b981', 0, 1)")
    cat_id = cur.lastrowid
    conn.execute("INSERT INTO studio_job_meta (video_id, category_id, updated_at) VALUES (99, ?, 1)", (cat_id,))
    conn.commit()
    conn.close()

    delivered = qr.deliver_video_copy(99, "春季小麦防病", "abc12345", src)
    assert delivered is not None and delivered.is_file()
    assert delivered.parent.name == "农资科普"
    assert qr.get_job_meta(99)["delivered_path"] == str(delivered)


def test_indextts2_adapter_params_and_flask_endpoints(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "adapter"))
    import heygem_index_tts2_adapter as adapter

    monkeypatch.setattr(adapter, "CONFIG_PATH", tmp_path / "config.json")
    client = adapter.app.test_client()

    # Capabilities & config CRUD
    r = client.get("/v1/tts/capabilities")
    assert r.status_code == 200
    assert len(r.get_json()["emo_vector_labels"]) == 8

    r2 = client.put("/v1/tts/config", json={"index_tts_diffusion_steps": 18, "index_tts_speed": 1.12})
    assert r2.status_code == 200
    assert r2.get_json()["config"]["index_tts_diffusion_steps"] == 18

    # Effective parameter merging: explicit num_beams disables auto_fast
    cfg = adapter._load_config()
    long_text = "这是一个用于验证参数合并的长句。" * 40
    params_auto = adapter._resolve_effective_params(cfg, {"text": long_text}, long_text)
    assert params_auto["profile_name"] == "fast"
    params_explicit = adapter._resolve_effective_params(
        cfg,
        {"text": long_text, "tts_params": {"num_beams": 4, "diffusion_steps": 30, "emo_mode": "emo_vector", "emo_vector": [0.5, 0, 0, 0, 0, 0, 0, 0], "speed": 1.2}},
        long_text,
    )
    assert params_explicit["profile_name"] == "custom"
    assert params_explicit["num_beams"] == 4
    assert params_explicit["diffusion_steps"] == 30
    assert params_explicit["emo_mode"] == "emo_vector"
    assert abs(params_explicit["duration_factor"] - round(1 / 1.2, 4)) < 1e-3

    # Edge cases: empty text -> 400; missing audio -> 400; cancel idle -> 200
    assert client.post("/v1/invoke", json={"text": "   "}).status_code == 400
    assert client.post("/v1/invoke", json={"text": "你好", "reference_audio": "Z:/no_such_audio.wav"}).status_code == 400
    assert client.post("/v1/tts/cancel").status_code == 200


def test_studio_fastapi_full_lifecycle(isolated_env):
    from studio.server import app

    with TestClient(app) as client:
        # 1. Index HTML & OpenAPI
        idx = client.get("/")
        assert idx.status_code == 200
        assert "数字人一体化工作台" in idx.text

        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        paths = openapi.json()["paths"]
        assert "/api/v1/jobs" in paths
        assert "/api/v1/tts/synthesize" in paths

        # 2. Health & Presets & Categories
        health = client.get("/api/v1/health").json()
        assert "overall" in health and "alerts" in health
        presets = client.get("/api/v1/tts/presets").json()["items"]
        assert len(presets) >= 3

        cat_res = client.post("/api/v1/categories", json={"name": "自动化测试分类", "color": "#22c55e"})
        assert cat_res.status_code == 200
        cat_id = cat_res.json()["item"]["id"]

        # 3. Import a real synthetic MP4 model via local path (tests ffmpeg voice extraction!)
        sample_mp4 = isolated_env["tmp"] / "avatar_sample.mp4"
        _make_tiny_mp4(sample_mp4)
        m_res = client.post("/api/v1/models", data={"name": "测试数字人A", "local_video_path": str(sample_mp4)})
        assert m_res.status_code == 200, m_res.text
        model = m_res.json()["item"]
        assert model["video_exists"] is True
        assert model["audio_exists"] is True

        # Stream model video with HTTP Range header
        stream_r = client.get(f"/api/v1/media/model/{model['id']}", headers={"Range": "bytes=0-1023"})
        assert stream_r.status_code in (200, 206)

        # 4. Upload audio asset + batch create jobs
        sample_wav = isolated_env["tmp"] / "voice_clip.wav"
        _make_tiny_wav(sample_wav)
        with open(sample_wav, "rb") as fh:
            up_res = client.post("/api/v1/audios/upload", data={"category_id": str(cat_id)}, files={"files": ("clip1.wav", fh, "audio/wav")})
        assert up_res.status_code == 200, up_res.text
        audio_id = up_res.json()["items"][0]["id"]

        # Edge cases: invalid model -> 404; empty text & no audio -> 400
        assert client.post("/api/v1/jobs", json={"model_id": 99999, "text": "hello"}).status_code == 404
        assert client.post("/api/v1/jobs", json={"model_id": model["id"]}).status_code == 400

        # Create text+tts_params job with auto category creation
        j1 = client.post(
            "/api/v1/jobs",
            json={
                "model_id": model["id"],
                "name": "AI文案测试",
                "category_name": "AI自动新建分类",
                "text": "大家好，这是自动化接口测试。",
                "tts_params": {"model_version": "IndexTTS-2", "speed": 1.05, "diffusion_steps": 15},
            },
        )
        assert j1.status_code == 200, j1.text
        job1_id = j1.json()["job_id"]
        assert j1.json()["item"]["category_name"] == "AI自动新建分类"
        assert j1.json()["item"]["tts_params"]["speed"] == 1.05

        # Batch create from audio asset
        jb = client.post(
            "/api/v1/jobs/batch",
            json={"items": [{"model_id": model["id"], "audio_asset_id": audio_id, "category_id": cat_id}]},
        )
        assert jb.status_code == 200
        job2_id = jb.json()["job_ids"][0]

        # 5. Cancel waiting job -> non-destructive cancelled -> Retry -> back to waiting
        c_res = client.post(f"/api/v1/jobs/{job1_id}/cancel")
        assert c_res.status_code == 200
        assert c_res.json()["item"]["status"] == "cancelled"

        r_res = client.post(f"/api/v1/jobs/{job1_id}/retry")
        assert r_res.status_code == 200
        assert r_res.json()["item"]["status"] == "waiting"

        # 6. Simulate completed MP4 and verify download & recategorization move
        out_mp4 = isolated_env["delivery_dir"] / f"{job2_id:04d}_clip1_deadbeef.mp4"
        _make_tiny_mp4(out_mp4)
        conn = sqlite3.connect(isolated_env["db"])
        conn.execute("UPDATE video SET status='success', progress=100, code='deadbeef' WHERE id=?", (job2_id,))
        conn.execute("UPDATE studio_job_meta SET delivered_path=? WHERE video_id=?", (str(out_mp4), job2_id))
        conn.commit()
        conn.close()

        dl = client.get(f"/api/v1/jobs/{job2_id}/download")
        assert dl.status_code == 200
        assert len(dl.content) > 1000
        assert "attachment" in dl.headers.get("content-disposition", "")

        # Move to category -> delivered file follows into subfolder
        mv = client.patch(f"/api/v1/jobs/{job2_id}", json={"category_id": cat_id})
        assert mv.status_code == 200
        moved_path = Path(mv.json()["item"]["paths"]["delivered_path"])
        assert moved_path.is_file() and moved_path.parent.name == "自动化测试分类"

        # 7. Pause & Resume Queue API
        assert client.post("/api/v1/queue/pause").json()["paused"] is True
        assert client.post("/api/v1/queue/resume").json()["paused"] is False

        # 8. Audio recategorization PATCH & Windows quoted local path support
        a_upd = client.patch(f"/api/v1/audios/{audio_id}", json={"category_id": None, "name": "重命名音频"})
        assert a_upd.status_code == 200
        assert a_upd.json()["item"]["name"] == "重命名音频"
        assert a_upd.json()["item"]["category_id"] is None

        quoted_mp4 = f'"{sample_mp4}"'
        m_q = client.post("/api/v1/models", data={"name": "引号路径模特", "local_video_path": quoted_mp4})
        assert m_q.status_code == 200, m_q.text
        assert m_q.json()["item"]["video_exists"] is True

        # 9. Retry on active pending job must be rejected (409), delete model with active job must be rejected (409)
        conn = sqlite3.connect(isolated_env["db"])
        conn.execute("UPDATE video SET status='pending', audio_path='gone_temp.wav' WHERE id=?", (job1_id,))
        conn.commit()
        conn.close()
        assert client.post(f"/api/v1/jobs/{job1_id}/retry").status_code == 409
        assert client.delete(f"/api/v1/models/{model['id']}").status_code == 409

        # 10. Retry on failed text job with missing temp audio clears stale audio_path
        conn = sqlite3.connect(isolated_env["db"])
        conn.execute("UPDATE video SET status='failed', audio_path='gone_temp.wav' WHERE id=?", (job1_id,))
        conn.commit()
        conn.close()
        r_retry = client.post(f"/api/v1/jobs/{job1_id}/retry")
        assert r_retry.status_code == 200
        assert r_retry.json()["item"]["audio_path"] == ""


def test_real_gen_video_log_telemetry_and_oom_isolation():
    import core.heygem_queue_runner as qr

    old_code = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cur_code = "17d98c8b-e780-47d6-b963-6eb2bc4a5339"
    # 包含上一条旧任务的 CUDA OOM + 当前 60fps 任务的 av_transfer 预读取（不能误算为已渲染！）+ 真实 avi write success
    realistic_logs = f"""
2026-11-22 11:09:00,000-INFO: {old_code} -> failed
RuntimeError: CUDA out of memory. Tried to allocate 4.00 GiB
2026-11-22 11:11:08,691-INFO: {cur_code} -> video_info:[60.0/2160/3840/4.0], audio_info:[3.0]
2026-11-22 11:11:15,598-INFO: {cur_code} ->get_aud_feat1 success
2026-11-22 11:11:27,666-INFO: {cur_code} -> av_transfer -> batch_size_count:89/2, current_idx:180
"""
    tel_pre = qr.parse_gen_video_render_telemetry(cur_code, estimated_total_frames=90, log_text=realistic_logs)
    assert tel_pre["total_frames"] == 180
    assert tel_pre["rendered_frames"] == 0
    assert float(tel_pre["progress_float"]) < 30.0
    assert tel_pre["container_error"] == ""  # 不应被历史 OOM 污染

    with_avi = realistic_logs + f"""
2026-11-22 11:11:28,689-INFO: {cur_code} -> [0] avi write success, current_idx: 2.
2026-11-22 11:11:34,689-INFO: {cur_code} -> [44] avi write success, current_idx: 90.
"""
    tel_mid = qr.parse_gen_video_render_telemetry(cur_code, estimated_total_frames=90, log_text=with_avi)
    assert tel_mid["total_frames"] == 180
    assert tel_mid["rendered_frames"] == 90
    assert 55.0 <= float(tel_mid["progress_float"]) <= 62.0
    assert float(tel_mid["render_fps"]) > 10.0
    assert tel_mid["container_error"] == ""


def test_emotion_normalization_and_strict_emo_audio_and_reset_stuck_rows(isolated_env, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "adapter"))
    import heygem_index_tts2_adapter as adapter
    import indextts2_studio_worker as worker
    import core.heygem_queue_runner as qr

    # 1) 8维情感向量超 0.8 自动归一化
    norm = worker._normalize_emo_vector([0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.0])
    assert abs(sum(norm) - 0.8) < 1e-3

    # 2) emo_mode=emo_audio 但文件缺失必须显式返回 400（禁止静默回退）
    client = adapter.app.test_client()
    tiny_ref = isolated_env["voice_root"] / "origin_audio" / "format_test_ref.wav"
    _make_tiny_wav(tiny_ref)
    resp = client.post(
        "/v1/invoke",
        json={
            "text": "测试情感参考音缺失报错",
            "reference_audio": str(tiny_ref),
            "chunk_length": "not_a_number",
            "tts_params": {"emo_mode": "emo_audio", "emo_audio_prompt": "Z:/missing_emo.wav"},
        },
    )
    assert resp.status_code == 400

    # 3) reset_stuck_rows 应包含 name 字段，且识别中文 TTS 阶段不提前 180s 误杀
    monkeypatch.setattr(qr, "DB_PATH", isolated_env["db"])
    monkeypatch.setattr(qr, "OUTPUT_ROOT", isolated_env["output_root"])
    monkeypatch.setattr(qr, "DELIVERY_ROOT", isolated_env["delivery_dir"])
    now_ms = int(time.time() * 1000)
    old_240s_ms = now_ms - 240 * 1000
    old_timeout_ms = now_ms - (qr.PENDING_WITH_CODE_TIMEOUT_SECONDS + 120) * 1000

    out_done = isolated_env["output_root"] / "11111111-2222-3333-4444-555555555555-r.mp4"
    _make_tiny_mp4(out_done)

    conn = sqlite3.connect(isolated_env["db"])
    conn.execute(
        "INSERT INTO video (id, status, message, model_id, param, created_at, name) VALUES (201, 'pending', '正在合成第 2/4 段语音...', 1, ?, ?, '长文配音任务')",
        (json.dumps({"_run_started_at_ms": old_240s_ms, "_queue_owner": "runner"}), old_240s_ms),
    )
    conn.execute("INSERT INTO studio_job_meta (video_id, phase, updated_at) VALUES (201, 'tts_synthesizing', ?)", (now_ms,))
    conn.execute(
        "INSERT INTO video (id, status, message, model_id, code, param, created_at, name) VALUES (202, 'pending', '渲染中', 1, '11111111-2222-3333-4444-555555555555', ?, ?, '已出片恢复任务')",
        (json.dumps({"_run_started_at_ms": old_timeout_ms, "_queue_owner": "runner"}), old_timeout_ms),
    )
    conn.commit()
    conn.close()

    qr.reset_stuck_rows()
    assert qr.fetch_row("SELECT status FROM video WHERE id=201")["status"] == "pending"
    assert qr.fetch_row("SELECT status FROM video WHERE id=202")["status"] == "success"


def test_system_ctrl_docker_autostart_guard_and_recover_endpoint(isolated_env, monkeypatch):
    from studio import system_ctrl
    from studio.server import app

    # 1) start_docker_services 在 Docker 未运行时应自动拉起 Docker Desktop 并等待就绪
    fake_desktop = isolated_env["tmp"] / "Docker Desktop.exe"
    fake_desktop.write_bytes(b"MZ")
    daemon_checks = {"n": 0}
    popen_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_daemon_ready(timeout: float = 5.0) -> bool:
        daemon_checks["n"] += 1
        # 前两次返回 False（未就绪 -> 触发拉起），第 3 次返回 True（已就绪）
        return daemon_checks["n"] >= 3

    class FakeProc:
        pid = 42424
        def poll(self):
            return None

    class FakeCompleted:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(system_ctrl, "_is_docker_daemon_ready", fake_daemon_ready)
    monkeypatch.setattr(system_ctrl, "_resolve_docker_desktop_exe", lambda: fake_desktop)
    monkeypatch.setattr(system_ctrl.time, "sleep", lambda _s: None)
    monkeypatch.setattr(system_ctrl.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(list(cmd)), FakeProc())[1])
    monkeypatch.setattr(system_ctrl.subprocess, "run", lambda cmd, **kw: (run_calls.append(list(cmd)), FakeCompleted())[1])
    monkeypatch.setattr(system_ctrl, "_http_get_json", lambda url, timeout=2.5: {"code": 10004})

    ok, detail = system_ctrl.start_docker_services(restart_gen_video=False)
    assert ok is True, detail
    assert any("Docker Desktop.exe" in " ".join(c) for c in popen_calls)
    assert any(c[:3] == ["docker", "compose", "up"] for c in run_calls)

    # 2) Docker Desktop 缺失时给出明确失败原因
    monkeypatch.setattr(system_ctrl, "_is_docker_daemon_ready", lambda timeout=5.0: False)
    monkeypatch.setattr(system_ctrl, "_resolve_docker_desktop_exe", lambda: None)
    ok_miss, detail_miss = system_ctrl.start_docker_services(restart_gen_video=False)
    assert ok_miss is False
    assert "Docker Desktop" in detail_miss

    # 3) self_heal_recover 必须同时拉起队列调度器与 Guard 守护进程
    guard_called = {"n": 0}
    monkeypatch.setattr(system_ctrl, "get_docker_status", lambda: {"daemon_ok": True, "f2f_api_ok": True, "containers": {}})
    monkeypatch.setattr(system_ctrl, "start_tts_adapter_silent", lambda wait_seconds=25: (True, "tts ok"))
    monkeypatch.setattr(system_ctrl, "start_queue_daemon_silent", lambda: (True, "daemon ok"))
    monkeypatch.setattr(
        system_ctrl,
        "start_queue_guard_silent",
        lambda: (guard_called.__setitem__("n", guard_called["n"] + 1), (True, "guard ok"))[1],
    )
    monkeypatch.setattr(system_ctrl, "get_tts_service_status", lambda: {"mode": "index-tts2", "ok": True, "live_status": {"state": "idle"}})
    monkeypatch.setattr(system_ctrl, "get_system_health", lambda force=False: {"overall": "healthy", "alerts": []})

    res = system_ctrl.self_heal_recover(restart_render_container=False, requeue_stuck=False)
    assert res["ok"] is True
    assert guard_called["n"] == 1
    assert any("Guard" in s["step"] for s in res["steps"])

    # 4) /api/v1/system/recover 无请求体、空对象、显式参数均应正常（消除可变默认参数）
    captured_args: list[tuple[bool, bool]] = []
    monkeypatch.setattr(
        system_ctrl,
        "self_heal_recover",
        lambda restart_render_container=False, requeue_stuck=True: (
            captured_args.append((restart_render_container, requeue_stuck)),
            {"ok": True, "steps": []},
        )[1],
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/system/recover").status_code == 200
        assert client.post("/api/v1/system/recover", json={}).status_code == 200
        assert client.post("/api/v1/system/recover", json={"restart_render_container": True, "requeue_stuck": False}).status_code == 200
    assert captured_args == [(False, True), (False, True), (True, False)]


def test_indextts2_concurrent_validation_isolation_and_cancel_window(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "adapter"))
    import heygem_index_tts2_adapter as adapter

    monkeypatch.setattr(adapter, "CONFIG_PATH", tmp_path / "config.json")
    client = adapter.app.test_client()

    # 模拟任务 A 正在持锁合成中（尚未启动子进程或子进程刚准备拉起）
    adapter._GENERATE_LOCK.acquire()
    try:
        adapter._ACTIVE_PROC = None
        adapter._CANCEL_EVENT.clear()
        adapter._update_status(
            state="synthesizing",
            job_id="active99",
            stage="synthesizing",
            progress=48,
            message="正在合成第 2/4 段",
            last_error=None,
        )

        # 并发无效请求 B 触发 400（缺音频 / 缺情感音 / 空文案）绝不能覆盖活跃任务状态
        r_bad1 = client.post("/v1/invoke", json={"text": "测试串扰", "reference_audio": "Z:/missing_ref_xyz.wav"})
        assert r_bad1.status_code == 400
        r_bad2 = client.post("/v1/invoke", json={"text": "   "})
        assert r_bad2.status_code == 400

        snap = adapter._snapshot_status()
        assert snap["state"] == "synthesizing"
        assert snap["job_id"] == "active99"
        assert snap["progress"] == 48
        assert snap["last_error"] is None

        # 在锁已被占用但 _ACTIVE_PROC 仍为 None 的极短准备窗口内调用 /v1/tts/cancel，必须准确返回已下发中断
        r_cancel = client.post("/v1/tts/cancel")
        assert r_cancel.status_code == 200
        body = r_cancel.get_json()
        assert body["cancelled"] is True
        assert body["killed_process"] is False
        assert "中断" in body["msg"]
        assert "没有正在执行" not in body["msg"]
        assert adapter._CANCEL_EVENT.is_set() is True
        assert adapter._snapshot_status()["state"] == "cancelling"
    finally:
        adapter._CANCEL_EVENT.clear()
        adapter._SYNTH_ACTIVE = False
        adapter._GENERATE_LOCK.release()

    # 锁释放且无活跃子进程时调用 /v1/tts/cancel，返回无活跃任务
    r_idle = client.post("/v1/tts/cancel")
    assert r_idle.status_code == 200
    assert r_idle.get_json()["cancelled"] is False
    assert "没有正在执行" in r_idle.get_json()["msg"]
    assert adapter._CANCEL_EVENT.is_set() is False


def test_jobs_preflight_validation_batch_atomicity_and_local_audio_import(isolated_env):
    from studio.server import app

    sample_mp4 = isolated_env["tmp"] / "valid_model.mp4"
    sample_wav = isolated_env["tmp"] / "valid_audio.wav"
    emo_wav = isolated_env["tmp"] / "emo_ref.wav"
    _make_tiny_mp4(sample_mp4)
    _make_tiny_wav(sample_wav)
    _make_tiny_wav(emo_wav)

    with TestClient(app) as client:
        # 导入一个完整有效模特
        m_ok = client.post("/api/v1/models", data={"name": "完好模特", "local_video_path": str(sample_mp4)}).json()["item"]

        # 构造底模缺失的模特与音色缺失的模特
        conn = sqlite3.connect(isolated_env["db"])
        conn.execute("INSERT INTO voice (id, origin_audio_path, asr_format_audio_url, created_at) VALUES (501, 'origin_audio/gone.wav', 'Z:/gone_voice.wav', 1)")
        conn.execute("INSERT INTO f2f_model (id, name, video_path, audio_path, voice_id, created_at) VALUES (501, '缺底模模特', 'missing_video.mp4', 'origin_audio/gone.wav', ?, 1)", (m_ok["voice_id"],))
        conn.execute("INSERT INTO f2f_model (id, name, video_path, audio_path, voice_id, created_at) VALUES (502, '缺音色模特', ?, 'origin_audio/gone.wav', 501, 1)", (m_ok["video_path"],))
        conn.commit()
        conn.close()

        # 1) 前置校验：底模视频文件不存在 -> 400
        r_no_vid = client.post("/api/v1/jobs", json={"model_id": 501, "text": "测试缺失底模"})
        assert r_no_vid.status_code == 400
        assert "底模视频文件缺失" in r_no_vid.json()["detail"]

        # 2) 前置校验：文本模式参考音色文件不存在 -> 400
        r_no_voice = client.post("/api/v1/jobs", json={"model_id": 502, "text": "测试缺失音色"})
        assert r_no_voice.status_code == 400
        assert "参考音色" in r_no_voice.json()["detail"]

        # 3) 前置校验：emo_mode="emo_audio" 时情感参考音为空或缺失 -> 400；存在（含 Windows 双引号）-> 200
        r_no_emo = client.post(
            "/api/v1/jobs",
            json={"model_id": m_ok["id"], "text": "测试缺情感音", "tts_params": {"emo_mode": "emo_audio", "emo_audio_prompt": ""}},
        )
        assert r_no_emo.status_code == 400

        r_miss_emo = client.post(
            "/api/v1/jobs",
            json={"model_id": m_ok["id"], "text": "测试假情感音", "tts_params": {"emo_mode": "emo_audio", "emo_audio_prompt": "Z:/no_such_emo.wav"}},
        )
        assert r_miss_emo.status_code == 400

        r_ok_emo = client.post(
            "/api/v1/jobs",
            json={"model_id": m_ok["id"], "text": "测试有效情感音", "tts_params": {"emo_mode": "emo_audio", "emo_audio_prompt": f'"{emo_wav}"'}},
        )
        assert r_ok_emo.status_code == 200, r_ok_emo.text
        assert r_ok_emo.json()["item"]["tts_params"]["emo_audio_prompt"] == str(emo_wav).replace("\\", "/")

        # 4) 批量接口原子性：第 2 条校验失败时，第 1 条与第 3 条绝不能残留入队，且不创建分类与临时音频
        before_jobs = len(client.get("/api/v1/jobs").json()["items"])
        before_temp_wavs = set(isolated_env["output_root"].glob("studio_*.wav"))

        r_batch_fail = client.post(
            "/api/v1/jobs/batch",
            json={
                "items": [
                    {"model_id": m_ok["id"], "name": "批量有效1", "audio_path": str(sample_wav), "category_name": "绝不应残留的原子分类"},
                    {"model_id": m_ok["id"], "name": "批量无效2", "text": "坏情感音", "tts_params": {"emo_mode": "emo_audio", "emo_audio_prompt": "Z:/bad.wav"}},
                    {"model_id": m_ok["id"], "name": "批量有效3", "text": "正常文案"},
                ]
            },
        )
        assert r_batch_fail.status_code == 400
        assert "第 2 条任务" in r_batch_fail.json()["detail"]

        after_jobs = len(client.get("/api/v1/jobs").json()["items"])
        after_temp_wavs = set(isolated_env["output_root"].glob("studio_*.wav"))
        cat_names = [c["name"] for c in client.get("/api/v1/categories").json()["items"]]
        assert after_jobs == before_jobs
        assert after_temp_wavs == before_temp_wavs
        assert "绝不应残留的原子分类" not in cat_names

        # 5) 本机路径音频快速导入（支持双引号清洗 + 原子校验）
        r_imp_fail = client.post("/api/v1/audios/import-local", json={"paths": [f'"{sample_wav}"', "Z:/missing_local.wav"]})
        assert r_imp_fail.status_code == 404
        assert client.get("/api/v1/audios").json()["total"] == 0

        r_imp_ok = client.post("/api/v1/audios/import-local", json={"paths": [f'"{sample_wav}"']})
        assert r_imp_ok.status_code == 200, r_imp_ok.text
        assert r_imp_ok.json()["count"] == 1
        assert r_imp_ok.json()["items"][0]["source_type"] == "local_import"

        # 6) 预设保存与删除 + 模特/作品重命名
        p_new = client.post("/api/v1/tts/presets", json={"name": "临时回归预设", "params": {"speed": 1.1}}).json()["item"]
        assert client.delete(f"/api/v1/tts/presets/{p_new['id']}").status_code == 200
        assert all(p["id"] != p_new["id"] for p in client.get("/api/v1/tts/presets").json()["items"])

        m_ren = client.patch(f"/api/v1/models/{m_ok['id']}", json={"name": "已重命名模特"})
        assert m_ren.status_code == 200 and m_ren.json()["item"]["name"] == "已重命名模特"
        j_ren = client.patch(f"/api/v1/jobs/{r_ok_emo.json()['job_id']}", json={"name": "已重命名作品"})
        assert j_ren.status_code == 200 and j_ren.json()["item"]["name"] == "已重命名作品"


def test_queue_recovery_meta_webhook_and_backend_error_codes(isolated_env, monkeypatch):
    import http.server
    import threading
    import core.heygem_queue_runner as qr

    monkeypatch.setattr(qr, "DB_PATH", isolated_env["db"])
    monkeypatch.setattr(qr, "OUTPUT_ROOT", isolated_env["output_root"])
    monkeypatch.setattr(qr, "DELIVERY_ROOT", isolated_env["delivery_dir"])
    qr.ensure_retry_triggers()

    # 启动真实本地 Webhook 接收器，验证 HTTP POST 回调内容与去重标记
    received_webhooks: list[dict] = []

    class HookHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            received_webhooks.append(json.loads(raw))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), HookHandler)
    hook_url = f"http://127.0.0.1:{srv.server_port}/hook"
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()

    try:
        now_ms = int(time.time() * 1000)
        old_timeout_ms = now_ms - (qr.PENDING_WITH_CODE_TIMEOUT_SECONDS + 120) * 1000

        code1 = "11111111-1111-1111-1111-111111111111"
        code2 = "22222222-2222-2222-2222-222222222222"
        code3 = "33333333-3333-3333-3333-333333333333"
        code4 = "44444444-4444-4444-4444-444444444444"
        code5 = "55555555-5555-5555-5555-555555555555"

        for c in (code1, code2, code3):
            _make_tiny_mp4(isolated_env["output_root"] / f"{c}-r.mp4")

        conn = sqlite3.connect(isolated_env["db"])
        # 分支 1：reset_stuck_rows 超时 pending 文件恢复
        conn.execute(
            "INSERT INTO video (id, status, message, model_id, code, param, created_at, name) VALUES (301, 'pending', '渲染中', 1, ?, ?, ?, '恢复分支1')",
            (code1, json.dumps({"_run_started_at_ms": old_timeout_ms, "_queue_owner": "runner"}), old_timeout_ms),
        )
        conn.execute(
            "INSERT INTO studio_job_meta (video_id, phase, progress_float, webhook_url, webhook_notified, updated_at) VALUES (301, 'face2face_rendering', 50.0, ?, 0, ?)",
            (hook_url, now_ms),
        )
        conn.commit()
        conn.close()

        qr.reset_stuck_rows()
        assert qr.fetch_row("SELECT status, progress FROM video WHERE id=301")["status"] == "success"
        m301 = qr.get_job_meta(301)
        assert m301["phase"] == "success"
        assert float(m301["progress_float"]) == 100.0
        assert Path(str(m301["delivered_path"])).is_file()
        assert int(m301["webhook_notified"]) == 1

        # 分支 2（10004 文件恢复）& 分支 3（10000/status=1 文件恢复）& 10002 非可重试失败 & 9999 可重试失败上限
        conn = sqlite3.connect(isolated_env["db"])
        for vid, code, name, max_r in [
            (302, code2, "恢复分支2_10004", 2),
            (303, code3, "恢复分支3_status1", 2),
            (304, code4, "错误码10002永久失败", 2),
            (305, code5, "错误码9999重试上限", 1),
        ]:
            conn.execute(
                "INSERT INTO video (id, status, message, model_id, code, param, created_at, name) VALUES (?, 'pending', '渲染中', 1, ?, ?, ?, ?)",
                (vid, code, json.dumps({"_submitted_at_ms": now_ms, "_queue_owner": "runner"}), now_ms, name),
            )
            conn.execute(
                "INSERT INTO studio_job_meta (video_id, phase, progress_float, retry_count, max_retries, webhook_url, webhook_notified, updated_at) VALUES (?, 'face2face_rendering', 40.0, 0, ?, ?, 0, ?)",
                (vid, max_r, hook_url, now_ms),
            )
        conn.commit()
        conn.close()

        def fake_http_get_json(url: str, timeout: int = 20):
            if code2 in url:
                return {"code": 10004, "msg": "task not found"}
            if code3 in url:
                return {"code": 10000, "data": {"status": 1, "progress": 80, "msg": "处理中"}}
            if code4 in url:
                return {"code": 10002, "msg": "bad video codec"}
            if code5 in url:
                return {"code": 9999, "msg": "connection refused"}
            return {"code": 10004}

        monkeypatch.setattr(qr, "http_get_json", fake_http_get_json)
        qr.sync_pending_from_backend()

        # 验证分支 2 与分支 3 均同步了 meta 与 delivered_path
        for vid in (302, 303):
            assert qr.fetch_row("SELECT status FROM video WHERE id=?", (vid,))["status"] == "success"
            meta = qr.get_job_meta(vid)
            assert meta["phase"] == "success"
            assert float(meta["progress_float"]) == 100.0
            assert Path(str(meta["delivered_path"])).is_file()
            assert int(meta["webhook_notified"]) == 1

        # 验证 10002 走了 _fail_video_permanently 与 job.failed Webhook
        r304 = qr.fetch_row("SELECT status, message FROM video WHERE id=304")
        assert r304["status"] == "failed" and "[不可重试]" in r304["message"]
        m304 = qr.get_job_meta(304)
        assert m304["phase"] == "failed" and m304["error_code"] == "BACKEND_10002"
        assert int(m304["webhook_notified"]) == 1

        # 验证 9999 第 1 次进入 waiting 且 retry_count=1
        assert qr.fetch_row("SELECT status FROM video WHERE id=305")["status"] == "waiting"
        assert int(qr.get_job_meta(305)["retry_count"]) == 1

        # 再次放回 pending 触发第 2 次 9999 -> 达到 max_retries=1 上限，永久失败并触发 job.failed Webhook
        qr.update_video(305, status="pending", code=code5, param=json.dumps({"_submitted_at_ms": now_ms, "_queue_owner": "runner"}))
        qr.sync_pending_from_backend()
        r305 = qr.fetch_row("SELECT status, message FROM video WHERE id=305")
        assert r305["status"] == "failed" and "[已达重试上限]" in r305["message"]
        m305 = qr.get_job_meta(305)
        assert m305["error_code"] == "MAX_RETRIES_EXCEEDED"
        assert int(m305["webhook_notified"]) == 1

        # 核对收到的全部 5 个 Webhook 事件
        events_by_vid = {int(h["video_id"]): h["event"] for h in received_webhooks}
        assert events_by_vid == {
            301: "job.completed",
            302: "job.completed",
            303: "job.completed",
            304: "job.failed",
            305: "job.failed",
        }
    finally:
        srv.shutdown()
        srv.server_close()


def test_review_round2_real_guard_no_self_match_and_docker_stop_graceful(isolated_env, monkeypatch):
    from studio import system_ctrl

    # 1) 真实调用 PowerShell/CIM 查询（严禁 Mock），绝不可把查询进程自身匹配为运行中的 Guard！
    fake_pids = system_ctrl._find_processes_by_cmdline(
        ["powershell.exe", "pwsh.exe"],
        "heygem-queue-guard-nonexistent-sentinel.ps1",
        use_cache=False,
    )
    assert fake_pids == []

    # 2) 非法 HEYGEM_DOCKER_READY_TIMEOUT 不应抛 ValueError
    monkeypatch.setenv("HEYGEM_DOCKER_READY_TIMEOUT", "not-an-int")
    monkeypatch.setattr(system_ctrl, "_is_docker_daemon_ready", lambda timeout=5.0: False)
    monkeypatch.setattr(system_ctrl, "_resolve_docker_desktop_exe", lambda: None)
    ok, detail = system_ctrl._ensure_docker_desktop_ready()
    assert ok is False and "Docker Desktop" in detail

    # 3) Docker 未运行时 stop_background_services 应优雅跳过容器停止并返回 ok=True
    monkeypatch.setattr(system_ctrl, "_find_processes_by_cmdline", lambda *a, **kw: [])
    monkeypatch.setattr(system_ctrl, "_find_python_processes", lambda *a, **kw: [])
    monkeypatch.setattr(system_ctrl, "_read_live_pid_file", lambda _p: None)
    stop_res = system_ctrl.stop_background_services()
    assert stop_res["ok"] is True
    assert any("跳过容器停止" in s["detail"] for s in stop_res["steps"])


def test_review_round2_adapter_cancel_no_state_regression_and_dir_type_validation(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "adapter"))
    import heygem_index_tts2_adapter as adapter

    monkeypatch.setattr(adapter, "CONFIG_PATH", tmp_path / "config.json")
    client = adapter.app.test_client()

    # 1) 持锁状态下传入目录路径与非字符串参数 -> 400 且不污染活跃状态
    adapter._GENERATE_LOCK.acquire()
    try:
        with adapter._PROC_LOCK:
            adapter._SYNTH_GEN += 1
            adapter._SYNTH_ACTIVE = True
            adapter._ACTIVE_PROC = None
            adapter._CANCEL_EVENT.clear()
        adapter._update_status(state="synthesizing", job_id="job_iso", progress=62, message="合成中", last_error=None)

        assert client.post("/v1/invoke", json={"text": "目录测试", "reference_audio": str(tmp_path)}).status_code == 400
        assert client.post("/v1/invoke", json={"text": 12345, "reference_audio": "x.wav"}).status_code == 400
        assert client.post("/v1/invoke", json={"text": "类型测试", "reference_audio": ["not", "str"]}).status_code == 400

        snap = adapter._snapshot_status()
        assert snap["state"] == "synthesizing" and snap["job_id"] == "job_iso" and snap["progress"] == 62

        # 2) 模拟竞态：在 _terminate_active_proc 期间，合成线程已响应取消、落入 cancelled 并释放活跃标记
        def simulate_worker_finished_cancel() -> bool:
            adapter._update_status(state="cancelled", stage="cancelled", message="语音合成已被用户中断", progress=0)
            with adapter._PROC_LOCK:
                adapter._SYNTH_ACTIVE = False
            adapter._GENERATE_LOCK.release()
            return True

        monkeypatch.setattr(adapter, "_terminate_active_proc", simulate_worker_finished_cancel)
        r_cancel = client.post("/v1/tts/cancel")
        assert r_cancel.status_code == 200
        assert r_cancel.get_json()["cancelled"] is True
        # 最终状态必须保持 cancelled，绝不可被 /v1/tts/cancel 倒退覆写回 cancelling
        assert adapter._snapshot_status()["state"] == "cancelled"
    finally:
        with adapter._PROC_LOCK:
            adapter._SYNTH_ACTIVE = False
            adapter._CANCEL_EVENT.clear()
        if adapter._GENERATE_LOCK.locked():
            adapter._GENERATE_LOCK.release()


def test_review_round2_global_emo_audio_check_corrupt_import_rollback_and_blank_names(isolated_env, monkeypatch):
    from studio import server as studio_server
    from studio.server import app

    sample_mp4 = isolated_env["tmp"] / "m.mp4"
    sample_wav = isolated_env["tmp"] / "ok.wav"
    corrupt_mp3 = isolated_env["tmp"] / "corrupt.mp3"
    _make_tiny_mp4(sample_mp4)
    _make_tiny_wav(sample_wav)
    corrupt_mp3.write_bytes(b"NOT_A_VALID_AUDIO_STREAM_0000")

    with TestClient(app) as client:
        m_ok = client.post("/api/v1/models", data={"name": "正常模特", "local_video_path": str(sample_mp4)}).json()["item"]

        # 1) 全局 adapter/config.json 设默认 emo_mode=emo_audio 且文件缺失 -> 省略 emo_mode 的请求也必须前置 400 拦截
        monkeypatch.setattr(
            studio_server,
            "_load_adapter_config_fallback",
            lambda: {"index_tts_emo_mode": "emo_audio", "index_tts_emo_audio_prompt": "Z:/missing_global_emo.wav"},
        )
        r_single = client.post("/api/v1/jobs", json={"model_id": m_ok["id"], "text": "未显式传emo_mode"})
        assert r_single.status_code == 400
        assert "emo_audio" in r_single.json()["detail"]

        r_batch = client.post(
            "/api/v1/jobs/batch",
            json={"items": [{"model_id": m_ok["id"], "text": "批量省略emo_mode"}]},
        )
        assert r_batch.status_code == 400
        assert len(client.get("/api/v1/jobs").json()["items"]) == 0

        # 恢复正常默认配置
        monkeypatch.setattr(studio_server, "_load_adapter_config_fallback", lambda: {"index_tts_emo_mode": "same_as_speaker"})

        # 2) import-local 第 1 条合法 + 第 2 条损坏 MP3 -> 400 且磁盘零残留、DB 零残留
        audio_dir = isolated_env["data_dir"] / "studio_assets" / "audio"
        before_files = set(audio_dir.glob("*")) if audio_dir.exists() else set()
        r_corrupt = client.post("/api/v1/audios/import-local", json={"paths": [str(sample_wav), str(corrupt_mp3)]})
        assert r_corrupt.status_code == 400
        after_files = set(audio_dir.glob("*")) if audio_dir.exists() else set()
        assert after_files == before_files
        assert client.get("/api/v1/audios").json()["total"] == 0

        # 3) 空白名称均应返回 400 拒绝
        assert client.post("/api/v1/categories", json={"name": "   "}).status_code == 400
        assert client.post("/api/v1/model-groups", json={"name": "   "}).status_code == 400
        assert client.post("/api/v1/tts/presets", json={"name": "   ", "params": {}}).status_code == 400
        assert client.patch(f"/api/v1/models/{m_ok['id']}", json={"name": "   "}).status_code == 400

        j_ok = client.post("/api/v1/jobs", json={"model_id": m_ok["id"], "text": "正常任务"}).json()["item"]
        assert client.patch(f"/api/v1/jobs/{j_ok['id']}", json={"name": "   "}).status_code == 400

        a_ok = client.post("/api/v1/audios/import-local", json={"paths": [str(sample_wav)]}).json()["items"][0]
        assert client.patch(f"/api/v1/audios/{a_ok['id']}", json={"name": "   "}).status_code == 400

        # 4) /api/v1/tts/synthesize 不存在的 preset_id 返回 404
        r_bad_preset = client.post(
            "/api/v1/tts/synthesize",
            json={"text": "试听测试", "voice_id": m_ok["voice_id"], "preset_id": 99999},
        )
        assert r_bad_preset.status_code == 404


def test_review_round2_queue_zero_max_retries_and_retry_resets_stale_progress(isolated_env, monkeypatch):
    import core.heygem_queue_runner as qr

    monkeypatch.setattr(qr, "DB_PATH", isolated_env["db"])
    monkeypatch.setattr(qr, "OUTPUT_ROOT", isolated_env["output_root"])
    monkeypatch.setattr(qr, "DELIVERY_ROOT", isolated_env["delivery_dir"])
    qr.ensure_retry_triggers()

    now_ms = int(time.time() * 1000)
    code_zero = "66666666-6666-6666-6666-666666666666"
    code_prog = "77777777-7777-7777-7777-777777777777"

    conn = sqlite3.connect(isolated_env["db"])
    conn.execute(
        "INSERT INTO video (id, status, message, model_id, code, param, created_at, name, progress) VALUES (401, 'pending', '渲染中', 1, ?, ?, ?, '零重试任务', 50)",
        (code_zero, json.dumps({"_submitted_at_ms": now_ms, "_queue_owner": "runner"}), now_ms),
    )
    conn.execute(
        "INSERT INTO studio_job_meta (video_id, phase, progress_float, rendered_frames, eta_seconds, retry_count, max_retries, updated_at) VALUES (401, 'face2face_rendering', 50.0, 100, 20, 0, 0, ?)",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO video (id, status, message, model_id, code, param, created_at, name, progress) VALUES (402, 'pending', '渲染中', 1, ?, ?, ?, '重试清进度任务', 65)",
        (code_prog, json.dumps({"_submitted_at_ms": now_ms, "_queue_owner": "runner"}), now_ms),
    )
    conn.execute(
        "INSERT INTO studio_job_meta (video_id, phase, progress_float, rendered_frames, eta_seconds, retry_count, max_retries, updated_at) VALUES (402, 'face2face_rendering', 65.0, 120, 30, 0, 2, ?)",
        (now_ms,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(qr, "http_get_json", lambda url, timeout=20: {"code": 9999, "msg": "connection refused"})
    qr.sync_pending_from_backend()

    # 401 设置 max_retries=0：绝不能被 0 or 3 短路，第 1 次失败即永久失败
    r401 = qr.fetch_row("SELECT status, message FROM video WHERE id=401")
    assert r401["status"] == "failed" and "[已达重试上限]" in r401["message"]
    assert int(qr.get_job_meta(401)["retry_count"]) == 0

    # 402 自动重试回 waiting 时必须重置 progress_float/rendered_frames/eta_seconds
    r402 = qr.fetch_row("SELECT status, progress FROM video WHERE id=402")
    assert r402["status"] == "waiting" and int(r402["progress"]) == 0
    m402 = qr.get_job_meta(402)
    assert int(m402["retry_count"]) == 1
    assert float(m402["progress_float"]) == 0.0
    assert int(m402["rendered_frames"]) == 0
    assert int(m402["eta_seconds"]) == -1


def _make_silent_mp4(path: Path) -> None:
    """生成 1 秒纯画面无音轨短视频，用于验证静音底模视频导入容错。"""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=green:s=160x120:d=1:r=25",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and path.is_file(), f"ffmpeg silent failed: {res.stderr}"


def test_onboarding_discover_cleanup_missing_silent_and_safe_relink(isolated_env, monkeypatch):
    from studio import system_ctrl
    from studio.server import app

    shoot_dir = isolated_env["delivery_dir"] / "拍摄的视频"
    shoot_dir.mkdir(parents=True, exist_ok=True)
    v1 = shoot_dir / "福联测试.mp4"
    v2 = shoot_dir / "静音出镜.mp4"
    _make_tiny_mp4(v1)
    _make_silent_mp4(v2)
    (shoot_dir / "empty.mp4").write_bytes(b"")
    (shoot_dir / "说明.txt").write_text("ignore", encoding="utf-8")

    corrupt_mp4 = isolated_env["tmp"] / "broken.mp4"
    corrupt_mp4.write_bytes(b"NOT_A_VALID_VIDEO_FILE_123456")

    with TestClient(app) as client:
        # 1) 本地发现：只列出有效非空视频，排除空文件与文本文件
        d0 = client.get("/api/v1/models/discover-local").json()
        names = {it["name"] for it in d0["items"]}
        assert names == {"福联测试", "静音出镜"}
        assert all(it["already_imported"] is False for it in d0["items"])

        # 2) 省略 name 时自动取文件名导入，且 discover-local 立即标为 already_imported=True
        r_auto = client.post("/api/v1/models", data={"local_video_path": str(v1)})
        assert r_auto.status_code == 200, r_auto.text
        m1 = r_auto.json()["item"]
        assert m1["name"] == "福联测试"
        assert m1["video_exists"] is True and m1["audio_exists"] is True and m1["has_voice"] is True

        d1 = client.get("/api/v1/models/discover-local").json()
        by_name = {it["name"]: it for it in d1["items"]}
        assert by_name["福联测试"]["already_imported"] is True
        assert by_name["静音出镜"]["already_imported"] is False

        # 3) 静音无音轨视频导入成功不崩溃，audio_exists=False
        r_silent = client.post("/api/v1/models", data={"local_video_path": str(v2)})
        assert r_silent.status_code == 200, r_silent.text
        m_silent = r_silent.json()["item"]
        assert m_silent["name"] == "静音出镜"
        assert m_silent["video_exists"] is True and m_silent["audio_exists"] is False and m_silent["has_voice"] is False

        # 4) 损坏视频导入必须返回 400 且 temp 目录零残留
        before_temp = set(isolated_env["output_root"].glob("*"))
        r_bad = client.post("/api/v1/models", data={"name": "坏文件", "local_video_path": str(corrupt_mp4)})
        assert r_bad.status_code == 400
        assert set(isolated_env["output_root"].glob("*")) == before_temp

        # 5) 损坏视频 relink 到已有完好模特 -> 400 且原底模完好无损
        orig_bytes = Path(m1["video_abs_path"]).read_bytes()
        r_bad_relink = client.post(f"/api/v1/models/{m1['id']}/relink", data={"local_video_path": str(corrupt_mp4)})
        assert r_bad_relink.status_code == 400
        assert Path(m1["video_abs_path"]).read_bytes() == orig_bytes

        # 6) 构造 3 条缺失底模的旧模特（其中 1 条有 waiting 活跃任务）+ 1 条保留音色，验证 cleanup-missing 精准清理且护航活跃任务和声音库
        conn = sqlite3.connect(isolated_env["db"])
        now_ms = int(time.time() * 1000)
        conn.execute("INSERT INTO voice (id, origin_audio_path, asr_format_audio_url, created_at) VALUES (901, 'origin_audio/old.wav', 'old.wav', ?)", (now_ms,))
        conn.execute("INSERT INTO f2f_model (id, name, video_path, audio_path, voice_id, created_at) VALUES (901, '旧缺失1', 'gone1.mp4', 'origin_audio/old.wav', 901, ?)", (now_ms,))
        conn.execute("INSERT INTO f2f_model (id, name, video_path, audio_path, voice_id, created_at) VALUES (902, '旧缺失2', 'gone2.mp4', '', NULL, ?)", (now_ms,))
        conn.execute("INSERT INTO f2f_model (id, name, video_path, audio_path, voice_id, created_at) VALUES (903, '旧缺失活跃', 'gone3.mp4', '', NULL, ?)", (now_ms,))
        conn.execute("INSERT INTO video (id, status, model_id, created_at, name) VALUES (950, 'waiting', 903, ?, '占用活跃任务')", (now_ms,))
        conn.execute("INSERT INTO video (id, status, model_id, created_at, name) VALUES (951, 'success', 901, ?, '历史成片')", (now_ms,))
        conn.commit()
        conn.close()

        c_res = client.post("/api/v1/models/cleanup-missing")
        assert c_res.status_code == 200
        cdata = c_res.json()
        assert cdata["deleted_count"] == 2
        assert set(cdata["deleted_ids"]) == {901, 902}
        assert cdata["skipped_active_ids"] == [903]

        rem_ids = {it["id"] for it in client.get("/api/v1/models").json()["items"]}
        assert m1["id"] in rem_ids and m_silent["id"] in rem_ids and 903 in rem_ids
        assert 901 not in rem_ids and 902 not in rem_ids

        # 历史声音库与历史成片必须完好保留
        conn = sqlite3.connect(isolated_env["db"])
        assert conn.execute("SELECT COUNT(*) FROM voice WHERE id=901").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM video WHERE id=951").fetchone()[0] == 1
        conn.close()

        # 7) pick-file 参数校验与响应透传
        assert client.post("/api/v1/system/pick-file", json={"kind": "invalid"}).status_code == 422
        monkeypatch.setattr(system_ctrl, "pick_local_files", lambda kind="video", multi=False, initial_dir=None: {"ok": True, "cancelled": False, "paths": [str(v1)]})
        p_ok = client.post("/api/v1/system/pick-file", json={"kind": "video", "multi": False})
        assert p_ok.status_code == 200 and p_ok.json()["paths"] == [str(v1)]

        monkeypatch.setattr(system_ctrl, "pick_local_files", lambda kind="video", multi=False, initial_dir=None: {"ok": True, "cancelled": True, "paths": []})
        p_cancel = client.post("/api/v1/system/pick-file", json={"kind": "audio", "multi": True})
        assert p_cancel.status_code == 200 and p_cancel.json()["cancelled"] is True


def test_review_thumb_relink_voice_probe_hardening_and_local_guards(isolated_env, monkeypatch):
    import shutil

    from studio import server as studio_server
    from studio import system_ctrl
    from studio.server import app

    tmp = isolated_env["tmp"]
    silent_mp4 = tmp / "silent_src.mp4"
    talking_mov = tmp / "talking_src.mov"
    fake_jpg_mp4 = tmp / "still_image_as_video.mp4"
    fake_mp3_art_mp4 = tmp / "mp3_cover_as_video.mp4"
    bad_exe = tmp / "danger.exe"
    bad_exe.write_bytes(b"MZ_FAKE_EXE")

    _make_silent_mp4(silent_mp4)
    # 生成有声 .mov（验证 relink 换后缀清理旧文件 + 为原静音模特补提音色 + 缩略图刷新）
    res_mov = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=160x120:d=1:r=25",
            "-f", "lavfi", "-i", "sine=frequency=520:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(talking_mov),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert res_mov.returncode == 0 and talking_mov.is_file()

    # 生成伪装成 .mp4 的单帧 JPEG 静态图
    real_jpg = tmp / "pic.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=yellow:s=64x64:d=0.1", "-frames:v", "1", str(real_jpg)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    shutil.copy2(real_jpg, fake_jpg_mp4)

    # 生成带封面图 (attached_pic) 的 MP3 并改名为 .mp4
    mp3_with_art = tmp / "song.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-i", str(real_jpg),
            "-map", "0:a", "-map", "1:v",
            "-c:a", "libmp3lame", "-c:v", "mjpeg",
            "-disposition:v:0", "attached_pic",
            str(mp3_with_art),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    shutil.copy2(mp3_with_art, fake_mp3_art_mp4)

    with TestClient(app) as client:
        # 1) 静态图片与带封面图的 MP3 伪装为 .mp4 必须被 _probe_media_streams 拒之门外且零磁盘拷贝残留
        before_files = set(isolated_env["output_root"].glob("*"))
        assert client.post("/api/v1/models", data={"local_video_path": str(fake_jpg_mp4)}).status_code == 400
        assert client.post("/api/v1/models", data={"local_video_path": str(fake_mp3_art_mp4)}).status_code == 400
        assert set(isolated_env["output_root"].glob("*")) == before_files

        # 2) 导入静音视频 -> 获取 JPEG 缩略图成功并缓存
        r_m = client.post("/api/v1/models", data={"name": "静音模特", "local_video_path": str(silent_mp4)})
        assert r_m.status_code == 200
        m = r_m.json()["item"]
        assert m["thumb_url"].startswith(f"/api/v1/media/model/{m['id']}/thumb")
        assert m["has_voice"] is False and m["voice_id"] is None
        old_video_path = Path(m["video_abs_path"])
        assert old_video_path.is_file()

        r_thumb = client.get(m["thumb_url"])
        assert r_thumb.status_code == 200
        assert r_thumb.headers["content-type"].startswith("image/jpeg")
        assert r_thumb.content[:3] == b"\xff\xd8\xff"

        # 3) 用有声 .mov relink 静音模特 -> 自动补建音色、清理旧 .mp4 孤儿文件、缩略图刷新正常
        r_rel = client.post(f"/api/v1/models/{m['id']}/relink", data={"local_video_path": str(talking_mov)})
        assert r_rel.status_code == 200, r_rel.text
        m2 = r_rel.json()["item"]
        assert m2["video_exists"] is True
        assert m2["has_voice"] is True and m2["audio_exists"] is True and m2["voice_id"] is not None
        assert Path(m2["video_abs_path"]).suffix.lower() == ".mov"
        assert not old_video_path.exists()

        r_thumb2 = client.get(m2["thumb_url"])
        assert r_thumb2.status_code == 200 and r_thumb2.content[:3] == b"\xff\xd8\xff"

        # 4) pick-file 服务端并发锁 409 防护
        assert system_ctrl._PICK_DIALOG_LOCK.acquire(blocking=False)
        try:
            r_busy = client.post("/api/v1/system/pick-file", json={"kind": "video", "multi": False})
            assert r_busy.status_code == 409
        finally:
            system_ctrl._PICK_DIALOG_LOCK.release()

        # 5) open-media 后缀白名单与安全防护
        assert client.post("/api/v1/system/open-media", json={"path": str(bad_exe)}).status_code == 400
        assert client.post("/api/v1/system/open-media", json={"path": str(tmp / "not_exist.mp4")}).status_code == 404
        monkeypatch.setattr(system_ctrl, "open_media_in_player", lambda p: (True, f"opened:{Path(p).name}"))
        r_open_ok = client.post("/api/v1/system/open-media", json={"path": str(talking_mov)})
        assert r_open_ok.status_code == 200 and "opened:" in r_open_ok.json()["message"]

        # 6) 远程非本机客户端调用 pick-file / open-path / open-media 必须返回 403
        monkeypatch.setattr(studio_server, "_is_local_client", lambda req: False)
        assert client.post("/api/v1/system/pick-file", json={"kind": "video"}).status_code == 403
        assert client.post("/api/v1/system/open-path", json={"path": str(tmp)}).status_code == 403
        assert client.post("/api/v1/system/open-media", json={"path": str(talking_mov)}).status_code == 403


def test_workbench_access_info_video_thumb_and_no_hub_branding(isolated_env, monkeypatch):
    """验证新版可视化工作台定位（去除中枢API）、本机/服务器访问信息隔离与成品视频缩略图回退逻辑。"""
    import studio.server as studio_server
    import studio.system_ctrl as system_ctrl
    from studio.db import get_conn, upsert_job_meta

    tmp = isolated_env["tmp"]
    avatar_mp4 = tmp / "host_avatar.mp4"
    result_mp4 = tmp / "finished_result.mp4"
    _make_tiny_mp4(avatar_mp4)
    _make_tiny_mp4(result_mp4)

    # 1) 验证网络访问信息在不同 bind_host 下的行为（且过滤代理 TUN 198.18.0.0/15）
    acc_default = system_ctrl.get_network_access_info(port=8390, bind_host="0.0.0.0")
    assert acc_default["lan_enabled"] is True
    assert acc_default["local_url"] == "http://127.0.0.1:8390"
    assert all("198.18." not in u and "198.19." not in u for u in acc_default["lan_urls"])

    acc_local_only = system_ctrl.get_network_access_info(port=8390, bind_host="127.0.0.1")
    assert acc_local_only["lan_enabled"] is False
    assert acc_local_only["lan_urls"] == []

    with TestClient(studio_server.app) as client:
        # 2) 首页包含 6 大独立系统视图且不出现“统一中枢”字样
        idx = client.get("/")
        assert idx.status_code == 200
        for view_id in ("view-create", "view-voice", "view-tasks", "view-library", "view-models", "view-system"):
            assert f'id="{view_id}"' in idx.text
        assert "统一中枢" not in idx.text

        openapi = client.get("/openapi.json").json()
        assert "统一中枢" not in openapi["info"]["title"]

        # 3) health 接口返回 access 字段，且本机/远程请求互不污染缓存
        monkeypatch.setattr(studio_server, "_is_local_client", lambda req: True)
        h_local = client.get("/api/v1/health?force=true").json()
        assert h_local["access"]["client_is_local"] is True
        assert h_local["access"]["client_mode"] == "local"

        monkeypatch.setattr(studio_server, "_is_local_client", lambda req: False)
        h_remote = client.get("/api/v1/health").json()
        assert h_remote["access"]["client_is_local"] is False
        assert h_remote["access"]["client_mode"] == "remote"

        monkeypatch.setattr(studio_server, "_is_local_client", lambda req: True)
        h_local_again = client.get("/api/v1/health").json()
        assert h_local_again["access"]["client_is_local"] is True
        assert h_local_again["access"]["client_mode"] == "local"

        # 4) 成品视频缩略图提取、成片移走时回退人物封面、删除任务时自动清理缓存
        m_res = client.post("/api/v1/models", data={"name": "封面测试人物", "local_video_path": str(avatar_mp4)})
        assert m_res.status_code == 200
        model_id = m_res.json()["item"]["id"]

        j_res = client.post("/api/v1/jobs", json={"model_id": model_id, "name": "成品封面测试", "text": "测试封面"})
        assert j_res.status_code == 200
        job_id = j_res.json()["job_id"]

        delivered = isolated_env["delivery_dir"] / f"{job_id}_成品封面测试.mp4"
        delivered.write_bytes(result_mp4.read_bytes())
        conn = get_conn()
        try:
            conn.execute("UPDATE video SET status='success', progress=100 WHERE id=?", (job_id,))
            conn.commit()
        finally:
            conn.close()
        upsert_job_meta(job_id, phase="success", progress_float=100.0, delivered_path=str(delivered))

        job_detail = client.get(f"/api/v1/jobs/{job_id}").json()["item"]
        assert job_detail["thumb_url"].startswith(f"/api/v1/media/video/{job_id}/thumb")

        t1 = client.get(job_detail["thumb_url"])
        assert t1.status_code == 200
        assert t1.headers["content-type"].startswith("image/jpeg")
        assert t1.content[:3] == b"\xff\xd8\xff"
        cached_thumb = studio_server._video_thumb_path(job_id)
        assert cached_thumb.is_file()

        # 模拟成片被移走 -> 缩略图仍优雅回退人物底模封面，不返回 404
        delivered.unlink()
        t_fallback = client.get(f"/api/v1/media/video/{job_id}/thumb")
        assert t_fallback.status_code == 200
        assert t_fallback.content[:3] == b"\xff\xd8\xff"

        job_after_move = client.get(f"/api/v1/jobs/{job_id}").json()["item"]
        assert job_after_move["file_exists"] is False
        assert job_after_move["thumb_url"] == f"/api/v1/media/model/{model_id}/thumb"

        # 删除任务自动清理成片缩略图缓存
        del_r = client.delete(f"/api/v1/jobs/{job_id}")
        assert del_r.status_code == 200
        assert not cached_thumb.exists()


def test_review_round3_uploads_contracts_proxy_and_friendly_errors(isolated_env, monkeypatch):
    """回归验证：反向代理远程识别、分组置空归一化、离线语音友好报错及前后端关键字段契约一致性。"""
    import studio.server as studio_server
    import studio.system_ctrl as system_ctrl
    from studio.db import get_conn

    tmp = isolated_env["tmp"]
    avatar_mp4 = tmp / "contract_avatar.mp4"
    _make_tiny_mp4(avatar_mp4)

    with TestClient(studio_server.app) as client:
        # 1) 首页带 no-cache，避免旧版浏览器缓存残留
        idx = client.get("/")
        assert idx.status_code == 200
        assert "no-cache" in idx.headers.get("cache-control", "").lower()

        # 2) 反向代理携带 X-Forwarded-For / X-Real-IP 必须识别为远程客户端并拦截宿主机桌面动作
        h_xff = client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"}).json()
        assert h_xff["access"]["client_is_local"] is False
        assert h_xff["access"]["client_mode"] == "remote"
        assert client.post(
            "/api/v1/system/pick-file",
            json={"kind": "video"},
            headers={"X-Forwarded-For": "203.0.113.42"},
        ).status_code == 403

        h_xreal = client.get("/api/v1/health", headers={"X-Real-IP": "198.51.100.7"}).json()
        assert h_xreal["access"]["client_is_local"] is False

        # 3) 模特分组传 0 或 None 均归一化为 SQL NULL，确保 group_id=ungrouped 查询一致
        g_res = client.post("/api/v1/model-groups", json={"name": "契约测试分组"})
        assert g_res.status_code == 200
        gid = g_res.json()["item"]["id"]

        m_res = client.post("/api/v1/models", data={"name": "分组测试人物", "local_video_path": str(avatar_mp4)})
        assert m_res.status_code == 200
        mid = m_res.json()["item"]["id"]

        assert client.patch(f"/api/v1/models/{mid}", json={"group_id": gid}).status_code == 200
        assert client.patch(f"/api/v1/models/{mid}", json={"group_id": 0}).status_code == 200

        conn = get_conn()
        try:
            db_gid = conn.execute("SELECT group_id FROM f2f_model WHERE id=?", (mid,)).fetchone()[0]
        finally:
            conn.close()
        assert db_gid is None
        ungrouped = client.get("/api/v1/models?group_id=ungrouped").json()["items"]
        assert any(it["id"] == mid for it in ungrouped)

        # 4) IndexTTS2 语音引擎离线时返回友好中文提示，不裸露 WinError/urlopen 堆栈
        monkeypatch.setattr(system_ctrl, "TTS_BASE_URL", "http://127.0.0.1:19997")
        r_tts = client.post("/api/v1/tts/synthesize", json={"text": "测试离线报错", "voice_id": m_res.json()["item"]["voice_id"]})
        assert r_tts.status_code == 503
        detail = r_tts.json()["detail"]
        assert "WinError" not in detail and "urlopen" not in detail
        assert "18180" in detail and "一键启动服务" in detail

    # 5) 静态前端与后端关键契约防回归检查（防止字段名错配导致上传/配置加载静默失效）
    app_js = (studio_server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'fd.append("video"' not in app_js
    assert 'fd.append("files", f)' in app_js
    assert 'res.deleted_count' in app_js and 'res.removed_count' not in app_js
    assert 'cfgRes.config' in app_js
    assert 'sourceType === "tts"' in app_js
    assert 'local_audio_path' not in app_js
    assert 'group_id: groupVal ? Number(groupVal) : null' in app_js







