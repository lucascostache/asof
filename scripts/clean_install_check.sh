#!/usr/bin/env bash
# Install AsOf with ONE pip line into a brand-new virtual environment, from a git address, and check that the installed copy works on its own:
# from an empty folder, with a throwaway HOME, never importing this checkout.
#
#   scripts/clean_install_check.sh                         # installs from this checkout's committed HEAD (git+file://...)
#   scripts/clean_install_check.sh git+https://host/owner/repo.git      # installs from a remote, exactly as a pilot user would
#   PYTHON=python3.12 scripts/clean_install_check.sh       # choose the interpreter
#
# It needs network access (pip downloads setuptools to build, and the 'mcp' package for the second half). It writes only under a temporary folder,
# which it removes. Exit 0 only if every check passed. This is not a "clean machine": it is a clean environment on this machine.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-git+file://localhost$HERE}"        # "localhost" on purpose: the pip that ships with Python 3.10 (23.0.1) refuses "name[extra] @ git+file:///path" (no host); it accepts https addresses
PY="${PYTHON:-python3}"
TMP="$(cd "$(mktemp -d)" && pwd -P)"          # the real path: on macOS the temporary folder is behind a symlink (/var -> /private/var), and step 2 compares paths
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home" "$TMP/work"
export HOME="$TMP/home"
unset PYTHONPATH ANTHROPIC_API_KEY OPENAI_API_KEY ASOF_SYNC_ENABLED ASOF_SYNC_ENDPOINT ASOF_SYNC_API_KEY || true
cd "$TMP/work"

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "source: $SRC   python: $("$PY" --version 2>&1)"

step "1. core, one line:  pip install \"$SRC\""
"$PY" -m venv "$TMP/core"
"$TMP/core/bin/pip" list --disable-pip-version-check --format=freeze | sort > before.txt      # what a new environment already holds (pip; setuptools too before Python 3.12)
"$TMP/core/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check "$SRC"
"$TMP/core/bin/pip" list --disable-pip-version-check --format=freeze | sort > after.txt
comm -13 before.txt after.txt > core.txt                                                        # what the install added
cat core.txt
[ "$(wc -l < core.txt | tr -d ' ')" = "1" ] || fail "the core install pulled in other packages (it must have no dependencies)"

