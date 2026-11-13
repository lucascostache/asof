"""The web-search leak, side by side: the same question asked (a) straight to a provider with its own server-side search ON, and (b) through the AsOf gateway.

    python scripts/leak_demo.py                       # offline: a local MOCK provider, SYNTHETIC question. No network, no key.
    python scripts/leak_demo.py --live --provider anthropic --model MODEL                      # REAL calls (billable). Needs ANTHROPIC_API_KEY.
    python scripts/leak_demo.py --live --provider openai --model gpt-4o-search-preview --plain-model gpt-4o    # needs OPENAI_API_KEY

It prints, in order: (a) the direct answer, which can contain an event dated after T, and the provider's report that it searched; (b1) the same request through the gateway, REFUSED before anything is sent;
(b2) the same question through the gateway with search off, and its receipt; (b3) what an operator who allows search would get: the same leak, but recorded, with the receipt FLAGGED and the run not clean.

Offline, the "provider" is a tiny local server that behaves the way a searching provider does (it answers from a post-T article when a search tool is requested, and says it does not know otherwise). That shows the
mechanism; it proves nothing about any real provider. Only --live talks to one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from asof import GatewayPolicy, GatewayRuleError, Ledger, ModelGateway, Session
from asof import modelrules as R
from asof import pitfin as P

ROOT = Path(__file__).resolve().parents[1]
LEAK = "Acme Synthetic Corp reported fourth-quarter 2022 revenue of $412 million on 2023-02-02."
NOPE = "I do not have information about that as of 2023-01-20."


def _asks_for_search(body: dict) -> bool:
    return bool(R.requested_server_tools(str(body.get("model", "")), body))


def mock_provider() -> tuple[str, list[dict], threading.Thread, HTTPServer]:
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):                                                # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "search": _asks_for_search(body)})
            if _asks_for_search(body):
                out = {"model": "mock-anthropic-1", "stop_reason": "end_turn", "usage": {"input_tokens": 30, "output_tokens": 25, "server_tool_use": {"web_search_requests": 1}},
                       "content": [{"type": "server_tool_use", "name": "web_search", "input": {"query": "Acme Synthetic Corp Q4 2022 revenue"}}, {"type": "web_search_tool_result", "content": []},
                                   {"type": "text", "text": LEAK}]}
            else:
                out = {"model": "mock-anthropic-1", "stop_reason": "end_turn", "usage": {"input_tokens": 30, "output_tokens": 12}, "content": [{"type": "text", "text": NOPE}]}
            raw = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):                                        # noqa: D401
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_port}", seen, t, srv


def direct(provider: str, base: str | None, key: str, model: str, question: str, search: bool) -> dict:
    """A request straight to the provider with NO AsOf in between (that is the point of (a)). Standard library only."""
    if provider == "anthropic":
        url = (base or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        body: dict = {"model": model, "max_tokens": 512, "messages": [{"role": "user", "content": question}]}
        if search:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url = (base or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": question}]}
        if search:
            body["web_search_options"] = {}
        headers = {"Authorization": f"Bearer {key}"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"the provider returned HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None


def text_of(provider: str, data: dict) -> str:
    if provider == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def line(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the web-search leak, direct vs through the gateway")
    ap.add_argument("--live", action="store_true", help="REAL, billable provider calls. Without it a local mock provider is used and nothing leaves this machine.")
    ap.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic", help="with --live: which provider")
    ap.add_argument("--model", default=None, help="with --live: the model (for openai, a search-capable one such as gpt-4o-search-preview)")
    ap.add_argument("--plain-model", default=None, help="with --live and openai: a model without built-in search for (b2)")
    ap.add_argument("--questions", default=str(ROOT / "examples" / "pitfin" / "sample_questions.json"))
    ap.add_argument("--question-id", default="syn-01")
    a = ap.parse_args(argv)

    qs = {q.id: q for q in P.load_questions(a.questions)}
    if a.question_id not in qs:
        print(f"no question {a.question_id!r} in {a.questions}", file=sys.stderr)
        return 2
    q = qs[a.question_id]
    provider, key, base, srv = a.provider, "mock-key", None, None
    model = a.model or "mock-model"
    plain = a.plain_model or model
    if a.live:
        env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        key = os.environ.get(env, "")
        if not key or not a.model:
            print(f"--live needs --model and {env} in the environment. Nothing was called.", file=sys.stderr)
            return 2
        if provider == "openai" and not a.plain_model:
            print("--live with openai needs --plain-model (a model without built-in search) for the gateway run. Nothing was called.", file=sys.stderr)
            return 2
        print(f"LIVE: this makes real, billable calls to {provider} (about 3). The question is: {q.question}")
    else:
        base, seen, _, srv = mock_provider()
        print("OFFLINE DEMO with a local MOCK provider and a SYNTHETIC question: it shows the mechanism, not any real provider's behaviour.")
    print(f"\nQuestion (T = {q.as_of}): {q.question}\nThe true answer is an event dated {q.event_date}, AFTER T: a correct answer means information from after T got in.")

    def leaks(text: str) -> str:
        return f"mentions a date or fact after T: {P.mentions_future(text, q.as_of, q.later_facts)};  matches the true answer: {P.grade(q.answer, text)}"

    # (a) direct, search ON
    line("(a) DIRECT to the provider, server-side search ON (no AsOf)")
    data = direct(provider, base, key, model, q.question, True)
    ans = text_of(provider, data)
    print(f"request tools/params asked for: {R.requested_server_tools(model, {'tools': [{'type': 'web_search_20250305'}]} if provider == 'anthropic' else {'web_search_options': {'x': 1}})}")
    print(f"answer: {ans}\nprovider says it ran: {R.server_tools_used(data) or 'nothing reported'}\n-> {leaks(ans)}\nNothing in the answer says when its information is from, and no record exists of what was searched.")

    # (b1) gateway, same request
    line("(b1) THROUGH THE GATEWAY, the same request (search ON)")
    tmp = Path(tempfile.mkdtemp(prefix="asof-leak-demo-"))
    gw_default, ledger = ModelGateway(), Ledger(tmp / "ledger.db")
    s = Session(q.as_of, ledger=ledger, gateway=gw_default)
    tools_param = {"tools": [{"type": "web_search_20250305", "name": "web_search"}]} if provider == "anthropic" else {"web_search_options": {}}
    before = len(seen) if not a.live else None
    try:
        s.model(f"{provider}:{model}", messages=[{"role": "user", "content": q.question}], api_key=key, base_url=base, **({"web_search_options": {"search_context_size": "low"}} if provider == "openai" else tools_param))
        print("UNEXPECTED: the gateway let a server-side search through")
        return 1
    except GatewayRuleError as e:
        print(f"REFUSED by the gateway (rule {e.rule}):\n  {e}")
        if before is not None:
            print(f"requests that reached the provider because of it: {len(seen) - before}")
        print(f"it is a ledger event: {[ (x.type, x.data().get('gateway', {}).get('rule')) for x in ledger.events(session_id=s.id, type='model_error') ]}")

    # (b2) gateway, search off
    line("(b2) THROUGH THE GATEWAY, the same question with search off (the default)")
    r = s.model(f"{provider}:{plain}", messages=[{"role": "user", "content": q.question}], api_key=key, base_url=base)
    print(f"answer: {r.text}\n-> {leaks(r.text)}")
    print("receipt (hashes and counts, never the text):")
    print(json.dumps(s.model_receipt(), indent=2, sort_keys=True, default=str))
    print(f"run clean: {s.clean}")

    # (b3) gateway, search allowed by an operator
    line("(b3) WHAT AN OPERATOR WHO ALLOWS SEARCH GETS: the same leak, recorded and FLAGGED")
    ledger2 = Ledger(tmp / "ledger-allowed.db")
    s2 = Session(q.as_of, ledger=ledger2, gateway=ModelGateway(GatewayPolicy(allow_server_tools=True)))
    r2 = s2.model(f"{provider}:{model}", messages=[{"role": "user", "content": q.question}], api_key=key, base_url=base, **tools_param)
    print(f"answer: {r2.text}\n-> {leaks(r2.text)}")
    rec = s2.model_receipt()
    print(f"receipt label: {rec['label']}   flags: {json.dumps(rec['flags'])}   server tools used: {rec['server_tools_used']}\nrun clean: {s2.clean}  (a report cannot call this run clean)")
    print(f"\nLedgers (verify with `strathora verify --ledger <file>`): {tmp / 'ledger.db'}  {tmp / 'ledger-allowed.db'}")
    ledger.close()
    ledger2.close()
    if srv:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
