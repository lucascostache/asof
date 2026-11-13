#!/bin/sh
# Run the CI steps (see .github/workflows/ci.yml) on every supported Python, locally. Needs `uv` (pip install uv). Usage: scripts/test_matrix.sh [3.10 3.11 ...]
set -u
cd "$(dirname "$0")/.."
VERSIONS="${*:-3.10 3.11 3.12 3.13 3.14}"
STATUS=0
for v in $VERSIONS; do
  d=".matrix/py$v"
  rm -rf "$d"
  uv venv -q --python "$v" "$d" || { echo "== $v: could not create a venv"; STATUS=1; continue; }
  uv pip install -q --python "$d/bin/python" -e '.[dev]' || { echo "== $v: install failed"; STATUS=1; continue; }
  echo "== Python $("$d/bin/python" --version 2>&1 | cut -d' ' -f2)"
  "$d/bin/python" -m ruff check asof scripts demo --select E9,F,B -q && echo "   ruff:   ok" || { echo "   ruff:   FAILED"; STATUS=1; }
  "$d/bin/python" -m mypy asof --ignore-missing-imports 2>&1 | tail -1 | sed 's/^/   mypy:   /'
  "$d/bin/python" -m pytest -q -p no:cacheprovider 2>&1 | tail -1 | sed 's/^/   pytest: /'
  "$d/bin/python" -m pytest -q -p no:cacheprovider >/dev/null 2>&1 || STATUS=1
done
exit $STATUS
