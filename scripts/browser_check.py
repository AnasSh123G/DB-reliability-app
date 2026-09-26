"""Run the arrival export prototype in a real (headless) Chromium and check it against Python.

    .venv/bin/python scripts/browser_check.py [data/ml/arrival_exports/<stamp>]

Serves scripts/browser_check/ (the page), onnxruntime-web's dist/ (the WASM
runtime the frontend ships) and the export directory on localhost, opens the
page in Playwright's cached Chromium and waits for the page to post its
findings. Writes <export>/browser_check.json; exits 1 if any candidate is
outside tolerance or failed to run.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = ROOT / "scripts" / "browser_check"
ORT_DIR = ROOT / "bahn-buddy-predict" / "node_modules" / "onnxruntime-web" / "dist"
CHROME = sorted((Path.home() / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux64/chrome"))
TIMEOUT_S = 300
TOLERANCE = {"quantiles_raw": 1e-3, "probs_raw": 1e-5, "rule_logic": 1e-5, "histogram": 0.0}


def _handler(export_dir: Path, results: dict):
    routes = {"/ort/": ORT_DIR, "/export/": export_dir, "/": PAGE_DIR}

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".mjs": "text/javascript", ".wasm": "application/wasm"}

        def translate_path(self, path):
            path = path.split("?", 1)[0]
            for prefix, base in routes.items():
                if path.startswith(prefix):
                    target = (base / path[len(prefix):]).resolve()
                    if base.resolve() not in (target, *target.parents):
                        return str(base / "__forbidden__")
                    return str(target)
            return str(PAGE_DIR / "__missing__")

        def do_POST(self):
            if self.path != "/result":
                self.send_error(404)
                return
            results["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    return Handler


def evaluate(result: dict, manifest: dict) -> dict:
    problems = []
    if "error" in result:
        problems.append(f"page: {result['error']}")
    for name, entry in manifest["candidates"].items():
        got = result.get("candidates", {}).get(name)
        if got is None or "error" in got:
            problems.append(f"{name}: {None if got is None else got['error']}")
            continue
        if entry["kind"] == "histogram":
            if got["point_max_abs"] > TOLERANCE["histogram"] or got["pct_max_abs"] > TOLERANCE["histogram"] \
                    or got["levels_differing"]:
                problems.append(f"{name}: browser lookup differs from Python")
            continue
        raw_tol = TOLERANCE["quantiles_raw" if got["output"] == "quantiles" else "probs_raw"]
        if got["raw_max_abs_vs_python"] > raw_tol:
            problems.append(f"{name}: raw output differs by {got['raw_max_abs_vs_python']:.2e} (> {raw_tol})")
        if got["js_rule_on_python_raw_max_abs"] > TOLERANCE["rule_logic"] \
                or got["js_rule_on_python_raw_pct_max_abs"] > TOLERANCE["rule_logic"]:
            problems.append(f"{name}: JavaScript decision rule differs from Python")
        if got["browser_points_differing"]:
            problems.append(f"{name}: {got['browser_points_differing']} browser points differ by > 1e-3 min")
    return {"tolerance": TOLERANCE, "problems": problems, "passed": not problems}


def main() -> int:
    exports = ROOT / "data" / "ml" / "arrival_exports"
    export_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else sorted(exports.iterdir())[-1]
    manifest = json.loads((export_dir / "manifest.json").read_text())
    if not CHROME:
        raise SystemExit("no Playwright Chromium under ~/.cache/ms-playwright")
    results: dict = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(export_dir, results))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/arrival_check.html"
    with tempfile.TemporaryDirectory() as profile:
        chrome = subprocess.Popen(
            [str(CHROME[-1]), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + TIMEOUT_S
            while "body" not in results and time.monotonic() < deadline and chrome.poll() is None:
                time.sleep(0.5)
        finally:
            chrome.terminate()
            chrome.wait(10)
            server.shutdown()
    if "body" not in results:
        raise SystemExit(f"the page posted no result within {TIMEOUT_S}s (chrome exit {chrome.returncode})")
    body = results["body"]
    body["chromium"] = str(CHROME[-1].parent.parent.name)
    body["evaluation"] = evaluate(body, manifest)
    (export_dir / "browser_check.json").write_text(json.dumps(body, indent=1))
    print(json.dumps(body, indent=1))
    return 0 if body["evaluation"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
