"""The AsOf widget kit: web components for receipts and ledgers, a plugin API, Jupyter display and a static HTML report.

* The components live in ``asof-widgets.js`` and ``asof-widgets.css`` (dependency-free; no network calls). Each reads JSON from its ``data`` attribute or ``.data`` property.
* ``session.show("receipt")`` returns an object that renders in Jupyter (``_repr_html_``) and has ``.html()``.
* ``strathora report --html out.html`` writes one self-contained file with the widgets inlined.
* ``register(name, compute_fn, render_spec)`` adds a widget: ``compute_fn(**opts)`` returns the JSON the component reads; ``render_spec`` names the component (``{"tag": "asof-series", "title": ...}``).
  ``rolling_sharpe`` ships as the example plugin (see README.md in this folder).

Output is deterministic: the same inputs give byte-identical HTML. A widget made from data marked synthetic shows a small "synthetic" tag.
"""
from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import compute, redact
from .compute import gap_breakdown, leak_overlay, rolling_sharpe

HERE = Path(__file__).parent
TAGS = ("asof-receipt", "asof-clock-timeline", "asof-quality-panel", "asof-chain-status", "asof-rolling-sharpe", "asof-leak-overlay", "asof-gap-breakdown", "asof-series")

__all__ = ["register", "unregister", "names", "show", "Widget", "css", "js", "element", "page", "compute", "rolling_sharpe", "leak_overlay", "gap_breakdown", "TAGS"]


def css() -> str:
    return (HERE / "asof-widgets.css").read_text()


def js() -> str:
    return (HERE / "asof-widgets.js").read_text()


def _json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------- the plugin registry
@dataclass(frozen=True)
class Plugin:
    name: str
    compute: Callable[..., Mapping[str, Any]]
    spec: Mapping[str, Any]


_REGISTRY: dict[str, Plugin] = {}


def register(name: str, compute_fn: Callable[..., Mapping[str, Any]], render_spec: Mapping[str, Any], *, replace: bool = False) -> Plugin:
    """Add a widget. ``compute_fn(**opts)`` must return the JSON-able dict the component reads; ``render_spec`` must name a component: ``{"tag": "asof-series", "title": "..."}``.
    A name that exists is refused unless ``replace=True``; a ``tag`` that is not one of the kit's components is refused (a plugin supplies data, not new markup)."""
    if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("a widget name is letters, digits, '-' and '_'")
    if not callable(compute_fn):
        raise TypeError("compute_fn must be callable")
    if render_spec.get("tag") not in TAGS:
        raise ValueError(f"render_spec['tag'] must be one of {list(TAGS)}")
    if name in _REGISTRY and not replace:
        raise ValueError(f"a widget named {name!r} is already registered (pass replace=True to replace it)")
    p = Plugin(name, compute_fn, dict(render_spec))
    _REGISTRY[name] = p
    return p


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def names() -> list[str]:
    return sorted(set(_REGISTRY) | set(_SESSION_WIDGETS))


# ---------------------------------------------------------------- markup
def _fallback(tag: str, data: Any) -> str:
    """Plain text inside the element, shown only if scripts are blocked (a notebook that strips them, a reader with JS off). The component replaces it as soon as it runs."""
    d = data if isinstance(data, Mapping) else {}
    n = {"asof-receipt": lambda: f"Receipt: {d.get('kind')} {d.get('target')} at {d.get('as_of')}, served {d.get('served')}.",
         "asof-clock-timeline": lambda: f"Three clocks: {len(d.get('events') or [])} events.",
         "asof-quality-panel": lambda: f"Data quality: {d.get('by_severity') or 'no summary'}.",
         "asof-chain-status": lambda: f"Ledger chain: {d.get('status') or ('intact' if d.get('ok') else 'not intact')}, {d.get('n_events')} events.",
         "asof-rolling-sharpe": lambda: f"Rolling Sharpe: {len(d.get('points') or [])} points.",
         "asof-leak-overlay": lambda: f"Look-ahead leak overlay: largest gap {(d.get('summary') or {}).get('max_abs_gap')} (observed).",
         "asof-gap-breakdown": lambda: f"Gap {d.get('gap')} split into {len(d.get('components') or [])} parts.",
         "asof-series": lambda: f"Series: {len(d.get('series') or [])}."}
    return _html.escape(n[tag]())


