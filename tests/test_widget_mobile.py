"""On a phone the tiles sit two to a row, the return-gap tile runs across both, and a number never wraps in the middle (and never spills out of its box instead).
Measured in a real browser at phone widths; skipped, not passed, without Playwright and a browser."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from asof.widgets import compute, element, page, redact

FIX = Path(__file__).parent / "fixtures" / "widgets"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def body() -> str:
    a = compute.gap_breakdown(load("attribution"), synthetic=True)
    items = [("asof-gap-breakdown", a), ("asof-gap-breakdown", redact.widget("asof-gap-breakdown", a, "shared")), ("asof-receipt", {**load("receipt"), "synthetic": True}),
             ("asof-quality-panel", {**load("quality"), "synthetic": True}), ("asof-rolling-sharpe", load("rolling-sharpe")), ("asof-leak-overlay", load("leak-overlay")),
             ("asof-clock-timeline", {**load("timeline"), "synthetic": True}), ("asof-chain-status", {**load("chain"), "synthetic": True})]
    long = compute.gap_breakdown({"gap": -1234.56, "unit": "pp", "headline": {"sharpe_gap": -123.45, "method": "m"}, "components": [                 # a stress case: values much longer than the fixture's
        {"id": "leakage", "name": "Look-ahead leakage", "value": -1000.01, "label": "OBSERVED"}, {"id": "unexplained", "name": "Unexplained", "value": -234.55, "label": "UNKNOWN"}]})
    return "".join(element(t, d, synthetic=True) for t, d in items) + element("asof-gap-breakdown", a, synthetic=True, compact=True) + element("asof-gap-breakdown", long, synthetic=True)


@pytest.fixture(scope="module")
def pw():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                                          # noqa: BLE001  (no browser installed)
            pytest.skip(f"no Chromium for Playwright: {type(e).__name__}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def doc(tmp_path_factory):
    f = tmp_path_factory.mktemp("m") / "m.html"
    f.write_text(page(body(), title="mobile"))
    return f


MEASURE = """() => {
  const lines = el => { const r = document.createRange(); r.selectNodeContents(el); return new Set([...r.getClientRects()].map(x => Math.round(x.top))).size; };
  const out = { grids: [], wide: [], wrapped: [], spilled: [], scroll: document.scrollingElement.scrollWidth - window.innerWidth, numbers: 0 };
  for (const g of document.querySelectorAll('.asof-tiles')) {
    out.grids.push(new Set([...g.children].map(c => Math.round(c.getBoundingClientRect().left))).size);      // how many columns the tiles really sit in
    const w = g.querySelector('.asof-tile-wide');
    if (w) out.wide.push([Math.round(w.getBoundingClientRect().width), Math.round(g.getBoundingClientRect().width), [...g.children].indexOf(w)]);
  }
  for (const el of document.querySelectorAll('.asof-tile:not(.asof-tile-name) b, .asof-gbv, .asof-gbs')) {
    if (!el.textContent.trim()) continue;
    out.numbers++;
    if (getComputedStyle(el).whiteSpace !== 'nowrap') out.wrapped.push('may wrap: ' + el.textContent);
    if (lines(el) > 1) out.wrapped.push(el.textContent);
    const box = (el.closest('.asof-tile') || el).getBoundingClientRect(), r = document.createRange(); r.selectNodeContents(el);
    const right = Math.max(...[...r.getClientRects()].map(x => x.right));
    if (right > box.right + 0.5) out.spilled.push(el.textContent);
  }
  return out; }"""


@pytest.mark.parametrize("width", [280, 320, 360, 390, 430])
def test_on_a_phone_tiles_are_two_to_a_row_the_return_gap_is_full_width_and_no_number_wraps_or_spills(pw, doc, width):
    pg = pw.new_page(viewport={"width": width, "height": 800}, reduced_motion="reduce")
    pg.goto(doc.as_uri())
    pg.wait_for_timeout(300)
    m = pg.evaluate(MEASURE)
    assert m["grids"] and set(m["grids"]) == {2}, m["grids"]                                # every tile row, in every widget: two columns
    assert len(m["wide"]) == 3 and all(abs(w - g) <= 1 and i == 0 for w, g, i in m["wide"]), m["wide"]      # the return-gap tile (full and compact internal views) is first and as wide as the row
    assert m["numbers"] > 30 and m["wrapped"] == [] and m["spilled"] == [], (m["wrapped"], m["spilled"])    # "-19.01 pp" and the like: one line, inside its box
    assert m["scroll"] <= 0                                                                 # and the page does not scroll sideways
    pg.close()


def test_on_a_desktop_the_tiles_are_not_forced_into_two_columns(pw, doc):
    pg = pw.new_page(viewport={"width": 900, "height": 800}, reduced_motion="reduce")
    pg.goto(doc.as_uri())
    pg.wait_for_timeout(300)
    m = pg.evaluate(MEASURE)
    assert max(m["grids"]) >= 4 and m["grids"][0] == 5 and m["wrapped"] == [] and m["spilled"] == []      # the internal gap breakdown: five tiles in one row
    assert all(w < g for w, g, _ in m["wide"])                                              # and the return-gap tile is an ordinary tile there
    pg.close()
