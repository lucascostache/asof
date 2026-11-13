#!/usr/bin/env python3
"""Run the README's Quickstart, exactly as written, against an installed copy of AsOf, and compare the output with the README's.

    python scripts/quickstart_check.py --python /path/to/venv/bin/python      # the environment the package was installed into

Used by the release workflow (on the wheel it is about to publish) and after a release (on the copy installed from PyPI). It works in a new
empty folder, never imports the checkout, and needs only the standard library. It checks, in order: `strathora --version` prints this
README's version line, step 1 (`strathora init`), step 2 (the Python block) prints the README's output (a receipt id and a hash differ on
every run, so those two lines are compared by shape), and step 3 (`strathora verify`) prints `OK`. Exit 0 on success, 1 on any difference.
"""
import argparse
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def block(readme: str, name: str) -> str:
    m = re.search(rf"<!-- {name}:start -->\n```\w*\n(.*?)```\n<!-- {name}:end -->", readme, re.S)
    if not m:
        sys.exit(f"the README has no {name} block")
    return m.group(1)


def run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"FAIL: {shlex.join(cmd)} exited {r.returncode}\n{r.stdout}{r.stderr}")
        sys.exit(1)
    return r.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python", required=True, help="the Python of the environment AsOf is installed in")
    ap.add_argument("--readme", default=str(ROOT / "README.md"), help="the README whose Quickstart is run (default: this checkout's)")
    a = ap.parse_args()
    readme = Path(a.readme).read_text()
    py = Path(a.python)
    strathora = py.parent / "strathora"
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        where = run([str(py), "-c", "import asof, sys; print(asof.__file__)"], work).strip()
        if str(ROOT) in where:
            print(f"FAIL: asof was imported from the checkout ({where}), not from the installed copy")
            return 1
        print(f"asof imported from: {where}")
        v = run([str(strathora), "--version"], work).strip()
        print(f"strathora --version: {v}")
        if version and v != f"strathora-asof {version.group(1)}":
            print(f"FAIL: expected strathora-asof {version.group(1)}")
            return 1
        init = shlex.split(block(readme, "quickstart-init").strip())
        assert init[0] == "strathora", init
        run([str(strathora), *init[1:]], work)
        (work / "try_asof.py").write_text(block(readme, "quickstart-code"))
        got = run([str(py), "try_asof.py"], work).splitlines()
        want = block(readme, "quickstart-output").splitlines()
        if len(got) != len(want):
            print("FAIL: the Quickstart printed a different number of lines\n--- got\n" + "\n".join(got) + "\n--- README\n" + "\n".join(want))
            return 1
        for g, w in zip(got, want, strict=True):                       # the lengths were checked just above
            pattern = re.escape(w).replace(re.escape("<id>"), r"[0-9a-f]{12}").replace(re.escape("<hash>"), r"[0-9a-f]{8,64}")
            if not re.fullmatch(pattern, g):
                print(f"FAIL: the Quickstart printed\n  {g}\nwhere the README shows\n  {w}")
                return 1
        print(f"quickstart output: {len(got)} lines, as in the README")
        verify = shlex.split(block(readme, "quickstart-verify").strip())
        out = run([str(strathora), *verify[1:]], work).strip()
        print(f"strathora verify: {out}")
        if not out.startswith("OK: "):
            print("FAIL: the ledger did not verify")
            return 1
    print("OK: the README Quickstart works as written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
