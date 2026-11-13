"""The README's quickstart must keep working: run the example and check the claims it prints."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_quickstart_example_runs_and_prints_what_the_readme_says():
    out = subprocess.run([sys.executable, str(ROOT / "examples" / "quickstart.py")], capture_output=True, text=True, timeout=60, check=True).stdout
    assert "closes the agent can see on Jan 21: [148.0, 150.0]" in out and "closes the agent can see on Jan 24: [148.0, 150.0, 155.0]" in out
    assert "news: ['ACME-SYN raises guidance']" in out and "blocked: future=1" in out and "ledger verifies: True (17 events)" in out
    assert "memory label 'unknown'" in out
    readme = (ROOT / "docs" / "REFERENCE.md").read_text()                # the reference page shows this example's output (it was the README until README v1)
    for line in ("closes the agent can see on Jan 21: [148.0, 150.0]", "ledger verifies: True (17 events)"):
        assert line in readme
