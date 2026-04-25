#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "[1/2] Running py_compile..."
python3 - <<'PY'
from pathlib import Path
import py_compile

paths = [Path("os_agent.py")]
paths.extend(sorted(Path("src").rglob("*.py")))
paths.extend(sorted(Path("scripts").rglob("*.py")))

for path in paths:
    py_compile.compile(str(path), doraise=True)

print(f"py_compile OK ({len(paths)} files)")
PY

echo "[2/2] Running config validation..."
python3 os_agent.py --check -c config.yaml

echo "Self-check completed successfully."
