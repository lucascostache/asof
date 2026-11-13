"""Pilot template: put AsOf around ONE data source, ONE tool and ONE model. Edit the three sections marked EDIT; everything else can stay.

    python wrap_agent.py --selftest        # runs the whole thing on SYNTHETIC data with a scripted stand-in model: no keys, no network. Do this first.
    python wrap_agent.py --as-of 2024-03-01T14:00:00Z     # your data, your tool, your model (its key comes from the environment, never from this file)

Nothing here sends anything anywhere unless you also set ASOF_SYNC_ENABLED=1, ASOF_SYNC_ENDPOINT and ASOF_SYNC_API_KEY (the key we give you). See docs/pilot/DATA_HANDLING.md.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from asof import Ledger, ModelResponse, Session, sources
from asof.sync import SyncConfig, Syncer

# ----------------------------------------------------------------------------------------------------------------------------------------------- EDIT 1 of 3: ONE data source
# A file of records with two time columns: when the thing HAPPENED (event_time) and when it became KNOWABLE to anyone (available_at).
# If you have no available_at column, say so: records without one are withheld, never guessed. Parquet and SQL work the same way (see the README, "Sources").
DATA_FILE = "data/prices.csv"
SOURCE = dict(event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC")      # column names in YOUR file


# ----------------------------------------------------------------------------------------------------------------------------------------------- EDIT 2 of 3: ONE tool
# Any function your agent already calls. It must return a list of items; each item needs a timestamp field (here "published_at").
# Items dated after the session clock are dropped before the agent sees them; items with no timestamp are withheld.
def news_search(query: str) -> list[dict]:
    raise NotImplementedError("replace this with a call to your real news or data tool")


TOOL = dict(name="news_search", fn=news_search, timestamp_field="published_at")


# ----------------------------------------------------------------------------------------------------------------------------------------------- EDIT 3 of 3: ONE model
# "provider:name" with provider anthropic, openai or local (any OpenAI-compatible server). The key is read from the environment (ANTHROPIC_API_KEY / OPENAI_API_KEY),
# sent to the provider and nowhere else, and never written to the ledger. AsOf does not choose a model for you.
MODEL = "anthropic:<the model you use>"


def run(as_of: str, ledger: Path, data_file: Path, tool: dict, model: str, provider=None, privacy: str = "hashes_only") -> int:
    with Session(as_of, privacy=privacy, ledger_path=ledger) as s:
        s.add_source(sources.CSV(data_file, **SOURCE))
        s.add_tool(tool["name"], tool["fn"], timestamp_field=tool["timestamp_field"])
        if provider:
            s.register_provider(*provider)
        # your agent's calls go through the session instead of straight to the source, the tool and the model:
        rows = s.query(data_file.stem, lookback="30d")
        items = s.tool(tool["name"], query="latest")
        reply = s.model(model, messages=[{"role": "user", "content": f"Given {len(rows)} price rows and these headlines, hold or review?\n" + "\n".join(str(i.get("title")) for i in items)}])
        s.decision("review" if "review" in reply.text.lower() else "hold", reason="template decision")
        print(f"as of {as_of}: the agent saw {len(rows)} records and {len(items)} items")
        print("\n\n".join(str(r) for r in s.receipt(last=False)))
    v = Ledger(ledger).verify()
    print(f"\nledger {ledger}: {'OK' if v.ok else 'FAIL'}: {v.n_events} events, anchor {v.anchor}")
    if os.environ.get("ASOF_SYNC_ENABLED") == "1":                                                          # opt in, explicitly
        res = Syncer(Ledger(ledger), SyncConfig.from_env()).push()
        print(f"synced {res.sent} event(s); acknowledged through {res.acked_through}; {res.payloads_included} carried a payload")
    return 0 if v.ok else 1


def selftest() -> int:
    d = Path(tempfile.mkdtemp(prefix="asof-pilot-selftest-"))
    (d / "prices.csv").write_text("symbol,date,close,available_at\nSYN,2023-01-18,10.0,2023-01-18T21:00:00Z\nSYN,2023-01-19,11.0,2023-01-19T21:00:00Z\nSYN,2023-01-23,15.0,2023-01-23T21:00:00Z\n")
    news = [{"title": "[SYNTHETIC] earlier headline", "published_at": "2023-01-19T12:00:00Z"}, {"title": "[SYNTHETIC] a later headline", "published_at": "2023-01-23T12:00:00Z"}]
    tool = dict(name="news_search", fn=lambda query: news, timestamp_field="published_at")
    print("SELFTEST on synthetic data: no keys, no network, a scripted stand-in for the model.\n")
    return run("2023-01-20T12:00:00Z", d / "ledger.db", d / "prices.csv", tool, "standin:scripted", provider=("standin", lambda req: ModelResponse("(scripted) hold")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--as-of", help="the session clock, with a time zone, e.g. 2024-03-01T14:00:00Z")
    ap.add_argument("--ledger", default="~/.asof/ledger.db")
    ap.add_argument("--privacy", choices=("hashes_only", "full"), default="hashes_only")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.as_of:
        print("give --as-of (or run --selftest first)", file=sys.stderr)
        return 2
    return run(a.as_of, Path(a.ledger).expanduser(), Path(DATA_FILE), TOOL, MODEL, privacy=a.privacy)


if __name__ == "__main__":
    sys.exit(main())
