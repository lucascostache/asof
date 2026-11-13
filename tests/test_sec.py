"""C: the SEC source. No network: a canned response in EDGAR's column layout, a fake clock, and a local HTTP server."""
import hashlib
import json
import random
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from asof import Session, SourceError, sources
from asof.sources.sec import ENV_USER_AGENT, Cache, RateLimiter, check_user_agent

from conftest import T

UA = "Example Research ops@example.org"
ROOT = Path(__file__).resolve().parent.parent


def fixture():
    recent = {"accessionNumber": ["0009999991-23-000106", "0009999991-23-000999", "0009999991-23-000077"], "form": ["10-K", "10-K/A", "10-Q"],
              "filingDate": ["2023-11-03", "2023-12-15", "2023-08-04"], "reportDate": ["2023-09-30", "2023-09-30", "2023-07-01"],
              "acceptanceDateTime": ["2023-11-03T09:08:27.000Z", "2023-12-15T10:00:00.000Z", None], "primaryDocument": ["a.htm", "b.htm", "c.htm"]}
    return {"cik": "0009999991", "name": "Example Inc", "filings": {"recent": recent, "files": [{"name": "CIK0009999991-submissions-001.json"}]}}


class Fetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, url, ua):
        self.calls.append((url, ua))
        if url.endswith("CIK0009999991.json"):
            return json.dumps(fixture()).encode()
        return json.dumps({"accessionNumber": ["0009999991-01-000001"], "form": ["10-K"], "filingDate": ["2001-12-01"], "reportDate": ["2001-09-30"],
                           "acceptanceDateTime": ["2001-12-01T12:00:00.000Z"], "primaryDocument": ["old.txt"]}).encode()


def make(tmp_path, fetch=None, **kw):
    kw.setdefault("cache_dir", tmp_path / "cache")
    kw.setdefault("sleep", lambda s: None)
    return sources.SECFilings(UA, [9999991], fetch=fetch or Fetcher(), **kw)


# ------------------------------------------------------------------ the User-Agent is the user's own
def test_a_user_agent_is_required_and_must_identify_the_user(monkeypatch):
    monkeypatch.delenv(ENV_USER_AGENT, raising=False)
    for bad in ("", "<set by user>", "   ", "x", "python-requests", "Just A Name With No Contact"):
        with pytest.raises(SourceError, match="User-Agent"):
            sources.SECFilings(bad, [9999991], fetch=Fetcher(), cache_dir=None)
    with pytest.raises(SourceError, match="User-Agent"):
        sources.SECFilings(None, [9999991], fetch=Fetcher(), cache_dir=None)                       # no default, even from the environment, until the user sets it
    monkeypatch.setenv(ENV_USER_AGENT, "Env Research me@env-domain.example")
    assert sources.SECFilings(None, [9999991], fetch=Fetcher(), cache_dir=None).user_agent == "Env Research me@env-domain.example"
    assert check_user_agent("Acme Corp https://acme.example/contact") == "Acme Corp https://acme.example/contact"
    with pytest.raises(SourceError, match="at least one CIK"):
        sources.SECFilings(UA, [], fetch=Fetcher(), cache_dir=None)


def test_the_package_contains_no_email_address_except_documentation_examples():
    pat = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    found = {}
    for f in (ROOT / "asof").rglob("*.py"):
        for m in pat.findall(f.read_text()):
            found.setdefault(m, set()).add(f.name)
    unexpected = {m: fs for m, fs in found.items() if not m.endswith(("@example.org", "@example.com"))}
    assert not unexpected, f"real-looking addresses in the package: {unexpected}"


def test_the_configured_user_agent_is_sent_on_every_request(tmp_path):
    f = Fetcher()
    list(make(tmp_path, f).records())
    assert f.calls and all(ua == UA for _, ua in f.calls)


# ------------------------------------------------------------------ fair access: rate limit
class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_requests_are_spaced_so_the_rate_never_exceeds_10_per_second(tmp_path):
    clk, starts = FakeClock(), []

    def fetch(url, ua):
        starts.append(clk.now())
        clk.t += 0.005                                                                         # each request takes 5 ms
        return json.dumps({"filings": {"recent": {}, "files": []}}).encode()
    src = sources.SECFilings(UA, list(range(1, 31)), fetch=fetch, cache_dir=None, clock=clk.now, sleep=clk.sleep)
    list(src.records())
    assert len(starts) == 30
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 0.125 - 1e-9                                                           # spaced by the configured interval
    for i, t0 in enumerate(starts):
        assert sum(1 for t in starts if t0 <= t < t0 + 1.0) <= 10                              # no 1-second window holds more than 10 requests
    assert 29 * 0.125 <= starts[-1] - starts[0] < 29 * 0.125 + 0.2


def test_a_limit_above_the_sec_ceiling_is_refused():
    for bad in (0.0, 0.05, 0.099):
        with pytest.raises(SourceError, match="10 requests per second"):
            RateLimiter(bad)
        with pytest.raises(SourceError, match="10 requests per second"):
            sources.SECFilings(UA, [1], fetch=Fetcher(), cache_dir=None, min_interval_s=bad)
    RateLimiter(0.1)


