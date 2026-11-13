"""R7: credentials never reach the ledger; hashes_only keeps raw data out of it."""
import pytest

from asof import ModelResponse, Session, sources

from conftest import ListSource, rec

KEY = "sk-" + "ant-CANARYKEY0123456789abcdef"          # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)
AWS_ID = "AK" + "IAABCDEFGHIJKLMNOP"                    # the same, for the shape of an AWS access key id
CANARIES = ["CANARY-PAYLOAD-ACME-SYN-ALPHA", "CANARY-FILTER-STRATEGY", "CANARY-TOOL-ARG", "CANARY-TOOL-RESULT", "CANARY-PROMPT-TEXT", "CANARY-RESPONSE-TEXT",
            "CANARY-SYSTEM-PARAM", "CANARY-RECORD-ID"]


def run_everything(path, privacy):
    s = Session("2023-01-21T14:00:00Z", ledger_path=path, privacy=privacy)
    s.add_source(ListSource("d", [rec("CANARY-RECORD-ID", "2023-01-01T00:00:00Z", {"note": "CANARY-PAYLOAD-ACME-SYN-ALPHA", "k": "CANARY-FILTER-STRATEGY"}),
                                  rec("CANARY-RECORD-ID-FUT", "2030-01-01T00:00:00Z", {"note": "CANARY-PAYLOAD-ACME-SYN-ALPHA", "k": "CANARY-FILTER-STRATEGY"})]))
    s.add_tool("t", lambda q: [{"timestamp": "2023-01-01T00:00:00Z", "text": "CANARY-TOOL-RESULT"}])
    s.register_provider("fake", lambda req: ModelResponse("CANARY-RESPONSE-TEXT"))
    s.query("d", k="CANARY-FILTER-STRATEGY")
    s.tool("t", q="CANARY-TOOL-ARG", api_key=KEY) if False else s.tool("t", q="CANARY-TOOL-ARG")
    s.model("fake:m", messages=[{"role": "user", "content": "CANARY-PROMPT-TEXT " + KEY}], api_key=KEY, system="CANARY-SYSTEM-PARAM", temperature=0)
    receipts = s.receipt(last=False)
    s.close()
    return s, receipts


def file_text(path):
    return path.read_bytes().decode("latin-1")


def test_hashes_only_keeps_every_canary_out_of_the_ledger_file(ledger_path):
    _, receipts = run_everything(ledger_path, "hashes_only")
    text = file_text(ledger_path)
    for c in CANARIES:
        assert c not in text, c
    assert KEY not in text and "CANARYKEY" not in text
    assert "sha256" in text                                                                    # it still records hashes
    assert all("CANARY" not in r.to_json() for r in receipts)                                  # receipts carry hashes, not ids, in this mode


def test_hashes_only_still_records_counts_names_times_and_the_model_id(ledger_path):
    s, _ = run_everything(ledger_path, "hashes_only")
    q = s.events("query")[0].data()
    assert q["counts"]["served"] == 1 and q["counts"]["blocked_future"] == 1 and q["source"] == "d" and "filters_sha256" in q and "filters" not in q
    m = s.events("model_call")[0].data()
    assert m["model_id"] == "fake:m" and set(m["param_sha256"]) == {"system", "temperature"} and all(len(h) == 64 for h in m["param_sha256"].values())
    assert "params_sha256" in m and "params" not in m and "messages" not in m and "response_text" not in m
    assert len(m["prompt_sha256"]) == 64 and len(m["response_sha256"]) == 64
    assert s.verify_ledger().ok


def test_full_mode_keeps_the_data_but_never_the_key(ledger_path):
    _, receipts = run_everything(ledger_path, "full")
    text = file_text(ledger_path)
    for c in ("CANARY-PAYLOAD-ACME-SYN-ALPHA", "CANARY-TOOL-RESULT", "CANARY-PROMPT-TEXT", "CANARY-RESPONSE-TEXT", "CANARY-RECORD-ID"):
        assert c in text, c
    assert KEY not in text and "CANARYKEY" not in text and "[REDACTED]" in text                # the key that was pasted into a prompt is scrubbed too
    assert all(KEY not in r.to_json() for r in receipts)


def test_credential_shaped_keys_and_values_are_redacted_everywhere(make_session):
    s = make_session()
    s.add_tool("t", lambda **kw: [{"timestamp": "2023-01-01T00:00:00Z", "Authorization": "Bearer abcdefghijklmnop", "password": "hunter2", "ok": "fine"}])
    s.tool("t", api_key="whatever", x_api_key="x", token_note=AWS_ID, note="ghp_" + "a" * 30)
    blob = str([e.payload for e in s.events()])
    for leaked in ("whatever", "hunter2", "abcdefghijklmnop", AWS_ID, "ghp_" + "a" * 30):
        assert leaked not in blob, leaked
    assert "fine" in blob


def test_max_tokens_style_parameters_are_not_mistaken_for_secrets(make_session):
    s = make_session()
    s.register_provider("fake", lambda req: ModelResponse("x"))
    s.model("fake:m", messages=[{"role": "user", "content": "q"}], max_tokens=100)
    assert s.events("model_call")[0].data()["params"] == {"max_tokens": 100}


def test_an_unknown_privacy_mode_is_rejected(ledger_path):
    with pytest.raises(ValueError, match="privacy"):
        Session("2023-01-21T14:00:00Z", ledger_path=ledger_path, privacy="verbose")


def test_no_api_key_reaches_any_log_stdout_or_stderr(ledger_path, caplog, capsys):
    import logging
    with caplog.at_level(logging.DEBUG):
        run_everything(ledger_path, "full")
        run_everything(ledger_path.parent / "other.db", "hashes_only")
    out = capsys.readouterr()
    for stream in (caplog.text, out.out, out.err):
        assert KEY not in stream and "CANARYKEY" not in stream
    assert KEY.encode() not in ledger_path.read_bytes() and KEY.encode() not in (ledger_path.parent / "other.db").read_bytes()


def test_no_file_in_the_repository_holds_a_key_shaped_string():
    """The tests need fake keys to prove they are redacted, but a literal that looks like a key can make a secret scanner (or GitHub push protection) stop a push. Every fake
    key is therefore built from two parts at run time. This scans the repository for the shapes the redaction itself knows, plus a few other providers' tokens."""
    import re
    from pathlib import Path
    from asof.ledger import redact
    root = Path(__file__).resolve().parent.parent
    shapes = re.compile(r"sk-[A-Za-z0-9_\-]{12,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}|\bxox[abprs]-[A-Za-z0-9\-]{10,}|\bAIza[0-9A-Za-z_\-]{30,}"
                        r"|\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY")
    assert redact(f"x {KEY} y {AWS_ID}") == "x [REDACTED] y [REDACTED]" and shapes.search(KEY) and shapes.search(AWS_ID)      # the fakes ARE key-shaped once assembled: the tests still test something
    hits = []
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not any(part.startswith(".") or part in ("build", "__pycache__") or part.endswith(".egg-info") for part in p.relative_to(root).parts)):
        data = f.read_bytes()
        if b"\0" in data[:8000]:
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            m = shapes.search(line)
            if m:
                hits.append(f"{f.relative_to(root)}:{n}: {m.group(0)[:12]}...")
    assert hits == [], "\n".join(hits)
