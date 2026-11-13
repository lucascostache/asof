#!/bin/sh
# Everything that can be checked WITHOUT your keys, then the exact commands for what needs them. This script never reads, prints or uses an API key.
#
#   scripts/verify_all.sh                    # the test matrix (needs `uv`), the tamper demo, the offline demo, then your next commands
#   scripts/verify_all.sh 3.12 3.13          # only those Python versions in the matrix
#   scripts/verify_all.sh --print-only       # skip the checks; just print your next commands
#
# Exit 0 only if every check that ran passed. The commands it prints are for YOU to run: the author has never run them against a live service.
set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PRINT_ONLY=0
[ "${1:-}" = "--print-only" ] && { PRINT_ONLY=1; shift; }
PY="${PYTHON:-}"
[ -z "$PY" ] && { [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python" || PY="python3"; }
STATUS=0
section() { printf '\n== %s\n' "$1"; }

if [ "$PRINT_ONLY" -eq 0 ]; then
  section "1. test matrix (ruff, mypy, pytest on each supported Python)"
  if command -v uv >/dev/null 2>&1; then
    scripts/test_matrix.sh "$@" || STATUS=1
  else
    echo "   uv is not installed (pip install uv), so the multi-Python matrix was NOT run."
    echo "   Running pytest once on the current interpreter instead:"
    "$PY" -m pytest -q -p no:cacheprovider 2>&1 | tail -3 || STATUS=1
    STATUS=1
  fi
  section "2. tamper demo (a copy of a throwaway ledger; one byte altered, caught, restored)"
  scripts/tamper_demo.sh || STATUS=1
  section "3. the pitch demo, offline (no network, no model call)"
  DEMO_TMP="$(mktemp -d "${TMPDIR:-/tmp}/asof-verify.XXXXXX")"
  "$PY" demo/run_demo.py --no-network --ledger "$DEMO_TMP/ledger.db" --out "$DEMO_TMP/out" >"$DEMO_TMP/demo.txt" 2>&1 && echo "   ok (output kept in $DEMO_TMP/demo.txt)" || { echo "   FAILED: see $DEMO_TMP/demo.txt"; STATUS=1; }
fi

mark() { eval "v=\${$1:-}"; [ -n "$v" ] && echo "set" || echo "NOT set"; }
section "YOUR NEXT COMMANDS (these need your keys; run them yourself)"
cat <<TXT

Environment right now (names only, values are never shown):  ANTHROPIC_API_KEY: $(mark ANTHROPIC_API_KEY)   OPENAI_API_KEY: $(mark OPENAI_API_KEY)   ASOF_SEC_USER_AGENT: $(mark ASOF_SEC_USER_AGENT)

A. EDGAR: check how acceptanceDateTime should be read (SEC fair-access rules apply; it needs YOUR contact details as the User-Agent)
   export ASOF_SEC_USER_AGENT='<Your Organisation your-email@your-domain>'
   $PY scripts/smoke_edgar.py --cik <CIK of a company you choose> --n 3
   Then open each "EDGAR filing index" URL it prints and compare the page's "Accepted" time (US Eastern) with the raw and converted times.
   (add --no-index to make a single request and do all the comparing by hand). Do not change SECFilings' acceptance_tz unless every filing says the UTC reading agrees.

B. Anthropic: ONE real call, prints the receipt and verifies the ledger (exit 1 on any failure)
   export ANTHROPIC_API_KEY=...        # type it in your own terminal; never paste a key into a chat or a file
   $PY scripts/smoke_anthropic.py --model <a model name you have access to>

C. OpenAI: the same
   export OPENAI_API_KEY=...
   $PY scripts/smoke_openai.py --model <a model name you have access to>

D. Claude Desktop (MCP): follow docs/CLAUDE_DESKTOP.md (setup, then the 3 test prompts)

E. Strathora (private build): Settings > Connections > AsOf clients > Create key, then
   export ASOF_SYNC_API_KEY=<the key it shows once>
   $PY demo/run_demo.py            # step 6 syncs to http://localhost:3000/api/asof/sync and prints the link to open

F. What each result means and what to do if one fails: docs/pilot/LIMITATIONS.md and docs/REFERENCE.md "What AsOf does NOT prove".

TXT
if [ "$STATUS" -eq 0 ]; then [ "$PRINT_ONLY" -eq 1 ] && echo "(--print-only: no checks were run)" || echo "ALL KEYLESS CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi
exit $STATUS
