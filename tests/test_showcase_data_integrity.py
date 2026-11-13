"""The data-integrity showcase (showcase/): the fixture is reproducible and really contains what the manifest says; the real checks' results are what the README says (including the NOT DETECTED ones);
the monitoring preview is labelled as simulated; the exported snippets are standalone and carry the synthetic tag. The slow strategy run is not repeated here: its committed outputs are checked instead."""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SC = ROOT / "showcase"
FIX = SC / "fixtures" / "data_integrity"
sys.path.insert(0, str(SC))

from data_integrity import detect, monitor  # noqa: E402
from data_integrity.common import MANIFEST, run_checks  # noqa: E402


def rows(name: str) -> list[dict[str, str]]:
    with open(FIX / name) as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- the fixture
def test_the_fixture_regenerates_to_the_same_bytes(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "generate.py").write_text((FIX / "generate.py").read_text())
    r = subprocess.run([sys.executable, str(work / "generate.py")], capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONHASHSEED": "31337"})
    assert r.returncode == 0, r.stderr
    for p in sorted(FIX.rglob("*")):
        if p.is_file() and p.name != "generate.py" and "__pycache__" not in p.parts:
            assert (work / p.relative_to(FIX)).read_bytes() == p.read_bytes(), f"{p.relative_to(FIX)} is not reproducible"


def test_the_manifest_matches_what_is_actually_in_the_files_counted_independently_of_the_checks():
    P = MANIFEST["planted"]
    news = rows("news.csv")
    assert sum(1 for r in news if r["available_at"] == "") == P["timestamp_gaps"]["rows"] == len(news) // 10                      # 10%
    fund = rows("fundamentals.csv")
    versions: dict[tuple[str, str], set[str]] = {}
    for r in fund:
        versions.setdefault((r["symbol"], r["period_end"]), set()).add(r["eps"])
    assert sum(1 for v in versions.values() if len(v) > 1) == P["restatement"]["n_restated"]
    est = rows("estimates.csv")
    assert len(est) == P["backfill"]["rows"] and len({r["available_at"][:13] for r in est}) == 1                                    # every row became available in the same hour
    assert min(r["period_end"] for r in est) < "2022-02-01"                                                                          # ... but is dated back to 2021-2022
    bars = rows("bars.csv")
    last = {s for s in {r["symbol"] for r in bars} if any(r["symbol"] == s and r["open_time"] == MANIFEST["period"][1] for r in bars)}
    assert len({r["symbol"] for r in bars}) - len(last) == len(P["survivorship"]["delisted"]) == 6
    assert sum(1 for r in bars if r["symbol"] in P["bar_close"]["tickers"]) == P["bar_close"]["rows"]
    assert all(r["vendor_available_at"].endswith(("13:30:00Z", "14:30:00Z")) for r in bars if r["symbol"] in P["bar_close"]["tickers"])        # 09:30 New York


# ---------------------------------------------------------------- the real checks, compared with the manifest
@pytest.fixture(scope="module")
def day1():
    return run_checks("day1")


def test_every_planted_issue_is_found_by_a_flag_with_the_planted_count(day1):
    t = {r["planted"]: r for r in detect.table(day1) if r["check"].startswith(("quality", "bars"))}
    for k in ("backfill", "restatement", "bar_close", "timestamp_gaps", "survivorship"):
        assert t[k]["detected"] == "DETECTED" and t[k]["found_count"] == t[k]["planted_count"], k
    assert t["survivorship"]["severity"] == "warning" and t["survivorship"]["planted_count"] == 6


def test_survivorship_is_flagged_on_the_survivors_only_list_with_exactly_the_delisted_names_and_not_on_the_real_universe():
    planted = sorted(MANIFEST["planted"]["survivorship"]["delisted"])
    naive, real = detect.survivorship_check("survivors_only"), detect.survivorship_check("as_of")
    assert naive["flagged"] and naive["listed_not_members"] == planted and naive["n_members"] == 44 and naive["n_listed"] == 50
    assert not real["flagged"] and real["n_members"] == 50 and real["listed_not_members"] == []                      # no false positive on the universe as it really was
    end = datetime(2023, 12, 29, 22, 0, tzinfo=timezone.utc)
    assert not detect.survivorship_check("survivors_only", at=end)["flagged"]                                      # after the last delisting the list is right: said in the note
    rs = [r for r in detect.table(run_checks("day1")) if r["planted"] == "survivorship"]
    assert [r["detected"] for r in rs] == ["DETECTED", "MEASURED"] and rs[1]["found_count"] == 6


def test_a_flag_that_was_not_planted_is_listed_not_hidden(day1):
    assert {"source": "news", "flag": "gaps"} == {k: v for k, v in detect.unplanted(day1)[0].items() if k in ("source", "flag")}


def test_the_committed_detection_and_raw_report_are_what_the_code_produces_now(day1):
    out = SC / "out"
    assert json.loads((out / "detection.json").read_text())["rows"] == detect.table(day1)
    raw = json.loads((out / "data_integrity_report.json").read_text())
    now = json.loads(json.dumps(day1, sort_keys=True, default=str))
    # The ledger's first event records the Python version, so the chain's head hash is the committed one only on the Python that wrote the committed file. Everything else must be equal on any version.
    committed_hash, hash_now = raw["day1"]["chain"].pop("head_hash"), now["chain"].pop("head_hash")
    assert raw["day1"] == now                                                                                    # reproducible raw output: every check result and count
    assert len(hash_now) == 64 and (hash_now == committed_hash or sys.version_info[:2] != (3, 13))


# ---------------------------------------------------------------- the clean control: what the checks say when nothing is planted
@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    from data_integrity import control
    d = tmp_path_factory.mktemp("control")
    counts = control.write_clean_control(d)
    return d, counts, control.run()


def test_the_control_really_has_none_of_the_planted_problems_counted_independently_of_the_checks(clean):
    d, counts, _ = clean

    def read(name):
        with open(d / name) as f:
            return list(csv.DictReader(f))
    gone = set(MANIFEST["planted"]["survivorship"]["delisted"])
    bars, fund, news, est, uni = (read(n) for n in ("bars.csv", "fundamentals.csv", "news.csv", "estimates.csv", "universe.csv"))
    assert all(r["vendor_available_at"].endswith(("20:20:00Z", "21:20:00Z")) for r in bars)                      # 16:20 New York, after the close, for every ticker
    assert len({(r["symbol"], r["period_end"]) for r in fund}) == len(fund) and {r["version"] for r in fund} == {"original"}
    assert all(r["available_at"] for r in news) and len(news) == 3000
    assert len({r["available_at"][:13] for r in est}) == 40 and all(r["available_at"][:10] > r["period_end"] for r in est)      # spread out, and after the period each one describes
    assert {r["action"] for r in uni} == {"add"} and not gone & ({r["symbol"] for r in bars} | {r["symbol"] for r in uni})
    last = MANIFEST["period"][1]
    assert {r["symbol"] for r in bars} == {r["symbol"] for r in bars if r["open_time"] == last}                  # every ticker trades to the last day: no survivorship to find
    assert counts == {"bars.csv": len(bars), "fundamentals.csv": len(fund), "news.csv": len(news), "estimates.csv": len(est), "universe.csv": len(uni)}


def test_on_the_control_no_planted_flag_fires_and_the_one_false_positive_is_reported(clean):
    _, _, res = clean
    assert res["planted_flags_raised_on_control"] == [] and res["chain_ok"] is True
    assert res["flags_per_source"] == {"bars": [], "estimates": [], "fundamentals": [], "news": ["gaps"], "universe": []}
    assert [(f["source"], f["flag"], f["severity"], f["basis"], f["n"]) for f in res["false_positives"]] == [("news", "gaps", "warning", "heuristic", 56)]      # a false positive, kept and counted
    assert res["survivorship_at_start"]["flagged"] is False and res["survivorship_at_start"]["n_listed"] == res["survivorship_at_start"]["n_members"] == 44
    assert json.loads((SC / "out" / "control.json").read_text()) == json.loads(json.dumps(res, sort_keys=True, default=str))
    from data_integrity import control
    assert control.section(res) in (SC / "web" / "README.md").read_text()                                        # the README says what the run says, false positive included


def test_the_false_positive_counted_without_the_library_nights_weekends_and_holidays_are_out(clean):
    """Recounted here with plain code: the steps between stories, in New York dates. Without a calendar, the nights and weekends were 516 of 530 'gaps'. With the exchange calendar,
    what is left is quiet stretches inside a day (longer than 5 times the median step within a day) and trading days with no story. The holidays come from a published list."""
    from statistics import median_low
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    holidays = {"2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
                "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25"}
    d, _, res = clean
    with open(d / "news.csv") as f:
        ts = sorted({datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")) for r in csv.DictReader(f)})
    steps = [((b - a).total_seconds(), a.astimezone(ny).date(), b.astimezone(ny).date(), a) for a, b in zip(ts, ts[1:])]
    within = [s for s in steps if s[1] == s[2]]
    med = median_low([s[0] for s in within])
    long_quiet = [s for s in within if s[0] > 5 * med]

    def trading_days_between(x, y):
        out, cur = [], x + timedelta(days=1)
        while cur < y:
            if cur.weekday() < 5 and cur.isoformat() not in holidays:
                out.append(cur)
            cur += timedelta(days=1)
        return out
    empty_days = [day for s in steps if s[1] != s[2] for day in trading_days_between(s[1], s[2])]
    g = res["gaps_detail"]["news"]
    assert med == g["median_step_seconds"] == 4318
    assert len(long_quiet) == g["n_gaps_within_day"] == 53 and len(empty_days) == g["n_sessions_missed"] == g["n_gaps_missed_sessions"] == 3
    assert g["n_gaps"] == 56 == len(long_quiet) + len(empty_days)
    nights = [s for s in steps if s[1] != s[2] and not trading_days_between(s[1], s[2])]                           # nights, weekends and holidays: from one day's stories to the next
    assert sum(1 for s in nights if s[0] > 5 * med) > 400                                                          # hundreds of them are long enough to be "gaps" on a wall clock; none is counted


# ---------------------------------------------------------------- the monitoring preview
def test_the_monitoring_preview_is_a_diff_of_two_manual_runs_and_says_so():
    raw = json.loads((SC / "out" / "data_integrity_report.json").read_text())
    alerts = monitor.diff(raw["day1"], raw["day30"])
    assert [a["flag"] for a in alerts] == ["bulk_backfill", "feed_delay", "chain"] and alerts[-1]["level"] == "ok"
    assert any("3,200 rows" in a["text"] for a in alerts) and any("2 min" in a["text"] and "46 min" in a["text"] for a in alerts)
    assert monitor.LABEL == "Simulated: two manual check runs, not live monitoring."
    html = (SC / "web" / "monitoring-preview.html").read_text()
    assert monitor.LABEL in html and 'class="asof-synth"' in html


# ---------------------------------------------------------------- the strategy runs (committed outputs) and the exports
def test_the_locked_run_is_worse_than_the_naive_one_in_every_overlay_and_each_says_synthetic():
    for k in ("equity", "sharpe", "hit_rate"):
        d = json.loads((SC / "out" / f"leak_overlay_{k}.json").read_text())
        assert d["synthetic"] is True and d["label"] == "difference attributable to look-ahead (observed)"
        lk = [p["v"] for p in d["locked"] if p["v"] is not None]
        un = [p["v"] for p in d["unlocked"] if p["v"] is not None]
        assert lk and un and un[-1] > lk[-1], k
    s = json.loads((SC / "out" / "strategy_summary.json").read_text())
    assert s["final"]["locked_run"]["chain_ok"] is True and s["final"]["unlocked_equity"] > s["final"]["locked_equity"]


@pytest.mark.parametrize("name", ["data-quality", "leak-overlay", "leak-overlay-sharpe", "leak-overlay-hitrate", "monitoring-preview"])
def test_each_exported_snippet_is_standalone_compact_synthetic_and_makes_no_network_calls(name):
    h = (SC / "web" / f"{name}.html").read_text()
    assert h.startswith("<!DOCTYPE html>") and 'class="asof-synth"' in h or " synthetic" in h
    assert re.search(r"<style>[^<]*\.asof-w", h) and "<script>" in h                                         # the widget kit is inlined
    assert not re.search(r'\b(?:src|href)="https?://', h) and "fetch(" not in h and "XMLHttpRequest" not in h
    assert " compact" in h or "asof-compact" in h
    assert (SC / "web" / f"{name}.png").exists() and (SC / "web" / f"{name}.png").stat().st_size > 5000