# ------------------------------------------------------------------ the local cache, keyed and checked by sha256
def test_cache_hits_make_no_request_and_files_are_named_by_the_sha256_of_the_url(tmp_path):
    f = Fetcher()
    src = make(tmp_path, f)
    first = [r.to_dict() for r in src.records()]
    n = len(f.calls)
    assert n == 2 and src.requests_made == 2
    second = [r.to_dict() for r in make(tmp_path, f).records()]                                # a fresh source object, same cache directory
    assert len(f.calls) == n and second == first                                               # no new request
    url = "https://data.sec.gov/submissions/CIK0009999991.json"
    path = tmp_path / "cache" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    d = json.loads(path.read_text())
    assert d["url"] == url and d["body_sha256"] == hashlib.sha256(d["body"].encode()).hexdigest()


def test_a_tampered_or_corrupt_cache_entry_is_ignored_and_refetched(tmp_path):
    f = Fetcher()
    list(make(tmp_path, f).records())
    for p in (tmp_path / "cache").glob("*.json"):
        d = json.loads(p.read_text())
        d["body"] = d["body"].replace("10-K", "8-K")                                           # body changed, hash not
        p.write_text(json.dumps(d))
    before = len(f.calls)
    recs = list(make(tmp_path, f).records())
    assert len(f.calls) == before + 2 and "8-K" not in {r.payload["form"] for r in recs}
    (next((tmp_path / "cache").glob("*.json"))).write_text("garbage")
    list(make(tmp_path, f).records())                                                          # still works


def test_stale_entries_expire_and_a_non_json_response_is_never_cached(tmp_path):
    f = Fetcher()
    list(make(tmp_path, f, cache_ttl_s=-1).records())
    before = len(f.calls)
    list(make(tmp_path, f, cache_ttl_s=-1).records())
    assert len(f.calls) == before + 2                                                          # ttl already passed: refetched
    bad_dir = tmp_path / "bad"
    with pytest.raises(SourceError, match="not JSON"):
        list(sources.SECFilings(UA, [1], fetch=lambda u, a: b"<html>blocked</html>", cache_dir=bad_dir, sleep=lambda s: None).records())
    assert not list(bad_dir.glob("*.json")) if bad_dir.exists() else True


def test_cache_unit(tmp_path):
    t = [100.0]
    c = Cache(tmp_path, ttl_s=10, now=lambda: t[0])
    assert c.get("u") is None
    c.put("u", b'{"a": 1}')
    assert c.get("u") == b'{"a": 1}'
    t[0] = 111.0
    assert c.get("u") is None


# ------------------------------------------------------------------ how acceptanceDateTime is read
def sec(tz=None, **kw):
    return sources.SECFilings(UA, [1], fetch=Fetcher(), cache_dir=None, **({"acceptance_tz": tz} if tz else {}), **kw)


@pytest.mark.parametrize("raw, tz, expected", [
    ("2023-11-03T09:08:27.000Z", "UTC", "2023-11-03T09:08:27Z"),                  # the label is trusted
    ("2023-11-03T09:08:27.000Z", "America/New_York", "2023-11-03T13:08:27Z"),     # digits read as New York time, daylight (UTC-4)
    ("2023-12-15T10:00:00.000Z", "America/New_York", "2023-12-15T15:00:00Z"),     # winter (UTC-5)
    ("2023-12-15T10:00:00.000Z", "UTC", "2023-12-15T10:00:00Z"),
    ("2023-11-05T01:30:00.000Z", "America/New_York", "2023-11-05T06:30:00Z"),     # ambiguous (clocks go back): the LATER instant (EST), not 05:30Z (EDT)
    ("2023-03-12T02:30:00.000Z", "America/New_York", "2023-03-12T07:30:00Z"),     # non-existent (clocks go forward): the later reading
    ("2023-11-03T09:08:27+00:00", "America/New_York", "2023-11-03T13:08:27Z"),
])
def test_acceptance_time_conversion(raw, tz, expected):
    got, problem = sec(tz).acceptance_to_utc(raw)
    assert problem is None and got == T(expected)


