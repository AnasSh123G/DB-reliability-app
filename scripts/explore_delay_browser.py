"""Check the actual frontend predictor against Python fixtures in Chromium.

Offline only: serves candidate fixtures under the app's existing model API
paths, transpiles the unchanged tabm.ts module, and uses the installed WASM
runtime. Run under a private memory-capped systemd service.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dbrel.ml.arrival import distribution_point
from explore_compact_delay import guard


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runtime-root", type=Path, required=True)
    p.add_argument("--experiment", type=Path, required=True)
    args = p.parse_args()
    guard()
    runtime = args.runtime_root / "bahn-buddy-predict/node_modules"
    source = ROOT / "bahn-buddy-predict/src/lib/tabm.ts"
    transpile = """const fs=require('fs'); const ts=require(process.argv[1]);
process.stdout.write(ts.transpileModule(fs.readFileSync(process.argv[2],'utf8'),
{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText);"""
    js = subprocess.check_output(["node", "-e", transpile, str(runtime / "typescript"), str(source)], text=True)
    js = js.replace('from "onnxruntime-web/wasm"', 'from "/ort/ort.wasm.min.mjs"')
    js += '\nort.env.wasm.wasmPaths="/ort/"; ort.env.wasm.numThreads=1;\n'
    check_dir = args.experiment / "browser_check"
    manifest = json.loads((check_dir / "manifest.json").read_text())
    [candidate] = manifest["candidates"]
    ref = json.loads((check_dir / "reference/reference.json").read_text())

    def array(spec):
        return np.fromfile(check_dir / "reference" / spec["file"], dtype=spec["dtype"]).reshape(spec["shape"])

    cats = array(ref["inputs"]["cat_robust"])
    nums = array(ref["inputs"]["num_robust"])
    raw = array(ref["expected"][candidate]["raw"])
    prep = json.loads((check_dir / candidate / "prep.json").read_text())
    fixtures = {"inputs": [{"cat": c.tolist(), "num": n.tolist()} for c, n in zip(cats, nums)], "cases": {}}
    cases = [("median", "median", 3)]
    if prep["output"] == "probs":
        cases += [("window3", "window", 3), ("window5", "window", 5)]
    for name, rule, half in cases:
        if prep["output"] == "probs":
            point, pct = distribution_point(raw, np.asarray(prep["edges"]), rule, half)
        else:
            point, pct = raw[:, 1], raw
        fixtures["cases"][name] = {
            "meta": {"kind": "arrival-net", "output": prep["output"], "sides": ["arr"],
                     "point_rule": rule, "edges": prep["edges"], "window_half_min": half},
            "expected": [{"point_min": float(q), "p25_min": float(v[0]), "p75_min": float(v[2])}
                         for q, v in zip(point, pct)]}

    page = """<!doctype html><meta charset='utf-8'><script type='module'>
import {predictBatch} from '/predictor.mjs';
import * as ort from '/ort/ort.wasm.min.mjs';
const result={cases:{}};
try {
 const fixture=await (await fetch('/fixture.json')).json();
 for(const [name,entry] of Object.entries(fixture.cases)) {
  const t=performance.now();
  const got=await predictBatch(name, fixture.inputs);
  const batchMs=performance.now()-t;
  let maxAbs=0, wrongShape=0;
  got.forEach((p,i)=>{
   if(Object.keys(p).join()!=='arr'||p.arr.rule!==entry.meta.point_rule)wrongShape++;
   for(const key of ['point_min','p25_min','p75_min'])
    maxAbs=Math.max(maxAbs,Math.abs(p.arr[key]-entry.expected[i][key]));
  });
  let singleMax=0;
  const latencies=[];
  const sample=Array.from({length:21},(_,i)=>i===20?fixture.inputs.length-1:i);
  for(const i of sample) {
   const s=performance.now();const [one]=await predictBatch(name,[fixture.inputs[i]]);
   latencies.push(performance.now()-s);
   for(const key of ['point_min','p25_min','p75_min'])
    singleMax=Math.max(singleMax,Math.abs(one.arr[key]-got[i].arr[key]));
  }
  latencies.sort((a,b)=>a-b);
  result.cases[name]={rows:got.length,max_abs_vs_python:maxAbs,single_vs_batch_max_abs:singleMax,
     wrong_shape:wrongShape,load_and_batch_ms:batchMs,single_p50_ms:latencies[10],single_p95_ms:latencies[19],
     single_unknown_categories_checked:fixture.inputs[sample[20]].cat.every(x=>x===0)};
 }
 if(await (await fetch('/tree.onnx')).ok) {
  try {
   const s=await ort.InferenceSession.create('/tree.onnx',{executionProviders:['wasm']});
   const out=(await s.run({X:new ort.Tensor('float32',new Float32Array([-1,0.5,2]),[3,1])})).Y.data;
   result.tree_smoke={values:Array.from(out),passed:Array.from(out).join()=='2,2,7'};
   await s.release();
  } catch(error) {result.tree_smoke={passed:false,error:String(error)};}
 }
} catch(error) {result.error=String(error);}
await fetch('/result',{method:'POST',body:JSON.stringify(result)});
</script>"""
    results = {}

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript",
                          ".wasm": "application/wasm"}
        def translate_path(self, path):
            if path.startswith("/ort/"):
                base = runtime / "onnxruntime-web/dist"
                target = (base / path[5:].split("?", 1)[0]).resolve()
                return str(target) if base.resolve() in target.parents else str(base / "missing")
            return str(args.experiment / "missing")
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/ort/"):
                return super().do_GET()
            if path == "/":
                body, mime = page.encode(), "text/html"
            elif path == "/predictor.mjs":
                body, mime = js.encode(), "text/javascript"
            elif path == "/fixture.json":
                body, mime = json.dumps(fixtures).encode(), "application/json"
            elif path == "/tree.onnx" and (args.experiment / "tree.onnx").exists():
                body, mime = (args.experiment / "tree.onnx").read_bytes(), "application/octet-stream"
            elif path.startswith("/api/model/") and path.endswith("/model.onnx"):
                body, mime = (check_dir / candidate / "model.onnx").read_bytes(), "application/octet-stream"
            elif path.startswith("/api/model/") and path.endswith("/meta.json"):
                body, mime = json.dumps(fixtures["cases"][path.split("/")[3]]["meta"]).encode(), "application/json"
            else:
                return self.send_error(404)
            self.send_response(200); self.send_header("Content-Type", mime); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            results.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204); self.end_headers()
        def log_message(self, *a):
            pass

    chrome = sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux64/chrome"))[-1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Chromium's singleton socket has the Unix pathname limit. Keep its own
    # TMPDIR short as well as its profile, without leaving this repository.
    with tempfile.TemporaryDirectory(prefix=".bt-", dir=ROOT) as scratch:
        with (args.experiment / "chromium-stderr.log").open("w") as stderr:
            proc = None
            try:
                proc = subprocess.Popen([str(chrome), "--headless=new", "--disable-gpu", "--no-first-run",
                                         f"--user-data-dir={scratch}/p", f"http://127.0.0.1:{server.server_port}/"],
                                        env={**os.environ, "TMPDIR": scratch},
                                        stdout=subprocess.DEVNULL, stderr=stderr)
                until = time.monotonic()+180
                while not results and time.monotonic() < until and proc.poll() is None:
                    time.sleep(0.25)
                if not results:
                    results["error"] = f"Chromium returned no result (exit={proc.poll()}); see chromium-stderr.log"
            except OSError as error:
                results["error"] = f"Chromium launch failed: {error}; see chromium-stderr.log"
            finally:
                try:
                    if proc is not None:
                        if proc.poll() is None:
                            proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            results["error"] = "Chromium required forced shutdown; see chromium-stderr.log"
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                results["error"] = "Chromium did not exit after kill; see chromium-stderr.log"
                finally:
                    server.shutdown()
    results["passed"] = bool(results.get("cases")) and "error" not in results and all(
        d["max_abs_vs_python"] <= 1e-3 and d["single_vs_batch_max_abs"] <= 1e-3 and d["wrong_shape"] == 0
        for d in results.get("cases", {}).values())
    results["runtime_version"] = json.loads((runtime / "onnxruntime-web/package.json").read_text())["version"]
    (args.experiment / "actual_frontend_browser.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
