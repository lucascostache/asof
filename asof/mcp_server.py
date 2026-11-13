"""An MCP server that puts an AsOf :class:`~asof.session.Session` behind a handful of tools.

It needs the ``mcp`` package: install AsOf with its ``[mcp]`` extra from the repository (see the README), or add ``pip install 'mcp>=2,<3'`` to an existing install.
(Do not ``pip install asof`` from PyPI: that name there belongs to an unrelated project.) Run: ``strathora serve-mcp --config asof.config.json`` (stdio).

**The clock belongs to the server.** The calling model never holds the Session and cannot pass a time to ``query``, ``tool_call`` or ``model_call``;
it can only move the clock through ``set_clock`` / ``advance_clock``, and then only if the operator allows it, only forwards, and never past the
operator's ``max_as_of`` ceiling. Every change, and every refused attempt, is a ledger event.

**Nothing is re-implemented here.** Every tool calls the same Session methods as the Python API, so R1 to R8 are enforced by the same code.

Tools: ``set_clock``, ``advance_clock``, ``query``, ``tool_call``, ``model_call`` (off unless enabled), ``get_receipt``, ``verify_ledger``, ``ledger_head``.

``get_receipt`` returns the *agent view* by default (no blocked counts: a count would tell the model that something exists after T). The operator
reads the full receipt with ``strathora receipt <id>``.

Config file (JSON; relative paths resolve against the file's directory; unknown keys are errors)::

    {"as_of": "2023-01-21T14:00:00Z", "max_as_of": "2023-03-01T00:00:00Z", "allow_model_clock": false, "privacy": "full",
     "ledger_path": "ledger.db", "allow_untimestamped": false, "receipt_detail": "agent", "enable_model_call": false,
     "sources": [{"type": "csv", "path": "prices.csv", "event_time": "date", "available_at": "available_at", "key": ["symbol", "date"], "assume_tz": "UTC"}],
     "tools": [{"name": "news_search", "callable": "mypackage.news:search", "timestamp_field": "published_at", "pass_as_of": true}],
     "providers": [{"name": "mine", "callable": "mypackage.llm:call"}]}

``callable`` entries import and run code you name in the config: the config file is as trusted as the code you run.
"""
from __future__ import annotations

import argparse
import importlib
import json
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as McpToolError
except ImportError as e:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("the MCP server needs the 'mcp' package, which is not installed in this Python. Add it with:  pip install 'mcp>=2,<3'  "
                      "(the name 'asof' on PyPI is an unrelated project: do not install AsOf from there)") from e

from . import __version__, sources as src
from .clock import parse_duration
from .errors import AsOfError
from .gateway import RESERVED
from .ledger import iso
from .receipt import receipt_from_ledger
from .redaction import scrub
from .session import Session

MAX_ROWS = 500


@dataclass(frozen=True)
class ServerPolicy:
    """What the operator allows the calling model to do."""
    allow_model_clock: bool = False          # may the model move the clock at all (forwards only, never past the ceiling)
    receipt_detail: str = "agent"            # "agent" (no blocked counts) or "full"
    enable_model_call: bool = False          # expose model_call
    max_rows: int = MAX_ROWS                 # most records one query may return
    allowed_models: tuple[str, ...] = ()     # what model_call may name: exact ids ("anthropic:claude-x") or a whole provider ("local:*"). Nothing built in is reachable unless it is listed here
    operator_providers: tuple[str, ...] = () # providers the operator registered in the config: always allowed

    def __post_init__(self) -> None:
        if self.receipt_detail not in ("agent", "full"):
            raise ValueError("receipt_detail must be 'agent' or 'full'")
        if isinstance(self.allowed_models, str) or not all(isinstance(m, str) and ":" in m for m in self.allowed_models):
            raise ValueError("allowed_models must be a list of 'provider:name' strings (or 'provider:*' for a whole provider)")

    def model_allowed(self, model: Any) -> bool:
        if not isinstance(model, str):
            return False
        provider, sep, name = model.partition(":")
        if not sep or not provider or not name:
            return True                      # not a model id at all: the gateway says so in its own words (nothing can be sent)
        return provider in self.operator_providers or model in self.allowed_models or f"{provider}:*" in self.allowed_models


