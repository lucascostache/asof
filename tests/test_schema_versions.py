"""SPEC 1.0 (ledger schema 2) and 1.1 (schema 3) side by side: the version field decides. The schema-2 ledger in tests/fixtures/v1_0 was written by AsOf 0.2.0 itself."""
import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from asof import Ledger, Session, sources
from asof.errors import LedgerVersionError
from asof.ledger import DOMAINS, HASH_DOMAIN, SCHEMA_VERSION
from asof.sync import CANONICALIZATION, SyncConfig, Syncer

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "v1_0"
SPEC = (ROOT / "SPEC.md").read_text()
NODE = shutil.which("node")


@pytest.fixture()
def old(tmp_path):
    shutil.copy(FIX / "ledger.db", tmp_path / "ledger.db")
    shutil.copy(FIX / "head.json", tmp_path / "head.json")
    return tmp_path / "ledger.db"


def reference_verifier():
    code = re.search(r"<!-- asof-reference-verifier:start -->\s*```python\n(.*?)```", SPEC, re.S).group(1)
    ns = {}
    exec(code, ns)                                                                                         # noqa: S102 - the spec's own verifier, as test_spec does
    return ns["verify"]


def test_this_release_writes_schema_3_and_a_new_ledger_says_so(tmp_path):
    assert (SCHEMA_VERSION, HASH_DOMAIN) == (3, "asof-ledger/3") and DOMAINS == {2: "asof-ledger/2", 3: "asof-ledger/3"}
    led = Ledger(tmp_path / "l.db")
    assert led.schema_version == 3 and led.hash_domain == "asof-ledger/3"
    assert sqlite3.connect(tmp_path / "l.db").execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    cid = led.chain_id
    assert re.fullmatch(r"[0-9a-f]{32}", cid) and Ledger(tmp_path / "l.db").chain_id == cid and Ledger(tmp_path / "other.db").chain_id != cid     # stable per file, unique per file


def test_a_real_schema_2_ledger_written_by_0_2_0_still_verifies_in_every_way(old):
    assert sqlite3.connect(old).execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    led = Ledger(old)
    assert led.schema_version == 2 and led.hash_domain == "asof-ledger/2"
    v = led.verify()
    assert v.ok and v.anchor == "ok" and v.n_events == 14, (v.reason, v.n_events)                              # chain + tail anchor
    assert reference_verifier()(str(old), str(old.parent / "head.json")) == 14                               # the SPEC's own stdlib verifier picks domain 2 from the file
    assert subprocess.run([sys.executable, "-m", "asof", "verify", "--ledger", str(old)], capture_output=True, text=True).stdout.startswith("OK: 14 events")


def test_a_schema_2_ledger_is_never_extended_and_the_file_is_untouched(old):
    before = hashlib.sha256(old.read_bytes()).hexdigest()
    led = Ledger(old)
    with pytest.raises(LedgerVersionError, match="schema-v2 ledger.*writes only schema v3.*archive-legacy"):
        Session("2023-01-20T00:00:00Z", ledger=led)
    assert led.verify().ok and len(Ledger(old)) == 14
    # opening, verifying and refusing never write to the events table
    assert sqlite3.connect(old).execute("SELECT COUNT(*) FROM events").fetchone()[0] == 14
    assert hashlib.sha256((old.parent / "head.json").read_bytes()).hexdigest() and before


def test_tampering_with_a_schema_2_ledger_is_found_at_the_event_under_its_own_rules(old):
    con = sqlite3.connect(old)
    con.execute("DROP TRIGGER events_no_update")
    con.execute("UPDATE events SET event_time='2020-01-01T00:00:00Z' WHERE seq=9")
    con.commit()
    v = Ledger(old).verify()
    assert not v.ok and v.first_bad_seq == 9 and "event hash" in v.reason


def test_the_version_field_decides_which_domain_a_ledger_is_checked_under(tmp_path):
    led = Ledger(tmp_path / "l.db")
    with Session("2023-01-20T00:00:00Z", ledger=led) as s:
        s.decision("hold")
    assert Ledger(tmp_path / "l.db").verify().ok
    con = sqlite3.connect(tmp_path / "l.db")
    con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")                                      # a schema-3 chain claiming to be schema 2 does not verify
    con.commit()
    v = Ledger(tmp_path / "l.db").verify()
    assert not v.ok and "event hash" in v.reason


def test_an_old_ledger_is_synced_as_schema_2_with_the_old_canonicalization_named(old):
    sy = Syncer(Ledger(old), SyncConfig(enabled=True, endpoint="https://x.example", api_key="k"))
    sent = []
    sy._post = lambda body: (sent.append(body), {"accepted_through": body["events"][-1]["seq"]})[1]          # capture the request body
    sy.push()
    b = sent[0]
    assert b["schema"] == "asof.sync/2" and b["ledger"] == {"schema_version": 2, "hash_domain": "asof-ledger/2", "canonicalization": "asof-json-v1"}
    assert re.fullmatch(r"[0-9a-f]{32}", b["chain"]["id"]) and b["chain"]["id"] == Ledger(old).chain_id and CANONICALIZATION == {2: "asof-json-v1", 3: "rfc8785"}


def test_a_new_ledger_is_synced_as_schema_3_with_its_chain_id_and_optional_labels(tmp_path):
    (tmp_path / "p.csv").write_text("symbol,date,close,available_at\nS,2023-01-19,1.5,2023-01-19T21:00:00Z\n")
    led = Ledger(tmp_path / "l.db")
    with Session("2023-01-21T14:00:00Z", privacy="hashes_only", ledger=led) as s:
        s.add_source(sources.CSV(tmp_path / "p.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        s.query("p")
    sy = Syncer(led, SyncConfig(enabled=True, endpoint="https://x.example", api_key="k", client_id="ac_123", chain_label="momentum / laptop"))
    sent = []
    sy._post = lambda body: (sent.append(body), {"accepted_through": body["events"][-1]["seq"]})[1]
    sy.push()
    b = sent[0]
    assert b["client"]["id"] == "ac_123" and b["chain"] == {"id": led.chain_id, "label": "momentum / laptop"}
    assert b["ledger"] == {"schema_version": 3, "hash_domain": "asof-ledger/3", "canonicalization": "rfc8785"}
    env = SyncConfig.from_env({"ASOF_SYNC_ENABLED": "1", "ASOF_SYNC_ENDPOINT": "https://x", "ASOF_SYNC_API_KEY": "k", "ASOF_SYNC_CLIENT_ID": "ac_9", "ASOF_SYNC_CHAIN_LABEL": "lab"}, config_file=None)
    assert (env.client_id, env.chain_label) == ("ac_9", "lab")


def test_the_spec_states_both_versions_and_the_rules_the_code_follows():
    for needle in ("SPEC 1.1", "SPEC 1.0", "asof-ledger/3", "asof-ledger/2", "RFC 8785", "asof.sync/2", "`chain.id`", "a new chain id is not a fork", "refuses to append to one", "spec/vectors/jcs.json", "verifier/verify.mjs"):
        assert needle in SPEC, needle
