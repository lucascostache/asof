"""Presets: ready-made configurations of a :class:`~asof.session.Session`.

A preset is a plain, frozen config object. It states every rule it sets in ``rules`` (printed by ``describe()`` and reproduced in docs/REFERENCE.md), refuses
components that would break those rules, and builds a Session with ``build()``. Nothing here is new machinery: each rule is an existing Session or source
setting, fixed so it cannot be switched off by mistake.

Every preset, always: the clock only moves forward (R5); a record or tool item with no usable timestamp is withheld (R2); every call is a ledger event (R6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .clock import TimeLike, parse_time
from .errors import AsOfError
from .session import Session
from .sources.base import Source
from .sources.macro import MacroCalendar
from .sources.sec import SECFilings


class PresetError(AsOfError, ValueError):
    """A component or setting was given that would break a rule the preset exists to enforce."""


@dataclass(frozen=True)
class ToolDef:
    """A tool to register on the session: ``fn`` must return items carrying a timestamp in ``timestamp_field``."""
    name: str
    fn: Callable[..., Any]
    timestamp_field: str = "timestamp"
    items: str | None = None
    pass_as_of: bool = False
    assume_tz: str | None = None


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    rules: tuple[str, ...]                                   # every rule this preset sets, in plain words
    session: Mapping[str, Any]                               # fixed Session keyword arguments
    allow_sources: tuple[type, ...] | None = None            # None: any source; else only these source classes
    require_sources: tuple[type, ...] = ()                   # at least one source of each of these classes
    allow_tools: bool = True
    require_tools: int = 0
    max_as_of: str | None = None                             # a clock ceiling, if the preset sets one
    must_be_before: str | None = None                        # as_of must be strictly before this instant
    extra: Mapping[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [f"{self.name}: {self.description}", "Rules:"]
        lines += [f"  - {r}" for r in self.rules]
        return "\n".join(lines)

    def check(self, as_of: TimeLike, sources: Sequence[Source], tools: Sequence[ToolDef]) -> None:
        if self.must_be_before is not None and parse_time(as_of, what="as_of") >= parse_time(self.must_be_before):
            raise PresetError(f"{self.name}: as_of {parse_time(as_of).isoformat()} must be before {self.must_be_before}")
        for s in sources:
            if self.allow_sources is not None and not isinstance(s, self.allow_sources):
                raise PresetError(f"{self.name}: a {type(s).__name__} source is not allowed (allowed: {', '.join(c.__name__ for c in self.allow_sources) or 'none'}; "
                                  f"see the preset's rules)")
        for need in self.require_sources:
            if not any(isinstance(s, need) for s in sources):
                raise PresetError(f"{self.name}: needs a {need.__name__} source")
        if tools and not self.allow_tools:
            raise PresetError(f"{self.name}: tools are not allowed in this preset (it uses your own sources only)")
        if len(tools) < self.require_tools:
            raise PresetError(f"{self.name}: needs {self.require_tools} tool(s) (for example the news tool)")

    def build(self, as_of: TimeLike, *, sources: Sequence[Source] = (), tools: Sequence[ToolDef] = (), ledger_path: str | Path = "~/.asof/ledger.db",
              privacy: str = "full", **overrides: Any) -> Session:
        """Build the Session. ``privacy`` is the only session setting you may choose; asking to change any rule the preset fixes raises :class:`PresetError`."""
        if overrides:
            raise PresetError(f"{self.name}: these settings are fixed by the preset and cannot be changed: {sorted(overrides)}")
        self.check(as_of, sources, tools)
        s = Session(as_of, privacy=privacy, ledger_path=ledger_path, max_as_of=self.max_as_of, **self.session)
        try:
            for src in sources:
                s.add_source(src)
            for t in tools:
                s.add_tool(t.name, t.fn, timestamp_field=t.timestamp_field, items=t.items, pass_as_of=t.pass_as_of, assume_tz=t.assume_tz)
        except Exception:
            s.close()
            raise
        return s


def time_locked_research() -> Preset:
    """SEC filings plus your own sources, with news (or any search tool) routed through the time-locked proxy."""
    return Preset(
        name="time_locked_research",
        description="Research with SEC filings, your own data, and a proxied news tool, all locked to one time.",
        rules=("allow_untimestamped is False and cannot be changed: anything without a usable timestamp is withheld.",
               "The clock only moves forward.",
               "Sources: an SECFilings source is required; your own sources (CSV, JSONL, Parquet, SQL, MacroCalendar) are allowed alongside it.",
               "At least one tool (the news tool) is required, and it is reached only through the time-locked proxy: items dated after the clock are dropped.",
               "privacy is yours to choose ('full' or 'hashes_only'); credentials are never recorded in either mode."),
        session={"allow_untimestamped": False}, require_sources=(SECFilings,), require_tools=1)


def backtest_safe_agent() -> Preset:
    """Your own historical data only, a clock that can only advance, and a decision logged before every step."""
    return Preset(
        name="backtest_safe_agent",
        description="A backtesting agent that can use only your own sources and must record a decision before time moves on.",
        rules=("allow_untimestamped is False and cannot be changed.",
               "The clock only moves forward.",
               "Sources: your own local sources only (CSV, JSONL, Parquet, SQL, MacroCalendar). SECFilings, which uses the network, is refused.",
               "Tools: none. Nothing outside your sources can reach the agent through this preset.",
               "decision_gate is on and cannot be changed: after any query, tool or model call the clock refuses to advance until the agent has logged a decision "
               "with Session.decision(); every decision is a ledger event stamped with the clock time.",
               "privacy is yours to choose."),
        session={"allow_untimestamped": False, "decision_gate": True},
        allow_sources=_local_source_types(), allow_tools=False)


def _local_source_types() -> tuple[type, ...]:
    from .sources.files import CSV, JSONL, Parquet
    from .sources.sql import SQL
    return (CSV, JSONL, Parquet, SQL, MacroCalendar)


def earnings_analysis(earnings_date: str | datetime) -> Preset:
    """SEC filings and a macro calendar, with the clock held strictly before an earnings date."""
    when = parse_time(earnings_date, what="earnings_date")
    ceiling = (when - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    return Preset(
        name="earnings_analysis",
        description=f"Analyse a company before its earnings on {when.isoformat()}: SEC filings and the macro calendar only, never at or after that instant.",
        rules=("allow_untimestamped is False and cannot be changed.",
               "The clock only moves forward.",
               f"as_of must be strictly before the earnings date ({when.isoformat()}), and the clock has a ceiling one microsecond before it: it can never reach it.",
               "Sources: an SECFilings source and a MacroCalendar source are both required; other sources are allowed.",
               "Filings and macro releases appear only once their acceptance or release time has passed (inclusive of that instant).",
               "privacy is yours to choose."),
        session={"allow_untimestamped": False}, require_sources=(SECFilings, MacroCalendar), max_as_of=ceiling, must_be_before=when.isoformat().replace("+00:00", "Z"))


PRESETS: dict[str, Callable[..., Preset]] = {"time_locked_research": time_locked_research, "backtest_safe_agent": backtest_safe_agent,
                                             "earnings_analysis": earnings_analysis}
