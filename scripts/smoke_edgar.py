#!/usr/bin/env python3
"""Fetch real SEC filings with YOUR User-Agent and settle how EDGAR's acceptanceDateTime should be read.

    export ASOF_SEC_USER_AGENT='Your Organisation you@your-domain'      # YOUR contact details: the SEC requires them
    python scripts/smoke_edgar.py --cik <CIK> --n 3                     # the CIK of the company you want to check: there is no default
    python scripts/smoke_edgar.py --cik <CIK> --n 4 --write-fixture tests/fixtures/edgar_known_filings.json

For each filing it prints (1) the raw acceptanceDateTime from the submissions JSON, (2) what AsOf converts it to under the default reading (digits as New
York time, errs late) and under "UTC" (trust the Z), (3) the "Accepted" time EDGAR's own filing-index page shows (New York time), and (4) which reading
agrees with the index page. Fair access: requests are spaced >= 0.125 s and responses are cached.

Honest limits: the parsing of the filing-index page follows its documented layout and has NOT been run against live EDGAR by the author. If a page cannot be
parsed the script says so and prints the part it looked at; it never guesses. Do not switch SECFilings to acceptance_tz="UTC" unless every filing here says
the UTC reading agrees.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from asof import SourceError
from asof.sources.sec import BASE, SECFilings

NY = ZoneInfo("America/New_York")
ACCEPTED = [re.compile(r'Accepted</div>\s*<div[^>]*class="info"[^>]*>\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})', re.I),
            re.compile(r"Accepted[^0-9]{0,120}(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.I | re.S)]


def parse_accepted(html: str) -> str | None:
    for pat in ACCEPTED:
        m = pat.search(html)
        if m:
            return m.group(1).replace("T", " ")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cik", required=True, help="the company's CIK number (required: there is no default company)")
    ap.add_argument("--n", type=int, default=3, help="how many filings (with an acceptance time) to check")
    ap.add_argument("--forms", default="10-K,10-Q,8-K", help="comma-separated form types")
    ap.add_argument("--user-agent", help="your User-Agent (or set ASOF_SEC_USER_AGENT)")
    ap.add_argument("--write-fixture", metavar="PATH", help="write the filings as a fixture for tests/test_sec.py")
    ap.add_argument("--base-url", default=BASE, help="submissions API base (override for a local mock)")
    ap.add_argument("--index-base-url", default="https://www.sec.gov/Archives/edgar/data/", help="filing-index base (override for a local mock)")
    ap.add_argument("--cache-dir", default="~/.asof/cache/sec")
    ap.add_argument("--min-interval", type=float, default=0.125)
    ap.add_argument("--no-index", action="store_true", help="do not fetch the filing-index pages (one request in all); check each filing by hand with the URL printed below")
    a = ap.parse_args()
    try:
        src = SECFilings(a.user_agent, [a.cik], forms=a.forms.split(","), include_older=False, base_url=a.base_url, cache_dir=a.cache_dir, min_interval_s=a.min_interval)
        recs = [r for r in src.records() if r.payload.get("acceptance_datetime")][: a.n]
    except SourceError as e:
        print(f"smoke_edgar: {e}", file=sys.stderr)
        return 2
    if not recs:
        print("smoke_edgar: no filings with an acceptance time came back", file=sys.stderr)
        return 1
    utc = SECFilings(src.user_agent, [a.cik], acceptance_tz="UTC", cache_dir=None, fetch=lambda u, ua: b"{}")
    fixture: list[dict] = []
    verdicts: list[str] = []
    by_hand: list[tuple] = []
    print(f"{'accession':22} {'form':6} {'raw acceptanceDateTime':26} {'AsOf default (NY digits)':26} {'AsOf UTC label':26} {'EDGAR index Accepted (NY)':26} verdict")
    for r in recs:
        p = r.payload
        raw = p["acceptance_datetime"]
        acc = p["accession_number"]
        url = f"{a.index_base_url.rstrip('/')}/{int(p['cik'])}/{acc.replace('-', '')}/{acc}-index.htm"
        by_hand.append((acc, str(p["form"]), raw, r.available_at, url))
        if a.no_index:
            html, note = "", "index page not fetched (--no-index): compare by hand with the URL below"
        else:
            src._limiter.wait()
            try:
                html = src._fetch(url, src.user_agent).decode("utf-8", "replace")
            except SourceError as e:
                html, note = "", f"index page unavailable ({e})"
            else:
                note = ""
        accepted = parse_accepted(html) if html else None
        default = r.available_at
        trusting, _ = utc.acceptance_to_utc(raw)
        if accepted is None:
            verdict = note or "could not parse the index page: compare by eye"
            truth = None
        else:
            truth = datetime.fromisoformat(accepted).replace(tzinfo=NY).astimezone(timezone.utc)
            if trusting == truth:
                verdict = "UTC label AGREES with EDGAR's index (default reading is later than needed: safe)"
            elif default == truth:
                verdict = "the JSON digits are NEW YORK time (the Z label is wrong): keep the default"
            else:
                verdict = "NEITHER reading matches: do not rely on either, investigate"
            verdicts.append(verdict)
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else "n/a"       # noqa: E731
        print(f"{acc:22} {str(p['form']):6} {raw:26} {fmt(default):26} {fmt(trusting):26} {accepted or 'n/a':26} {verdict}")
        if accepted is not None:
            fixture.append({"cik": p["cik"], "accession": acc, "form": p["form"], "acceptance_datetime_raw": raw, "accepted_new_york": accepted,
                            "source": "submissions JSON + EDGAR filing-index page", "fetched_on": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    print("\nCHECK BY HAND (open each URL; EDGAR's filing-index page shows 'Accepted' in US Eastern time):")
    for i, (acc, form, raw, ours, url) in enumerate(by_hand, 1):
        print(f"  [{i}] accession {acc}  form {form}")
        print(f"      raw acceptanceDateTime (submissions JSON): {raw}")
        print(f"      AsOf available_at (UTC), default reading : {ours.strftime('%Y-%m-%dT%H:%M:%SZ') if ours else 'n/a'}")
        print(f"      EDGAR filing index                       : {url}")
    print(f"\n{src.requests_made} request(s) to the submissions API (cache: {a.cache_dir}); User-Agent: {src.user_agent}")
    if a.write_fixture:
        Path(a.write_fixture).write_text(json.dumps({"_comment": "Written by scripts/smoke_edgar.py with the user's own User-Agent. See tests/test_sec.py.",
                                                     "schema": 1, "filings": fixture}, indent=2) + "\n")
        print(f"wrote {len(fixture)} filing(s) to {a.write_fixture}")
    if verdicts and all(v.startswith("UTC label AGREES") for v in verdicts):
        print("All filings agree with the UTC reading. You may set acceptance_tz='UTC' (the default stays safe either way).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
