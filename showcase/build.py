"""One command for the whole data-integrity showcase:   python showcase/build.py

fixture -> real checks (day 1 and day 30) -> detection table -> strategy locked vs unlocked -> monitoring diff -> showcase/web/ (HTML always, PNG when Playwright is installed) -> README.
Everything is synthetic; nothing uses the network.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from data_integrity import control, detect, export, monitor, strategy                     # noqa: E402
from data_integrity.common import MANIFEST, OUT, WEB                              # noqa: E402


def main() -> int:
    subprocess.run([sys.executable, str(HERE / "fixtures" / "data_integrity" / "generate.py")], check=True)
    report = detect.main()                                                          # raw reports -> out/data_integrity_report.json, the detection table -> out/detection.*
    detection = json.loads((OUT / "detection.json").read_text())
    control.main()                                                                  # the same checks on the clean control -> out/control.json
    s = strategy.run()
    for k, v in s["overlays"].items():
        (OUT / f"leak_overlay_{k}.json").write_text(json.dumps(v, indent=1, sort_keys=True) + "\n")
    (OUT / "strategy_summary.json").write_text(json.dumps({"final": s["final"], "rules": s["rules"]}, indent=1, sort_keys=True) + "\n")
    alerts = monitor.diff(report["day1"], report["day30"])
    (OUT / "monitoring_alerts.json").write_text(json.dumps({"label": monitor.LABEL, "alerts": alerts}, indent=1, sort_keys=True) + "\n")
    new_rows = sum(r["n_records"] for n, r in report["day30"]["sources"].items() if n not in report["day1"]["sources"]) + (report["day30"]["sources"]["news"]["n_records"] - report["day1"]["sources"]["news"]["n_records"])
    cards = export.snippets(report, s["overlays"], alerts, new_rows)
    export.write_html(cards)
    files, why = export.write_png(export.ASSETS)
    (WEB / "README.md").write_text(export.readme(detection, s, alerts, MANIFEST))
    print(f"\nwrote {len(cards)} HTML snippets" + (f" and {len(files)} PNGs" if files else f"; PNGs skipped: {why}") + f" in {WEB}")
    print("\nalerts (simulated, two manual runs):")
    for a in alerts:
        print(f"  {a['level']:8s} {a['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
