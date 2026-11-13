"""Optional ledger sync: push events to an HTTPS endpoint you configure. **Off by default; nothing is sent unless you turn it on.**

For trusted TLS roots on Pythons that ship none, add ``certifi`` (``pip install 'certifi>=2023.7.22'``, or AsOf's ``[server]`` extra); sync itself uses only the standard library.

Opt in explicitly, either in code (``SyncConfig(enabled=True, endpoint=..., api_key=...)``) or with ``ASOF_SYNC_ENABLED=1``, ``ASOF_SYNC_ENDPOINT`` and
``ASOF_SYNC_API_KEY`` (or a config file ``~/.asof/sync.json``: ``{"enabled": true, "endpoint": "...", "api_key_env": "NAME_OF_ENV_VAR"}``; the key itself is
never read from a file). The API key is sent as ``Authorization: Bearer`` and nowhere else: not in the ledger, a log line, an error message or the body.

What is sent (schema ``asof.sync/2``, specified in SPEC.md section 7)
-----------------------------------------------------------
Every event always goes as an *envelope*: ``seq, id, session_id, type, call_id, event_time, available_at, recorded_at, payload_sha256, prev_hash, hash``. That
is enough for the receiver to verify the whole hash chain without seeing a payload. The ``payload`` itself is added only when:

* the event's session ran in ``hashes_only`` mode (its payload was reduced to hashes and metadata when it was written, so it holds no raw prompt, response,
  system prompt, model parameter value, tool argument, filter, record id or record), or
* the session ran in ``full`` mode **and** you also set ``send_payloads=True`` (a second, separate opt-in).

So with ``hashes_only`` sessions nothing raw leaves the machine, whatever the sync settings.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import __version__
from .errors import AsOfError
from .ledger import Ledger, LedgerEvent, redact
from .redaction import scrub

log = logging.getLogger("asof.sync")
SCHEMA = "asof.sync/2"
CANONICALIZATION = {2: "asof-json-v1", 3: "rfc8785"}      # named in the body so a receiver knows how to re-canonicalise a payload
ENV_PREFIX = "ASOF_SYNC_"
_TRUE = ("1", "true", "yes", "on")


class SyncError(AsOfError):
    """A sync request failed. The message never contains the API key."""


class SyncDisabledError(SyncError):
    """Sync was asked to run but has not been enabled."""


@dataclass(frozen=True)
class SyncConfig:
    endpoint: str = ""
    api_key: str = field(default="", repr=False)              # never shown in repr, never logged
    enabled: bool = False
    send_payloads: bool = False                                # also send payloads of ``full``-mode sessions
    batch_size: int = 100
    timeout: float = 30.0
    retries: int = 2
    ca_file: str | None = None                                 # extra trusted CA (a private endpoint)
    allow_insecure_localhost: bool = False                     # http:// is accepted only for localhost, and only with this flag (testing)
    client_id: str = ""                                        # optional: the id Strathora gave this credential's owner; the receiver refuses the batch if it is not the key's owner
    chain_label: str = ""                                      # optional: a name for this ledger (a machine, a strategy) shown next to its chain id

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, config_file: str | Path | None = "~/.asof/sync.json") -> "SyncConfig":
        """Load from the optional config file, then the environment (the environment wins). With neither, sync is disabled."""
        env = dict(os.environ if environ is None else environ)
        data: dict[str, Any] = {}
        if config_file:
            p = Path(config_file).expanduser()
            if p.exists():
                try:
                    data = json.loads(p.read_text())
                except (OSError, ValueError) as e:
                    raise SyncError(f"cannot read sync config {p}: {e}") from None
                if "api_key" in data:
                    raise SyncError(f"{p}: do not put the API key in a file; use \"api_key_env\": \"NAME_OF_ENV_VAR\"")
        key_env = data.get("api_key_env", ENV_PREFIX + "API_KEY")
        def get(name: str, default: Any = None) -> Any:
            return env.get(ENV_PREFIX + name.upper(), data.get(name, default))

        return cls(endpoint=str(get("endpoint", "") or ""), api_key=env.get(key_env, ""), enabled=str(get("enabled", False)).lower() in _TRUE,
                   send_payloads=str(get("send_payloads", False)).lower() in _TRUE, batch_size=int(get("batch_size", 100)), timeout=float(get("timeout", 30.0)),
                   ca_file=get("ca_file"), allow_insecure_localhost=str(get("allow_insecure_localhost", False)).lower() in _TRUE,
                   client_id=str(get("client_id", "") or ""), chain_label=str(get("chain_label", "") or ""))

    def validate(self) -> None:
        if not self.enabled:
            raise SyncDisabledError("sync is not enabled (it is off by default): set enabled=True, or ASOF_SYNC_ENABLED=1")
        if not self.endpoint or not self.api_key:
            raise SyncError("sync needs an endpoint and an API key (ASOF_SYNC_ENDPOINT, ASOF_SYNC_API_KEY or SyncConfig)")
        u = urlsplit(self.endpoint)
        local = (u.hostname or "") in ("localhost", "127.0.0.1", "::1")
        if u.scheme != "https" and not (u.scheme == "http" and local and self.allow_insecure_localhost):
            raise SyncError("the sync endpoint must be https:// (plain http is accepted only for localhost with allow_insecure_localhost=True)")
        if self.batch_size < 1 or self.batch_size > 1000:
            raise SyncError("batch_size must be between 1 and 1000")


@dataclass(frozen=True)
class SyncResult:
    sent: int                      # events sent in this run
    acked_through: int             # the highest seq the receiver has acknowledged (cumulative)
    batches: int
    payloads_included: int         # how many of the events sent carried a payload


def _safe(text: str, cfg: SyncConfig) -> str:
    return scrub(text, extra=(cfg.api_key,) if cfg.api_key else ())            # the key in whatever form the receiver repeated it


class Syncer:
    """Pushes the events after the last acknowledged one. The cursor lives beside the ledger (``sync_cursor.json``) and is per endpoint."""

    def __init__(self, ledger: Ledger, config: SyncConfig, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ledger, self.cfg, self._sleep = ledger, config, sleep
        self.cursor_path = ledger.path.with_name(ledger.path.stem + ".sync_cursor.json")
        self._privacy: dict[str, str] = {}

    # ---------------------------------------------------------------- cursor
    def _key(self) -> str:
        return hashlib.sha256(self.cfg.endpoint.encode()).hexdigest()[:16]

    def acked(self) -> int:
        try:
            return int(json.loads(self.cursor_path.read_text()).get(self._key(), 0))
        except (OSError, ValueError, TypeError):
            return 0

    def _set_acked(self, seq: int) -> None:
        try:
            d = json.loads(self.cursor_path.read_text())
        except (OSError, ValueError):
            d = {}
        d[self._key()] = seq
        tmp = self.cursor_path.with_name(self.cursor_path.name + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.cursor_path)

    # ---------------------------------------------------------------- what to send
    def _session_privacy(self, session_id: str) -> str:
        if session_id not in self._privacy:
            start = self.ledger.events(session_id=session_id, type="session_start")
            self._privacy[session_id] = start[0].data().get("privacy", "full") if start else "full"        # unknown: treat as full, i.e. withhold the payload
        return self._privacy[session_id]

    def wire_event(self, e: LedgerEvent) -> dict[str, Any]:
        """One event as sent. The envelope always; the payload only under the rules in the module docstring."""
        privacy = self._session_privacy(e.session_id)
        out: dict[str, Any] = {"seq": e.seq, "id": e.id, "session_id": e.session_id, "type": e.type, "call_id": e.call_id, "event_time": e.event_time,
                               "available_at": e.available_at, "recorded_at": e.recorded_at, "payload_sha256": e.payload_sha256, "prev_hash": e.prev_hash,
                               "hash": e.hash, "privacy": privacy}
        if privacy == "hashes_only" or self.cfg.send_payloads:
            out["payload"] = redact(e.data())                                                  # redacted again on the way out: credentials never leave
        return out

    def _pending(self) -> list[LedgerEvent]:
        after = self.acked()
        return [e for e in self.ledger.events() if e.seq > after]

    # ---------------------------------------------------------------- network
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, sort_keys=True).encode()
        req = urllib.request.Request(self.cfg.endpoint, data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.api_key}", "User-Agent": f"asof/{__version__}", "X-AsOf-Schema": SCHEMA})
        ctx = None
        if self.cfg.endpoint.startswith("https"):
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=self.cfg.ca_file or certifi.where())
            except ImportError:
                ctx = ssl.create_default_context(cafile=self.cfg.ca_file)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            retry = e.code >= 500 or e.code == 429
            raise SyncError(f"{'retryable ' if retry else ''}HTTP {e.code} from the sync endpoint: {_safe(e.read().decode('utf-8', 'replace')[:300], self.cfg)}") from None
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
            raise SyncError(f"retryable: could not reach the sync endpoint: {_safe(str(getattr(e, 'reason', e)), self.cfg)}") from None
        except (ValueError, UnicodeDecodeError):
            raise SyncError("the sync endpoint returned something that is not JSON") from None

    def push(self) -> SyncResult:
        """Send every event after the last acknowledged one, in batches. Raises :class:`SyncDisabledError` unless sync was enabled."""
        self.cfg.validate()
        pending = self._pending()
        sent = batches = included = 0
        acked = self.acked()
        for i in range(0, len(pending), self.cfg.batch_size):
            chunk = pending[i:i + self.cfg.batch_size]
            events = [self.wire_event(e) for e in chunk]
            client: dict[str, Any] = {"name": "asof", "version": __version__}
            if self.cfg.client_id:
                client["id"] = self.cfg.client_id
            chain: dict[str, Any] = {"id": self.ledger.chain_id}
            if self.cfg.chain_label:
                chain["label"] = self.cfg.chain_label
            body = {"schema": SCHEMA, "client": client, "chain": chain,
                    "ledger": {"schema_version": self.ledger.schema_version, "hash_domain": self.ledger.hash_domain, "canonicalization": CANONICALIZATION[self.ledger.schema_version]},
                    "events": events}
            for attempt in range(self.cfg.retries + 1):
                try:
                    reply = self._post(body)
                    break
                except SyncError as e:
                    if "retryable" not in str(e) or attempt == self.cfg.retries:
                        raise
                    log.warning("sync attempt %d failed (%s); retrying", attempt + 1, e)
                    self._sleep(min(2 ** attempt, 8))
            through = reply.get("accepted_through")
            if not isinstance(through, int) or through < acked:
                raise SyncError("the sync endpoint's reply must be JSON with an integer 'accepted_through' (the highest seq it stored)")
            acked = max(acked, through)
            self._set_acked(acked)
            sent += len(events)
            batches += 1
            included += sum("payload" in ev for ev in events)
            if through < chunk[-1].seq:                                                         # a partial acceptance: stop here and report it
                break
        log.info("sync: sent %d event(s) in %d batch(es); acknowledged through %d", sent, batches, acked)
        return SyncResult(sent, acked, batches, included)
