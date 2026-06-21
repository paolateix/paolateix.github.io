#!/usr/bin/env python3
"""
ETAgent local server — run once, then open docs/ETAgent.html in your browser.
Usage: python server.py
"""
import subprocess
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import json
import threading

PORT = 7432
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monday_smartling_agent.py")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence request logs

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        dry_run = params.get("dry_run", ["false"])[0] == "true"

        cmd = [sys.executable, SCRIPT]
        if dry_run:
            cmd.append("--dry-run")

        try:
            result = subprocess.run(
                cmd,
                cwd=os.path.dirname(SCRIPT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            success = result.returncode == 0
        except subprocess.TimeoutExpired:
            output = "ERROR: Script timed out after 2 minutes."
            success = False
        except Exception as e:
            output = f"ERROR: {e}"
            success = False

        body = json.dumps({"success": success, "output": output}).encode()
        self.send_response(200)
        self.send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"ETAgent server running on http://localhost:{PORT}")
    print(f"Open docs/ETAgent.html in your browser to use the UI.")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