def element(tag: str, data: Any, *, synthetic: bool = False, compact: bool = False, downloadable: bool = False, shared_data: Mapping[str, Any] | None = None, secret: str | bytes | None = None,
            rounded: bool = False) -> str:
    """One custom element with its data attached. Deterministic: sorted keys, fixed separators. ``compact=True`` shows the 3-4 headline numbers (and the chart, where there is one) and no tables.
    ``downloadable=True`` adds a "Download image" button. The PNG is made in the browser, on a click, and the DEFAULT picture is the SHARED view: for an internal widget that has a shared form, that form is
    made here (``shared_data``, or from ``data`` and ``secret`` / ``$ASOF_CLIENT_SECRET``) and embedded as ``shared-data``; the internal picture is a second button and carries an INTERNAL banner.
    Corners are square by default; ``rounded=True`` adds the ``rounded`` attribute for the older rounded look."""
    if tag not in TAGS:
        raise ValueError(f"unknown component {tag!r}")
    syn = bool(synthetic or (isinstance(data, Mapping) and data.get("synthetic") is True))
    if tag in ("asof-quality-panel", "asof-receipt"):
        data = redact.scrub_plain(data)                     # an unkeyed row-id hash never rides along inside an element, whatever the caller passed
    if downloadable and shared_data is None:
        shared_data = redact.shared_payload(tag, data, secret)
    shared_attr = f' shared-data="{_html.escape(_json(redact.scrub_plain(shared_data)), quote=True)}"' if downloadable and shared_data is not None else ""
    return f'<{tag} data="{_html.escape(_json(data), quote=True)}"{" synthetic" if syn else ""}{" compact" if compact else ""}{" downloadable" if downloadable else ""}{" rounded" if rounded else ""}{shared_attr}>{_fallback(tag, data)}</{tag}>'


def page(body: str, *, title: str = "AsOf report", lang: str = "en") -> str:
    """A self-contained HTML document: the stylesheet and the script inlined, nothing fetched."""
    return (f'<!DOCTYPE html>\n<html lang="{_html.escape(lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_html.escape(title)}</title>'
            f'<style>html{{background:#08090b;color:#f4f4f5}}body{{margin:0;padding:28px 18px;font:14px/1.5 -apple-system,"Helvetica Neue",Arial,sans-serif}}main{{max-width:760px;margin:0 auto}}h1{{font-size:20px;margin:0 0 6px}}'
            f'h2{{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:rgba(244,244,245,.58);margin:22px 0 8px}}p.note{{color:rgba(244,244,245,.6);font-size:12.5px}}</style>'
            f'<style>\n{css()}</style></head><body><main>\n{body}\n</main><script>\n{js()}</script></body></html>\n')


@dataclass(frozen=True)
class Widget:
    """What ``show`` returns: renders in Jupyter and can be written out. ``.tag`` and ``.data`` are what the component reads."""
    name: str
    tag: str
    data: Mapping[str, Any]
    synthetic: bool = False
    compact: bool = False
    downloadable: bool = False
    shared_data: Mapping[str, Any] | None = None                   # the shared form of ``data`` (never the secret), made by ``show`` for the "Download image" button
    rounded: bool = False                                          # square corners are the default theme; True gives the older rounded look

    def html(self, *, inline: bool = True) -> str:
        """The element, with the stylesheet and script inlined once (``inline=True``), or the bare element for a page that already loads the kit."""
        el = element(self.tag, self.data, synthetic=self.synthetic, compact=self.compact, downloadable=self.downloadable, shared_data=self.shared_data, rounded=self.rounded)
        return f"<style>{css()}</style>{el}<script>{js()}</script>" if inline else el

    def _repr_html_(self) -> str:
        return self.html()

    def save(self, path: str | Path, *, title: str | None = None) -> Path:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(page(self.html(inline=False), title=title or f"AsOf: {self.name}"))
        return p


# ---------------------------------------------------------------- built-in widgets of a session
def _receipt(s: Any, call_id: str | None = None, **_: Any) -> dict[str, Any]:
    r = s.receipt(call_id=call_id) if call_id else s.receipt()
    return dict(r.to_dict())


