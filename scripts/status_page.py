#!/usr/bin/env python3
"""
status_page.py — live demo dashboard for the Undermind sandbox ISO.

Serves a single self-refreshing HTML page on :8080 (stdlib http.server only).
The page shows: report steps (the JSON produced by sandbox_autorun.sh), the
recent proxy log tail, handoff rows from the SQLite store, and the training
export tail. Read-only; no auth; bind to 127.0.0.1 or 0.0.0.0 via --host.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "report_path": "/tmp/undermind-report.json",
    "log_path": "/var/log/undermind-demo.log",
    "db_path": "/opt/undermind/data/undermind.db",
    "export_path": "/opt/undermind/data/training_export.jsonl",
}

REFRESH_SECONDS = 5


def read_report() -> str:
    try:
        with open(STATE["report_path"], encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "<p class='muted'>No report yet — the autorun is still working…</p>"
    phase = html.escape(str(data.get("phase", "?")))
    boot = html.escape(str(data.get("boot_ts", "?")))
    rows = []
    for step in data.get("steps", []):
        status = str(step.get("status", "?")).lower()
        rows.append(
            f"<tr class='row-{status}'>"
            f"<td>{html.escape(str(step.get('elapsed_s', '')))}s</td>"
            f"<td><span class='badge badge-{status}'>{status}</span></td>"
            f"<td>{html.escape(str(step.get('step', '')))}</td>"
            f"<td class='detail'>{html.escape(str(step.get('detail', '')))}</td>"
            f"</tr>"
        )
    rows_html = "".join(rows) or "<tr><td colspan='4' class='muted'>No steps recorded yet.</td></tr>"
    return (
        f"<h2>Phase: {phase} <small>booted {boot}</small></h2>"
        "<table><thead><tr><th>+s</th><th>Status</th><th>Step</th><th>Detail</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )


def read_log_tail(lines: int = 30) -> str:
    try:
        with open(STATE["log_path"], encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError:
        return "<p class='muted'>Log file not found.</p>"
    return "<pre>" + html.escape("".join(tail)) + "</pre>"


def read_handoffs(limit: int = 10) -> str:
    if not os.path.exists(STATE["db_path"]):
        return "<p class='muted'>Database not created yet.</p>"
    try:
        conn = sqlite3.connect(STATE["db_path"])
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts_ns, trigger, confidence, status, latency_ms, substr(branch,1,60) AS branch"
            " FROM handoffs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return f"<p class='muted'>DB error: {html.escape(str(exc))}</p>"
    if not rows:
        return "<p class='muted'>No handoffs recorded yet.</p>"
    body = ""
    for r in rows:
        confidence = "—" if r["confidence"] is None else f"{r['confidence']:.3f}"
        latency = "—" if r["latency_ms"] is None else f"{r['latency_ms']:.0f}ms"
        clock = time.strftime("%H:%M:%S", time.gmtime(r["ts_ns"] / 1e9))
        body += (
            "<tr>"
            f"<td>{clock}</td>"
            f"<td>{html.escape(str(r['trigger']))}</td>"
            f"<td>{confidence}</td>"
            f"<td>{html.escape(str(r['status']))}</td>"
            f"<td>{latency}</td>"
            f"<td class='detail'>{html.escape(str(r['branch']))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>UTC</th><th>Trigger</th><th>Conf</th><th>Status</th>"
        "<th>Latency</th><th>Branch</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def read_export_tail(lines: int = 5) -> str:
    try:
        with open(STATE["export_path"], encoding="utf-8") as fh:
            tail = fh.readlines()[-lines:]
    except OSError:
        return "<p class='muted'>training_export.jsonl not created yet.</p>"
    pretty = []
    for line in tail:
        try:
            record = json.loads(line)
            pretty.append(
                f"intent {record.get('intent_id', '?')[:8]}… "
                f"count={record.get('count')} "
                f"signature={record.get('signature', '')}"
            )
        except ValueError:
            pretty.append(line.strip()[:100])
    return "<pre>" + html.escape("\n".join(pretty) or "(empty)") + "</pre>"


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Undermind — live demo</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #101418; color: #e8e6e3; }}
  h1 {{ font-weight: 600; }} h2 small {{ color: #8a9299; font-weight: 400; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #2a3138; font-size: 0.92rem; }}
  th {{ color: #8a9299; font-weight: 500; }}
  pre {{ background: #0a0d10; border: 1px solid #2a3138; padding: 0.8rem; overflow-x: auto; font-size: 0.8rem; }}
  .badge {{ padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.78rem; }}
  .badge-ok {{ background: #1d3b28; color: #7ddba3; }}
  .badge-fail {{ background: #47201f; color: #f2a09b; }}
  .badge-warn {{ background: #453a1a; color: #e8cf7c; }}
  .badge-skip {{ background: #24303a; color: #9fb4c4; }}
  .row-fail td {{ color: #f2a09b; }}
  .muted {{ color: #8a9299; }}
  .detail {{ max-width: 34rem; overflow-wrap: anywhere; color: #c3cbd2; }}
</style>
<meta http-equiv="refresh" content="{refresh}">
</head>
<body>
<h1>Undermind — sandbox live demo</h1>
<section>{report}</section>
<h2>Recent handoffs</h2>
<section>{handoffs}</section>
<h2>Training export (tail)</h2>
<section>{export}</section>
<h2>Demo log (tail)</h2>
<section>{log}</section>
</body>
</html>
"""


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        page = PAGE_TEMPLATE.format(
            refresh=REFRESH_SECONDS,
            report=read_report(),
            handoffs=read_handoffs(),
            export=read_export_tail(),
            log=read_log_tail(),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Undermind demo status page")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log", default=STATE["log_path"])
    parser.add_argument("--report", default=STATE["report_path"])
    parser.add_argument("--db", default=STATE["db_path"])
    parser.add_argument("--export", default=STATE["export_path"])
    args = parser.parse_args()

    STATE["log_path"] = args.log
    STATE["report_path"] = args.report
    STATE["db_path"] = args.db
    STATE["export_path"] = args.export

    server = ThreadingHTTPServer((args.host, args.port), StatusHandler)
    print(f"Undermind status page: http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
