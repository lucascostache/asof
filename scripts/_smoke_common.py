"""Shared by the smoke scripts. These make REAL calls with YOUR keys: AsOf's author has never run them against the live services."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from asof import Ledger, Session


def fail(msg: str, code: int = 2) -> "None":
    print(f"smoke: {msg}", file=sys.stderr)
    raise SystemExit(code)


def run_model_smoke(provider: str, key_env: str, args) -> int:
    """ONE real call, then the receipt and the ledger check. Exit 0 only if every check below passes; any failure is printed, and the exit code is 1 (2 for a usage error)."""
    import os
    if not os.environ.get(key_env) and not args.api_key_from_env_is_optional:
        fail(f"set {key_env} first. This script makes exactly ONE real call to {provider} and prints the receipt; nothing else is sent.")
    if not args.model:
        fail(f"give --model (for example --model {provider}-model-name). AsOf does not choose a model for you: names and availability change.")
    ledger = Path(args.ledger).expanduser() if args.ledger else Path(tempfile.mkdtemp(prefix="asof-smoke-")) / "ledger.db"
    extra = {"base_url": args.base_url} if args.base_url else {}
    problems: list[str] = []
    reply = receipt = None
    with Session(args.as_of, ledger_path=ledger, privacy=args.privacy) as s:
        try:
            reply = s.model(f"{provider}:{args.model}", messages=[{"role": "user", "content": args.prompt}], max_tokens=args.max_tokens, **extra)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the script must say so and exit non-zero (AsOf's own errors never contain the key)
            problems.append(f"the model call failed: {type(e).__name__}: {str(e)[:300]}")
        try:
            receipt = s.receipt()
        except Exception as e:  # noqa: BLE001
            problems.append(f"no receipt could be built: {type(e).__name__}: {str(e)[:200]}")
    if reply is not None:
        print("model reply:", reply.text.strip()[:300] or "(empty)")
        print("reported model version:", reply.model_version, "| finish:", reply.finish_reason, "| usage:", json.dumps(dict(reply.usage)))
        if not reply.text.strip():
            problems.append("the model returned an empty reply")
    print()
    if receipt is not None:
        print(receipt)
        if receipt.kind != "model" or not receipt.model:
            problems.append(f"the last receipt is a {receipt.kind} receipt, not a model receipt")
        elif receipt.model.get("label") not in ("unknown", "observed"):
            problems.append(f"the model memory label is {receipt.model.get('label')!r}; it must be 'unknown' or 'observed', never stronger")
    print()
    led = Ledger(ledger)
    v = led.verify()
    print(f"ledger {ledger}\n  verifies: {v.ok} ({v.n_events} events, tail anchor: {v.anchor})" + ("" if v.ok else f"\n  REASON: {v.reason}"))
    if not v.ok:
        problems.append(f"the ledger does not verify: {v.reason}")
    elif v.anchor != "ok":
        problems.append(f"the tail anchor check is {v.anchor!r}, not 'ok'")
    key = os.environ.get(key_env, "")
    leaked = bool(key) and key.encode() in ledger.read_bytes()
    print(f"  API key present in the ledger file: {leaked}  (must be False)")
    if leaked:
        problems.append("the API key is in the ledger file")
    print(f"  head: {json.dumps(led.head().to_dict())}")
    if receipt is not None:
        print(f"  show it again later with: python -m asof receipt {receipt.call_id} --ledger {ledger}")
    if problems:
        print(f"\nRESULT: FAIL ({len(problems)} problem{'s' if len(problems) != 1 else ''})", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("\nRESULT: PASS (one call made, receipt printed, ledger verifies, key not stored)")
    return 0


def add_model_args(ap) -> None:
    ap.add_argument("--model", help="the model name WITHOUT the provider prefix")
    ap.add_argument("--as-of", default="2023-01-21T14:00:00Z", help="the session clock (a time zone is required)")
    ap.add_argument("--privacy", choices=("full", "hashes_only"), default="full")
    ap.add_argument("--prompt", default="In one sentence: what is a time lock for an AI agent?")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--ledger", help="ledger file (default: a temporary one, so ~/.asof is untouched)")
    ap.add_argument("--base-url", help="override the API base URL (for example a local mock)")
    ap.set_defaults(api_key_from_env_is_optional=False)