def _fail(message: str) -> McpToolError:
    return McpToolError(scrub(message))                  # whatever is told to the calling model has credentials removed first


def _hidden(what: str, e: BaseException) -> McpToolError:
    """An exception AsOf did not raise itself (the operator's own tool or provider failed). Its text is not passed on to the calling model: it may hold a credential, or content
    from after the clock. The model is told that it failed; the operator finds the (cleaned) message in the ledger."""
    return McpToolError(f"{what} failed ({type(e).__name__}). The details are recorded in the operator's ledger.")


def _record_view(r: Any) -> dict[str, Any]:
    return {"record_id": r.record_id, "event_time": iso(r.event_time), "available_at": iso(r.available_at),
            "timing": "unknown" if r.available_at is None else "declared", "payload": dict(r.payload)}


def build_server(session: Session, policy: ServerPolicy | None = None, *, name: str = "asof") -> MCPServer:
    """Wrap ``session`` in an MCP server. The returned server owns the session's clock: nothing else should advance it."""
    policy = policy or ServerPolicy()
    server = MCPServer(name, instructions=(
        "AsOf serves data as it was knowable at a fixed time. You cannot choose the time for a query. "
        + ("You may move the clock forward only." if policy.allow_model_clock else "The clock is fixed by the operator."))
    )

    def guard_clock() -> None:
        if not policy.allow_model_clock:
            try:
                session.refuse_clock_move("the operator has not allowed the model to move the clock")
            except AsOfError:
                raise _fail("the clock is owned by the operator: this server does not allow the model to move it") from None

    def clock_result() -> dict[str, Any]:
        return {"as_of": iso(session.as_of), "ceiling": iso(session.clock.ceiling)}

    @server.tool()
    def set_clock(as_of: str) -> dict[str, Any]:
        """Move the clock to an absolute time (ISO-8601 with a time zone). Forward-only: an earlier time is refused. Refused if the operator has not allowed it."""
        guard_clock()
        try:
            before = session.as_of
            session.advance_to(as_of)
        except (AsOfError, ValueError) as e:
            raise _fail(str(e)) from None
        return {**clock_result(), "changed": session.as_of != before}

    @server.tool()
    def advance_clock(to: str | None = None, by: str | None = None) -> dict[str, Any]:
        """Move the clock forward, to an absolute time (``to``) or by a duration such as ``"1d"`` or ``"6h"`` (``by``). Give exactly one. Refused if the operator
        has not allowed it, if it would go backwards, or if it would pass the ceiling."""
        guard_clock()
        if (to is None) == (by is None):
            raise _fail("give exactly one of 'to' or 'by'")
        try:
            before = session.as_of
            session.advance_to(to if to is not None else before + parse_duration(by))      # type: ignore[arg-type]
        except OverflowError:
            raise _fail(f"the clock cannot be moved by {by!r}: that is past the last date there is") from None
        except (AsOfError, ValueError) as e:
            raise _fail(str(e)) from None
        return {**clock_result(), "changed": session.as_of != before}

    @server.tool()
    def query(source: str, filters: dict[str, Any] | None = None, lookback: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """Read records from a source as they were knowable at the server's clock. ``filters`` are equality filters on the payload (a list means any-of);
        ``lookback`` is a window such as "30d" before the clock, applied to each record's event_time. Returns the records and a receipt id."""
        try:
            cap = policy.max_rows if limit is None else min(limit, policy.max_rows)
            rows = session.query(source, filters=filters, lookback=lookback, limit=cap)
        except (AsOfError, ValueError) as e:
            raise _fail(str(e)) from None
        return {"as_of": iso(session.as_of), "served": len(rows), "records": [_record_view(r) for r in rows], "receipt_id": session.receipt().call_id}

    @server.tool()
    def tool_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call one of the registered tools through the time lock: items dated after the server's clock are dropped before you see them."""
        try:
            items = session.tool(name, **(arguments or {}))
        except (AsOfError, ValueError, TypeError) as e:
            raise _fail(str(e)) from None
        except Exception as e:  # noqa: BLE001 - the operator's tool raised something of its own
            raise _hidden(f"the tool {str(name)[:80]!r}", e) from None
        return {"as_of": iso(session.as_of), "served": len(items), "items": items, "receipt_id": session.receipt().call_id}

    if policy.enable_model_call:
        @server.tool()
        def model_call(model: str, messages: list[dict[str, Any]], params: dict[str, Any] | None = None) -> dict[str, Any]:
            """Call a model through the gateway ("provider:name"). Credentials, endpoints and the list of models are the operator's: you cannot set api_key, base_url or timeout,
            and only the models the operator listed can be called."""
            bad = sorted(set(params or {}) & set(RESERVED))
            try:
                if bad:                                                  # refused before anything is sent, and the attempt is a ledger event (names only, never the values)
                    session.refuse_model_call(model, messages, "operator_parameter", f"these parameters are controlled by the operator and cannot be set by the model: {bad}")
                if not policy.model_allowed(model):
                    session.refuse_model_call(model, messages, "model_not_allowed", f"model {str(model)[:200]!r} is not allowed on this server: only the models and providers the operator listed can be called")
            except AsOfError as e:
                raise _fail(str(e)) from None
            try:
                resp = session.model(model, messages=messages, **(params or {}))
            except (AsOfError, ValueError) as e:
                raise _fail(str(e)) from None
            except Exception as e:  # noqa: BLE001 - the operator's own provider raised something of its own
                raise _hidden("the model call", e) from None
            return {"as_of": iso(session.as_of), "text": resp.text, "finish_reason": resp.finish_reason, "receipt_id": session.receipt().call_id}

    @server.tool()
    def get_receipt(receipt_id: str) -> dict[str, Any]:
        """The receipt of an earlier call, rebuilt from the ledger. By default this is the agent view: it does not include blocked counts or the call's position in the ledger."""
        try:
            r = receipt_from_ledger(session.ledger, receipt_id)
        except AsOfError:
            raise _fail(f"no receipt {receipt_id!r} in this session") from None
        if r.session_id != session.id:
            raise _fail(f"no receipt {receipt_id!r} in this session")
        return r.to_dict() if policy.receipt_detail == "full" else r.agent_view()

    @server.tool()
    def verify_ledger() -> dict[str, Any]:
        """Recompute the ledger's hash chain and check its tail anchor. Reports whether it is intact, and the hash of its newest event."""
        v = session.verify_ledger()
        if policy.receipt_detail == "full":                              # the operator's view: how many events, and where and why it fails
            return {"ok": v.ok, "n_events": v.n_events, "anchor": v.anchor, "reason": v.reason, "warnings": list(v.warnings), "head_hash": v.head_hash}
        # The agent's view has no event count and no event number: the ledger grows by one more event when a call withheld something, so a count would tell the agent that later data exists.
        return {"ok": v.ok, "anchor": v.anchor, "head_hash": v.head_hash,
                "reason": None if v.ok else "the ledger does not verify; the operator can run `strathora verify` to see where and why"}

    @server.tool()
    def ledger_head() -> dict[str, Any]:
        """The hash of the newest ledger event and when it was recorded. Stored somewhere trusted, it lets a later cut of the ledger be detected."""
        h = session.ledger_head()
        return h if policy.receipt_detail == "full" else {"hash": h.get("hash"), "recorded_at": h.get("recorded_at")}      # no sequence number, for the same reason as above

    return server


# ------------------------------------------------------------------ config -> session
_TOP = {"as_of", "max_as_of", "allow_model_clock", "privacy", "ledger_path", "allow_untimestamped", "receipt_detail", "enable_model_call", "sources", "tools",
        "providers", "max_rows", "decision_gate", "allowed_models", "max_listed_blocked"}
_SOURCE_COMMON = {"type", "name", "event_time", "available_at", "record_id", "key", "assume_tz", "date_only_available_at", "version", "recorded_at", "calendar"}


def _import_callable(spec: str) -> Any:
    mod, sep, attr = spec.partition(":")
    if not sep or not mod or not attr:
        raise ValueError(f"callable must look like 'package.module:function', not {spec!r}")
    return getattr(importlib.import_module(mod), attr)


def _check_keys(d: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(d) - allowed)
    if extra:
        raise ValueError(f"unknown key(s) {extra} in {where}; allowed: {sorted(allowed)}")


def build_source(cfg: dict[str, Any], base: Path) -> Any:
    kind = cfg.get("type")

    def path(key: str) -> Path:
        p = Path(cfg[key]).expanduser()
        return p if p.is_absolute() else base / p

    common: dict[str, Any] = {k: cfg[k] for k in ("name", "record_id", "assume_tz", "date_only_available_at", "version", "recorded_at", "calendar") if k in cfg}
    if "key" in cfg:
        common["key"] = tuple(cfg["key"])
    times: dict[str, Any] = {"event_time": cfg.get("event_time"), "available_at": cfg.get("available_at")}
    if kind in ("csv", "jsonl", "parquet"):
        _check_keys(cfg, _SOURCE_COMMON | {"path"} | ({"coerce", "delimiter"} if kind == "csv" else set()), f"source {cfg.get('name', kind)}")
        cls: Any = {"csv": src.CSV, "jsonl": src.JSONL, "parquet": src.Parquet}[kind]
        extra = {k: cfg[k] for k in ("coerce", "delimiter") if k in cfg}
        return cls(path("path"), **times, **common, **extra)
    if kind == "sql":
        _check_keys(cfg, _SOURCE_COMMON | {"database", "query", "params"}, f"source {cfg.get('name', kind)}")
        if "name" not in cfg:
            raise ValueError("a sql source needs a 'name'")
        rest = {k: v for k, v in common.items() if k != "name"}
        return src.SQL(path("database"), cfg["query"], **times, name=cfg["name"], params=cfg.get("params", ()), **rest)
    if kind == "macro":
        _check_keys(cfg, {"type", "name", "path", "release_tz", "fmt"}, "a macro source")
        return src.MacroCalendar(path("path"), **{k: cfg[k] for k in ("name", "release_tz", "fmt") if k in cfg})
    if kind == "sec":
        _check_keys(cfg, {"type", "name", "user_agent", "user_agent_env", "ciks", "forms", "acceptance_tz", "cache_dir", "include_older"}, "an sec source")
        import os
        ua = cfg.get("user_agent") or os.environ.get(cfg.get("user_agent_env", "ASOF_SEC_USER_AGENT"), "")
        kw: dict[str, Any] = {k: cfg[k] for k in ("name", "forms", "acceptance_tz", "include_older") if k in cfg}
        if "cache_dir" in cfg:
            kw["cache_dir"] = path("cache_dir")
        return src.SECFilings(ua, cfg["ciks"], **kw)
    raise ValueError(f"unknown source type {kind!r}; use csv, jsonl, parquet, sql, macro or sec")


def session_from_config(cfg: dict[str, Any], base: Path | None = None) -> tuple[Session, ServerPolicy]:
    """Build the Session and policy a config describes."""
    base = base or Path.cwd()
    _check_keys(cfg, _TOP, "the config")
    if "as_of" not in cfg:
        raise ValueError("the config needs an 'as_of' (the clock's starting time, with a time zone)")
    allowed = cfg.get("allowed_models", [])
    if not isinstance(allowed, list):
        raise ValueError("allowed_models must be a list of 'provider:name' strings (or 'provider:*' for a whole provider)")
    policy = ServerPolicy(allow_model_clock=bool(cfg.get("allow_model_clock", False)), receipt_detail=cfg.get("receipt_detail", "agent"),      # built first: a bad policy stops before a session is opened
                          enable_model_call=bool(cfg.get("enable_model_call", False)), max_rows=int(cfg.get("max_rows", MAX_ROWS)), allowed_models=tuple(allowed),
                          operator_providers=tuple(str(pc["name"]) for pc in cfg.get("providers", [])))
    ledger = Path(cfg.get("ledger_path", "~/.asof/ledger.db")).expanduser()
    ledger = ledger if ledger.is_absolute() else base / ledger
    s = Session(cfg["as_of"], privacy=cfg.get("privacy", "full"), allow_untimestamped=bool(cfg.get("allow_untimestamped", False)), ledger_path=ledger,
                max_as_of=cfg.get("max_as_of"), decision_gate=bool(cfg.get("decision_gate", False)),
                **({"max_listed_blocked": cfg["max_listed_blocked"]} if "max_listed_blocked" in cfg else {}))
    for sc in cfg.get("sources", []):
        s.add_source(build_source(dict(sc), base))
    for tc in cfg.get("tools", []):
        tc = dict(tc)
        _check_keys(tc, {"name", "callable", "timestamp_field", "items", "pass_as_of", "assume_tz"}, f"tool {tc.get('name')}")
        s.add_tool(tc["name"], _import_callable(tc["callable"]), **{k: tc[k] for k in ("timestamp_field", "items", "pass_as_of", "assume_tz") if k in tc})
    for pc in cfg.get("providers", []):
        s.register_provider(pc["name"], _import_callable(pc["callable"]))
    return s, policy


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    p = Path(path).expanduser().resolve()
    try:
        return json.loads(p.read_text()), p.parent
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read config {p}: {e}") from e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="strathora serve-mcp", description="Serve an AsOf session over MCP (stdio).")
    ap.add_argument("--config", required=True, help="path to the JSON config file")
    ap.add_argument("--as-of", help="override the config's starting clock")
    ap.add_argument("--max-as-of", help="override the config's clock ceiling")
    ap.add_argument("--ledger", help="override the config's ledger path")
    ap.add_argument("--allow-model-clock", action="store_true", help="let the model move the clock forward (never past the ceiling)")
    a = ap.parse_args(argv)
    # Clients end the server in different ways: closing its input, SIGTERM, Ctrl-C (SIGINT), or closing the terminal (SIGHUP). Each must leave through the close below, so the
    # session gets its `session_end`. The handlers are installed before anything is set up, and raise only while the server is serving: a stop that arrives during setup
    # (found on CI: inside pydantic, building the server) is remembered and honoured where serving would begin, instead of tearing through library code. They raise at most
    # once, so a second signal cannot interrupt the close.
    state = {"phase": "starting", "stop": False}

    def _stop(signum: int, frame: Any) -> None:
        state["stop"] = True
        if state["phase"] == "serving":
            state["phase"] = "closing"
            raise SystemExit(0)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        try:
            signal.signal(getattr(signal, name), _stop)
        except (ValueError, OSError, AttributeError):        # not the main thread, or a platform without this signal: the server still works, it just cannot record this kind of stop
            pass
    try:
        cfg, base = load_config(a.config)
        for key, val in (("as_of", a.as_of), ("max_as_of", a.max_as_of), ("ledger_path", a.ledger)):
            if val:
                cfg[key] = val
        if a.allow_model_clock:
            cfg["allow_model_clock"] = True
        session, policy = session_from_config(cfg, base)
    except (ValueError, AsOfError, KeyError) as e:
        print(f"strathora serve-mcp: {e}", file=sys.stderr)
        return 2
    print(f"asof {__version__}: serving session {session.id}, clock {iso(session.as_of)}, ceiling {iso(session.clock.ceiling) or 'none'}, "
          f"model may move clock: {policy.allow_model_clock}", file=sys.stderr)                      # stderr only: stdout is the protocol channel
    try:
        try:
            server = build_server(session, policy)
            state["phase"] = "serving"
            if not state["stop"]:                            # a stop that came during setup: do not start serving
                server.run("stdio")
        except KeyboardInterrupt:                            # Ctrl-C where the handler could not be installed
            pass
        finally:
            state["phase"] = "closing"
    except SystemExit:                                       # the handler's one SystemExit, wherever it landed above
        pass
    finally:
        session.close()                                      # writes session_end. A hard kill (SIGKILL, power loss) cannot be recorded: the chain still verifies, the session simply has no end event
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
