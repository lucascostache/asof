#!/usr/bin/env python3
"""Search files for a secret and for every fragment of it, without ever printing the secret. Run it after anything that used a real key.

    python scripts/secret_search.py --env ANTHROPIC_API_KEY ./live-test-out ./logs some-file.txt
    python scripts/secret_search.py --env ANTHROPIC_API_KEY --env OPENAI_API_KEY --git .          # also every version of every file in the repository's history

The secret is read from the environment variable you name (never from the command line, which other users of the machine can see). For each secret it looks, in every file under the
paths given, for: the whole value; every 8- and every 10-character piece of it; and the same for its URL-encoded, base64 (three alignments, both alphabets) and hex forms.
It prints, per file, how many pieces were found (and where, as byte offsets), never the pieces themselves. Exit 0 if nothing was found anywhere, 1 if anything was, 2 for a usage error.

The first 8 characters of a provider key are often a fixed prefix that says nothing ("sk-ant-a"); pieces that lie entirely inside --public-prefix characters (default 0) are skipped,
so set it to the length of the fixed prefix if you get hits on ordinary text.
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path


def forms(secret: str) -> dict[str, str]:
    raw = secret.encode("utf-8")
    out = {"as is": secret, "url-encoded": urllib.parse.quote(secret, safe=""), "hex": raw.hex(), "HEX": raw.hex().upper()}
    for pad in (0, 1, 2):
        for name, enc in (("base64", base64.b64encode), ("base64url", base64.urlsafe_b64encode)):
            text = enc(b"\x00" * pad + raw).decode().rstrip("=")
            out[f"{name}, offset {pad}"] = (text[4:] if pad else text)[:-3]
    return {k: v for k, v in out.items() if len(v) >= 8}


def pieces(secret: str, skip: int) -> dict[bytes, str]:
    found: dict[bytes, str] = {}
    for label, text in forms(secret).items():
        start = skip if label == "as is" else 0
        for n in (8, 10):
            for i in range(start, len(text) - n + 1):
                found.setdefault(text[i:i + n].encode(), f"{n} characters, {label}")
    return found


def scan(data: bytes, secret: str, table: dict[bytes, str]) -> tuple[bool, dict[str, int], list[int]]:
    whole = secret.encode() in data
    kinds: dict[str, int] = {}
    offsets: list[int] = []
    if not any(p in data for p in table):                 # the usual case, and a fast one
        return whole, kinds, offsets
    for p, kind in table.items():
        at = data.find(p)
        if at >= 0:
            kinds[kind] = kinds.get(kind, 0) + 1
            offsets.append(at)
    return whole, kinds, sorted(set(offsets))[:5]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", action="append", required=True, metavar="NAME", help="environment variable holding a secret to look for (repeatable)")
    ap.add_argument("--git", metavar="REPO", help="also search every version of every file in this repository's history")
    ap.add_argument("--public-prefix", type=int, default=0, help="ignore pieces lying entirely within the first N characters of the secret (a fixed, non-secret prefix)")
    ap.add_argument("paths", nargs="*", help="files and folders to search (folders are searched recursively, hidden files included)")
    a = ap.parse_args()
    secrets = {}
    for name in a.env:
        value = os.environ.get(name, "")
        if len(value) < 12:
            print(f"secret_search: {name} is not set (or is shorter than 12 characters). Nothing was searched.", file=sys.stderr)
            return 2
        secrets[name] = value
    if not a.paths and not a.git:
        print("secret_search: give at least one path, or --git REPO", file=sys.stderr)
        return 2
    tables = {name: pieces(value, a.public_prefix) for name, value in secrets.items()}
    n_files = n_bytes = 0
    hits = 0

    def check(label: str, data: bytes) -> None:
        nonlocal hits, n_files, n_bytes
        n_files += 1
        n_bytes += len(data)
        for name, value in secrets.items():
            whole, kinds, offsets = scan(data, value, tables[name])
            if whole or kinds:
                hits += 1
                detail = ", ".join(f"{c} piece(s) of {k}" for k, c in sorted(kinds.items()))
                print(f"FOUND  {label}: {'THE WHOLE VALUE of ' + name + '; ' if whole else ''}{detail or 'pieces'} of ${name} (first at byte offsets {offsets})")

    for p in a.paths:
        path = Path(p).expanduser()
        if not path.exists():
            print(f"secret_search: {path} does not exist", file=sys.stderr)
            return 2
        for f in ([path] if path.is_file() else sorted(x for x in path.rglob("*") if x.is_file())):
            try:
                check(str(f), f.read_bytes())
            except OSError as e:
                print(f"could not read {f}: {e}", file=sys.stderr)
    if a.git:
        objs = subprocess.run(["git", "-C", a.git, "rev-list", "--all", "--objects"], capture_output=True, text=True, check=True).stdout.splitlines()
        seen = set()
        for line in objs:
            sha, _, name = line.partition(" ")
            if not name or sha in seen:
                continue
            seen.add(sha)
            if subprocess.run(["git", "-C", a.git, "cat-file", "-t", sha], capture_output=True, text=True).stdout.strip() == "blob":
                check(f"git:{name}@{sha[:7]}", subprocess.run(["git", "-C", a.git, "cat-file", "-p", sha], capture_output=True).stdout)
        log = subprocess.run(["git", "-C", a.git, "log", "--all", "--format=%H %an %ae %cn %ce %B"], capture_output=True).stdout
        check("git: commit messages and identities", log)
    per = ", ".join(f"${n}: {len(t)} pieces" for n, t in tables.items())
    print(f"searched {n_files} file(s), {n_bytes:,} bytes, for {per}, plus each whole value")
    print("RESULT: " + ("NOTHING FOUND" if not hits else f"FOUND in {hits} place(s)"))
    return 0 if not hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
