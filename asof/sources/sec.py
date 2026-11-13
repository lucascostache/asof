"""SECFilings: filing metadata from SEC EDGAR, with each filing's acceptance time as ``available_at``.

What this source is and is not
------------------------------
* It lists filings (form type, dates, accession number, document URL). It does **not** download or parse filing text or XBRL numbers.
* ``available_at`` is the filing's ``acceptanceDateTime``, converted to UTC. A filing is **never served before its acceptance time** (R1 inclusive
  boundary: at the accepted instant it is served, one microsecond earlier it is not). A filing with no acceptance time is withheld (R2).
* An amendment (for example 10-K/A) of the same form and period shares a ``record_id`` with the original, so it is a restatement (R3): the original is
  served until the amendment is accepted, and never the other way round.

How ``acceptanceDateTime`` is interpreted (exactly)
---------------------------------------------------
EDGAR's submissions JSON gives acceptance times like ``"2023-11-03T09:08:27.000Z"``: a trailing ``Z``, which would mean UTC. AsOf does not take that label on
trust by default. ``acceptance_tz`` controls the reading:

``acceptance_tz="America/New_York"`` (the default, which **errs late**)
    The ``Z`` is dropped and the digits are read as New York wall-clock time (what EDGAR's own filing-index page shows), then converted to UTC. In summer
    that is 4 hours later than the same digits read as UTC, in winter 5 hours. An ambiguous local time (clocks going back) resolves to the later instant.
``acceptance_tz="UTC"``
    The ``Z`` is trusted: the digits are UTC.

If the ``Z`` label is correct, the default withholds filings a few hours longer than necessary (safe). If the label were wrong and the digits were New York
time, trusting it would serve filings hours early (a leak). **The author has not confirmed against live EDGAR which reading is right.** ``scripts/smoke_edgar.py``
fetches real filings with *your* User-Agent and compares the JSON value with the "Accepted" time on EDGAR's filing-index page, and can write the result
as a fixture for ``tests/test_sec.py``. Switch to ``"UTC"`` only after that comparison agrees.

SEC fair access
---------------
* **Rate limit:** requests are spaced at least ``min_interval_s`` apart (default 0.125 s = 8 per second; values under 0.1 s are refused, so the SEC's
  10-per-second ceiling cannot be exceeded). Cache hits make no request.
* **User-Agent:** you must supply your own (``user_agent=`` or the ``ASOF_SEC_USER_AGENT`` environment variable), naming your organisation and a contact
  address or URL that is yours. There is no default and no address in this package. See https://www.sec.gov/os/webmaster-faq#developers
* **Cache:** each response is stored under ``cache_dir`` (default ``~/.asof/cache/sec``) as a file named by the sha256 of its URL, with the sha256 of the
  body recorded inside; a cache file whose body no longer matches its hash is ignored and refetched. Entries older than ``cache_ttl_s`` (default 24 h)
  are refetched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from ..errors import SourceError
from ..records import Record
from .base import Source, parse_source_time

BASE = "https://data.sec.gov/submissions/"
ENV_USER_AGENT = "ASOF_SEC_USER_AGENT"
MIN_ALLOWED_INTERVAL_S = 0.1                       # 10 requests per second is the SEC's ceiling
PLACEHOLDERS = {"", "<set by user>", "set by user", "user", "python-requests"}
_UTC_SUFFIX = re.compile(r"(Z|[+-]00:?00)$", re.IGNORECASE)
Fetch = Callable[[str, str], bytes]


def _default_fetch(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity", "Accept": "application/json"})
    try:
        ctx = None
        try:
            import ssl

            import certifi                                                         # optional: trusted roots on Pythons that ship none
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SourceError(f"SEC EDGAR returned HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"could not reach SEC EDGAR: {e.reason}") from e


def check_user_agent(user_agent: str | None) -> str:
    """The user's own User-Agent, or an error. It must identify them: a contact address (``@``) or a URL."""
    ua = (user_agent if user_agent is not None else os.environ.get(ENV_USER_AGENT, "")).strip()
    if ua.lower() in PLACEHOLDERS or len(ua) < 8 or not ("@" in ua or "http" in ua.lower()):
        raise SourceError(f"SECFilings needs your own User-Agent naming your organisation and a contact address or URL, for example 'Example Research ops@your-domain': "
                          f"pass user_agent= or set {ENV_USER_AGENT}. The SEC requires it; AsOf has no default and includes no address of its own.")
    return ua


class RateLimiter:
    """Spaces calls at least ``min_interval`` apart. The clock and sleep are injectable so the limit can be tested without waiting."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        if min_interval < MIN_ALLOWED_INTERVAL_S:
            raise SourceError(f"min_interval_s must be at least {MIN_ALLOWED_INTERVAL_S}: the SEC allows at most 10 requests per second")
        self.min_interval, self._clock, self._sleep = min_interval, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                self._sleep(gap)
                now = self._clock()
        self._last = now


class Cache:
    """A directory of fetched responses. The file name is the sha256 of the URL; the body's own sha256 is stored inside and checked on every read."""

    def __init__(self, directory: Path, ttl_s: float, now: Callable[[], float] = time.time) -> None:
        self.dir, self.ttl, self._now = Path(directory).expanduser(), ttl_s, now

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def get(self, url: str) -> bytes | None:
        try:
            d = json.loads(self._path(url).read_text())
            body = d["body"].encode("utf-8")
            if d["url"] != url or hashlib.sha256(body).hexdigest() != d["body_sha256"] or self._now() - float(d["fetched_at"]) > self.ttl:
                return None
            return body
        except (OSError, ValueError, KeyError, TypeError):
            return None                                                            # missing, unreadable, tampered or stale: fetch again

    def put(self, url: str, body: bytes) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            p = self._path(url)
            tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"url": url, "fetched_at": self._now(), "body_sha256": hashlib.sha256(body).hexdigest(), "body": body.decode("utf-8")}))
            os.replace(tmp, p)
        except (OSError, UnicodeDecodeError):
            pass                                                                    # a cache that cannot be written is only slower, never wrong


