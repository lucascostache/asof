"""Render the showcase assets into showcase/web/: standalone HTML snippets (widgets inlined, no network) and, when Playwright is installed, 2x dark-theme PNGs of the same cards in compact mode."""
from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Any

from asof import widgets as W
from asof.widgets.report import merge_quality

from .common import WEB
from .monitor import LABEL as MONITOR_LABEL

ASSETS = ["data-quality", "leak-overlay", "leak-overlay-sharpe", "leak-overlay-hitrate", "monitoring-preview"]

_FEED_CSS = """<style>
.di-feed { list-style: none; margin: 4px 0 0; padding: 0; font: 12.5px/1.5 ui-monospace, Menlo, monospace; }
.di-feed li { display: grid; grid-template-columns: 14px 132px 1fr; gap: 8px; padding: 6px 0; border-top: 1px solid var(--asof-line); }
.di-feed li:first-child { border-top: 0; }
.di-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; background: var(--asof-mut); }
.di-error .di-dot { background: var(--r); } .di-warning .di-dot { background: var(--soft); } .di-ok .di-dot { background: var(--g); } .di-ok .di-text { color: var(--g); }
.di-src { color: var(--asof-mut); } .di-sim { margin: 12px 0 0; font-weight: 600; color: var(--soft); }
</style>"""


def monitoring_card(alerts: list[dict[str, Any]], new_rows: int) -> str:
    """An alert list in the widgets' look. Not a library widget: it is made for this preview, from the two check runs' difference."""
    n_alerts = sum(1 for a in alerts if a["level"] in ("error", "warning"))
    tiles = [(str(n_alerts), "new alerts", "hot" if n_alerts else "ok"), (f"{new_rows:,}", "rows added", ""), ("verified" if alerts and alerts[-1]["level"] == "ok" else "check", "ledger chain", "ok" if alerts and alerts[-1]["level"] == "ok" else "hot"), ("2", "manual runs", "")]
    t = '<div class="asof-tiles">' + "".join(f'<div class="asof-tile{" asof-tile-" + c if c else ""}"><b>{_html.escape(v)}</b><span>{_html.escape(k)}</span></div>' for v, k, c in tiles) + "</div>"
    rows = "".join(f'<li class="di-{a["level"]}"><span class="di-dot" aria-hidden="true"></span><span class="di-src">{_html.escape(a["source"])}</span><span class="di-text">{_html.escape(a["text"])}</span></li>' for a in alerts)
    return (f'{_FEED_CSS}<div class="asof-w asof-compact asof-monitoring" role="group" aria-label="Monitoring preview: what changed between two manual check runs"><div class="asof-hd"><span class="asof-ttl">Monitoring preview · day 1 → day 30</span>'
            f'<span class="asof-synth" title="Made from synthetic data">synthetic</span></div>{t}<ol class="di-feed" aria-label="Alerts, oldest first">{rows}</ol><p class="di-sim">{_html.escape(MONITOR_LABEL)}</p></div>')


def snippets(report: dict[str, Any], overlays: dict[str, Any], alerts: list[dict[str, Any]], new_rows: int) -> dict[str, str]:
    """name -> the compact card's markup (a custom element with its data, or the monitoring card)."""
    return {"data-quality": W.element("asof-quality-panel", merge_quality(report["day1"]["sources"]) | {"synthetic": True}, synthetic=True, compact=True),
            "leak-overlay": W.element("asof-leak-overlay", overlays["equity"], synthetic=True, compact=True),
            "leak-overlay-sharpe": W.element("asof-leak-overlay", overlays["sharpe"], synthetic=True, compact=True),
            "leak-overlay-hitrate": W.element("asof-leak-overlay", overlays["hit_rate"], synthetic=True, compact=True),
            "monitoring-preview": monitoring_card(alerts, new_rows)}


def write_html(cards: dict[str, str]) -> list[Path]:
    WEB.mkdir(parents=True, exist_ok=True)
    out = []
    for name, markup in cards.items():
        p = WEB / f"{name}.html"
        p.write_text(W.page(markup, title=f"AsOf: {name} (synthetic)"))
        out.append(p)
    return out


