"""Check the actual frontend predictor against Python fixtures in Chromium.

Offline only: serves candidate fixtures under the app's existing model API
paths, transpiles the unchanged tabm.ts module, and uses the installed WASM
runtime. Run under a private memory-capped systemd service.
"""
from __future__ import annotations

import argparse
import hashlib
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
from browser_parity import POLICY, report_passes


def browser_identity(binary: Path, expected_sha256: str) -> dict[str, str]:
    """Return identity only when this exact browser binary matches its pin."""
    binary = Path(binary).expanduser().resolve(strict=True)
    if not binary.is_file():
        raise ValueError("Chromium binary is not a file")
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected_sha256):
        raise ValueError("Chromium sha256 pin must be 64 hexadecimal characters")
    with binary.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest.lower() != expected_sha256.lower():
        raise ValueError(f"Chromium sha256 mismatch for {binary}")
    completed = subprocess.run([str(binary), "--version"], check=True, capture_output=True,
                               text=True, timeout=15)
    version = completed.stdout.strip() or completed.stderr.strip()
    if not version:
        raise ValueError("Chromium version output is missing")
    return {"path": str(binary), "sha256": digest, "version": version}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runtime-root", type=Path, required=True)
    p.add_argument("--experiment", type=Path, required=True)
    p.add_argument("--chromium", type=Path, required=True)
    p.add_argument("--chromium-sha256", required=True)
    args = p.parse_args()
    guard()
    runtime = args.runtime_root / "bahn-buddy-predict/node_modules"
    source = ROOT / "bahn-buddy-predict/src/lib/tabm.ts"
    transpile = """const fs=require('fs'); const ts=require(process.argv[1]);
process.stdout.write(ts.transpileModule(fs.readFileSync(process.argv[2],'utf8'),
{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText);"""
    js = subprocess.check_output(["node", "-e", transpile, str(runtime / "typescript"), str(source)], text=True)
    js = js.replace('from "onnxruntime-web/wasm"', 'from "/ort/ort.wasm.min.mjs"')
    js += '\nort.env.wasm.wasmPaths="/ort/"; ort.env.wasm.numThreads=1;\nexport {distributionPoint};\n'
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
    identity_paths={'model':check_dir/candidate/'model.onnx', 'prep':check_dir/candidate/'prep.json',
                    'reference':check_dir/'reference/reference.json'}
    for name,spec in (('native_raw',ref['expected'][candidate]['raw']),
                      ('cat',ref['inputs']['cat_robust']),('num',ref['inputs']['num_robust'])):
        identity_paths[name]=check_dir/'reference'/spec['file']
    identities={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in identity_paths.items()}
    fixtures = {"inputs": [{"cat": c.tolist(), "num": n.tolist()} for c, n in zip(cats, nums)],
                "cpu_raw": raw.tolist(), "output": prep['output'], "cases": {}}
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
import {predictBatch,distributionPoint} from '/predictor.mjs';
import * as ort from '/ort/ort.wasm.min.mjs';
const result={cases:{}};
try {
 const fixture=await (await fetch('/fixture.json')).json();
 let webRaw=null;
 if(fixture.output==='probs') {
  const session=await ort.InferenceSession.create('/api/model/median/model.onnx',{executionProviders:['wasm']});
  const rows=fixture.inputs.length;
  const cat=new BigInt64Array(fixture.inputs.flatMap(p=>p.cat).map(BigInt));
  const num=new Float32Array(fixture.inputs.flatMap(p=>p.num));
  const out=(await session.run({cat:new ort.Tensor('int64',cat,[rows,fixture.inputs[0].cat.length]),
      num:new ort.Tensor('float32',num,[rows,fixture.inputs[0].num.length])})).probs;
  webRaw=out.data;result.web_raw={shape:Array.from(out.dims),data:Array.from(webRaw)};
  await session.release();
 }
 for(const [name,entry] of Object.entries(fixture.cases)) {
  const t=performance.now();
  const got=await predictBatch(name, fixture.inputs);
  const batchMs=performance.now()-t;
  let maxAbs=0, wrongShape=Number(got.length!==fixture.inputs.length), nonfinite=0,unordered=0;
  const fields=['point_min','p25_min','p75_min'];
  const fieldMax=Object.fromEntries(fields.map(k=>[k,0]));
  const rounded=Object.fromEntries(fields.map(k=>[k,0]));
  const shown=v=>v<1?0:Math.round(v);
  got.forEach((p,i)=>{
   if(Object.keys(p).join()!=='arr'||p.arr.rule!==entry.meta.point_rule)wrongShape++;
   for(const key of fields) {
    if(typeof p.arr[key]!=='number'||!Number.isFinite(p.arr[key])) {nonfinite++;continue;}
    const error=Math.abs(p.arr[key]-entry.expected[i][key]);
    fieldMax[key]=Math.max(fieldMax[key],error);maxAbs=Math.max(maxAbs,error);
    rounded[key]+=Number(shown(p.arr[key])!==shown(entry.expected[i][key]));
   }
   if(p.arr.p25_min>p.arr.p75_min||(entry.meta.point_rule==='median'&&
      (p.arr.point_min<p.arr.p25_min||p.arr.point_min>p.arr.p75_min)))unordered++;
  });
  let decoderNative=0;
  const decodedWeb=[];
  if(fixture.output==='probs') {
   const cpuRaw=new Float32Array(fixture.cpu_raw.flat());
   got.forEach((_,i)=>{
    const native=distributionPoint(cpuRaw,i,entry.meta.edges,entry.meta.point_rule,entry.meta.window_half_min);
    const decoded=distributionPoint(webRaw,i,entry.meta.edges,entry.meta.point_rule,entry.meta.window_half_min);
    decodedWeb.push(decoded);
    for(const key of fields)decoderNative=Math.max(decoderNative,Math.abs(native[key]-entry.expected[i][key]));
   });
  }
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
     single_unknown_categories_checked:fixture.inputs[sample[20]].cat.every(x=>x===0),
     field_max_abs:fieldMax,rounded_mismatches:rounded,nonfinite_outputs:nonfinite,unordered_outputs:unordered,
     decoder_native_max_abs:decoderNative,decoded_web:decodedWeb};
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
    chrome = args.chromium.expanduser().resolve()
    try:
        identity_before = browser_identity(chrome, args.chromium_sha256)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        identity_before = None
        results["error"] = f"Pinned Chromium identity check failed: {error}"

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

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Chromium's singleton socket has the Unix pathname limit. Keep its own
    # TMPDIR short as well as its profile, without leaving this repository.
    with tempfile.TemporaryDirectory(prefix=".bt-", dir=ROOT) as scratch:
        with (args.experiment / "chromium-stderr.log").open("w") as stderr:
            proc = None
            try:
                if identity_before is None:
                    raise OSError("pinned Chromium identity is unavailable")
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
    if identity_before is not None:
        try:
            identity_after = browser_identity(chrome, args.chromium_sha256)
            if identity_after != identity_before:
                results["error"] = "Chromium identity changed during execution"
            else:
                results["chromium"] = identity_after
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            results["error"] = f"Pinned Chromium identity check failed after execution: {error}"
    try:
        runtime_version = json.loads((runtime / "onnxruntime-web/package.json").read_text())["version"]
    except (OSError, KeyError, json.JSONDecodeError) as error:
        runtime_version = None
        results["error"] = f"ONNX Runtime Web version is missing: {error}"
    results["runtime_version"] = runtime_version
    results.update(parity_protocol=2, parity_policy=dict(POLICY), output=prep['output'],
                   exact_quantiles=prep['output']=='quantiles' and prep.get('config',{}).get('head')=='binned')
    results.update(fixture_rows=len(raw),fixture_identity=identities,
                   checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    if identities!={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in identity_paths.items()}:
        results['error']='Model or fixture identity changed during browser execution'
    if prep['output']=='probs' and 'error' not in results:
        try:
            payload=results.pop('web_raw')
            if payload['shape']!=list(raw.shape):
                raise ValueError('Web probability shape differs from native fixture')
            web=np.asarray(payload['data'],dtype=np.float64).reshape(raw.shape)
            native=raw.astype(np.float64)
            invalid=(~np.isfinite(web).all(axis=1)) | (web<0).any(axis=1) | (web>1).any(axis=1) \
                | (~np.isfinite(native).all(axis=1)) | (native<0).any(axis=1) | (native>1).any(axis=1) \
                | (np.abs(web.sum(axis=1)-1)>1e-4) | (np.abs(native.sum(axis=1)-1)>1e-4)
            if invalid.any():
                raise ValueError('Web/native probability rows must be finite and normalized')
            normalized_web=web/web.sum(axis=1,keepdims=True)
            normalized_native=native/native.sum(axis=1,keepdims=True)
            results['probability_metrics']={'rows':len(raw),'invalid_rows':0,
                'raw_linf':float(np.max(np.abs(web-native))),
                'normalized_tv':float(np.max(np.abs(normalized_web-normalized_native).sum(axis=1))/2),
                'cdf_linf':float(np.max(np.abs(np.cumsum(normalized_web,axis=1)-np.cumsum(normalized_native,axis=1))))}
            for name,rule,half in cases:
                point,pct=distribution_point(web,np.asarray(prep['edges']),rule,half)
                case=results['cases'][name]
                decoded=case.pop('decoded_web')
                if len(decoded)!=len(raw):
                    raise ValueError('same-Web-input decoder fixture rows differ')
                expected=np.column_stack((point,pct[:,0],pct[:,2]))
                got=np.array([[q[field] for field in ('point_min','p25_min','p75_min')] for q in decoded])
                case['decoder_web_max_abs']=float(np.max(np.abs(expected-got)))
        except (KeyError,ValueError,TypeError) as error:
            results['error']=f'Browser component parity failed: {error}'
    else:
        for case in results.get('cases',{}).values():
            case.pop('decoded_web',None)
    results["passed"] = "error" not in results and _valid_output_identity(results, args.chromium_sha256) \
        and runtime_version == "1.30.0" and report_passes(results)
    (args.experiment / "actual_frontend_browser.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0 if results["passed"] else 1


def _valid_output_identity(results: dict, expected_sha256: str) -> bool:
    identity = results.get("chromium")
    return (
        isinstance(identity, dict)
        and set(identity) == {"path", "sha256", "version"}
        and isinstance(identity["path"], str) and bool(identity["path"])
        and isinstance(identity["sha256"], str)
        and identity["sha256"].lower() == expected_sha256.lower()
        and isinstance(identity["version"], str) and bool(identity["version"])
    )


if __name__ == "__main__":
    sys.exit(main())
