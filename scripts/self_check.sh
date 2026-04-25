#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "[1/3] Running unittest suite..."
python3 -m unittest discover -s tests -p "test*.py"

echo "[2/3] Running py_compile..."
python3 - <<'PY'
from pathlib import Path
import py_compile

paths = [Path("os_agent.py")]
paths.extend(sorted(Path("src").rglob("*.py")))
paths.extend(sorted(Path("scripts").rglob("*.py")))
paths.extend(sorted(Path("tests").glob("test_*.py")))

for path in paths:
    py_compile.compile(str(path), doraise=True)

print(f"py_compile OK ({len(paths)} files)")
PY

if [ -f tests/harness/run_scenarios.py ]; then
  echo "[3/3] Running scenario harness..."
  python3 tests/harness/run_scenarios.py
else
  echo "[3/3] Scenario harness not found, skipped."
fi

echo "Self-check completed successfully."
