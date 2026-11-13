"""docs/pilot: the pilot package is executable where it can be, and honest everywhere: its template runs, its claims match the code, and it contains no invented numbers,
no model cutoff dates, no keys and no real contact details."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from asof import Session, contam, sources
from asof.sync import SyncConfig

ROOT = Path(__file__).resolve().parent.parent
PILOT = ROOT / "docs" / "pilot"
DOCS = {n: (PILOT / n).read_text() for n in ("INSTALL.md", "SUCCESS_CRITERIA.md", "DATA_HANDLING.md", "REPORT_TEMPLATE.md", "LIMITATIONS.md")}
EXTRA = {"CLAUDE_DESKTOP.md": (ROOT / "docs" / "CLAUDE_DESKTOP.md").read_text()}
PRIVATE_DOCS = ("DEPLOY.md", "OPERATIONS.md", "KEY_ROTATION.md")            # the service's own operations notes: kept in the private engine repository, not here


def test_the_five_documents_and_the_template_exist():
    assert set(DOCS) == {"INSTALL.md", "SUCCESS_CRITERIA.md", "DATA_HANDLING.md", "REPORT_TEMPLATE.md", "LIMITATIONS.md"}
    assert (PILOT / "examples" / "wrap_agent.py").exists()


def test_the_install_template_self_test_runs_and_prints_what_the_install_page_says():
    p = subprocess.run([sys.executable, str(PILOT / "examples" / "wrap_agent.py"), "--selftest"], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert p.returncode == 0, p.stdout + p.stderr
    out = p.stdout
    assert "(query: prices)" in out and "served 2" in out and out.count("blocked: future=1") == 2                  # "a query that served 2 and blocked 1, a tool call that served 1 and blocked 1"
    assert "memory label 'unknown'" in out and re.search(r"OK: 14 events, anchor ok", out) and "no keys, no network" in out
    for phrase in ("served 2 and blocked 1", "tool call that served 1 and blocked 1", "memory label is `unknown`", "OK: 14 events, anchor ok"):
        assert phrase in DOCS["INSTALL.md"], phrase


def test_the_template_does_not_run_without_an_as_of_and_never_reads_a_key_from_a_file():
    p = subprocess.run([sys.executable, str(PILOT / "examples" / "wrap_agent.py")], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert p.returncode == 2 and "--as-of" in p.stderr
    src = (PILOT / "examples" / "wrap_agent.py").read_text()
    assert not re.search(r"sk-[A-Za-z0-9]{12,}", src) and "api_key=" not in src                                    # keys come from the environment
    assert 'privacy: str = "hashes_only"' in src and '"hashes_only"' in src


def test_what_the_install_page_says_about_times_and_missing_timestamps_is_true(tmp_path):
    from asof.errors import NaiveTimeError
    with pytest.raises(NaiveTimeError, match="no time zone"):                                                       # the session CLOCK without a zone is refused
        Session("2023-01-21T14:00:00", ledger_path=tmp_path / "x.db")
    (tmp_path / "naive.csv").write_text("symbol,date,close,available_at\nS,2023-01-19,1.0,2023-01-19 21:00:00\nS,2023-01-18,2.0,2023-01-18 21:00:00\n")
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "l.db") as s:                                       # a RECORD time without a zone is never assumed UTC: withheld and counted
        s.add_source(sources.CSV(tmp_path / "naive.csv", event_time="date", available_at="available_at", key=("symbol", "date")))
        assert s.query("naive") == [] and s.receipt().blocked["untimestamped"] == 2
        assert s.events("source_loaded")[0].data()["flags"]["naive_available_at"] == 2
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "l3.db") as s3:                                     # ...unless you say how to read it
        s3.add_source(sources.CSV(tmp_path / "naive.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        assert len(s3.query("naive")) == 2
    (tmp_path / "none.csv").write_text("symbol,date,close,available_at\nS,2023-01-19,1.0,\nS,2023-01-18,2.0,2023-01-18T21:00:00Z\n")
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "l2.db") as s2:
        s2.add_source(sources.CSV(tmp_path / "none.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        rows = s2.query("none")
        r = s2.receipt()
    assert len(rows) == 1 and r.blocked["untimestamped"] == 1                                                       # a missing available_at: withheld and counted, not guessed
    t = DOCS["INSTALL.md"]
    assert "never assumed to be UTC" in t and "withheld and counted" in t and "session clock** without one is refused" in t and "blocked_untimestamped" in t


def test_the_cli_commands_and_error_messages_quoted_in_the_install_page_exist():
    from asof.cli import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "cmd")
    for cmd in ("receipt", "verify", "head", "sync"):
        assert cmd in sub.choices and f"strathora {cmd}" in DOCS["INSTALL.md"]
    v = subprocess.run([sys.executable, "-m", "asof", "--version"], capture_output=True, text=True)
    assert v.stdout.strip() in DOCS["INSTALL.md"] and v.stdout.startswith("strathora-asof ")                        # "strathora-asof 0.5.0"
    assert f"strathora_asof-{v.stdout.split()[1]}-py3-none-any.whl" in DOCS["INSTALL.md"]                            # the wheel's real file name for this version
    from asof.gateway import ModelGateway
    from asof.gateway import anthropic as call_anthropic, ModelRequest
    with pytest.raises(Exception, match="no API key for anthropic"):
        call_anthropic(ModelRequest("anthropic", "m", ({"role": "user", "content": "x"},), {}))                     # (the env var is absent in the test environment, or this would call out)
    assert ModelGateway.split("anthropic:m") == ("anthropic", "m") and "no API key for anthropic" in DOCS["INSTALL.md"]


def test_the_limits_the_data_handling_page_states_are_the_ones_in_the_client():
    with pytest.raises(Exception, match="between 1 and 1000"):
        SyncConfig(enabled=True, endpoint="https://x.example", api_key="k", batch_size=1001).validate()
    assert "1,000 events" in DOCS["DATA_HANDLING.md"] and "5 MB" in DOCS["DATA_HANDLING.md"]
    with pytest.raises(Exception, match="https"):
        SyncConfig(enabled=True, endpoint="http://remote.example/x", api_key="k").validate()                        # "refuses a non-https address (except localhost for testing)"
    SyncConfig(enabled=True, endpoint="http://localhost:3000/x", api_key="k", allow_insecure_localhost=True).validate()
    assert "AsOf's own library default is `full`" in DOCS["DATA_HANDLING.md"]
    import inspect
    assert inspect.signature(Session.__init__).parameters["privacy"].default == "full"                              # the page says so; this keeps it true


def test_the_limitations_page_matches_the_labels_the_library_can_produce():
    assert contam.LABELS == ("unknown", "observed")                                                                 # no "clean", no "verified"
    t = DOCS["LIMITATIONS.md"]
    assert "never \"Verified\"" in t and "`unknown`, or at most `observed`" in t and "no `clean` and no `verified`" in t
    for must in ("Timestamps are supplied by whoever owns the data", "head anchor must be stored somewhere you trust", "Only what flows through AsOf is locked", "not verified by the author"):
        assert must.lower() in t.lower(), must


def test_the_report_template_keeps_its_what_this_does_not_prove_section():
    t = DOCS["REPORT_TEMPLATE.md"]
    assert "## 8. What this does not prove" in t and "Do not shorten or remove this section." in t
    for b in ("source timestamps are true", "closed model forgot anything", "nothing was left unsent", "could **not** reach data another way", "generalise"):
        assert b in t, b


def test_the_success_criteria_ask_for_the_baseline_upfront_and_define_all_four_measures():
    t = DOCS["SUCCESS_CRITERIA.md"]
    for m in ("### 1. Leaks found", "### 2. Share of the live-versus-backtest gap explained", "### 3. Hours saved against manual review", "### 4. Decisions changed"):
        assert m in t
    assert "Baseline, asked upfront" in t and "before the pilot starts" in t and "not computable" in t and "No number without a link" in t


@pytest.mark.parametrize("name", ["INSTALL.md", "SUCCESS_CRITERIA.md", "DATA_HANDLING.md", "REPORT_TEMPLATE.md", "LIMITATIONS.md", "CLAUDE_DESKTOP.md"])
def test_no_invented_metrics_no_model_cutoffs_no_keys_no_real_contact_details(name):
    text = {**DOCS, **EXTRA}[name]
    assert not re.search(r"\d+(\.\d+)?\s?%", text), f"{name} contains a percentage: every figure in a pilot document must come from the customer or an evidence link"
    assert not re.search(r"cutoff[^.\n]{0,60}\b(19|20)\d\d\b", text, re.I), f"{name} states a model cutoff date"
    assert not re.search(r"sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}", text)
    for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        assert "your-domain" in m or m.endswith(("@example.org", "@example.com")), (name, m)


def test_install_page_time_budget_adds_up_and_is_honest_about_what_was_timed():
    t = DOCS["INSTALL.md"]
    rows = dict(re.findall(r"^\| ([^|]+?) \| (\d+) \|$", t.split("## Time budget")[1], re.M))
    parts = [int(v) for k, v in rows.items() if not k.startswith("**")]
    assert sum(parts) == 15 and "**Total**" in t and "estimates" in t and "Only steps 2 and 3 have been timed" in t


def test_the_pages_describe_chains_login_encryption_and_purge_limits_and_the_old_one_chain_claims_are_gone():
    dh, lim, inst = DOCS["DATA_HANDLING.md"], DOCS["LIMITATIONS.md"], DOCS["INSTALL.md"]
    for must in ("More than one ledger per customer", "A **new chain id is not a fork**", "Encryption at rest", "AES-256-GCM", "never stored in the database", "owner-only", "backups", "free space", "no public sign-up"):
        assert must.lower() in dh.lower(), must
    assert "plain (unencrypted) SQLite" not in dh and "no login or single sign-on" not in dh
    assert "One key is one ledger chain" not in lim and "no login, no encryption" not in lim and "in the engine process's memory" in lim and "nginx and Caddy configurations" in lim
    assert "One key belongs to **one ledger file**" not in inst and "ASOF_SYNC_CHAIN_LABEL" in inst
    assert "No Strathora endpoint exists yet" not in (ROOT / "README.md").read_text() + (ROOT / "docs" / "REFERENCE.md").read_text()


def test_the_pages_state_the_limits_of_execution_attribution_links_and_bars_and_what_hashes_only_hides():
    lim, dh = DOCS["LIMITATIONS.md"], DOCS["DATA_HANDLING.md"]
    for must in ("AsOf cannot see an exchange", "add up to the gap exactly", "Observed", "not Verified", "opportunity cost of an order that never filled is not attributed", "An exit order inside one bar is unknown",
                 "what was available, not what was used", "Bars are only as good as their close", "not synced"):
        assert must in lim, must
    assert "execution" in dh.lower() and "symbol, prices, sizes, fees, levels and position marks are hashed" in dh and "evidence links" in dh


def test_the_services_operations_notes_and_the_private_codebases_name_are_not_in_this_repository():
    """DEPLOY.md, OPERATIONS.md and KEY_ROTATION.md describe how the private service is run. They live with that service, and no file here names its code."""
    for name in PRIVATE_DOCS:
        assert not (PILOT / name).exists(), name
    hits = []
    for f in sorted(p for p in ROOT.rglob("*") if p.is_file() and not any(part.startswith(".") or part in ("build", "__pycache__") or part.endswith(".egg-info") for part in p.relative_to(ROOT).parts)):
        if f == Path(__file__).resolve():
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if re.search("strat" + "ton", line, re.I) or any(d in line for d in PRIVATE_DOCS):      # the private package's name, in two halves so this file does not hold it either
                hits.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert hits == [], "\n".join(hits)
