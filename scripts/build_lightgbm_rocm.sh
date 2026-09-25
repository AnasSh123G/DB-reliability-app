#!/usr/bin/env bash
# Build AMD's ROCm port of LightGBM for the local GPU, into its OWN venv.
#
# An experiment, not part of the pipeline: AMD's prebuilt amd_lightgbm wheel
# only carries gfx906 code (checked 2026-09-24 with clang-offload-bundler), so
# an RX 9070 XT (gfx1201) needs a source build. It goes into a separate venv so
# the project's .venv -- and any training running from it -- is untouched.
# Whether it computes correct results on this card is what
# scripts/check_lightgbm_rocm.py then tests.
#
# Needs: rocm-hip-runtime rocm-llvm (and cmake, ninja, git, uv).
# Usage: [LGBM_JOBS=2] scripts/build_lightgbm_rocm.sh [gfx-arch]   (default: gfx1201)
set -euo pipefail

ARCH="${1:-gfx1201}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROCM="${ROCM_PATH:-/opt/rocm}"
WORK="${LGBM_BUILD_DIR:-$ROOT/data/build/lightgbm-rocm}"
REPO="${LGBM_REPO:-https://github.com/AMD-Ecosystem/LightGBM.git}"
BRANCH="${LGBM_BRANCH:-release/4.6.0.99}"
VENV="$WORK/venv"
JOBS="${LGBM_JOBS:-$(nproc)}"

log() { printf '\n==> %s\n' "$*"; }

AGENTS="$("$ROCM/bin/rocminfo")"
grep -q "Name: *$ARCH" <<<"$AGENTS" || { echo "rocminfo does not list $ARCH" >&2; exit 1; }

mkdir -p "$WORK"
SRC="$WORK/src"
if [ ! -d "$SRC/.git" ]; then
  log "cloning $REPO ($BRANCH)"
  git clone --depth 1 --branch "$BRANCH" --recurse-submodules --shallow-submodules "$REPO" "$SRC"
fi

log "configuring for $ARCH"
export PATH="$ROCM/bin:$ROCM/lib/llvm/bin:$PATH"
cmake -S "$SRC" -B "$SRC/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DUSE_ROCM=1 \
  -DCMAKE_HIP_ARCHITECTURES="$ARCH" \
  -DCMAKE_PREFIX_PATH="$ROCM" \
  -DCMAKE_HIP_COMPILER="$ROCM/lib/llvm/bin/clang++"

log "building lib_lightgbm ($JOBS jobs)"
cmake --build "$SRC/build" -j "$JOBS"

log "installing into the separate venv $VENV"
[ -x "$VENV/bin/python" ] || uv venv --python 3.13 "$VENV"
uv pip install --python "$VENV/bin/python" numpy scipy scikit-build-core build
(cd "$SRC" && PATH="$VENV/bin:$PATH" sh ./build-python.sh install --precompile)

log "verifying"
"$VENV/bin/python" - <<'EOF'
import lightgbm as lgb
import numpy as np

print("lightgbm", lgb.__version__, lgb.__file__)
X = np.random.default_rng(0).random((20_000, 5))
y = X[:, 0] * 3 + X[:, 1]
b = lgb.train({"device_type": "cuda", "objective": "regression", "verbosity": -1},
              lgb.Dataset(X, label=y), num_boost_round=20)
print("GPU training OK, rmse", float(np.sqrt(np.mean((b.predict(X) - y) ** 2))))
EOF
