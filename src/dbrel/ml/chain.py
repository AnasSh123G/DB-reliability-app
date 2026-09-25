"""Run several commands one after another inside one launch unit.

`python -m dbrel.ml.chain '<json list of argv lists>'`. Every command runs even
if an earlier one failed (a failed bake-off does not make the TabM search
useless); each exit code is written to the log, and the chain exits non-zero if
any command did. A memory-limit kill stops the whole unit, chain included --
the unit's exit reason says so.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def main(argv: list[str]) -> int:
    commands = json.loads(argv[0])
    codes = []
    for i, command in enumerate(commands, 1):
        print(f"[chain {_stamp()}] step {i}/{len(commands)}: {' '.join(command)}", flush=True)
        code = subprocess.call(command)
        codes.append(code)
        print(f"[chain {_stamp()}] step {i}/{len(commands)} exit={code}", flush=True)
    print(f"[chain {_stamp()}] exit codes: {codes}", flush=True)
    return 0 if all(code == 0 for code in codes) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
