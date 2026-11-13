#!/bin/sh
# Alter ONE BYTE of a COPY of a ledger, show that `strathora verify` fails, put the byte back, show that it passes again. Your ledger is never opened for writing.
#
#   scripts/tamper_demo.sh                       # builds a small throwaway ledger (synthetic data) and uses that
#   scripts/tamper_demo.sh --ledger ~/.asof/ledger.db   # works on a COPY of yours; its sha256 is checked before and after
#
# Exit 0 only if: the copy verifies, the altered copy FAILS verification, the restored copy verifies and is byte-identical to the original, and the original is unchanged.
set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-}"
[ -z "$PY" ] && { [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python" || PY="python3"; }
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ledger) SRC="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "tamper_demo: unknown argument $1" >&2; exit 2 ;;
  esac
done
WORK="$(mktemp -d "${TMPDIR:-/tmp}/asof-tamper.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
fail() { echo "FAIL: $*"; exit 1; }

if [ -z "$SRC" ]; then
  SRC="$WORK/original.db"
  "$PY" - "$SRC" <<'PYEOF' || fail "could not build the throwaway ledger (is asof installed?)"
import sys, tempfile
from pathlib import Path
from asof import Session, sources
d = Path(tempfile.mkdtemp())
(d / "p.csv").write_text("symbol,date,close,available_at\nSYN,2023-01-19,1.0,2023-01-19T21:00:00Z\nSYN,2023-01-20,2.0,2023-01-20T21:00:00Z\nSYN,2023-01-23,3.0,2023-01-23T21:00:00Z\n")
with Session("2023-01-21T14:00:00Z", ledger_path=sys.argv[1]) as s:
    s.add_source(sources.CSV(d / "p.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
    s.query("p", filters={"symbol": "SYN"})
PYEOF
  echo "built a throwaway ledger (synthetic data): $SRC"
else
  SRC="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"
  [ -f "$SRC" ] || fail "no such ledger: $SRC"
fi
BEFORE="$(sha "$SRC")"
COPY="$WORK/copy.db"
cp "$SRC" "$COPY"
HEAD_SRC="$(dirname "$SRC")/$( [ "$(basename "$SRC")" = "ledger.db" ] && echo head.json || echo "$(basename "$SRC" .db).head.json")"
[ -f "$HEAD_SRC" ] && cp "$HEAD_SRC" "$WORK/$( [ "$(basename "$SRC")" = "ledger.db" ] && echo head.json || echo copy.head.json)"

verify() { "$PY" -m asof verify --ledger "$COPY" 2>&1; }

echo "1. the copy, untouched"
OUT="$(verify)"; RC=$?; echo "   $OUT"
[ $RC -eq 0 ] || fail "the untouched copy does not verify; nothing to demonstrate"

# Find a value stored in a LIVE event's payload: a digit right after a known key. The ledger file is SQLite; flipping a digit inside a TEXT value keeps the file valid and
# the same size. SQLite also leaves stale copies of rows in dead space after it splits a page, and flipping one of those changes nothing in the ledger (verify rightly does
# not care), so each candidate is tried and the one that sits in a live event is kept.
put() { printf "\\$(printf '%03o' "$2")" | dd of="$COPY" bs=1 seek="$1" conv=notrunc 2>/dev/null; }
OFFSET=""; TOKEN=""; SKIPPED=0
for T in '"served":' '"as_of":"' '"n_calls":'; do
  for O in $(LC_ALL=C grep -aboF -- "$T" "$COPY" | cut -d: -f1 | sort -rn); do
    OFF=$((O + ${#T}))
    ORIG="$(dd if="$COPY" bs=1 skip="$OFF" count=1 2>/dev/null | od -An -tu1 | tr -d ' ')"
    case "$ORIG" in 4[89]|5[0-7]) NEW=$(( (ORIG - 48 + 1) % 10 + 48 )) ;; *) NEW=$(( ORIG == 120 ? 121 : 120 )) ;; esac
    put "$OFF" "$NEW"
    if verify >/dev/null; then put "$OFF" "$ORIG"; SKIPPED=$((SKIPPED + 1)); else OFFSET="$OFF"; TOKEN="$T"; break 2; fi
  done
done
[ -n "$OFFSET" ] || fail "found no payload value in a live event to alter in this ledger"
[ "$SKIPPED" -gt 0 ] && echo "   (skipped $SKIPPED stale copy/copies of that text in dead space inside the SQLite file: altering those changes no event, so verify rightly ignores them)"

echo "2. one byte of the copy is altered: offset $OFFSET (the value after $TOKEN), byte $ORIG -> $NEW"
OUT="$(verify)"; RC=$?; echo "   $OUT"
[ $RC -ne 0 ] || fail "the altered copy STILL VERIFIES: tampering would go unnoticed"
echo "$OUT" | grep -q "at event" || fail "verification failed but did not say where"

echo "3. put the byte back"
put "$OFFSET" "$ORIG"
OUT="$(verify)"; RC=$?; echo "   $OUT"
[ $RC -eq 0 ] || fail "the restored copy does not verify"
[ "$(sha "$COPY")" = "$BEFORE" ] || fail "the restored copy is not byte-identical to the original"
echo "   restored copy is byte-identical to the original (sha256 $BEFORE)"

[ "$(sha "$SRC")" = "$BEFORE" ] || fail "THE ORIGINAL LEDGER CHANGED (it must never)"
echo "4. the original ledger is unchanged (sha256 equal before and after)"
echo "PASS: one altered byte was caught at the event it sits in, and undone; the original was never written."