def write_png(names: list[str]) -> tuple[list[Path], str | None]:
    """2x dark-theme PNGs of each card. Returns (files, None) or ([], why not). Needs `pip install playwright` and `playwright install chromium`."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "playwright is not installed: pip install playwright && playwright install chromium, then run again"
    files: list[Path] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 780, "height": 900}, device_scale_factor=2, color_scheme="dark", reduced_motion="reduce")
        page = ctx.new_page()
        for n in names:
            page.goto((WEB / f"{n}.html").as_uri())
            page.wait_for_selector(".asof-w")
            page.wait_for_timeout(300)
            out = WEB / f"{n}.png"
            page.locator(".asof-w").first.screenshot(path=str(out))
            files.append(out)
        b.close()
    return files, None


def readme(detection: dict[str, Any], strat: dict[str, Any], alerts: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    rows = detection["rows"]
    tbl = "| planted issue | found by | detected? | severity | planted | found |\n|---|---|---|---|---|---|\n" + "\n".join(
        f"| {r['planted']} | `{r['check']}` | **{r['detected']}** | {r['severity'] or '-'} | {r['planted_count']} | {'-' if r['found_count'] is None else r['found_count']} |" for r in rows)
    extra = "\n".join(f"- `{u['source']}`: `{u['flag']}` ({u['severity']}, n={u['n']}, {u['basis'] or 'counted'}) is not in the manifest." for u in detection["unplanted_flags"]) or "- none"
    f = strat["final"]
    al = "\n".join(f"- {a['level']}: {a['text']}" for a in alerts)
    from . import control
    ctl = control.section(json.loads((control.OUT / "control.json").read_text()))                 # written by control.main(), which build.py runs first
    return f"""# Data-integrity showcase (SYNTHETIC)

Everything here is made from a **synthetic** fixture: fictional tickers (`SYN01`..`SYN50`), made-up numbers, no real data, no keys, no network. Every picture carries the "synthetic" tag.
The checks are the real AsOf code (`asof.quality`, `asof.sources.Bars`, `Universe`, `Session`), run unchanged.

## Regenerate (one command)

```
python showcase/build.py
```

It regenerates the fixture, runs the checks, runs the strategy twice (about 2 minutes: the locked run makes about 1,400 time-locked queries), and rewrites everything in `showcase/out/` and `showcase/web/`.
The PNGs need `pip install playwright && playwright install chromium`; without it the HTML snippets and JSON are still written. Fixture, JSON and HTML are byte-identical on every run
with the same Python version (fixed seed, fixed clocks). A ledger's first event records the Python version, so the chain's head hash, and the files that show it, differ on another version; the check results do not (compared on 3.11 and 3.13). PNG bytes can differ slightly between machines because fonts and anti-aliasing do.

## The fixture (`showcase/fixtures/data_integrity/`)

50 fictional tickers, {manifest['period'][0]} to {manifest['period'][1]}: `bars.csv` (daily bars, {manifest['tables']['bars.csv']:,} rows), `fundamentals.csv` (quarterly EPS), `news.csv` (timestamps only, {manifest['tables']['news.csv']:,} rows),
`estimates.csv` (a consensus series), `universe.csv` (membership change log). `day30/` is the same data a month later. `manifest.json` is the ground truth of what was planted.

## What was planted, and what the real checks found (day 1)

{tbl}

- **Survivorship is not flagged by any data-quality check.** It shows only when a survivors-only list is compared with the universe as it was: {detection['survivorship']['as_of_universe_size_at_start']} names then, {detection['survivorship']['naive_universe_size']} in a list of today's survivors; the 6 missing are exactly the delisted ones. That is a measurement, not a flag.
- `severity` is `asof.quality.SEVERITY`; `bar_close` is `info` because the library repairs it (the bar is made available at its close) and counts the repair.

Flags the checks raised that were **not** planted (reported, not explained away):

{extra}

Nothing was tuned to the fixture. A planted issue that no check flags is shown as NOT DETECTED and left that way.

{ctl}

## Leak overlay: the same strategy on naive and on locked data

Strategy (synthetic, deliberately simple): rank-sum of 5-day momentum and the latest EPS change, hold the top {strat['rules']['hold']} names equal-weight for the day, decision at 10:00 New York.
**Unlocked** reads the vendor's timestamps at face value (10 tickers stamp each bar at the open), the latest EPS by period end (restated values, as if known at quarter end), and today's survivors. **Locked** runs the same code on a time-locked `Session`.

| | locked (as of) | unlocked (naive) |
|---|---|---|
| final equity (growth of 1) | {f['locked_equity']} | {f['unlocked_equity']} |

The shaded area is labelled "difference attributable to look-ahead (observed)": it is a measured difference between two runs, not proof of which of the three reads caused how much of it.

## Monitoring preview

**{MONITOR_LABEL}** Monitoring is not built. Two manual runs of the same checks, on day 1 and on day 30 (30 more days of news that arrive slowly, plus a new backfilled series), and the difference:

{al}

## Files

`web/data-quality.*`, `web/leak-overlay.*` (equity), `web/leak-overlay-sharpe.*`, `web/leak-overlay-hitrate.*`, `web/monitoring-preview.*` (`.html` = standalone, widgets inlined; `.png` = 2x, dark, compact).
`out/data_integrity_report.json` is the raw output of the checks (day 1 and day 30); `out/detection.{{json,txt}}` the table above; `out/strategy_summary.json` and `out/leak_overlay_*.json` the strategy runs.
"""
