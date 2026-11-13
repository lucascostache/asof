#!/usr/bin/env python3
"""AsOf demo, about three minutes.  ALL DATA IN THIS DEMO IS SYNTHETIC: a fictional company ("Acme Synthetic Corp", ticker ACME-SYN) with invented prices and headlines.

    python demo/run_demo.py                 # the whole story, offline; the model is a scripted stand-in
    python demo/run_demo.py --no-network    # the same, with a hard guarantee: no model call and no network of any kind (safe to run live)
    python demo/run_demo.py --pause         # wait for Enter between steps, to talk over it

If ASOF_SYNC_API_KEY is set (a key from Strathora: Settings > Connections > AsOf clients), step 6 also pushes the session to a local Strathora
(default http://localhost:3000/api/asof/sync, override with ASOF_SYNC_ENDPOINT or --sync URL) and prints where to open it.
Keys are read only from the environment, never from a file. A real model is used only with --model provider:name and its own key in the environment.

The demo keeps its own ledger (default ~/.asof-demo/ledger.db), so running it again continues one chain and the same Strathora key keeps working. If you delete that
file (--fresh), create a new client key in Strathora: one key is one ledger chain, and a second ledger under the same key is rejected as a fork.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from asof import ModelResponse, Session, sources
from asof.ledger import Ledger, default_head_path
from asof.sync import SyncConfig, Syncer

DATA = Path(__file__).parent / "data"
TICKER = "ACME-SYN"
AS_OF = "2023-02-01T23:00:00Z"                 # the evening before the (fictional) earnings day, Thursday 2023-02-02
LATER = "2023-02-03T00:00:00Z"                 # after the earnings article was published (2023-02-02T21:05:00Z)
RULE = "-" * 78


def say(text: str = "") -> None:
    print(text, flush=True)


def step(n: int, title: str, args: argparse.Namespace) -> None:
    if args.pause and n > 1:
        input("\n[press Enter] ")
    say(f"\n{RULE}\nSTEP {n}/6  {title}\n{RULE}")


def receipt_lines(r, label: str) -> None:
    t = dict(r.served_timing)
    blocked = {k: v for k, v in r.blocked.items() if v}
    say(f"  {label}")
    say(f"    served   {r.served}  ({t.get('declared', 0)} with declared timing, {t.get('unknown', 0)} unknown)")
    say(f"    blocked  {sum(blocked.values())}" + (f"  ({', '.join(f'{k}={v}' for k, v in blocked.items())})" if blocked else ""))
    say(f"    ledger   event {r.ledger_seq}, hash {str(r.ledger_hash)[:16]}...")


def run_cli(*argv: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-m", "asof", *argv], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def get_json(url: str, key: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ValueError, OSError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="AsOf demo (synthetic data)")
    ap.add_argument("--no-network", action="store_true", help="no model call and no network of any kind (step 6 is skipped)")
    ap.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    ap.add_argument("--model", default=os.environ.get("ASOF_DEMO_MODEL"), help="use a real model for step 4, e.g. anthropic:<model-id> (its key must be in the environment)")
    ap.add_argument("--sync", nargs="?", const="", default=None, metavar="URL", help="push to this Strathora endpoint (default http://localhost:3000/api/asof/sync)")
    ap.add_argument("--ledger", default="~/.asof-demo/ledger.db", help="the demo's own ledger (kept between runs)")
    ap.add_argument("--fresh", action="store_true", help="delete the demo ledger first (then use a NEW Strathora client key)")
    ap.add_argument("--out", help="keep the tampered copy and notes here (default: a temporary folder)")
    args = ap.parse_args()

    ledger_path = Path(args.ledger).expanduser()
    if args.fresh:
        for p in (ledger_path, default_head_path(ledger_path), ledger_path.with_name(ledger_path.stem + ".sync_cursor.json")):
            p.unlink(missing_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    out = Path(args.out).expanduser() if args.out else Path(tempfile.mkdtemp(prefix="asof-demo-"))
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    say("=" * 78)
    say("AsOf demo.  EVERYTHING BELOW IS SYNTHETIC: a fictional company, invented prices and headlines.")
    say("No real data, no real model, no real customer. The point is the mechanism, not the numbers.")
    say("=" * 78)

    # ------------------------------------------------------------------ 1
    step(1, f"Start a time-locked session: as of {AS_OF} (the evening before earnings day)", args)
    s = Session(as_of=AS_OF, privacy="hashes_only", ledger_path=ledger_path, max_as_of="2023-02-10T00:00:00Z")
    s.add_source(sources.CSV(DATA / "synthetic_prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
    news = json.loads((DATA / "synthetic_news.json").read_text())
    s.add_tool("news_search", lambda query: news, timestamp_field="published_at")
    say(f"  session {s.id}, privacy hashes_only (the ledger keeps hashes and counts, not the data)")
    say("  the clock only moves forward. Fictional earnings: published 2023-02-02T21:05:00Z, i.e. AFTER this session's clock.")

    # ------------------------------------------------------------------ 2
    step(2, "The agent asks for prices and for news", args)
    rows = s.query("synthetic_prices", filters={"symbol": TICKER}, lookback="30d")
    r_prices = s.receipt()
    say(f"  prices the agent sees ({len(rows)}): " + ", ".join(f"{x.event_time:%m-%d} {x.payload['close']}" for x in rows))
    items = s.tool("news_search", query="Acme Synthetic earnings")
    r_news = s.receipt()
    say(f"  news the agent sees ({len(items)}):")
    for it in items:
        say(f"    {it['published_at'][:16]}Z  {it['title']}")
    say("  The earnings article exists in the news source. The agent never receives it.")

    # ------------------------------------------------------------------ 3
    step(3, "The receipt: what was served, what was blocked (for the operator, not the agent)", args)
    receipt_lines(r_prices, "prices query")
    receipt_lines(r_news, "news tool call")
    blocked_news = r_news.blocked.get("future", 0)
    say(f"\n  -> {blocked_news} item dated after the clock was BLOCKED, and the block is a ledger event.")
    if blocked_news != 1:
        problems.append(f"expected exactly 1 blocked news item, got {blocked_news}")
    say("  What the agent may be shown (blocked counts left out: even a count says 'something exists later'):")
    say("    " + json.dumps({k: v for k, v in r_news.agent_view().items() if k in ("target", "served", "served_timing")}))
    # a model step, so the receipt has a memory label
    model_id, note = "standin:scripted", "a scripted stand-in: no model is called"
    if args.model and not args.no_network:
        model_id, note = args.model, "a real model, called with the key in your environment"
    prompt = "Summarise the headlines and say hold or review:\n" + "\n".join(f"- {it['title']}" for it in items)
    s.register_provider("standin", lambda req: ModelResponse("(scripted reply) Earnings have not been published as of this clock: hold.", "standin-1", "stop", {}))
    try:
        s.model(model_id, messages=[{"role": "user", "content": prompt}])
    except Exception as e:  # noqa: BLE001 - a live demo must not die on a provider problem
        say(f"  (the model call failed: {str(e)[:120]}; using the scripted stand-in instead)")
        model_id, note = "standin:scripted", "a scripted stand-in: no model is called"
        s.model(model_id, messages=[{"role": "user", "content": prompt}])
    r_model = s.receipt()
    m = r_model.model or {}
    say(f"\n  model: {m.get('id')}  ({note})")
    say(f"    model memory label: {m.get('label')}   declared cutoff: {m.get('declared_cutoff') or 'unknown'}   contamination score: "
        f"{m.get('contamination_score') if m.get('contamination_score') is not None else 'none registered'}")
    say("    AsOf does not control what a model remembers. 'unknown' stays unknown until someone measures it; it is never upgraded to 'clean'.")
    s.decision("hold", reason="earnings result not public as of this clock (scripted agent)")

    # ------------------------------------------------------------------ 4
    step(4, f"Move the clock past the earnings release ({LATER}): the article is now served", args)
    s.advance_to(LATER)
    items2 = s.tool("news_search", query="Acme Synthetic earnings")
    r_news2 = s.receipt()
    receipt_lines(r_news2, "news tool call, after the clock moved")
    say(f"  the agent now sees {len(items2)} headlines, including: {items2[-1]['title']}")
    if r_news2.blocked.get("future", 0) != 0 or len(items2) != len(items) + 1:
        problems.append("after advancing the clock the article should be served and nothing blocked")
    s.close()
    led = Ledger(ledger_path)
    v = led.verify()
    say(f"\n  ledger: {'OK' if v.ok else 'FAIL'}: {v.n_events} events, hash chain intact, tail anchor {v.anchor}  ({ledger_path})")
    if not v.ok:
        problems.append("the untouched ledger did not verify")

    # ------------------------------------------------------------------ 5
    step(5, "Tamper with a copy of the ledger, then verify it", args)
    copy = out / "tampered_ledger.db"
    shutil.copy2(ledger_path, copy)
    shutil.copy2(default_head_path(ledger_path), default_head_path(copy)) if default_head_path(ledger_path).exists() else None
    con = sqlite3.connect(copy)
    target = con.execute("SELECT seq, payload FROM events WHERE session_id=? AND type='block' AND payload LIKE '%\"kind\":\"tool\"%' ORDER BY seq LIMIT 1", (s.id,)).fetchone()
    if target is None:
        problems.append("could not find the news block event to tamper with")
    else:
        seq = target[0]
        say(f"  We edit one value of one event in the copy: event {seq} is the block that recorded the hidden article. We change its count from 1 to 0, as if nothing was blocked.")
        say("    $ sqlite3 tampered_ledger.db")
        say("    sqlite> DROP TRIGGER events_no_update;     -- the ledger refuses UPDATE; whoever has the file can remove that guard...")
        say(f"    sqlite> UPDATE events SET payload = replace(payload, '\"count\":1', '\"count\":0') WHERE seq = {seq};")
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET payload = replace(payload, '\"count\":1', '\"count\":0') WHERE seq = ?", (seq,))
        con.commit()
        con.close()
        say("  ...which is why the hash chain exists. Now:")
        say(f"    $ strathora verify --ledger {copy.name}")
        code, text = run_cli("verify", "--ledger", str(copy))
        say("    " + text.replace("\n", "\n    "))
        say(f"    (exit code {code})")
        if code == 0 or f"event {seq}" not in text:
            problems.append(f"verify should fail at event {seq}")
        else:
            say(f"  -> FAIL, at exactly the event we changed (#{seq}). The untouched ledger still says OK.")
    con.close() if target is None else None

    # ------------------------------------------------------------------ 6
    step(6, "Sync to Strathora (opt-in) and open the run and its receipt there", args)
    key = os.environ.get("ASOF_SYNC_API_KEY", "")
    endpoint = args.sync or os.environ.get("ASOF_SYNC_ENDPOINT") or "http://localhost:3000/api/asof/sync"
    if args.no_network:
        say("  skipped: --no-network. Nothing left this process.")
    elif not key and args.sync is None:
        say("  skipped: no ASOF_SYNC_API_KEY in the environment. Sync is off unless you turn it on.")
        say("  To see this step: in Strathora open Settings > Connections > AsOf clients, create a key, then run:")
        say("    export ASOF_SYNC_API_KEY=<the key it shows once>   &&   python demo/run_demo.py")
    elif not key:
        say("  --sync needs ASOF_SYNC_API_KEY in the environment (the key is never read from a file or hard-coded). Skipped.")
    else:
        local = endpoint.startswith(("http://localhost", "http://127.0.0.1"))
        try:
            res = Syncer(Ledger(ledger_path), SyncConfig(enabled=True, endpoint=endpoint, api_key=key, allow_insecure_localhost=local)).push()
            say(f"  sent {res.sent} event(s); Strathora acknowledged through event {res.acked_through}; {res.payloads_included} carried a payload (hashes_only: hashes and counts only)")
            origin = endpoint.split("/api/")[0]
            clients = get_json(f"{origin}/api/asof/clients", key) or {}
            mine = next((c for c in clients.get("items", []) if c.get("key_prefix") == key[:12]), None)
            if mine:
                say(f"\n  Open the run and its receipts:   {origin}/workspace/asof/{mine['client_id']}/{s.id}")
                head = get_json(f"{origin}/api/asof/head/{mine['client_id']}", key)
                if head:
                    code, text = run_cli("verify", "--ledger", str(ledger_path), "--head", json.dumps({k: head[k] for k in ("seq", "hash", "recorded_at")}))
                    say(f"\n  Strathora holds this client's head (event {head['seq']}). Verified against that copy: {text.splitlines()[0] if text else code}")
                    code2, text2 = run_cli("verify", "--ledger", str(copy), "--head", json.dumps({k: head[k] for k in ("seq", "hash", "recorded_at")}))
                    say(f"  The tampered copy against the same head: {text2.splitlines()[0] if text2 else code2}")
            else:
                say(f"\n  Open Runs in Strathora ({origin}/workspace/runs) and look for session {s.id} (source: AsOf).")
        except Exception as e:  # noqa: BLE001 - SyncError for an HTTP or connection problem; anything else must not break a live demo either
            say(f"  NOT SYNCED: {type(e).__name__}: {e}")
            say("  (the demo's other steps are unaffected. A 'fork' reply means this key already holds a different ledger: create a new client key.)")

    say(f"\n{RULE}\nDONE  session {s.id}.  All data was synthetic.  Files: {out}")
    say("What you saw: a lock that withholds what was not yet public, a receipt that says what it withheld, a clock that only moves forward, and a ledger that shows an edit.")
    say("What it does not prove: that the source's timestamps are true, that a model forgot anything, or that nothing was left unsent. See docs/pilot/LIMITATIONS.md.")
    if problems:
        say("\nDEMO SELF-CHECK FAILED:\n  " + "\n  ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
