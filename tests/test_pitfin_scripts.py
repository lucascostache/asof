"""scripts/pitfin_run.py and scripts/leak_demo.py: offline by default, nothing live without --live, and never a live call without a key. Run as subprocesses with every *_API_KEY removed from the environment."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

from asof import Ledger

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "pitfin" / "sample_questions.json"


def env():
    e = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k not in ("META_API_BASE_URL",)}
    e["NO_PROXY"] = "*"
    return e


def run(script, *args, timeout=120):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], capture_output=True, text=True, timeout=timeout, env=env())


def test_the_runner_offline_prints_the_table_writes_the_csv_and_a_ledger_that_verifies(tmp_path):
    out = tmp_path / "out"
    r = run("pitfin_run.py", "--questions", str(SAMPLE), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert "memory gap A-B" in r.stdout and "rememberer:standin" in r.stdout and "+100 pts" in r.stdout and "SYNTHETIC" in r.stdout and "say nothing about any real model" in r.stdout
    rows = list(csv.DictReader((out / "results.csv").open()))
    assert len(rows) == 5 * 4 * 4 and all(x["synthetic"] == "True" and x["call_id"] and len(x["prompt_sha256"]) == 64 for x in rows)
    scores = json.loads((out / "scores.json").read_text())
    assert scores["synthetic"] is True and scores["scores"]["rememberer:standin"]["memory_gap"] == 1.0 and scores["scores"]["reasoner:standin"]["skill_baseline"] == 1.0
    assert Ledger(out / "pitfin.ledger.db").verify().ok


def test_a_real_provider_is_refused_without_live_before_anything_is_created(tmp_path):
    out = tmp_path / "never"
    r = run("pitfin_run.py", "--questions", str(SAMPLE), "--models", "anthropic:claude-x,openai:gpt-y,meta:muse-spark-1.3", "--out", str(out))
    assert r.returncode == 2 and "Nothing is called without --live" in r.stderr and not out.exists()


def test_with_live_but_no_key_every_call_is_a_clear_error_and_nothing_reaches_a_network(tmp_path):
    out = tmp_path / "live-nokey"
    r = run("pitfin_run.py", "--questions", str(SAMPLE), "--models", "anthropic:claude-x,meta:muse-spark-1.3", "--variants", "A", "--live", "--out", str(out))
    assert r.returncode == 0, r.stderr
    rows = list(csv.DictReader((out / "results.csv").open()))
    assert len(rows) == 10 and all(x["error"] and "no API key" in x["error"] for x in rows) and all(x["correct"] == "" for x in rows)
    assert "n/a" in r.stdout and "10 with errors" in r.stdout                                                  # no answered cell is shown as 0%, and the count of errors is stated


def test_a_bad_question_file_or_variant_is_a_clear_exit_not_a_traceback(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": "x"}]))
    r = run("pitfin_run.py", "--questions", str(bad), "--out", str(tmp_path / "o"))
    assert r.returncode == 2 and "cannot read the questions" in r.stderr and "Traceback" not in r.stderr
    r2 = run("pitfin_run.py", "--questions", str(SAMPLE), "--variants", "XYZ", "--out", str(tmp_path / "o2"))
    assert r2.returncode == 2 and "Traceback" not in r2.stderr


def test_the_leak_demo_offline_shows_the_leak_the_refusal_the_clean_receipt_and_the_flagged_one():
    r = run("leak_demo.py")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "OFFLINE DEMO with a local MOCK provider and a SYNTHETIC question" in out
    marks = ["(a) DIRECT", "(b1) THROUGH", "(b2) THROUGH", "(b3) WHAT"]
    pos = [out.index(m) for m in marks] + [len(out)]
    a, b1, b2, b3 = (out[pos[i]:pos[i + 1]] for i in range(4))
    assert "provider says it ran: ['web_search']" in a and "matches the true answer: True" in a and "412" in a
    assert "REFUSED by the gateway (rule server_tools_off)" in b1 and "requests that reached the provider because of it: 0" in b1 and "model_error" in b1
    assert "I do not have information" in b2 and "matches the true answer: False" in b2 and '"flags": []' in b2 and "run clean: True" in b2 and '"prompt_hash"' in b2
    assert "receipt label: FLAGGED" in b3 and "SERVER_TOOL" in b3 and "run clean: False" in b3 and "cannot call this run clean" in b3


def test_the_leak_demo_with_live_but_no_key_or_model_exits_before_any_call():
    r = run("leak_demo.py", "--live")
    assert r.returncode == 2 and "Nothing was called" in r.stderr and "LIVE:" not in r.stdout
    r2 = run("leak_demo.py", "--live", "--provider", "openai", "--model", "gpt-4o-search-preview")
    assert r2.returncode == 2 and "Nothing was called" in r2.stderr


def test_the_leak_demo_receipt_holds_no_prompt_or_answer_text():
    r = run("leak_demo.py")
    receipt = r.stdout.split("receipt (hashes and counts, never the text):")[1].split("run clean:")[0]
    assert "Acme" not in receipt and "revenue" not in receipt and "do not have information" not in receipt
