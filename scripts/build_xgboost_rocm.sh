#!/usr/bin/env bash
# Build XGBoost against ROCm/HIP for the local AMD GPU and install it into .venv.
#
# Why this exists: PyPI's xgboost wheel is CPU/CUDA only, AMD's prebuilt ROCm
# wheels target Python 3.12 (this venv is 3.13), and gfx1201 (RX 9070 XT) is not
# one of their default targets. Upstream XGBoost (3.4.1 checked) has no HIP
# build option at all; AMD's fork does, newest on its release/3.2.0 branch.
# See openspec change add-offline-delay-model, design.md D10.
#
# Needs (Arch/CachyOS, official repos):
#   sudo pacman -S rocm-hip-runtime rocm-llvm hipcub rocprim rocthrust rccl
# (rccl: the fork includes rccl/rccl.h unconditionally in HIP builds)
# plus cmake, ninja and git.
#
# Re-runnable. A later `uv sync` reinstalls PyPI's CPU wheel (the ml extra
# requires `xgboost`, and the fork's distribution is named `amd_xgboost`); run
# this script again afterwards.
#
# Usage: scripts/build_xgboost_rocm.sh [gfx-arch]   (default: gfx1201)
set -euo pipefail

ARCH="${1:-gfx1201}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
ROCM="${ROCM_PATH:-/opt/rocm}"
WORK="${XGB_BUILD_DIR:-$ROOT/data/build/xgboost-rocm}"
REPO="${XGB_REPO:-https://github.com/ROCm/xgboost.git}"
BRANCH="${XGB_BRANCH:-release/3.2.0}"
# XGB_JOBS=2 leaves the rest of the CPU to a training run going on meanwhile.
JOBS="${XGB_JOBS:-$(nproc)}"

log() { printf '\n==> %s\n' "$*"; }

command -v cmake >/dev/null || { echo "cmake is required" >&2; exit 1; }
[ -x "$ROCM/bin/hipcc" ] || [ -x "$ROCM/lib/llvm/bin/clang++" ] || {
  echo "no HIP compiler under $ROCM -- install rocm-llvm and rocm-hip-runtime" >&2; exit 1; }
for header in hipcub/hipcub.hpp rocprim/rocprim.hpp thrust/version.h rccl/rccl.h; do
  [ -e "$ROCM/include/$header" ] || { echo "missing $ROCM/include/$header -- install hipcub/rocprim/rocthrust/rccl" >&2; exit 1; }
done

log "HIP smoke check: does ROCm see $ARCH?"
# Capture first: `rocminfo | grep -q` under pipefail fails whenever grep exits
# early and rocminfo dies of SIGPIPE, even though the arch was found.
AGENTS="$("$ROCM/bin/rocminfo")"
grep -q "Name: *$ARCH" <<<"$AGENTS" || { echo "rocminfo does not list $ARCH" >&2; exit 1; }

mkdir -p "$WORK"
SRC="$WORK/src-${BRANCH//\//-}"
if [ ! -d "$SRC/.git" ]; then
  log "cloning $REPO ($BRANCH)"
  git clone --depth 1 --branch "$BRANCH" --recurse-submodules --shallow-submodules "$REPO" "$SRC"
fi
grep -q "USE_HIP" "$SRC/CMakeLists.txt" || { echo "$REPO $BRANCH has no USE_HIP option" >&2; exit 1; }

log "configuring for $ARCH"
export PATH="$ROCM/bin:$ROCM/lib/llvm/bin:$PATH"
cmake -S "$SRC" -B "$SRC/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DUSE_HIP=ON \
  -DCMAKE_HIP_ARCHITECTURES="$ARCH" \
  -DCMAKE_PREFIX_PATH="$ROCM" \
  -DCMAKE_HIP_COMPILER="$ROCM/lib/llvm/bin/clang++"

log "building libxgboost ($JOBS jobs; ~20-40 min)"
cmake --build "$SRC/build" -j "$JOBS"

log "installing the Python package against the HIP library"
# The fork's distribution is named amd_xgboost but provides the same `xgboost`
# module as PyPI's wheel, so the PyPI one has to go first.
uv pip uninstall --python "$VENV/bin/python" xgboost 2>/dev/null || true
# Without use_system_libxgboost the packager takes the library just built in
# $SRC/lib/ (use_system_libxgboost would search the *build* interpreter's
# prefix, which under build isolation is not this venv).
uv pip install --python "$VENV/bin/python" --reinstall --no-deps "$SRC/python-package"

log "verifying"
"$VENV/bin/python" - <<'EOF'
import numpy as np
import xgboost as xgb

info = xgb.build_info()
print("xgboost", xgb.__version__, "USE_HIP =", info.get("USE_HIP"), "USE_CUDA =", info.get("USE_CUDA"))
assert info.get("USE_HIP"), "this build has no HIP support"
X = np.random.default_rng(0).random((10_000, 5)).astype(np.float32)
d = xgb.DMatrix(X, label=X[:, 0])
xgb.train({"device": "cuda", "tree_method": "hist"}, d, num_boost_round=5)
print("GPU training OK")
EOF