class SECFilings(Source):
    """``ciks``: company CIK numbers (``9999991`` or ``"0009999991"``; a made-up number, shown only for its shape). ``forms``: only these form types (for example ``("10-K", "10-Q", "8-K")``)."""

    def __init__(self, user_agent: str | None, ciks: Sequence[int | str], *, forms: Sequence[str] | None = None, name: str = "sec_filings",
                 acceptance_tz: str = "America/New_York", include_older: bool = True, fetch: Fetch | None = None, min_interval_s: float = 0.125,
                 cache_dir: str | Path | None = "~/.asof/cache/sec", cache_ttl_s: float = 86400.0, base_url: str = BASE,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.user_agent = check_user_agent(user_agent)
        if not ciks:
            raise SourceError("SECFilings needs at least one CIK")
        self.ciks = [str(int(str(c).strip())).zfill(10) for c in ciks]
        self.forms = tuple(forms) if forms else None
        self.name, self.acceptance_tz, self.include_older, self.base_url = name, acceptance_tz, include_older, base_url if base_url.endswith("/") else base_url + "/"
        self._fetch = fetch or _default_fetch
        self._limiter = RateLimiter(min_interval_s, clock, sleep)
        self._cache = Cache(Path(cache_dir), cache_ttl_s) if cache_dir else None
        self.requests_made = 0
        parse_source_time("2000-01-01T00:00:00", assume_tz=acceptance_tz, end_of_day=False)       # validates the zone now

    def _get(self, url: str) -> dict[str, Any]:
        raw = self._cache.get(url) if self._cache else None
        if raw is None:
            self._limiter.wait()
            raw = self._fetch(url, self.user_agent)
            self.requests_made += 1
            try:
                doc = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise SourceError(f"SEC EDGAR returned something that is not JSON for {url}") from e
            if self._cache:
                self._cache.put(url, raw)                                           # only a successfully parsed response is cached
            return doc
        return json.loads(raw)

    def records(self) -> Iterator[Record]:
        n = 0
        for cik in self.ciks:
            doc = self._get(f"{self.base_url}CIK{cik}.json")
            blocks = [doc.get("filings", {}).get("recent", {})]
            if self.include_older:
                for f in doc.get("filings", {}).get("files", []):
                    blocks.append(self._get(self.base_url + f["name"]))
            for block in blocks:
                for row in _columns_to_rows(block):
                    if self.forms and row.get("form") not in self.forms:
                        continue
                    yield self._record(cik, doc.get("name"), row, n)
                    n += 1

    def acceptance_to_utc(self, raw: Any) -> tuple[Any, str | None]:
        """EDGAR's ``acceptanceDateTime`` string to a UTC instant, by the rule in the module docstring. ``(None, problem)`` if it cannot be used."""
        if isinstance(raw, str) and self.acceptance_tz.upper() not in ("UTC", "Z"):
            raw = _UTC_SUFFIX.sub("", raw.strip())          # EDGAR labels the value 'Z'; the question is whether that label is right, so re-read the digits in acceptance_tz
        return parse_source_time(raw, assume_tz=self.acceptance_tz, end_of_day=False)

    def _record(self, cik: str, company: Any, row: dict[str, Any], n: int) -> Record:
        flags: list[str] = []
        av, problem = self.acceptance_to_utc(row.get("acceptanceDateTime"))
        if problem:
            flags.append(f"{problem}_available_at")
        ev, _ = parse_source_time(row.get("reportDate") or row.get("filingDate"), assume_tz="UTC", end_of_day=False)
        base_form = str(row.get("form", "")).split("/")[0]                     # 10-K/A shares an identity with 10-K for the same period
        period = row.get("reportDate") or row.get("filingDate") or row.get("accessionNumber")
        payload = {"cik": cik, "company": company, "form": row.get("form"), "accession_number": row.get("accessionNumber"),
                   "filing_date": row.get("filingDate"), "report_date": row.get("reportDate"), "acceptance_datetime": row.get("acceptanceDateTime"),
                   "primary_document": row.get("primaryDocument"), "is_amendment": "/" in str(row.get("form", ""))}
        if payload["accession_number"] and payload["primary_document"]:
            payload["url"] = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                              f"{str(payload['accession_number']).replace('-', '')}/{payload['primary_document']}")
        return Record(source_id=self.name, record_id=f"{cik}:{base_form}:{period}", event_time=ev, available_at=av, payload=payload,
                      flags=tuple(flags), order=n)


def _columns_to_rows(block: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """EDGAR returns columns (``{"form": [...], "filingDate": [...]}``); turn them into rows."""
    if not block:
        return
    cols = {k: v for k, v in block.items() if isinstance(v, list)}
    lengths = {len(v) for v in cols.values()}
    if len(lengths) > 1:
        raise SourceError("SEC EDGAR response has columns of different lengths")
    for i in range(lengths.pop() if lengths else 0):
        yield {k: v[i] for k, v in cols.items()}
