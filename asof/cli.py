"""The ``strathora`` command (also installed under its older name, ``asof``): init, verify, head, receipt, report, serve-mcp, sync."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .errors import AsOfError, LedgerError, LedgerVersionError
from .ledger import Ledger, archive_legacy, open_for_reading
from .receipt import receipt_from_ledger

DEFAULT_LEDGER = "~/.asof/ledger.db"

DEMO_CSV = """symbol,date,close,available_at
ACME-SYN,2023-01-18,147.00,2023-01-18T21:00:00Z
ACME-SYN,2023-01-19,148.00,2023-01-19T21:00:00Z
ACME-SYN,2023-01-20,150.00,2023-01-20T21:00:00Z
ACME-SYN,2023-01-23,155.00,2023-01-23T21:00:00Z
ACME-SYN,2023-01-24,152.50,2023-01-24T21:00:00Z
ACME-SYN,2023-01-25,158.00,2023-01-25T21:00:00Z
"""


def _ledger(path: str, *, must_exist: bool = False) -> Ledger:
    """Open a ledger. Commands that only READ evidence (verify, head, receipt) pass ``must_exist=True``: opening a path creates the file, so without this a mistyped
    path would be created empty and then reported as "OK: 0 events"."""
    p = Path(path).expanduser()
    if must_exist and not p.exists():
        raise LedgerError(f"no ledger at {p}: check the path (--ledger), or create one with `strathora init --ledger {p}`")
    return open_for_reading(p) if must_exist else Ledger(p)       # a ledger that cannot be written (read-only media, an archived copy) can still be read and verified


def cmd_init(a: argparse.Namespace) -> int:
    p = Path(a.ledger).expanduser()
    if p.exists() and a.archive_legacy:
        try:
            Ledger(p).close()
            print(f"{p} is already a current-version ledger; nothing to archive.")
        except LedgerVersionError:
            moved = archive_legacy(p)
            print(f"archived the old-version ledger to {moved}")
    led = _ledger(a.ledger)                                  # raises LedgerVersionError (with the fix) for an old-version file
    if not led.head_path.exists():                           # an existing ledger whose anchor file is gone: `init` is the explicit way to write one again (opening or verifying never does)
        led.write_anchor()
    print(f"ledger:  {led.path}  (schema v2, {len(led)} events)")
    print(f"anchor:  {led.head_path}")
    if a.demo:
        d = Path(a.demo).expanduser().resolve()
        d.mkdir(parents=True, exist_ok=True)
        (d / "prices.csv").write_text(DEMO_CSV)
        cfg = {"as_of": "2023-01-21T14:00:00Z", "max_as_of": "2023-01-25T23:59:59Z", "allow_model_clock": False, "privacy": "full",
               "ledger_path": str(led.path.resolve()),      # absolute: a relative path in a config is read against the CONFIG's folder, so `--ledger demo/ledger.db --demo demo` would point the server at demo/demo/ledger.db
               "sources": [{"type": "csv", "path": "prices.csv", "event_time": "date", "available_at": "available_at", "key": ["symbol", "date"], "assume_tz": "UTC"}]}
        (d / "asof.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        snippet = {"mcpServers": {"asof": {"command": sys.executable, "args": ["-m", "asof", "serve-mcp", "--config", str(d / "asof.config.json")]}}}
        print(f"\ndemo:    wrote {d / 'prices.csv'} and {d / 'asof.config.json'}")
        print("\nAdd this to your MCP client's config (Claude Desktop: claude_desktop_config.json):\n")
        print(json.dumps(snippet, indent=2))
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    led = _ledger(a.ledger, must_exist=True)
    v = led.verify(a.head, anchor="skip" if a.no_anchor else "require")
    if a.json:
        print(json.dumps({"ok": v.ok, "n_events": v.n_events, "anchor": v.anchor, "first_bad_seq": v.first_bad_seq, "reason": v.reason, "warnings": list(v.warnings),
                          "head_hash": v.head_hash}))
    elif v.ok:
        print(f"OK: {v.n_events} events, hash chain intact, tail anchor: {v.anchor}")
        for w in v.warnings:
            print(f"warning: {w}")
    else:
        print(f"FAIL: {v.reason}" + (f" (at event {v.first_bad_seq})" if v.first_bad_seq else ""), file=sys.stderr)
    return 0 if v.ok else 1


def cmd_report(a: argparse.Namespace) -> int:
    from .widgets import report
    html = report.build(Path(a.ledger).expanduser(), session_id=a.session, synthetic=a.synthetic, title=a.title, max_receipts=a.max_receipts, external=a.external)
    out = Path(a.html).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"wrote {out} ({len(html):,} bytes): one self-contained file, nothing is fetched when it opens")
    return 0


def cmd_head(a: argparse.Namespace) -> int:
    led = _ledger(a.ledger, must_exist=True)
    anchor = led.anchor()
    db = led.head()
    h, source = (anchor, str(led.head_path)) if anchor is not None else (db, "the ledger database (no head.json found)")
    print(json.dumps({**h.to_dict(), "source": source}))
    print("Store this somewhere you trust (git, email, another system). Verify later with: strathora verify --head '<that JSON>'", file=sys.stderr)
    if anchor is not None and (anchor.seq != db.seq or anchor.hash != db.hash):
        print(f"note: head.json (event {anchor.seq}) differs from the database tail (event {db.seq}); run `strathora verify` before trusting either", file=sys.stderr)
    return 0


def cmd_receipt(a: argparse.Namespace) -> int:
    led = _ledger(a.ledger, must_exist=True)
    try:
        r = receipt_from_ledger(led, a.call_id)
    except AsOfError as e:
        print(f"strathora receipt: {e}", file=sys.stderr)
        return 1
    if a.json or a.agent:                                     # the agent view exists only as JSON: `--agent` alone must never fall through to the operator's summary, which shows the blocked counts
        print(json.dumps(r.agent_view() if a.agent else r.to_dict(), indent=2, sort_keys=True))
    else:
        print(r.summary())
    return 0


def cmd_serve_mcp(a: argparse.Namespace, rest: list[str]) -> int:
    try:
        from .mcp_server import main as serve
    except ImportError as e:                                 # the optional 'mcp' package is missing: one line, not a traceback
        print(f"strathora serve-mcp: {e}", file=sys.stderr)
        return 2
    return serve(rest)


def cmd_sync(a: argparse.Namespace) -> int:
    from .sync import SyncConfig, SyncError, Syncer
    try:
        res = Syncer(_ledger(a.ledger), SyncConfig.from_env()).push()
    except SyncError as e:
        print(f"strathora sync: {e}", file=sys.stderr)
        return 1
    print(f"sent {res.sent} event(s) in {res.batches} batch(es); acknowledged through event {res.acked_through}; {res.payloads_included} carried a payload")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # the same program under two names: `strathora` and its alias `asof` (same function, same options); help text shows the name it was started as
    prog = Path(sys.argv[0]).name if Path(sys.argv[0]).name in ("asof", "strathora") else "strathora"
    p = argparse.ArgumentParser(prog=prog, description="AsOf: a time-lock connector for AI agents.",
                                epilog="Exit codes: 0 done; 1 the check failed (verify found a problem, no such receipt, sync was refused); "
                                       "2 the command could not run (the ledger is missing, unreadable or of another version; a wrong argument).")
    p.add_argument("--version", action="version", version=f"strathora-asof {__version__}")      # the distribution's name, whichever command name was typed
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda name, **kw: _add(name, **{"description": kw["help"][0].upper() + kw["help"][1:] + ".", **kw})      # type: ignore[method-assign]  # `strathora X --help` says what X does
    led = lambda sp: sp.add_argument("--ledger", default=DEFAULT_LEDGER, help=f"ledger file (default {DEFAULT_LEDGER})")      # noqa: E731
    s = sub.add_parser("init", help="create the ledger and its tail anchor (head.json)")
    led(s)
    s.add_argument("--archive-legacy", action="store_true", help="move an old-version ledger aside and start a fresh one")
    s.add_argument("--demo", metavar="DIR", help="also write a sample prices.csv and an MCP config into DIR and print the client snippet")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("verify", help="recompute the hash chain and check the tail anchor (exit 1 on failure)")
    led(s)
    s.add_argument("--head", help="a head you stored elsewhere: JSON text or a path to a head.json (checked instead of the local head.json)")
    s.add_argument("--no-anchor", action="store_true", help="check the chain only; a ledger cut off at the end would still pass")
    s.add_argument("--json", action="store_true", help="print one JSON object (ok, n_events, anchor, first_bad_seq, reason, warnings, head_hash); the exit code is the same")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("report", help="write a static, self-contained HTML report (chain, data quality, three clocks, receipts) with the AsOf widgets inlined")
    led(s)
    s.add_argument("--html", required=True, metavar="FILE", help="where to write the HTML file")
    s.add_argument("--session", help="only this session id (default: the whole ledger)")
    s.add_argument("--title", help="the page title")
    s.add_argument("--synthetic", action="store_true", help="mark every widget with the small 'synthetic' tag (use for demo and fixture data)")
    s.add_argument("--max-receipts", type=int, default=12, help="how many receipts to draw (default 12)")
    s.add_argument("--external", action="store_true", help="the report leaves the building: draw the data-quality panel in the shared view (source names aliased; lag statistics, row ids and custom thresholds withheld). "
                   "Row ids in the internal view are keyed with $ASOF_CLIENT_SECRET")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("head", help="print the ledger head {seq, hash, recorded_at} to store somewhere you trust")
    led(s)
    s.set_defaults(fn=cmd_head)
    s = sub.add_parser("receipt", help="print the receipt of a call, rebuilt from the ledger")
    s.add_argument("call_id", help="the receipt id that a query, tool call or model call returned (12 characters)")
    led(s)
    s.add_argument("--json", action="store_true", help="print the full receipt as JSON, blocked counts included (without it: a short text summary)")
    s.add_argument("--agent", action="store_true", help="print what the agent is allowed to see, as JSON: no blocked counts and no position in the ledger")
    s.set_defaults(fn=cmd_receipt)
    sub.add_parser("serve-mcp", help="serve a session over MCP (stdio); arguments are passed through, see `strathora serve-mcp --help`", add_help=False)
    s = sub.add_parser("sync", help="push new ledger events to the configured endpoint (only if sync is enabled)")
    led(s)
    s.set_defaults(fn=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve-mcp":                      # pass everything after the subcommand straight to the server's own parser
        return cmd_serve_mcp(argparse.Namespace(), argv[1:])
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except AsOfError as e:                                   # includes LedgerVersionError, whose message carries the fix
        print(f"strathora: {e}", file=sys.stderr)
        return 2
    except (sqlite3.Error, UnicodeError) as e:               # a ledger file that is damaged badly enough that it cannot be read at all: say so, never a traceback
        print(f"strathora: the ledger file is damaged and cannot be read ({type(e).__name__}: {e}). Run `strathora verify` on it; restore it from a backup if you have one.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
