"""The end-value label of a line chart sits in an empty strip to the right of the plot, level with the last dot: it can never lie on the line or on the dot.
Checked on the SVG the widgets make (every drawn x is left of the label) and on real geometry in a browser (bounding boxes, the line sampled along its length)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from asof.widgets import element, page

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures" / "widgets"
JS = ROOT / "asof" / "widgets" / "asof-widgets.js"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def series(vals):
    return {"title": "s", "series": [{"name": "a", "points": [{"t": f"2023-01-{i + 1:02d}T00:00:00Z", "v": v} for i, v in enumerate(vals)]}]}


SHAPES = {"rising": [1, 2, 3, 4, 5, 6], "falling": [6, 5, 4, 3, 2, 1], "flat": [2, 2, 2, 2], "ends-at-top": [0, 1, 0, 1, 0, 9], "ends-at-bottom": [5, 6, 5, 6, 5, -9], "trailing-gap": [1, 5, 2, None, None, None],
          "long-value": [1, 2, -12345.67]}


def cases():
    out = [{"name": "rolling-sharpe", "widget": "rolling-sharpe", "data": load("rolling-sharpe")}, {"name": "leak-overlay", "widget": "leak-overlay", "data": load("leak-overlay")}]
    return out + [{"name": k, "widget": "series", "data": series(v)} for k, v in SHAPES.items()]


@pytest.fixture(scope="module")
def svgs(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    f = tmp_path_factory.mktemp("e") / "c.json"
    f.write_text(json.dumps(cases()))
    r = subprocess.run([node, str(ROOT / "tests" / "widgets_render.js"), str(JS), str(f)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(r.stdout)


@pytest.mark.parametrize("name", [c["name"] for c in cases()])
def test_nothing_is_drawn_at_or_right_of_the_label_and_the_label_is_level_with_the_dot(svgs, name):
    svg = re.search(r"<svg\b.*?</svg>", svgs[name], re.S).group(0)                       # the first chart of the widget: the line chart
    label = re.search(r'<text class="asof-endv" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>', svg)
    dot = re.search(r'<circle class="asof-end" cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg)
    assert label and dot and "text-anchor" not in label.group(0)                         # starts at its x and runs to the right
    lx, ly, cx, cy, r = float(label.group(1)), float(label.group(2)), float(dot.group(1)), float(dot.group(2)), float(dot.group(3))
    assert lx >= cx + r + 5                                                              # clear of the dot and its halo
    assert abs((ly - 4) - cy) <= 12                                                      # level with the dot (nudged only to stay inside the chart)
    xs = [float(x) for d in re.findall(r'<path class="asof-(?:line|area)[^"]*" d="([^"]*)"', svg) for x in re.findall(r"[ML]([\d.]+) ", d)]
    xs += [float(x) for p in re.findall(r'<polygon class="asof-band" points="([^"]*)"', svg) for x in re.findall(r"([\d.]+),", p)]
    xs += [float(x) for x in re.findall(r'<line class="asof-(?:axis|zero|grid)"[^>]* x2="([\d.]+)"', svg)]
    assert xs and max(xs) < lx - 5, (max(xs), lx)                                        # every line, area, band, axis and grid line ends left of the label
    width = float(re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))
    assert lx + 6.7 * len(label.group(3)) <= width + (0 if name != "long-value" else 30)  # and an ordinary value fits in the strip (11px monospace is about 6.6 units a character)


# ---------------------------------------------------------------- real geometry
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


OVERLAP = """() => [...document.querySelectorAll('svg')].filter(s => s.querySelector('.asof-endv')).map(svg => {
  const t = svg.querySelector('.asof-endv').getBBox(), d = svg.querySelector('.asof-end').getBBox(), pad = 2;
  const hit = (x, y) => x >= t.x - pad && x <= t.x + t.width + pad && y >= t.y - pad && y <= t.y + t.height + pad;
  let onLine = 0;
  for (const p of svg.querySelectorAll('path.asof-line')) { const n = p.getTotalLength(); for (let i = 0; i <= 400; i++) { const q = p.getPointAtLength(n * i / 400); if (hit(q.x, q.y)) onLine++; } }
  const onDot = !(d.x > t.x + t.width || d.x + d.width < t.x || d.y > t.y + t.height || d.y + d.height < t.y);
  return { onLine, onDot, inside: t.x + t.width <= svg.viewBox.baseVal.width + 0.5, text: svg.querySelector('.asof-endv').textContent }; })"""


@pytest.mark.parametrize("width", [360, 820])
def test_in_a_browser_the_label_touches_neither_the_line_nor_the_dot(pw, tmp_path, width):
    body = element("asof-rolling-sharpe", load("rolling-sharpe"), synthetic=True) + element("asof-leak-overlay", load("leak-overlay"), synthetic=True) + "".join(
        element("asof-series", series(v), synthetic=True) for k, v in SHAPES.items() if k != "long-value")
    f = tmp_path / "e.html"
    f.write_text(page(body, title="end label"))
    pg = pw.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    pg.goto(f.as_uri())
    pg.wait_for_timeout(300)
    r = pg.evaluate(OVERLAP)
    assert len(r) == 8 and all(x["onLine"] == 0 and not x["onDot"] and x["inside"] for x in r), r
    pg.close()
