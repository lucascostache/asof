"""The tool proxy (rule R4).

Register tools; call them only through the session. Every item a tool returns must carry a timestamp: items dated after T are dropped (and
logged as ``blocked_future``); undated items are withheld unless the session allows untimestamped data, and are then labelled unknown (R2).

How results are read:

* a list of dicts, or a dict whose ``items=`` key holds a list, is a list of items; a single dict that has the timestamp field is one item;
* anything else (a string, a number, a dict with no timestamp field) is one **undated** item.

Limits, stated plainly: AsOf checks the timestamp field you name. It does not read article text, so a dated-before-T article that *mentions*
later events still passes; and a tool that mislabels its own dates is believed. Choose tools whose timestamps you trust, and use
``pass_as_of=True`` so a cooperating tool can also filter at the source.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .errors import ToolError
from .records import payload_sha256
from .sources.base import parse_source_time


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    timestamp_field: str = "timestamp"
    items: str | None = None
    pass_as_of: bool = False
    assume_tz: str | None = None
    date_only_end_of_day: bool = True


@dataclass(frozen=True)
class ToolOutcome:
    served: tuple[Any, ...]
    served_times: tuple[datetime | None, ...]                  # each served item's declared timestamp (None = unknown), parallel to ``served``
    unknown: tuple[Any, ...]                                   # undated items served because the session allowed it (subset of ``served``)
    blocked_future: tuple[str, ...]                            # payload hashes
    blocked_untimestamped: tuple[str, ...]
    n_items: int


class ToolProxy:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, fn: Callable[..., Any], *, timestamp_field: str = "timestamp", items: str | None = None, pass_as_of: bool = False,
                 assume_tz: str | None = None, date_only_end_of_day: bool = True) -> ToolSpec:
        if not callable(fn):
            raise ToolError(f"tool {name!r} must be callable")
        if not name or not isinstance(name, str):
            raise ToolError("a tool needs a name")
        if pass_as_of:
            params = inspect.signature(fn).parameters
            if "as_of" not in params and not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                raise ToolError(f"tool {name!r}: pass_as_of=True needs the function to accept an 'as_of' argument")
        spec = ToolSpec(name, fn, timestamp_field, items, pass_as_of, assume_tz, date_only_end_of_day)
        self._tools[name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolError(f"unknown tool {name!r}; registered tools are {sorted(self._tools)}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, as_of: datetime, allow_untimestamped: bool, kwargs: dict[str, Any]) -> ToolOutcome:
        spec = self.get(name)
        args = dict(kwargs)
        if spec.pass_as_of:
            args["as_of"] = as_of
        result = spec.fn(**args)
        return self.filter(spec, result, as_of, allow_untimestamped)

    def filter(self, spec: ToolSpec, result: Any, as_of: datetime, allow_untimestamped: bool) -> ToolOutcome:
        items = self._items(spec, result)
        served: list[Any] = []
        served_times: list[datetime | None] = []
        unknown: list[Any] = []
        future: list[str] = []
        untimed: list[str] = []
        for item in items:
            raw = item.get(spec.timestamp_field) if isinstance(item, dict) else None
            ts, _problem = parse_source_time(raw, assume_tz=spec.assume_tz, end_of_day=spec.date_only_end_of_day)
            if ts is None:                                                       # R4 -> R2: undated, or a time that cannot be trusted
                if allow_untimestamped:
                    served.append(item)
                    served_times.append(None)
                    unknown.append(item)
                else:
                    untimed.append(payload_sha256(item))
            elif ts > as_of:                                                     # R4: dated after T
                future.append(payload_sha256(item))
            else:
                served.append(item)
                served_times.append(ts)
        return ToolOutcome(tuple(served), tuple(served_times), tuple(unknown), tuple(future), tuple(untimed), len(items))

    @staticmethod
    def _items(spec: ToolSpec, result: Any) -> list[Any]:
        if spec.items is not None:
            if not isinstance(result, dict) or not isinstance(result.get(spec.items), list):
                raise ToolError(f"tool {spec.name!r} was registered with items={spec.items!r} but did not return a dict with a list under that key")
            return list(result[spec.items])
        if isinstance(result, (list, tuple)):
            return list(result)
        if isinstance(result, dict) and spec.timestamp_field in result:
            return [result]
        return [{"value": result}]                                               # no timestamp anywhere: one undated item
