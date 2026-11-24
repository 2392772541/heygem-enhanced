from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(r"D:\HeyGem-Enhanced")
ADAPTER_PY = Path(r"D:\HeyGem\tts-story-f5\venv\Scripts\python.exe")
ADAPTER_SCRIPT = PROJECT_ROOT / "adapter" / "heygem_index_tts2_adapter.py"
SCREENSHOT_DIR = PROJECT_ROOT / "logs" / "audit_shots"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def http_json(method: str, url: str, payload=None, timeout: float = 30.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))


def wait_url(url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    procs = []
    try:
        print("=== 1. Starting real IndexTTS2 Adapter (18180) & Studio Server (8390) ===")
        p_tts = subprocess.Popen(
            [str(ADAPTER_PY), str(ADAPTER_SCRIPT)],
            cwd=str(ADAPTER_SCRIPT.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        procs.append(p_tts)

        p_studio = subprocess.Popen(
            [sys.executable, "-m", "studio.server"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        procs.append(p_studio)

        assert wait_url("http://127.0.0.1:18180/healthz", 30), "IndexTTS2 adapter failed to start"
        assert wait_url("http://127.0.0.1:8390/api/v1/ping", 20), "Studio server failed to start"
        print("Both services UP!")

        # 2. Measure ping & parallel health latency
        t0 = time.time()
        _, ping = http_json("GET", "http://127.0.0.1:8390/api/v1/ping")
        t_ping = round((time.time() - t0) * 1000, 1)

        t1 = time.time()
        _, health = http_json("GET", "http://127.0.0.1:8390/api/v1/health", timeout=15)
        t_health = round((time.time() - t1) * 1000, 1)
        print(f"ping={t_ping}ms, health={t_health}ms")
        print(f"Docker f2f_api_ok={health['docker']['f2f_api_ok']}, TTS ok={health['tts']['ok']}, GPU={health['gpu']}")
        print(f"Models total={health['models_integrity']['total']}, missing={health['models_integrity']['missing_count']}")
        print(f"Alerts ({len(health['alerts'])}): {[a['code'] for a in health['alerts']]}")

        # 3. Real IndexTTS2 GPU synthesis (skip if --browser-only)
        if "--browser-only" not in sys.argv:
            print("\n=== 2. Real IndexTTS2 GPU Synthesis Test ===")
            _, voices = http_json("GET", "http://127.0.0.1:8390/api/v1/voices")
            valid_voice = next((v for v in voices["items"] if v["exists"]), None)
            assert valid_voice is not None, "No existing voice found for live test!"
            print(f"Using real voice_id={valid_voice['id']} ({valid_voice['display_name']})")

            observed_states = []
            stop_poll = threading.Event()

            def poll_tts():
                while not stop_poll.is_set():
                    try:
                        _, st = http_json("GET", "http://127.0.0.1:8390/api/v1/tts/status", timeout=3)
                        d = st.get("data") or {}
                        entry = f"{d.get('state')}:{d.get('progress')}%:{d.get('message')}"
                        if not observed_states or observed_states[-1] != entry:
                            observed_states.append(entry)
                    except Exception:
                        pass
                    stop_poll.wait(1.0)

            t_poller = threading.Thread(target=poll_tts, daemon=True)
            t_poller.start()
            t_synth0 = time.time()
            try:
                status_code, synth_res = http_json(
                    "POST",
                    "http://127.0.0.1:8390/api/v1/tts/synthesize",
                    payload={
                        "text": "你好，欢迎使用全新的数字人一体化工作台。",
                        "voice_id": valid_voice["id"],
                        "name": "_audit_live_test",
                        "tts_params": {
                            "model_version": "IndexTTS-2",
                            "diffusion_steps": 12,
                            "num_beams": 1,
                            "speed": 1.05,
                            "emo_mode": "emo_vector",
                            "emo_alpha": 0.6,
                            "emo_vector": [0.35, 0, 0, 0, 0, 0, 0.2, 0],
                        },
                    },
                    timeout=240,
                )
            finally:
                stop_poll.set()
                t_poller.join(timeout=3)

            synth_sec = round(time.time() - t_synth0, 2)
            item = synth_res["item"]
            print(f"Synthesis HTTP {status_code} in {synth_sec}s -> duration={item['duration']}s, size={item['size_kb']}KB")
            print(f"Observed live TTS progress transitions ({len(observed_states)}): {observed_states[:6]}")
            assert item["duration"] > 1.0 and item["size_kb"] > 30.0, f"Invalid synthesized audio: {item}"

            with urllib.request.urlopen(f"http://127.0.0.1:8390{item['stream_url']}", timeout=5) as r:
                assert r.status == 200 and len(r.read()) > 30000
            with urllib.request.urlopen(f"http://127.0.0.1:8390{item['download_url']}", timeout=5) as r:
                assert r.status == 200 and "attachment" in r.headers.get("Content-Disposition", "")

            _, health_after = http_json("GET", "http://127.0.0.1:8390/api/v1/health", timeout=15)
            print(f"GPU VRAM after synthesis exit: {health_after['gpu'].get('memory_used_mb')}MB / {health_after['gpu'].get('memory_total_mb')}MB")
            http_json("DELETE", f"http://127.0.0.1:8390/api/v1/audios/{item['id']}?remove_file=true")
            print("Cleaned up temporary test audio asset.")

        # 4. Real Playwright Headless Browser UI Audit
        print("\n=== 3. Playwright Human-Perspective Browser Audit ===")
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        console_errors = []
        page_errors = []
        failed_requests = []

        with sync_playwright() as pw:
            browser = None
            for ch in ("msedge", "chrome", None):
                try:
                    browser = pw.chromium.launch(channel=ch, headless=True) if ch else pw.chromium.launch(headless=True)
                    print(f"Launched browser channel={ch or 'bundled'}")
                    break
                except Exception as exc:
                    print(f"Browser channel={ch} unavailable: {exc}")
            assert browser is not None, "No Chromium/Edge/Chrome browser available"
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("response", lambda r: failed_requests.append(f"{r.status} {r.url}") if r.status >= 400 else None)

            page.goto("http://127.0.0.1:8390", wait_until="networkidle")
            page.wait_for_timeout(900)

            # 1) Create workspace: verify 3-column non-scrolling workbench & script entry
            assert page.locator("#view-create.active").count() == 1
            assert page.locator("#btnSubmitTtsVideo").is_visible()
            page.fill("#ttsTextInput", "大家好，欢迎体验全新的可视化数字人工作台。")
            page.wait_for_timeout(200)
            page.screenshot(path=str(SCREENSHOT_DIR / "audit_01_create.png"))

            # 2) Independent Voice Studio view: verify synced script & 8-dim emotion sliders
            page.click('.nav-btn[data-view="voice"]')
            page.wait_for_timeout(450)
            assert page.input_value("#voiceTextInput").startswith("大家好")
            page.click('#emoModeTabs button[data-emo="emo_vector"]')
            page.wait_for_timeout(250)
            assert page.locator("#emoVectorBox .emo-slider").count() == 8
            page.screenshot(path=str(SCREENSHOT_DIR / "audit_02_voice.png"))

            # 3) Tasks view
            page.click('.nav-btn[data-view="tasks"]')
            page.wait_for_timeout(500)
            tasks_cnt = page.locator("#tasksList .job-card").count()
            print(f"Tasks view rendered {tasks_cnt} job cards")
            page.screenshot(path=str(SCREENSHOT_DIR / "audit_03_tasks.png"))

            # 4) Library view (with cover thumbnails)
            page.click('.nav-btn[data-view="library"]')
            page.wait_for_timeout(600)
            lib_cnt = page.locator("#libraryGrid .media-card").count()
            print(f"Library view rendered {lib_cnt} video cards")
            page.screenshot(path=str(SCREENSHOT_DIR / "audit_04_library.png"))

            # 5) Models & Voices view
            page.click('.nav-btn[data-view="models"]')
            page.wait_for_timeout(500)
            models_cnt = page.locator("#modelsManageGrid .media-card").count()
            print(f"Models view rendered {models_cnt} model cards")
            page.screenshot(path=str(SCREENSHOT_DIR / "audit_05_models.png"))

            # 6) System & Services view (access info + logs + collapsible developer API tester)
            page.click('.nav-btn[data-view="system"]')
            page.wait_for_timeout(600)
            assert "127.0.0.1" in (page.locator("#sysLocalUrl").text_content() or "")
            logs_txt = page.locator("#logsConsole").text_content() or ""
            assert len(logs_txt) > 0
            page.screenshot(path=str(SCREENSHOT_DIR / "audit_06_system.png"))

            # Expand optional developer drawer and test GET /api/v1/health
            page.click("#devApiDrawer > summary")
            page.wait_for_timeout(300)
            page.click("text=填充: 健康检查")
            page.click("text=发送测试请求")
            page.wait_for_function(
                "() => (document.getElementById('apiTestResponse')?.textContent || '').includes('overall')",
                timeout=10000,
            )
            api_resp_text = page.locator("#apiTestResponse").text_content() or ""
            assert '"overall"' in api_resp_text, f"API tester failed: {api_resp_text[:200]}"
            print("Online API tester live GET /api/v1/health PASSED")

            browser.close()

        print(f"Browser console_errors={len(console_errors)}, page_errors={len(page_errors)}, failed_requests={len(failed_requests)}")
        if console_errors:
            print("CONSOLE ERRORS:", console_errors)
        if page_errors:
            print("PAGE ERRORS:", page_errors)
        if failed_requests:
            print("FAILED REQUESTS:", failed_requests)

        assert not console_errors, f"Console errors: {console_errors}"
        assert not page_errors, f"Page errors: {page_errors}"
        assert not failed_requests, f"Failed HTTP requests: {failed_requests}"

        print("\n=== ALL LIVE E2E AUDIT CHECKS PASSED! ===")
        return 0
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=4)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
