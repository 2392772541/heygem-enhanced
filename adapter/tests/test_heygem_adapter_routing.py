from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ADAPTER2_DIR = Path(r"D:\HeyGem-Enhanced\adapter")
if str(ADAPTER2_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER2_DIR))

import heygem_index_tts2_adapter as adapter  # noqa: E402


def _write_dummy_wav(path: Path, freq: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 22050
    t = np.linspace(0.0, 0.1, int(sr * 0.1), endpoint=False, dtype=np.float32)
    samples = 0.1 * np.sin(2.0 * np.pi * freq * t)
    sf.write(path, samples, sr, format="WAV")
    return path


class _FakeStudioProc:
    def __init__(self, job_file: Path, env: dict[str, str]):
        self.job_file = job_file
        self.env = env
        job = json.loads(job_file.read_text(encoding="utf-8"))
        out_files: list[str] = []
        for chunk in job.get("chunks") or []:
            out_path = Path(chunk["output_path"])
            _write_dummy_wav(out_path, freq=330.0)
            out_files.append(str(out_path))
        self.stdout = io.StringIO(json.dumps({"success": True, "files": out_files}) + "\n")
        self.stderr = io.StringIO('[PROGRESS] {"stage":"done","chunk_current":1,"chunk_total":1,"message":"done"}\n')
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass


class TestAdapter2Routing(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="m3_adapter2_test_")
        self.tmp_root = Path(self._tmp.name)

        self.orig_env = {
            k: os.environ.get(k)
            for k in (
                "INDEX_TTS_VOICES_REGISTRY",
                "MYVOICE_REGISTRY_PATH",
                "INDEX_TTS_CFG_PATH",
                "HEYGEM_BIZ_DB_PATH",
            )
        }
        self.orig_config_path = adapter.CONFIG_PATH
        self.orig_shared_root = adapter.SHARED_ROOT
        self.orig_origin_dir = adapter.ORIGIN_AUDIO_DIR

        self.shared_root = self.tmp_root / "shared_voice"
        self.origin_dir = self.shared_root / "origin_audio"
        self.origin_dir.mkdir(parents=True, exist_ok=True)
        adapter.SHARED_ROOT = self.shared_root
        adapter.ORIGIN_AUDIO_DIR = self.origin_dir

        self.config_path = self.tmp_root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "index_tts_model_version": "IndexTTS-2",
                    "index_tts_default_prompt": str(self.tmp_root / "legacy_should_be_ignored.wav"),
                    "index_tts_override_request_prompt": True,
                    "index_tts_cfg_path": str(self.tmp_root / "legacy_cfg_should_be_ignored.yaml"),
                    "voices_registry_path": str(self.tmp_root / "registry.json"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        adapter.CONFIG_PATH = self.config_path

        self.voice_a_gpt = self.tmp_root / "voices" / "voice_a" / "gpt_a.pth"
        self.voice_a_gpt.parent.mkdir(parents=True, exist_ok=True)
        self.voice_a_gpt.write_bytes(b"fake_gpt_a")
        self.voice_a_cfg = self.voice_a_gpt.parent / "config_a.yaml"
        self.voice_a_cfg.write_text(
            f"gpt_checkpoint: {self.voice_a_gpt.as_posix()}\n",
            encoding="utf-8",
        )
        self.voice_a_prompt = _write_dummy_wav(self.voice_a_gpt.parent / "prompt_a.wav", freq=440.0)

        self.voice_b_gpt = self.tmp_root / "voices" / "voice_b" / "gpt_b.pth"
        self.voice_b_gpt.parent.mkdir(parents=True, exist_ok=True)
        self.voice_b_gpt.write_bytes(b"fake_gpt_b")
        self.voice_b_cfg = self.voice_b_gpt.parent / "config_b.yaml"
        self.voice_b_cfg.write_text(
            f"gpt_checkpoint: {self.voice_b_gpt.as_posix()}\n",
            encoding="utf-8",
        )
        self.voice_b_prompt = _write_dummy_wav(self.voice_b_gpt.parent / "prompt_b.wav", freq=550.0)

        self.avatar_a_wav = _write_dummy_wav(self.origin_dir / "format_20260902115456435.wav", freq=220.0)
        self.avatar_b_wav = _write_dummy_wav(self.origin_dir / "format_20261001120000123.wav", freq=240.0)
        self.avatar_unbound_wav = _write_dummy_wav(self.origin_dir / "format_20261111000000999.wav", freq=260.0)
        self.custom_stem_wav = _write_dummy_wav(self.origin_dir / "custom_avatar_alpha.wav", freq=280.0)

        self.registry_path = self.tmp_root / "registry.json"
        self._write_registry(
            global_mode="auto",
            forced_voice_id="voice_a",
            bindings={
                "20260902115456435": {
                    "avatar_stem": "20260902115456435",
                    "voice_id": "voice_a",
                    "model_key": "best",
                    "prompt_key": "natural",
                },
                "20261001120000123": {
                    "avatar_stem": "20261001120000123",
                    "voice_id": "voice_b",
                    "model_key": "best",
                    "prompt_key": "natural",
                },
                "custom_avatar_alpha": {
                    "avatar_stem": "custom_avatar_alpha",
                    "voice_id": "voice_a",
                    "model_key": "best",
                    "prompt_key": "natural",
                },
                "20261111000000888": {
                    "avatar_stem": "20261111000000888",
                    "voice_id": "official_base",
                    "model_key": "",
                    "prompt_key": "",
                },
            },
        )
        os.environ["INDEX_TTS_VOICES_REGISTRY"] = str(self.registry_path)
        os.environ["HEYGEM_BIZ_DB_PATH"] = str(self.tmp_root / "missing_biz.db")
        os.environ.pop("INDEX_TTS_CFG_PATH", None)

        self.official_cfg = str((adapter.ENGINE_ROOT / "checkpoints" / "config.yaml").resolve())
        self.official_gpt = str((adapter.ENGINE_ROOT / "checkpoints" / "gpt.pth").resolve())
        self.client = adapter.app.test_client()
        self.captured_jobs: list[dict] = []
        self.captured_envs: list[dict[str, str]] = []

    def tearDown(self) -> None:
        for k, v in self.orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        adapter.CONFIG_PATH = self.orig_config_path
        adapter.SHARED_ROOT = self.orig_shared_root
        adapter.ORIGIN_AUDIO_DIR = self.orig_origin_dir
        with adapter._PROC_LOCK:
            adapter._SYNTH_ACTIVE = False
            adapter._CANCEL_EVENT.clear()
        self._tmp.cleanup()

    def _write_registry(
        self,
        *,
        global_mode: str = "auto",
        forced_voice_id: str = "voice_a",
        forced_model_key: str = "best",
        forced_prompt_key: str = "natural",
        bindings: dict | None = None,
        voices: list[dict] | None = None,
    ) -> None:
        if voices is None:
            voices = [
                {
                    "voice_id": "voice_a",
                    "index": 1,
                    "display_name": "Voice A",
                    "default_model_key": "best",
                    "default_prompt_key": "natural",
                    "models": {
                        "best": {
                            "key": "best",
                            "gpt_path": str(self.voice_a_gpt),
                            "cfg_path": str(self.voice_a_cfg),
                        }
                    },
                    "prompts": {
                        "natural": {
                            "key": "natural",
                            "path": str(self.voice_a_prompt),
                        }
                    },
                },
                {
                    "voice_id": "voice_b",
                    "index": 2,
                    "display_name": "Voice B",
                    "default_model_key": "best",
                    "default_prompt_key": "natural",
                    "models": {
                        "best": {
                            "key": "best",
                            "gpt_path": str(self.voice_b_gpt),
                            "cfg_path": str(self.voice_b_cfg),
                        }
                    },
                    "prompts": {
                        "natural": {
                            "key": "natural",
                            "path": str(self.voice_b_prompt),
                        }
                    },
                },
            ]
        data = {
            "schema_version": 1,
            "default_voice_id": "voice_a",
            "voices": voices,
            "heygem_routing": {
                "global_mode": global_mode,
                "forced_voice_id": forced_voice_id,
                "forced_model_key": forced_model_key,
                "forced_prompt_key": forced_prompt_key,
                "bindings": bindings if bindings is not None else {},
            },
        }
        self.registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _popen_side_effect(self, cmd, **kwargs):
        job_file = Path(cmd[cmd.index("--job-file") + 1])
        self.captured_jobs.append(json.loads(job_file.read_text(encoding="utf-8")))
        self.captured_envs.append(dict(kwargs.get("env") or {}))
        return _FakeStudioProc(job_file, kwargs.get("env") or {})

    def test_auto_mode_bound_variants_and_zero_cross_leakage(self) -> None:
        variants = [
            (str(self.avatar_a_wav), "voice_a", self.voice_a_cfg, self.voice_a_prompt),
            (
                '  "D:\\duix_avatar_data\\voice\\data\\origin_audio\\format_denoise_20260902115456435_part01.wav"  ',
                "voice_a",
                self.voice_a_cfg,
                self.voice_a_prompt,
            ),
            (
                "/code/data/origin_audio/20261001120000123.wav|||/code/data/origin_audio/20261001120000123.wav",
                "voice_b",
                self.voice_b_cfg,
                self.voice_b_prompt,
            ),
            (str(self.custom_stem_wav), "voice_a", self.voice_a_cfg, self.voice_a_prompt),
        ]
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect):
            for ref, expected_vid, expected_cfg, expected_prompt in variants:
                resp = self.client.post("/v1/invoke", json={"text": "测试防串音路由", "reference_audio": ref})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["X-TTS-Voice-Id"], expected_vid)
                self.assertEqual(resp.headers["X-TTS-Routing-Mode"], "auto")
                self.assertEqual(resp.headers["X-TTS-Voice-Mode"], "personal")
                self.assertEqual(resp.headers["X-TTS-Prompt-Overridden"], "true")
                self.assertEqual(resp.headers["X-TTS-Binding-Matched"], "true")
                job = self.captured_jobs[-1]
                self.assertEqual(Path(job["cfg_path"]).resolve(), expected_cfg.resolve())
                self.assertEqual(
                    Path(job["chunks"][0]["spk_audio_prompt"]).resolve(),
                    expected_prompt.resolve(),
                )

    def test_auto_mode_unbound_and_official_base_entry(self) -> None:
        explicit_official_wav = _write_dummy_wav(self.origin_dir / "format_20261111000000888.wav", freq=290.0)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect):
            for wav_path in (self.avatar_unbound_wav, explicit_official_wav):
                resp = self.client.post(
                    "/v1/invoke",
                    json={"text": "未绑定数字人走官方原版", "reference_audio": str(wav_path)},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["X-TTS-Voice-Id"], "official_base")
                self.assertEqual(resp.headers["X-TTS-Routing-Mode"], "auto")
                self.assertEqual(resp.headers["X-TTS-Voice-Mode"], "base")
                self.assertEqual(resp.headers["X-TTS-Prompt-Overridden"], "false")
                self.assertEqual(resp.headers["X-TTS-Binding-Matched"], "false")
                job = self.captured_jobs[-1]
                self.assertEqual(Path(job["cfg_path"]).resolve(), Path(self.official_cfg).resolve())
                self.assertEqual(
                    Path(job["chunks"][0]["spk_audio_prompt"]).resolve(),
                    wav_path.resolve(),
                )

            bad = self.client.post("/v1/invoke", json={"text": "缺少参考音", "reference_audio": ""})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(len(self.captured_jobs), 2)

    def test_global_override_hot_reload_without_restart(self) -> None:
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect):
            hz1 = self.client.get("/healthz").get_json()
            self.assertEqual(hz1["status"], "ok")
            self.assertEqual(hz1["routing_mode"], "auto")

            self._write_registry(
                global_mode="force_voice",
                forced_voice_id="voice_b",
                bindings={
                    "20260902115456435": {
                        "avatar_stem": "20260902115456435",
                        "voice_id": "voice_a",
                        "model_key": "best",
                        "prompt_key": "natural",
                    }
                },
            )
            hz2 = self.client.get("/healthz").get_json()
            self.assertEqual(hz2["status"], "ok")
            self.assertEqual(hz2["routing_mode"], "force_voice")
            self.assertEqual(hz2["active_voice_id"], "voice_b")

            r2 = self.client.post(
                "/v1/invoke",
                json={"text": "临时全部用B", "reference_audio": str(self.avatar_a_wav)},
            )
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.headers["X-TTS-Voice-Id"], "voice_b")
            self.assertEqual(r2.headers["X-TTS-Routing-Mode"], "force_voice")
            self.assertEqual(Path(self.captured_jobs[-1]["cfg_path"]).resolve(), self.voice_b_cfg.resolve())

            self._write_registry(
                global_mode="force_official",
                forced_voice_id="voice_b",
                bindings={
                    "20260902115456435": {
                        "avatar_stem": "20260902115456435",
                        "voice_id": "voice_a",
                        "model_key": "best",
                        "prompt_key": "natural",
                    }
                },
            )
            hz3 = self.client.get("/healthz").get_json()
            self.assertEqual(hz3["status"], "ok")
            self.assertEqual(hz3["routing_mode"], "force_official")
            self.assertEqual(hz3["voice_mode"], "base")

            r3 = self.client.post(
                "/v1/invoke",
                json={"text": "临时全部切回官方原版", "reference_audio": str(self.avatar_a_wav)},
            )
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(r3.headers["X-TTS-Voice-Id"], "official_base")
            self.assertEqual(r3.headers["X-TTS-Routing-Mode"], "force_official")
            self.assertEqual(r3.headers["X-TTS-Voice-Mode"], "base")
            self.assertEqual(Path(self.captured_jobs[-1]["cfg_path"]).resolve(), Path(self.official_cfg).resolve())
            self.assertEqual(
                Path(self.captured_jobs[-1]["chunks"][0]["spk_audio_prompt"]).resolve(),
                self.avatar_a_wav.resolve(),
            )

    def test_indextts_2_5_cannot_silently_drop_custom_cfg(self) -> None:
        with self.assertRaises(ValueError):
            adapter._resolve_and_validate_cfg_path("IndexTTS-2.5", str(self.voice_a_cfg))

        # Bound voice still routes to IndexTTS-2 even if config.json selects IndexTTS-2.5
        self.client.put("/v1/tts/config", json={"index_tts_model_version": "IndexTTS-2.5"})
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect):
            r = self.client.post(
                "/v1/invoke",
                json={"text": "2.5配置不吞掉绑定声音", "reference_audio": str(self.avatar_a_wav)},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["X-TTS-Voice-Id"], "voice_a")
            self.assertEqual(r.headers["X-TTS-Model-Version"], "IndexTTS-2")
            self.assertEqual(Path(self.captured_jobs[-1]["cfg_path"]).resolve(), self.voice_a_cfg.resolve())

    def test_strict_http_400_non_fallback_and_status_isolation(self) -> None:
        adapter._update_status(state="synthesizing", job_id="keep_me", progress=55, message="正在合成", last_error=None)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect) as mock_popen:
            # 1) Missing voice_id
            self._write_registry(
                bindings={"20260902115456435": {"voice_id": "missing_voice", "model_key": "best", "prompt_key": "natural"}}
            )
            r = self.client.post("/v1/invoke", json={"text": "缺档案", "reference_audio": str(self.avatar_a_wav)})
            self.assertEqual(r.status_code, 400)

            # 2) Missing cfg_path
            self._write_registry(
                bindings={"20260902115456435": {"voice_id": "voice_a", "model_key": "best", "prompt_key": "natural"}}
            )
            self.voice_a_cfg.unlink()
            r = self.client.post("/v1/invoke", json={"text": "缺cfg", "reference_audio": str(self.avatar_a_wav)})
            self.assertEqual(r.status_code, 400)
            self.voice_a_cfg.write_text(f"gpt_checkpoint: {self.voice_a_gpt.as_posix()}\n", encoding="utf-8")

            # 3) Missing gpt_checkpoint
            self.voice_a_gpt.unlink()
            r = self.client.post("/v1/invoke", json={"text": "缺gpt", "reference_audio": str(self.avatar_a_wav)})
            self.assertEqual(r.status_code, 400)
            self.voice_a_gpt.write_bytes(b"fake_gpt_a")

            # 4) Missing prompt WAV
            self.voice_a_prompt.unlink()
            r = self.client.post("/v1/invoke", json={"text": "缺prompt", "reference_audio": str(self.avatar_a_wav)})
            self.assertEqual(r.status_code, 400)
            _write_dummy_wav(self.voice_a_prompt, freq=440.0)

            # 5) Bound cfg pointing to official gpt.pth
            self.voice_a_cfg.write_text(f"gpt_checkpoint: {Path(self.official_gpt).as_posix()}\n", encoding="utf-8")
            r = self.client.post("/v1/invoke", json={"text": "伪装官方权重", "reference_audio": str(self.avatar_a_wav)})
            self.assertEqual(r.status_code, 400)
            self.voice_a_cfg.write_text(f"gpt_checkpoint: {self.voice_a_gpt.as_posix()}\n", encoding="utf-8")

            # 6) Corrupt registry.json
            self.registry_path.write_text("{corrupt", encoding="utf-8")
            hz_bad = self.client.get("/healthz").get_json()
            self.assertEqual(hz_bad["status"], "degraded")
            self.assertFalse(hz_bad["registry_valid"])
            r_bad = self.client.post("/v1/invoke", json={"text": "坏注册表", "reference_audio": str(self.avatar_unbound_wav)})
            self.assertEqual(r_bad.status_code, 400)

            self.assertEqual(mock_popen.call_count, 0)
            snap = adapter._snapshot_status()
            self.assertEqual(snap["job_id"], "keep_me")
            self.assertEqual(snap["progress"], 55)
            self.assertIsNone(snap["last_error"])

    def test_missing_empty_null_whitespace_and_nonstring_gpt_path_rejected(self) -> None:
        adapter._update_status(state="synthesizing", job_id="keep_me_gpt", progress=77, message="正在合成", last_error=None)
        cases: list[tuple[str, dict]] = [
            ("omitted", {"key": "best", "cfg_path": str(self.voice_a_cfg)}),
            ("empty", {"key": "best", "gpt_path": "", "cfg_path": str(self.voice_a_cfg)}),
            ("null", {"key": "best", "gpt_path": None, "cfg_path": str(self.voice_a_cfg)}),
            ("whitespace", {"key": "best", "gpt_path": "   ", "cfg_path": str(self.voice_a_cfg)}),
            ("quoted_whitespace", {"key": "best", "gpt_path": ' "   " ', "cfg_path": str(self.voice_a_cfg)}),
            ("non_string", {"key": "best", "gpt_path": 123, "cfg_path": str(self.voice_a_cfg)}),
        ]
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect) as mock_popen:
            for label, model_entry in cases:
                with self.subTest(case=label):
                    self._write_registry(
                        bindings={
                            "20260902115456435": {
                                "avatar_stem": "20260902115456435",
                                "voice_id": "voice_a",
                                "model_key": "best",
                                "prompt_key": "natural",
                            }
                        },
                        voices=[
                            {
                                "voice_id": "voice_a",
                                "index": 1,
                                "display_name": "Voice A",
                                "default_model_key": "best",
                                "default_prompt_key": "natural",
                                "models": {"best": model_entry},
                                "prompts": {
                                    "natural": {
                                        "key": "natural",
                                        "path": str(self.voice_a_prompt),
                                    }
                                },
                            }
                        ],
                    )
                    hz = self.client.get("/healthz").get_json()
                    self.assertFalse(hz["bindings_valid"])
                    self.assertIn("20260902115456435", hz["invalid_bindings"])
                    self.assertIn("gpt_path", hz["invalid_bindings"]["20260902115456435"])

                    resp = self.client.post(
                        "/v1/invoke",
                        json={"text": f"测试gpt_path拦截-{label}", "reference_audio": str(self.avatar_a_wav)},
                    )
                    self.assertEqual(resp.status_code, 400)
                    body = resp.get_json()
                    self.assertEqual(body["code"], 1)
                    self.assertIn("gpt_path", body["msg"])
            self.assertEqual(mock_popen.call_count, 0)
            snap = adapter._snapshot_status()
            self.assertEqual(snap["job_id"], "keep_me_gpt")
            self.assertEqual(snap["progress"], 77)
            self.assertIsNone(snap["last_error"])

    def test_index_tts_cfg_path_env_hijack_immunity(self) -> None:
        os.environ["INDEX_TTS_CFG_PATH"] = str(self.voice_a_cfg)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect):
            hz = self.client.get("/healthz").get_json()
            self.assertTrue(hz["index_tts_cfg_path_env_ignored"])

            r1 = self.client.post(
                "/v1/invoke",
                json={"text": "防劫持未绑定", "reference_audio": str(self.avatar_unbound_wav)},
            )
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.headers["X-TTS-Voice-Id"], "official_base")
            self.assertEqual(Path(self.captured_jobs[-1]["cfg_path"]).resolve(), Path(self.official_cfg).resolve())
            self.assertNotIn("INDEX_TTS_CFG_PATH", self.captured_envs[-1])

            r2 = self.client.post(
                "/v1/invoke",
                json={"text": "防劫持绑定B", "reference_audio": str(self.avatar_b_wav)},
            )
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.headers["X-TTS-Voice-Id"], "voice_b")
            self.assertEqual(Path(self.captured_jobs[-1]["cfg_path"]).resolve(), self.voice_b_cfg.resolve())
            self.assertNotIn("INDEX_TTS_CFG_PATH", self.captured_envs[-1])

    def test_biz_db_readonly_lookup_and_missing_db_resilience(self) -> None:
        db_path = self.tmp_root / "biz.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE voice (id INTEGER PRIMARY KEY, origin_audio_path TEXT, asr_format_audio_url TEXT)")
        conn.execute("CREATE TABLE f2f_model (id INTEGER PRIMARY KEY, name TEXT, audio_path TEXT, voice_id INTEGER)")
        conn.execute(
            "INSERT INTO voice (id, origin_audio_path, asr_format_audio_url) VALUES (?, ?, ?)",
            (1, "origin_audio/20260902115456435.wav", "D:/duix_avatar_data/voice/data/origin_audio/format_99999999999999999.wav"),
        )
        conn.execute(
            "INSERT INTO f2f_model (id, name, audio_path, voice_id) VALUES (?, ?, ?, ?)",
            (1, "数字人甲", "origin_audio\\20260902115456435.wav", 1),
        )
        conn.commit()
        conn.close()

        os.environ["HEYGEM_BIZ_DB_PATH"] = str(db_path)
        alt_wav = _write_dummy_wav(self.origin_dir / "format_99999999999999999.wav", freq=310.0)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=self._popen_side_effect):
            r = self.client.post("/v1/invoke", json={"text": "数据库反查", "reference_audio": str(alt_wav)})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["X-TTS-Voice-Id"], "voice_a")
            self.assertEqual(Path(self.captured_jobs[-1]["cfg_path"]).resolve(), self.voice_a_cfg.resolve())


if __name__ == "__main__":
    unittest.main()