def test_the_default_is_documented_to_err_late_and_never_serves_earlier_than_the_utc_reading():
    rng = random.Random(7)
    d, u = sec(), sec("UTC")
    assert d.acceptance_tz == "America/New_York"
    for _ in range(500):
        raw = (T("2015-01-01T00:00:00Z") + timedelta(seconds=rng.randrange(0, 10 * 365 * 86400))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        late, _ = d.acceptance_to_utc(raw)
        utc, _ = u.acceptance_to_utc(raw)
        assert late >= utc and late - utc in (timedelta(hours=4), timedelta(hours=5))
    with pytest.raises(SourceError, match="unknown time zone"):
        sec("America/Nowhere")


def test_missing_and_malformed_acceptance_times_are_withheld_not_guessed():
    s = sec()
    for raw in (None, "", "not a time", 12345):
        got, problem = s.acceptance_to_utc(raw)
        assert got is None and problem in ("missing", "unparseable")


# ------------------------------------------------------------------ a filing is never served before its acceptance time
@pytest.mark.parametrize("tz, accepted", [("UTC", "2023-11-03T09:08:27Z"), ("America/New_York", "2023-11-03T13:08:27Z")])
def test_never_served_before_acceptance_inclusive_at_it(tmp_path, tz, accepted, make_session):
    for offset, served in ((-timedelta(microseconds=1), False), (timedelta(0), True), (timedelta(microseconds=1), True)):
        s = Session(T(accepted) + offset, ledger_path=tmp_path / f"{tz.replace('/', '_')}{offset.microseconds}{offset.days}.db")
        s.add_source(make(tmp_path / "c", acceptance_tz=tz, include_older=False, forms=("10-K",)))
        got = s.query("sec_filings")
        assert (len(got) == 1) is served, (tz, offset)
        s.close()


def test_a_filing_without_an_acceptance_time_is_withheld_unless_the_session_opts_in(tmp_path):
    s = Session("2030-01-01T00:00:00Z", ledger_path=tmp_path / "l.db")
    s.add_source(make(tmp_path, include_older=False))
    assert [r.payload["form"] for r in s.query("sec_filings")] == ["10-K/A"]                    # the 10-Q (no acceptance) is withheld at any T; the amendment replaced the 10-K
    s2 = Session("2030-01-01T00:00:00Z", ledger_path=tmp_path / "l2.db", allow_untimestamped=True)
    s2.add_source(make(tmp_path / "c2", include_older=False))
    got = {r.payload["form"]: r for r in s2.query("sec_filings")}
    assert "10-Q" in got and s2.receipt().served_timing["unknown"] == 1
    s.close(); s2.close()


def test_the_original_is_served_until_the_amendment_is_accepted_and_never_afterwards_reversed(tmp_path):
    s = Session("2023-12-01T00:00:00Z", ledger_path=tmp_path / "l.db")
    s.add_source(make(tmp_path, include_older=False))
    assert [r.payload["form"] for r in s.query("sec_filings", form=["10-K", "10-K/A"])] == ["10-K"]
    s.advance_to("2023-12-15T14:59:59Z")                                                        # 10:00 NY on the 15th is 15:00Z: one second early
    assert [r.payload["form"] for r in s.query("sec_filings", form=["10-K", "10-K/A"])] == ["10-K"]
    s.advance_to("2023-12-15T15:00:00Z")
    assert [r.payload["form"] for r in s.query("sec_filings", form=["10-K", "10-K/A"])] == ["10-K/A"]
    s.close()


# ------------------------------------------------------------------ a real HTTP round trip (local server)
def test_real_http_request_carries_the_user_agent_and_is_spaced_in_real_time(tmp_path):
    import time
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((time.monotonic(), self.path, self.headers.get("User-Agent")))
            body = json.dumps(fixture() if "CIK0009999991.json" in self.path else {"filings": {"recent": {}}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import os
        os.environ["NO_PROXY"] = "*"
        src = sources.SECFilings(UA, [9999991], cache_dir=None, base_url=f"http://127.0.0.1:{srv.server_port}/submissions/", min_interval_s=0.12)
        recs = list(src.records())
    finally:
        srv.shutdown()
    assert len(recs) == 3 and len(seen) == 2
    assert all(ua == UA for _, _, ua in seen) and seen[0][1] == "/submissions/CIK0009999991.json"
    assert seen[1][0] - seen[0][0] >= 0.11                                                      # real wall-clock spacing between the two requests


# ------------------------------------------------------------------ real filings with known acceptance times
def known_filings():
    p = ROOT / "tests" / "fixtures" / "edgar_known_filings.json"
    return json.loads(p.read_text()).get("filings", [])


@pytest.mark.skipif(not known_filings(), reason="tests/fixtures/edgar_known_filings.json is empty: it needs real filings fetched with your own User-Agent "
                                                "(python scripts/smoke_edgar.py --write-fixture ...). Until then the real-world conversion is UNVERIFIED.")
@pytest.mark.parametrize("f", known_filings() or [{}], ids=lambda f: f.get("accession", "none"))
def test_conversion_against_real_filings_with_known_acceptance_times(f):
    from zoneinfo import ZoneInfo
    from datetime import datetime
    truth = datetime.fromisoformat(f["accepted_new_york"]).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(__import__("datetime").timezone.utc)
    default, _ = sec().acceptance_to_utc(f["acceptance_datetime_raw"])
    trusting, _ = sec("UTC").acceptance_to_utc(f["acceptance_datetime_raw"])
    assert default >= truth, f"the default reading is EARLIER than EDGAR's own accepted time for {f['accession']}: that would leak"
    print(f"{f['accession']}: raw {f['acceptance_datetime_raw']}  EDGAR-index (NY) {f['accepted_new_york']}  truth {truth.isoformat()}  "
          f"default {default.isoformat()}  UTC-label {trusting.isoformat()}  UTC label correct: {trusting == truth}")