step "2. the installed copy is the one that runs (not the checkout)"
WHERE="$("$TMP/core/bin/python" -c 'import asof; print(asof.__file__)')"
echo "$WHERE"
case "$WHERE" in "$TMP"/core/*) ;; *) fail "asof was imported from $WHERE" ;; esac
"$TMP/core/bin/strathora" --version
[ "$("$TMP/core/bin/strathora" --version)" = "$("$TMP/core/bin/asof" --version)" ] || fail "the alias 'asof' is not the same program as 'strathora'"
grep -q '^strathora-asof' core.txt || fail "the installed distribution is not called strathora-asof: $(cat core.txt)"

step "3. the widget files are in the install"
"$TMP/core/bin/python" -c 'from asof import widgets; import pathlib; d = pathlib.Path(widgets.__file__).parent; names = sorted(p.name for p in d.iterdir() if p.suffix in (".js", ".css")); print(names); assert names == ["asof-widgets.css", "asof-widgets.js"], names'

step "4. demo: init, a time-locked query, the receipt, verify"
"$TMP/core/bin/strathora" init --ledger demo/ledger.db --demo demo > init.out
"$TMP/core/bin/python" - > query.out <<'PY'
from asof import Session, sources
s = Session(as_of="2023-01-21T14:00:00Z", ledger_path="demo/ledger.db")
s.add_source(sources.CSV("demo/prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
rows = s.query("prices", symbol="ACME-SYN")
print("closes:", [r.payload["close"] for r in rows])
print("receipt_id:", s.receipt().call_id)
print(s.receipt().summary())
s.close()
PY
cat query.out
grep -q 'closes: \[147.0, 148.0, 150.0\]' query.out || fail "the time-locked query did not return the three closes knowable at the clock"
grep -q 'blocked: future=3' query.out || fail "the receipt does not show the three later closes as blocked"
"$TMP/core/bin/strathora" verify --ledger demo/ledger.db
"$TMP/core/bin/strathora" receipt "$(sed -n 's/^receipt_id: //p' query.out)" --ledger demo/ledger.db > /dev/null

step "5. report: one self-contained HTML file from the ledger"
"$TMP/core/bin/strathora" report --ledger demo/ledger.db --html report.html --synthetic
grep -q 'asof-receipt' report.html || fail "the report has no receipt widget"

step "6. a mistyped ledger path is refused, not created"
if "$TMP/core/bin/strathora" verify --ledger nowhere/ledger.db 2> missing.err; then fail "verify said OK for a ledger that does not exist"; fi
cat missing.err
[ ! -e nowhere ] || fail "verify created a ledger at a path that did not exist"

step "7. serve-mcp without the mcp package: one line, exit 2"
set +e
"$TMP/core/bin/strathora" serve-mcp --config demo/asof.config.json > /dev/null 2> nomcp.err
RC=$?
set -e
cat nomcp.err
[ "$RC" = "2" ] || fail "expected exit 2, got $RC"
if grep -q Traceback nomcp.err; then fail "a traceback was printed"; fi

step "8. with the MCP extra, one line:  pip install \"strathora-asof[mcp] @ $SRC\""
"$PY" -m venv "$TMP/mcp"
"$TMP/mcp/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check "strathora-asof[mcp] @ $SRC"
BEFORE="$("$TMP/mcp/bin/strathora" verify --ledger demo/ledger.db --json | "$TMP/mcp/bin/python" -c 'import json, sys; print(json.load(sys.stdin)["n_events"])')"
"$TMP/mcp/bin/python" - <<'PY'
import asyncio, sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def go():
    async with Client(StdioServerParameters(command=sys.executable, args=["-m", "asof", "serve-mcp", "--config", "demo/asof.config.json"])) as c:
        names = sorted(t.name for t in (await c.list_tools()).tools)
        q = (await c.call_tool("query", {"source": "prices", "filters": {"symbol": "ACME-SYN"}})).structured_content
        denied = await c.call_tool("advance_clock", {"by": "1d"})
        return names, q, denied

names, q, denied = asyncio.run(go())
print("tools:", names)
print("closes over MCP:", [r["payload"]["close"] for r in q["records"]])
assert [r["payload"]["close"] for r in q["records"]] == [147.0, 148.0, 150.0]
assert denied.is_error, "the model was allowed to move the clock"
print("clock move refused:", denied.content[0].text)
PY
"$TMP/mcp/bin/strathora" verify --ledger demo/ledger.db
AFTER="$("$TMP/mcp/bin/strathora" verify --ledger demo/ledger.db --json | "$TMP/mcp/bin/python" -c 'import json, sys; print(json.load(sys.stdin)["n_events"])')"
echo "ledger events before the MCP session: $BEFORE, after: $AFTER"
[ "$AFTER" -gt "$BEFORE" ] || fail "the MCP session's calls are not in the ledger the demo config names (it wrote somewhere else)"
[ -z "$(find . -name ledger.db -not -path './demo/ledger.db')" ] || fail "a second ledger file appeared: $(find . -name ledger.db)"

step "9. the README's Install and Quickstart, typed exactly as they are written, in a new empty folder"
# The commands and the code come out of the README itself (the committed one), so a README that drifts from the program fails here.
mkdir -p "$TMP/readme" "$TMP/bin"
git -C "$HERE" show HEAD:README.md > "$TMP/README.md" 2>/dev/null || cp "$HERE/README.md" "$TMP/README.md"
ln -s "$(command -v "$PY")" "$TMP/bin/python3"                       # the README says "python3": make that the interpreter under test
"$TMP/mcp/bin/python" - "$TMP/README.md" "$TMP/readme" "$SRC" <<'PY'
import pathlib, re, sys
readme, out, src = pathlib.Path(sys.argv[1]).read_text(), pathlib.Path(sys.argv[2]), sys.argv[3]

def block(name):
    m = re.search(rf"<!-- {name}:start -->\n```\w*\n(.*?)```\n<!-- {name}:end -->", readme, re.S)
    assert m, f"the README has no {name} block"
    return m.group(1)

install = re.search(r"## Install\n\n```bash\n(.*?)```", readme, re.S).group(1)
README_REQ = '"strathora-asof[mcp]"'
assert install.count(README_REQ) == 1, install
src = src if src.startswith("git+") else "git+" + src
(out / "1_install.sh").write_text(install.replace(README_REQ, f'"strathora-asof[mcp] @ {src}"'))      # the only change: install the source under test instead of the release on PyPI
(out / "2_init.sh").write_text(block("quickstart-init"))
(out / "try_asof.py").write_text(block("quickstart-code"))
(out / "3_verify.sh").write_text(block("quickstart-verify"))
(out / "expected.txt").write_text(block("quickstart-output"))
(out / "expected_verify.txt").write_text(re.search(r"It prints `(OK: [^`]+)`", readme).group(1) + "\n")
PY
(
  cd "$TMP/readme"
  export PATH="$TMP/bin:/usr/bin:/bin" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_QUIET=1
  set +u; . ./1_install.sh; set -u                                   # python3 -m venv .venv && source .venv/bin/activate; pip install "git+..."
  . ./2_init.sh > init.out
  python try_asof.py > got.txt                                       # "Save this as try_asof.py and run python try_asof.py"
  . ./3_verify.sh > got_verify.txt
)
cat "$TMP/readme/got.txt" "$TMP/readme/got_verify.txt"
"$TMP/mcp/bin/python" - "$TMP/readme" <<'PY'
import pathlib, re, sys
d = pathlib.Path(sys.argv[1])
got = re.sub(r"hash [0-9a-f]{12,}\.\.\.", "hash <hash>...", re.sub(r"^Receipt [0-9a-f]{12} ", "Receipt <id> ", (d / "got.txt").read_text(), flags=re.M))
want = (d / "expected.txt").read_text()
assert got == want, f"the quickstart printed something other than the README shows:\n--- got\n{got}\n--- README\n{want}"
assert (d / "got_verify.txt").read_text() == (d / "expected_verify.txt").read_text(), (d / "got_verify.txt").read_text()
print("the output is the README's, line for line (receipt id and hash aside)")
PY
case "$("$TMP/readme/.venv/bin/python" -c 'import asof; print(asof.__file__)')" in "$TMP"/readme/.venv/*) ;; *) fail "the README install did not import its own copy" ;; esac

printf '\nPASS: installed from %s and every check passed\n' "$SRC"
