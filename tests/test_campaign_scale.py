"""Campaign J and item 14: the size of ``block`` events can be capped without changing any count, the baseline script runs, and docs/BASELINE.md says what docs/baseline.json holds."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from asof import Session
pytest.importorskip("mcp")
from asof.mcp_server import session_from_config  # noqa: E402
from asof.sources import CSV

ROOT = Path(__file__).resolve().parents[1]


def _csv(tmp_path: Path, past: int = 10, future: int = 20) -> Path:
    p = tmp_path / "d.csv"
    lines = ["id,t,avail,v"]
    lines += [f"p{i},2023-01-01T00:00:00Z,2023-01-01T00:00:{i:02d}Z,{i}" for i in range(past)]
    lines += [f"f{i},2023-06-01T00:00:00Z,2023-06-01T00:00:{i:02d}Z,{i}" for i in range(future)]
    p.write_text("\n".join(lines) + "\n")
    return p


def _blocks(tmp_path: Path, name: str, **opt):
    with Session("2023-03-01T00:00:00Z", ledger_path=tmp_path / f"{name}.db", **opt) as s:
        s.add_source(CSV(_csv(tmp_path), name="d", event_time="t", available_at="avail", record_id="id"))
        rows = s.query("d")
        rec = s.receipt()
        ev = [e.data() for e in s.events(type="block")]
        ok = s.verify_ledger().ok
    return rows, rec, ev, ok


def test_by_default_every_withheld_record_is_named_in_the_block_event(tmp_path):
    rows, rec, ev, ok = _blocks(tmp_path, "default")
    assert len(rows) == 10 and ok and len(ev) == 1
    assert ev[0]["count"] == 20 and len(ev[0]["listed_payload_sha256"]) == 20 and ev[0]["list_truncated"] is False
    assert rec.blocked["future"] == 20


@pytest.mark.parametrize("cap", [0, 5])
def test_a_lower_cap_names_fewer_records_and_changes_no_count_and_nothing_that_is_served(tmp_path, cap):
    base_rows, base_rec, _, _ = _blocks(tmp_path, "default")
    rows, rec, ev, ok = _blocks(tmp_path, f"cap{cap}", max_listed_blocked=cap)
    assert ok and [r.record_id for r in rows] == [r.record_id for r in base_rows]
    assert ev[0]["count"] == 20 and len(ev[0]["listed_payload_sha256"]) == cap and ev[0]["list_truncated"] is True      # the count stays exact; the event says the list is cut
    assert rec.blocked == base_rec.blocked and rec.blocked["future"] == 20 and rec.served == base_rec.served


@pytest.mark.parametrize("bad", [-1, 1.5, "10", True, None])
def test_a_cap_that_is_not_a_whole_number_from_zero_up_is_refused(tmp_path, bad):
    with pytest.raises(ValueError, match="max_listed_blocked"):
        Session("2023-03-01T00:00:00Z", ledger_path=tmp_path / "l.db", max_listed_blocked=bad)


def test_the_mcp_config_passes_the_cap_on(tmp_path):
    _csv(tmp_path)
    cfg = {"as_of": "2023-03-01T00:00:00Z", "ledger_path": "ledger.db", "max_listed_blocked": 3,
           "sources": [{"type": "csv", "name": "d", "path": "d.csv", "event_time": "t", "available_at": "avail", "record_id": "id"}]}
    s, _ = session_from_config(cfg, tmp_path)
    try:
        assert s.max_listed_blocked == 3
        s.query("d")
        assert len(s.events(type="block")[0].data()["listed_payload_sha256"]) == 3
    finally:
        s.close()


def test_the_baseline_script_runs_every_step_at_a_thousandth_of_the_size(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_baseline.py"), "--work", str(tmp_path / "w"), "--scale", "0.001"], capture_output=True, text=True, timeout=300,
                       env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads((tmp_path / "w" / "baseline.json").read_text())
    res = out["results"]
    assert list(res) == ["ledger-build", "ledger-verify", "query-1m", "session-1m", "advance-10k", "advance-10k-unlisted"]
    assert res["ledger-verify"]["ok"] is True and res["ledger-verify"]["anchor"] == "ok"
    assert res["advance-10k"]["ledger_events"] == res["advance-10k-unlisted"]["ledger_events"]                # the same events ...
    assert res["advance-10k-unlisted"]["ledger_mb"] < res["advance-10k"]["ledger_mb"]                        # ... in a smaller file when nothing is listed


def test_the_baseline_page_holds_the_numbers_of_the_recorded_run_and_no_others():
    data = json.loads((ROOT / "docs" / "baseline.json").read_text())
    page = (ROOT / "docs" / "BASELINE.md").read_text()
    assert data["scale"] == 1.0 and data["results"]["ledger-build"]["events"] == 1_000_000 and data["results"]["advance-10k"]["steps"] == 10_000
    m = data["machine"]
    for must in (m["cpu"], f"{m['cores']} cores", f"{m['memory_gb']} GB", m["python"], m["sqlite"], data["date"], data["asof_version"]):
        assert str(must) in page, must
    r = data["results"]
    for step, key in (("ledger-build", "seconds"), ("ledger-build", "file_mb"), ("ledger-build", "peak_memory_mb"), ("ledger-verify", "seconds"), ("ledger-verify", "peak_memory_mb"),
                      ("query-1m", "query_seconds"), ("query-1m", "peak_memory_mb"), ("session-1m", "load_source_seconds"), ("session-1m", "query_seconds"), ("session-1m", "peak_memory_mb"),
                      ("advance-10k", "seconds"), ("advance-10k", "milliseconds_per_step"), ("advance-10k", "ledger_mb"), ("advance-10k", "peak_memory_mb"),
                      ("advance-10k-unlisted", "seconds"), ("advance-10k-unlisted", "ledger_mb"), ("advance-10k-unlisted", "peak_memory_mb")):
        assert f"{r[step][key]:,}" in page, (step, key, r[step][key])
    assert "not a benchmark" in page.lower() and "one machine" in page.lower()
