from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple


def _resolve_db_path() -> Path:
    override = os.getenv("HEYGEM_DB_OVERRIDE")
    if override:
        return Path(override)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "Duix.Avatar" / "biz.db"
    return Path.home() / "AppData" / "Roaming" / "Duix.Avatar" / "biz.db"


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DB_PATH = _resolve_db_path()

PID_FILES = {
    "queue_guard": PROJECT_ROOT / "heygem-queue-guard.pid",
    "queue_daemon": PROJECT_ROOT / "heygem-queue-daemon.pid",
    "index_tts2_adapter": PROJECT_ROOT / "heygem-index-tts2.pid",
}

PROCESS_SPECS = {
    "queue_guard": {"name": "powershell.exe", "pattern": "*heygem-queue-guard.ps1*"},
    "queue_daemon": {"name": "python.exe", "pattern": "*heygem_queue_runner.py*"},
    "index_tts2_adapter": {"name": "python.exe", "pattern": "*heygem_index_tts2_adapter.py*"},
}


def clear_screen() -> None:
    os.system("cls")


def print_section(title: str) -> None:
    print(title)
    print("-" * len(title))


def print_table(headers, rows) -> None:
    widths = [len(h) for h in headers]
    normalized_rows = []
    for row in rows:
        values = ["" if value is None else str(value) for value in row]
        normalized_rows.append(values)
        for i, value in enumerate(values):
            widths[i] = max(widths[i], len(value))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for values in normalized_rows:
        print("  ".join(values[i].ljust(widths[i]) for i in range(len(values))))


def run_powershell(command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return (result.stdout or "").strip()


def get_matching_processes(process_name: str, pattern: str) -> List[Dict]:
    escaped_name = process_name.replace("'", "''")
    escaped_pattern = pattern.replace("'", "''")
    command = (
        "$items = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.ProcessId -ne $PID -and $_.Name -eq '{escaped_name}' "
        f"-and $_.CommandLine -and $_.CommandLine -like '{escaped_pattern}' }} | "
        "Select-Object ProcessId, ParentProcessId, Name, CommandLine;"
        "if ($items) { $items | ConvertTo-Json -Compress }"
    )
    raw = run_powershell(command)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        return [parsed]
    return parsed if isinstance(parsed, list) else []


def get_logical_root_processes(processes: List[Dict]) -> List[Dict]:
    if not processes:
        return []

    pid_set = {
        int(item["ProcessId"])
        for item in processes
        if item.get("ProcessId") not in (None, "")
    }
    roots = []
    for item in processes:
        pid = item.get("ProcessId")
        parent_pid = item.get("ParentProcessId")
        if pid in (None, ""):
            continue
        try:
            normalized_parent = int(parent_pid) if parent_pid not in (None, "") else None
        except (TypeError, ValueError):
            normalized_parent = None
        if normalized_parent not in pid_set:
            roots.append(item)

    return roots or processes


def get_pidfile_status() -> List[Tuple[str, str, str]]:
    rows = []
    for name, path in PID_FILES.items():
        if not path.exists():
            rows.append((name, "missing", ""))
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        rows.append((name, "present", raw))
    return rows


def get_docker_status() -> List[Tuple[str, str]]:
    try:
        res = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}	{{.Status}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if res.returncode != 0:
            return [("docker", "Docker daemon not reachable")]
        rows = []
        for line in res.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("	", 1)
            name = parts[0]
            status = parts[1] if len(parts) > 1 else ""
            if "duix" in name.lower():
                rows.append((name, status))
        return rows or [("containers", "No duix-avatar containers running")]
    except Exception as exc:
        return [("docker", f"Error: {exc}")]


def get_healthz_status() -> Tuple[str, str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18180/healthz", timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return ("ok", body)
    except Exception as exc:
        return ("inactive", str(exc))


def get_db_snapshot() -> Tuple[List[Tuple], List[Tuple], str]:
    if not DB_PATH.exists():
        return ([], [], f"Database not found: {DB_PATH}")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        counts = conn.execute(
            "SELECT status, count(*) AS total FROM video GROUP BY status ORDER BY status"
        ).fetchall()
        count_rows = [(r["status"], r["total"]) for r in counts]

        current = conn.execute(
            """
            SELECT id, ifnull(name, '') AS name, status, progress, ifnull(code, '') AS code, ifnull(message, '') AS message
            FROM video
            WHERE status IN ('pending', 'failed')
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()
        current_rows = [(r["id"], r["name"][:20], r["status"], r["progress"], r["code"][:12], r["message"][:40]) for r in current]
        conn.close()
        return (count_rows, current_rows, "")
    except Exception as exc:
        return ([], [], f"SQLite error: {exc}")


def render() -> None:
    print("==================================================")
    print("      HeyGem-Enhanced 运行状态监控控制台         ")
    print("==================================================")
    print(time.strftime("当前时间: %Y-%m-%d %H:%M:%S"))
    print(f"数据库: {DB_PATH}")
    print()

    print_section("[PID 记录文件]")
    print_table(["组件", "文件状态", "记录 PID"], get_pidfile_status())
    print()

    print_section("[系统进程状态]")
    proc_rows = []
    for name, spec in PROCESS_SPECS.items():
        matches = get_matching_processes(spec["name"], spec["pattern"])
        if not matches:
            proc_rows.append((name, "已停止 (stopped)", 0, "-"))
            continue
        logical_roots = get_logical_root_processes(matches)
        pids = ",".join(str(item.get("ProcessId", "")) for item in logical_roots)
        proc_rows.append((name, "运行中 (running)", len(logical_roots), pids))
    print_table(["服务名称", "运行状态", "进程数", "进程 PID"], proc_rows)
    print()

    print_section("[Docker 容器状态]")
    print_table(["容器名称", "运行状态"], get_docker_status())
    print()

    print_section("[IndexTTS2 兼容服务状态]")
    health_state, health_body = get_healthz_status()
    print_table(["状态", "详情"], [(health_state, health_body[:100])])
    print()

    counts, current, db_error = get_db_snapshot()
    print_section("[任务统计 (video 表)]")
    if db_error:
        print(db_error)
    elif counts:
        print_table(["任务状态", "总计"], counts)
    else:
        print("未发现视频任务数据。")
    print()

    print_section("[排队中 / 异常任务 (最近 10 条)]")
    if db_error:
        print(db_error)
    elif current:
        print_table(["ID", "作品名称", "状态", "进度", "任务代码", "说明"], current)
    else:
        print("当前无等待中或失败的任务。")


def main() -> None:
    parser = argparse.ArgumentParser(description="HeyGem 状态监控工具")
    parser.add_argument("--once", action="store_true", help="输出一次后退出")
    args = parser.parse_args()

    while True:
        clear_screen()
        render()
        if args.once:
            return
        time.sleep(3)


if __name__ == "__main__":
    main()
