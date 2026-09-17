"""Local-only web dashboard for the ghost trader. Serves a single HTML page
plus a JSON endpoint it polls -- no framework, no external dependency,
nothing leaves your machine.

Run it alongside (or instead of) `python3 -m src.status`:

    python3 -m src.dashboard

then open the printed http://127.0.0.1:8765 URL in a browser. It's read-only
against the same state files `python3 -m src.live` writes, so it's safe to
run at the same time as the live bot.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .dashboard_data import build_dashboard_data

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _make_handler(config_path: str, refresh_ms: int):
    html_template = (WEB_DIR / "index.html").read_text()
    html = html_template.replace("__REFRESH_MS__", str(refresh_ms))
    html_bytes = html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/data"):
                try:
                    query = parse_qs(urlparse(self.path).query)
                    market = (query.get("market") or ["polymarket"])[0]
                    payload = build_dashboard_data(config_path, market=market)
                    body = json.dumps(payload).encode("utf-8")
                    status = 200
                except Exception as exc:  # keep the dashboard alive even if a read glitches
                    body = json.dumps({"has_data": False, "message": f"Error reading state: {exc}"}).encode("utf-8")
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass  # the browser polls every few seconds; keep the console quiet

    return Handler


def run_dashboard(config_path: str = "config.yaml") -> None:
    config = yaml.safe_load(Path(config_path).read_text())
    dash_cfg = config.get("dashboard", {})
    host = dash_cfg.get("host", "127.0.0.1")
    port = dash_cfg.get("port", 8765)
    refresh_ms = int(dash_cfg.get("refresh_seconds", 5) * 1000)

    handler = _make_handler(config_path, refresh_ms)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    print("This only reads local files -- safe to leave open alongside `python3 -m src.live`.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Run the local ghost-trader web dashboard.")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    run_dashboard(args.config)


if __name__ == "__main__":
    main()
