#!/usr/bin/env python3
"""A live test of the model gateway with YOUR keys: a few small real calls to Anthropic and OpenAI, with everything that is checked written to a report.

    set -a; . /path/to/keys.env; set +a          # ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENAI_TEST_MODEL (and optionally ANTHROPIC_TEST_MODEL)
    python scripts/live_test.py --out ./live-test-out

For each provider, through the gateway (so every step is a ledger event with a receipt):

  1. call      one ordinary call; the reply, the reported model version, the usage and the flat receipt.
  2. error     a real error from the provider with the real key in use (a model name that does not exist). The key must not be in the message.
  3. wrong-key a request with a key that is not yours (your key with its last six characters replaced). Providers echo part of a rejected key back;
               nothing of it may survive in the message.
  4. tools     the MCP server's own tool definitions, sent as the provider's tool format, with a prompt that asks for the `query` tool. This says whether
               the provider ACCEPTS the schemas as they are (the optional arguments are advertised as "this type or null") and whether the model then
               produces a well-formed call. OpenAI is also asked once in its strict mode; that result is reported but does not decide pass or fail.

Then: the ledger must verify, and neither key (nor the wrong key, nor the last eight characters of either) may be anywhere in the ledger file, the report
or anything this script printed.

It makes about four small requests per provider (two of them are rejected by the provider and cost nothing). Keys are read from the environment, sent to
their provider and nowhere else, and never printed. Needs the `mcp` package for step 4. Exit 0 only if every required check passed.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from asof import Ledger, Session
from asof.errors import GatewayError

NO_SUCH_MODEL = "asof-live-test-no-such-model"
OUT = io.StringIO()                                           # everything printed is also kept, so it can be searched for the keys at the end


def say(*a: Any) -> None:
    text = " ".join(str(x) for x in a)
    print(text)
    OUT.write(text + "\n")


def mcp_tools() -> list[dict[str, Any]]:
    """The tool definitions an MCP client receives from `strathora serve-mcp`, as plain dicts."""
    from mcp import Client

    from asof.mcp_server import build_server
    with tempfile.TemporaryDirectory() as d:
        s = Session("2023-01-21T14:00:00Z", ledger_path=Path(d) / "schema.db")

        async def go() -> list[dict[str, Any]]:
            async with Client(build_server(s)) as c:
                return [{"name": t.name, "description": (t.description or "").strip(), "schema": dict(t.input_schema)} for t in (await c.list_tools()).tools]
        try:
            return asyncio.run(go())
        finally:
            s.close()


def as_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]


def as_openai(tools: list[dict[str, Any]], strict: bool = False) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"], **({"strict": True} if strict else {})}} for t in tools]


def nullable_args(tools: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in tools:
        for arg, schema in t["schema"].get("properties", {}).items():
            if any(x.get("type") == "null" for x in schema.get("anyOf", [])):
                out.setdefault(t["name"], []).append(arg)
    return out


def attempt(s: Session, model: str, messages: list[dict[str, Any]], **params: Any) -> tuple[Any, str | None]:
    try:
        return s.model(model, messages=messages, **params), None
    except GatewayError as e:
        return None, str(e)


def run_provider(s: Session, provider: str, model: str, key: str, tools: list[dict[str, Any]], base_url: str | None, cap: str) -> dict[str, Any]:
    extra: dict[str, Any] = {"base_url": base_url} if base_url else {}
    ask = [{"role": "user", "content": "In one short sentence: what is a time lock for an AI agent?"}]
    res: dict[str, Any] = {"provider": provider, "model": model, "checks": {}, "notes": []}
    ok = res["checks"]

    # 1. one ordinary call
    resp, err = attempt(s, f"{provider}:{model}", ask, **{cap: 64}, **extra)
    if err and cap == "max_completion_tokens" and "max_completion_tokens" in err:
        cap = "max_tokens"
        res["notes"].append("this endpoint does not take max_completion_tokens; max_tokens was used instead")
        resp, err = attempt(s, f"{provider}:{model}", ask, **{cap: 64}, **extra)
    res["token_cap_parameter"] = cap
    ok["call succeeded"] = err is None and bool(resp and resp.text.strip())
    if resp is not None:
        rec = s.model_receipt()
        res["call"] = {"reply": resp.text.strip()[:200], "model_version": resp.model_version, "finish_reason": resp.finish_reason, "usage": dict(resp.usage), "receipt": rec}
        ok["receipt label is unknown or observed, never stronger"] = str(rec.get("label", rec.get("memory_label", ""))).lower() in ("unknown", "observed")
        say(f"  1. call: OK. reply: {resp.text.strip()[:100]!r}  version: {resp.model_version}  usage: {json.dumps(dict(resp.usage))[:160]}")
        say(f"     receipt: {json.dumps(rec, sort_keys=True, default=str)[:400]}")
    else:
        res["call"] = {"error": err}
        say(f"  1. call: FAILED: {err[:300] if err else 'empty reply'}")

    # 2. a real error with the real key in use
    _, err = attempt(s, f"{provider}:{NO_SUCH_MODEL}", ask, **{cap: 16}, **extra)
    res["error_no_such_model"] = err
    ok["a real provider error was returned as a GatewayError"] = bool(err) and "HTTP" in (err or "")
    ok["the key is not in that error"] = bool(err) and key not in err and key[-8:] not in err
    say(f"  2. error (no such model): {(err or 'NO ERROR (unexpected)')[:260]}")

    # 3. a key that is not the real one: providers echo part of it back
    wrong = key[:-6] + "ZZZZZZ"
    _, err = attempt(s, f"{provider}:{model}", ask, **{cap: 16}, api_key=wrong, **extra)
    res["error_wrong_key"] = err
    ok["a wrong key is rejected by the provider"] = bool(err) and "HTTP 401" in (err or "")
    ok["nothing of the wrong key is in that error"] = bool(err) and wrong not in err and wrong[-4:] not in err and not re.search(r"\*{4,}", err)
    say(f"  3. error (wrong key): {(err or 'NO ERROR (unexpected)')[:260]}")

    # 4. the MCP tool schemas, as this provider's tools
    want = [{"role": "user", "content": "Use the query tool to read the source named news, with a lookback of 30d. Do not answer in words; call the tool."}]
    shaped = as_anthropic(tools) if provider == "anthropic" else as_openai(tools)
    resp, err = attempt(s, f"{provider}:{model}", want, tools=shaped, **{cap: 300}, **extra)
    ok["the provider accepts the MCP tool schemas as they are"] = err is None
    calls = [dict(c) for c in (resp.tool_calls if resp is not None else ())]
    res["tools"] = {"sent": [t["name"] for t in tools], "nullable_arguments": nullable_args(tools), "error": err, "tool_calls": calls, "text": (resp.text.strip()[:200] if resp is not None else None)}
    good = [c for c in calls if c.get("name") == "query" and isinstance(c.get("arguments"), dict) and c["arguments"].get("source") == "news"]
    ok["the model produced a well-formed query call"] = bool(good)
    if err:
        say(f"  4. tools: REJECTED: {err[:400]}")
    else:
        say(f"  4. tools: accepted ({len(tools)} tools; nullable arguments: {json.dumps(nullable_args(tools))}). model's calls: {json.dumps(calls)[:300] or 'none'}")
    if provider == "openai":
        _, serr = attempt(s, f"{provider}:{model}", want, tools=as_openai(tools, strict=True), **{cap: 300}, **extra)
        res["tools"]["strict_mode"] = {"accepted": serr is None, "error": serr}
        say(f"     strict mode (reported only): {'accepted' if serr is None else 'rejected: ' + serr[:300]}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="a folder for the ledger and report.json (created if missing)")
    ap.add_argument("--anthropic-model", default=os.environ.get("ANTHROPIC_TEST_MODEL"), help="default: $ANTHROPIC_TEST_MODEL")
    ap.add_argument("--openai-model", default=os.environ.get("OPENAI_TEST_MODEL"), help="default: $OPENAI_TEST_MODEL")
    ap.add_argument("--only", choices=("anthropic", "openai"), help="test one provider")
    ap.add_argument("--anthropic-base-url", help="for testing this script against a local stand-in")
    ap.add_argument("--openai-base-url", help="for testing this script against a local stand-in")
    a = ap.parse_args()
    plan = [p for p in (("anthropic", "ANTHROPIC_API_KEY", a.anthropic_model, a.anthropic_base_url, "max_tokens"),
                        ("openai", "OPENAI_API_KEY", a.openai_model, a.openai_base_url, "max_completion_tokens")) if a.only in (None, p[0])]
    for provider, env, model, _, _ in plan:
        if not os.environ.get(env):
            print(f"live_test: {env} is not set. Nothing was sent.", file=sys.stderr)
            return 2
        if not model:
            print(f"live_test: no model for {provider}: give --{provider}-model or set {provider.upper()}_TEST_MODEL. Nothing was sent.", file=sys.stderr)
            return 2
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    ledger = out / "ledger.db"
    tools = mcp_tools()
    say(f"MCP tools to offer: {[t['name'] for t in tools]}")
    results, secrets = [], []
    with Session("2023-01-21T14:00:00Z", ledger_path=ledger, privacy="full") as s:      # full privacy mode on purpose: error text is stored readable, so a leak would be visible
        for provider, env, model, base, cap in plan:
            key = os.environ[env]
            secrets += [key, key[-8:], key[:-6] + "ZZZZZZ"]
            say(f"\n== {provider}:{model}")
            results.append(run_provider(s, provider, str(model), key, tools, base, cap))
        session_id = s.id
    led = Ledger(ledger)
    v = led.verify()
    types: dict[str, int] = {}
    for e in led.events():
        types[e.type] = types.get(e.type, 0) + 1
    final = {"ledger verifies with its anchor": bool(v.ok) and v.anchor == "ok"}
    report = {"session": session_id, "ledger": {"path": str(ledger), "events": v.n_events, "by_type": types, "head": led.head().to_dict()}, "providers": results}
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    blob = ledger.read_bytes()
    final["no key, wrong key or key ending in the ledger file"] = not any(x.encode() in blob for x in secrets)
    final["no key, wrong key or key ending in the report or the output"] = not any(x in text or x in OUT.getvalue() for x in secrets)
    report["final_checks"] = final
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    say(f"\nledger {ledger}: {v.n_events} events {json.dumps(types)}, verifies: {v.ok}, anchor: {v.anchor}")
    say("\nCHECKS")
    failed = 0
    for r in results:
        for name, passed in r["checks"].items():
            failed += not passed
            say(f"  {'PASS' if passed else 'FAIL'}  {r['provider']}: {name}")
    for name, passed in final.items():
        failed += not passed
        say(f"  {'PASS' if passed else 'FAIL'}  {name}")
    say(f"\nreport: {out / 'report.json'}")
    say(f"RESULT: {'PASS' if not failed else f'FAIL ({failed} check(s))'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
