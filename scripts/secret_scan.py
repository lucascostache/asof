#!/usr/bin/env python3
"""Scan a git repository for secrets, e-mail addresses and personal data before it is shared: every version of every file reachable from any ref (the whole
history), the working tree, commit messages, and the author and committer identities (which become public on a push).

    python scripts/secret_scan.py                       # this repository
    python scripts/secret_scan.py /path/to/repo yourname yourlogin yourcompany      # also report these words wherever they appear

Read-only: it runs `git` and prints. Secrets are masked in the output. Standard library only.

This is a first pass, not a guarantee. The rules are the common token shapes (provider keys, private-key blocks, JWTs, URLs with credentials, generic
`secret = "..."` assignments) plus e-mail addresses, home-directory paths, phone numbers and public IP addresses. A dedicated scanner such as gitleaks
or trufflehog knows more shapes; run one as well before a public release. The test suite deliberately contains fake keys ("canaries") to prove they are
redacted: they are reported here like anything else, so read each hit.
"""
import collections
import re
import subprocess
import sys
from pathlib import Path

repo = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)
personal_words = [w.lower() for w in sys.argv[2:]]


def git(*a, binary=False):
    r = subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


RULES = {
    "anthropic key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    "openai key": r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}",
    "generic sk- token": r"\bsk-[A-Za-z0-9]{16,}",
    "aws access key id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "github token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    "google api key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
    "stripe key": r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}",
    "private key block": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY",
    "jwt": r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    "url with credentials": r"[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:[^/\s:@'\"]+@[^\s'\"]+",
    "bearer token literal": r"Bearer\s+[A-Za-z0-9_\-\.=]{24,}",
    "secret-looking assignment": r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|client[_-]?secret|auth)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
    "email address": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "home directory path": r"(?:/Users|/home)/[A-Za-z0-9._\-]+",
    "windows user path": r"[A-Za-z]:\\Users\\[A-Za-z0-9._\-]+",
    "phone number": r"(?<![\w.\-])\+?\d{1,3}[ .\-]?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?![\w\-])",
    "public ipv4": r"\b(?!(?:10|127)\.)(?!192\.168\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)(?!0\.)(?:\d{1,3}\.){3}\d{1,3}\b",
}
if personal_words:
    RULES["personal word"] = r"(?i)" + "|".join(re.escape(w) for w in personal_words)
COMPILED = {k: re.compile(v) for k, v in RULES.items()}
NOT_SECRET = ("email address", "home directory path", "windows user path", "phone number", "public ipv4", "personal word")


def mask(rule, s):
    return s if rule in NOT_SECRET or len(s) <= 12 else f"{s[:8]}...{s[-3:]} ({len(s)} chars)"


def new_findings():
    return collections.defaultdict(lambda: collections.defaultdict(list))


def scan_text(text, where, out):
    for n, line in enumerate(text.splitlines(), 1):
        for rule, rx in COMPILED.items():
            for m in rx.finditer(line):
                v = m.group(0)
                if rule == "public ipv4" and not all(int(p) <= 255 for p in v.split(".")):
                    continue
                out[rule][mask(rule, v)].append(f"{where}:{n}")


# 1. every version of every file in the history
versions = {}
for line in git("rev-list", "--all", "--objects").splitlines():
    sha, _, path = line.partition(" ")
    if path:
        versions.setdefault(sha, path)
hist, n_versions, n_binary = new_findings(), 0, 0
for sha, path in versions.items():
    if git("cat-file", "-t", sha).strip() != "blob":
        continue
    data = git("cat-file", "-p", sha, binary=True)
    n_versions += 1
    if b"\0" in data[:8000]:
        n_binary += 1
        continue
    scan_text(data.decode("utf-8", "replace"), f"{path}@{sha[:7]}", hist)

# 2. the working tree: tracked files as they are now, plus untracked files that are not ignored
tree = new_findings()
files = [f for f in git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0") if f]
for f in files:
    try:
        data = (Path(repo) / f).read_bytes()
    except OSError:
        continue
    if b"\0" not in data[:8000]:
        scan_text(data.decode("utf-8", "replace"), f, tree)

# 3. commit messages and identities
msgs, idents, n_commits = new_findings(), collections.Counter(), 0
for entry in git("log", "--all", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e").split("\x1e"):
    entry = entry.strip("\n")
    if not entry:
        continue
    h, an, ae, cn, ce, body = entry.split("\x1f", 5)
    n_commits += 1
    idents[f"author    {an} <{ae}>"] += 1
    idents[f"committer {cn} <{ce}>"] += 1
    scan_text(body, f"commit-message@{h[:7]}", msgs)


def report(title, found, limit=8):
    print(f"\n===== {title}")
    if not any(found.values()):
        print("  nothing matched any rule")
        return
    for rule in RULES:
        vals = found.get(rule)
        if not vals:
            continue
        print(f"  [{rule}] {len(vals)} distinct value(s), {sum(len(v) for v in vals.values())} occurrence(s)")
        for v, wheres in sorted(vals.items(), key=lambda kv: -len(kv[1]))[:limit]:
            names = sorted({w.rsplit(":", 1)[0].split("@")[0] for w in wheres})
            print(f"      {v!r}  x{len(wheres)}  in {', '.join(names[:4])}{' ...' if len(names) > 4 else ''}")
        if len(vals) > limit:
            print(f"      ... and {len(vals) - limit} more distinct value(s)")


print(f"repository: {repo}")
print(f"history: {n_commits} commits, {n_versions} file versions read ({n_binary} binary, not scanned); working tree: {len(files)} files")
print(f"rules: {', '.join(RULES)}")
report("HISTORY (every version of every file, all refs)", hist)
report("WORKING TREE (what is there now)", tree)
report("COMMIT MESSAGES", msgs)
print("\n===== COMMIT IDENTITIES (these become public on a push)")
for k, v in sorted(idents.items()):
    print(f"  {v:4d}  {k}")
