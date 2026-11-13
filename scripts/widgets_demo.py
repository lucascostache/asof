"""Write a local demo page of every AsOf widget, from the SYNTHETIC fixtures in tests/fixtures/widgets (no network, no keys).

    python scripts/widgets_demo.py [out.html]      # default: examples/widgets/demo.html

Open it with:  open examples/widgets/demo.html      (every widget on it carries the small "synthetic" tag)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from asof import widgets as W

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "widgets"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def main(argv: list[str] | None = None) -> int:
    out = Path((argv or sys.argv[1:] or [str(ROOT / "examples" / "widgets" / "demo.html")])[0]).expanduser()
    parts = [("Receipt", "asof-receipt", load("receipt")), ("Three clocks", "asof-clock-timeline", load("timeline")), ("Data quality", "asof-quality-panel", load("quality")),   # every widget reads the one shared fixture
             ("Ledger chain", "asof-chain-status", load("chain")), ("Rolling Sharpe, as of", "asof-rolling-sharpe", load("rolling-sharpe")),
             ("Look-ahead leak overlay", "asof-leak-overlay", load("leak-overlay")), ("Gap breakdown", "asof-gap-breakdown", W.gap_breakdown(load("attribution")))]
    body = ["<h1>AsOf widgets</h1>", '<p class="note">All data on this page is SYNTHETIC (a fictional strategy and a demo session). No network was used to make or to open this page.</p>']
    body += ["<h2>At a glance (compact: for a website or a demo)</h2>"] + [W.element(tag, data, synthetic=True, compact=True) for _, tag, data in parts]
    body += ['<p class="note">Detailed mode below is what the Verumark report and <code>strathora report --html</code> use.</p>']
    for title, tag, data in parts:
        body += [f"<h2>{title}</h2>", W.element(tag, data, synthetic=True)]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(W.page("\n".join(body), title="AsOf widgets (synthetic demo)"))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
