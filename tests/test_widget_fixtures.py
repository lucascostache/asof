"""The widget fixtures are pinned: regenerating them gives the same bytes on any day, in any process, with any hash seed. Snapshots and demo screenshots depend on it."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "widgets"
NAMES = ("receipt", "timeline", "quality", "chain", "returns", "rolling-sharpe", "leak-overlay", "attribution")


def test_regenerating_the_fixtures_gives_the_same_bytes_whatever_the_day_or_hash_seed(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "generate.py").write_text((FIX / "generate.py").read_text())                 # a copy: its output goes beside it, so the committed files are not touched
    r = subprocess.run([sys.executable, str(work / "generate.py")], capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONHASHSEED": "777"})
    assert r.returncode == 0, r.stderr
    for name in NAMES:
        assert (work / f"{name}.json").read_bytes() == (FIX / f"{name}.json").read_bytes(), f"{name}.json is not reproducible"


def test_the_pinned_clock_is_after_the_data_so_the_quality_fixture_has_no_future_dated_error():
    import json
    q = json.loads((FIX / "quality.json").read_text())
    assert q["by_severity"]["error"] == 0 and not any("future_dated" in i["flag"] for i in q["issues"])        # the one issue in the demo is the late backfill (a warning), not an artefact of the pinned clock


def test_no_fixture_contains_the_date_it_was_generated_on():
    text = "".join((FIX / f"{n}.json").read_text() for n in NAMES)
    assert "2025-" not in text and "2026-" not in text and "2027-" not in text           # every wall-clock value is the pinned 2023-02-15