def _timeline(s: Any, **_: Any) -> dict[str, Any]:
    evs = s.ledger.events(session_id=s.id)
    return {"as_of": s.as_of.isoformat().replace("+00:00", "Z"), "events": [{"seq": e.seq, "type": e.type, "event_time": e.event_time, "available_at": e.available_at, "recorded_at": e.recorded_at} for e in evs]}


def _quality(s: Any, source: str | None = None, view: str = "internal", secret: str | bytes | None = None, **_: Any) -> dict[str, Any]:
    q = s.quality(source) if source else s.quality()
    if source:
        return redact.apply(q, view, secret)
    from .report import merge_quality                     # imported here: report imports this module
    return merge_quality(q, view=view, secret=secret)


def _chain(s: Any, **_: Any) -> dict[str, Any]:
    v = s.verify_ledger(anchor="skip")
    return {"ok": v.ok, "status": "intact" if v.ok else "broken", "n_events": v.n_events, "head_hash": v.head_hash, "first_bad_seq": v.first_bad_seq, "reason": v.reason, "anchor": v.anchor,
            "warnings": list(v.warnings)}


_SESSION_WIDGETS: dict[str, tuple[str, Callable[..., Mapping[str, Any]]]] = {
    "receipt": ("asof-receipt", _receipt), "clock-timeline": ("asof-clock-timeline", _timeline), "quality-panel": ("asof-quality-panel", _quality), "chain-status": ("asof-chain-status", _chain),
    "leak-overlay": ("asof-leak-overlay", lambda s, **o: compute.leak_overlay(**o)), "gap-breakdown": ("asof-gap-breakdown", lambda s, **o: compute.gap_breakdown(**o)),
}


def show(session: Any, widget: str, **opts: Any) -> Widget:
    """Render a widget for a session. Built in: ``receipt`` (``call_id=``), ``clock-timeline``, ``quality-panel`` (``source=``), ``chain-status``, ``leak-overlay`` and ``gap-breakdown`` (their data comes from
    ``rows=`` / ``attribution=``); plus any registered plugin, e.g. ``rolling-sharpe`` (``rows=``, ``window=``). ``synthetic=True`` adds the small "synthetic" tag; ``compact=True`` shows only the headline numbers (no tables).
    ``downloadable=True`` adds a "Download image" button (the SHARED view by default; the internal picture is a second button with an INTERNAL banner). Corners are square by default; ``rounded=True`` gives the older rounded look.
    Every widget takes ``view=`` (``internal`` default, ``shared``, ``showcase``; see :mod:`asof.widgets.redact`) and ``secret=`` (the per-client key for row ids; default ``$ASOF_CLIENT_SECRET``).
    ``internal`` is unchanged; ``shared`` is for what leaves the building."""
    compact = bool(opts.pop("compact", False))
    downloadable = bool(opts.pop("downloadable", False))
    rounded = bool(opts.pop("rounded", False))
    view, secret = redact.check_view(opts.pop("view", "internal")), opts.pop("secret", None)
    synthetic = bool(opts.get("synthetic"))
    if widget in _REGISTRY:
        p = _REGISTRY[widget]
        data = redact.widget(p.spec["tag"], dict(p.compute(**opts)), view, secret)
        if p.spec.get("title") and "title" not in data:
            data["title"] = p.spec["title"]
        return Widget(widget, p.spec["tag"], data, synthetic or data.get("synthetic") is True, compact, downloadable, redact.shared_payload(p.spec["tag"], data, secret) if downloadable else None, rounded)
    if widget in _SESSION_WIDGETS:
        tag, fn = _SESSION_WIDGETS[widget]
        data = dict(fn(session, **({**opts, "view": view, "secret": secret} if widget == "quality-panel" else opts)))
        if widget != "quality-panel":
            data = redact.widget(tag, data, view, secret)
        if synthetic:
            data["synthetic"] = True
        return Widget(widget, tag, data, synthetic or data.get("synthetic") is True, compact, downloadable, redact.shared_payload(tag, data, secret) if downloadable else None, rounded)
    raise ValueError(f"no widget named {widget!r}; available: {names()}")


# ---------------------------------------------------------------- the example plugin
register("rolling-sharpe", compute.rolling_sharpe, {"tag": "asof-rolling-sharpe", "title": "Rolling Sharpe, as of"})
