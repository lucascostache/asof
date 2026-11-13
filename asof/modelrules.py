"""The gateway's rules, as pure functions of a request and a response (no network, no clock, nothing recorded here).

These answer five questions about a model call:

1. Is a **server-side tool** involved (web search, fetch, code execution, remote MCP, file search)? Those run at the provider, outside the AsOf tool proxy, so they can bring in information from
   after the clock. By default they are refused; if an operator allows them, the call is flagged and the run cannot be called clean.
2. Is the model one that **trains on your inputs** (its name contains ``contributor``)? Refused unless explicitly allowed.
3. Does the request lean on **provider-side conversation state** (``previous_response_id``, a stored thread, a cached context)? Then the receipt could not say everything the model saw. Refused: the gateway sends
   the full context every time.
4. What were the **tokens and reasoning settings**, in one shape for every provider?
5. What **tool calls** did the model ask the *caller* to make (those must go through the AsOf proxy) and which tools did the *provider* run itself?

Detection is by name and shape, from what each provider documents today. It is a safety net, not a proof: a provider that runs a tool without saying so cannot be caught here, which is why the receipt says
what the provider reported and the README says what this does not prove.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

# a tool entry whose "type" starts with one of these runs at the provider (or calls out to a remote server), not in the caller's process
SERVER_TOOL_TYPES = ("web_search", "web_fetch", "browser", "code_execution", "code_interpreter", "file_search", "mcp", "image_generation", "url_context", "google_search", "x_search",
                     "retrieval", "grounding")
# typed tools that the CALLER runs (Anthropic's bash, text editor and computer-use tools return a request that the caller carries out), so they are not server-side
CLIENT_TYPED_PREFIXES = ("bash", "text_editor", "computer", "memory")
# request parameters that switch a provider's built-in search or grounding on
SERVER_TOOL_PARAMS = ("web_search_options", "search_parameters", "enable_search", "google_search", "grounding", "web_search", "use_search", "search")
# model names that search by themselves, whatever tools are listed
SEARCHING_MODEL_MARKERS = ("search-preview", "sonar", ":online", "-online")
# request parameters that point at state kept by the provider
STATE_PARAMS = ("previous_response_id", "conversation", "conversation_id", "thread_id", "cached_content", "container", "context_id")
CLIENT_TOOL_TYPES = (None, "", "function", "custom")


def _truthy(v: Any) -> bool:
    return bool(v) and v != "off" and v != "none"


def requested_server_tools(model: str, params: Mapping[str, Any]) -> list[str]:
    """The server-side tools this request asks for, as short names (``web_search``, ``mcp`` ...), in a stable order. Empty when there are none."""
    found: list[str] = []
    tools = params.get("tools")
    if isinstance(tools, (list, tuple)):
        for t in tools:
            if isinstance(t, Mapping):
                ty = t.get("type")
                if ty in CLIENT_TOOL_TYPES or any(str(ty).startswith(p) for p in CLIENT_TYPED_PREFIXES):
                    continue                                    # a plain function, or a typed tool the caller itself carries out
                found.append(str(ty))                           # any other provider-defined type (known ones are in SERVER_TOOL_TYPES) is run by the provider
    for k in SERVER_TOOL_PARAMS:
        if _truthy(params.get(k)):
            found.append(k)
    name = model.lower()
    for m in SEARCHING_MODEL_MARKERS:
        if m in name:
            found.append(f"model:{m}")
    return sorted(set(found))


def state_params(params: Mapping[str, Any]) -> list[str]:
    """Parameters that ask the provider to keep or reuse conversation state. ``store=True`` counts (the completion is kept at the provider); ``store=False`` does not."""
    out = [k for k in STATE_PARAMS if _truthy(params.get(k))]
    if params.get("store") is True:
        out.append("store")
    return sorted(out)


def is_contributor(model_name: str) -> bool:
    """A model whose name says it is a *contributor* tier trains on what it is sent. Matched on the model name only, case-insensitively."""
    return "contributor" in model_name.lower()


def reasoning_effort(params: Mapping[str, Any]) -> str | None:
    """The reasoning setting as a short string, from whichever form the provider uses (``reasoning_effort``, ``reasoning: {effort}``, ``thinking: {budget_tokens}``). ``None`` if none was set."""
    e = params.get("reasoning_effort")
    if isinstance(e, str) and e:
        return e
    r = params.get("reasoning")
    if isinstance(r, Mapping) and isinstance(r.get("effort"), str):
        return str(r["effort"])
    t = params.get("thinking")
    if isinstance(t, Mapping) and t.get("type") in ("enabled", "adaptive"):
        b = t.get("budget_tokens")
        return f"budget:{b}" if isinstance(b, int) else str(t.get("type"))
    return None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def normalise_usage(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Tokens in one shape for every provider: ``input``, ``output``, ``cached`` (read from a prompt cache), ``reasoning``. ``None`` means the provider did not say, which is not the same as 0."""
    u = usage or {}
    inp = _int(u.get("input_tokens", u.get("prompt_tokens")))
    out = _int(u.get("output_tokens", u.get("completion_tokens")))
    cached = _int(u.get("cache_read_input_tokens"))
    if cached is None:
        d = u.get("prompt_tokens_details") or u.get("input_tokens_details")
        cached = _int(d.get("cached_tokens")) if isinstance(d, Mapping) else None
    reasoning = _int(u.get("reasoning_tokens"))
    if reasoning is None:
        d = u.get("completion_tokens_details") or u.get("output_tokens_details")
        reasoning = _int(d.get("reasoning_tokens")) if isinstance(d, Mapping) else None
    return {"input": inp, "output": out, "cached": cached, "reasoning": reasoning}


def server_tools_used(data: Mapping[str, Any]) -> list[str]:
    """Server-side tools the provider *reports having run*, read from a raw response body (Anthropic content blocks and usage, OpenAI annotations and Responses-style output items)."""
    found: set[str] = set()
    for b in data.get("content") or []:                                          # Anthropic
        if isinstance(b, Mapping):
            ty = str(b.get("type", ""))
            if ty == "server_tool_use":
                found.add(str(b.get("name") or "server_tool"))
            elif ty.endswith("_tool_result") and ty != "tool_result":
                found.add(ty[: -len("_tool_result")])
    st = (data.get("usage") or {}).get("server_tool_use") if isinstance(data.get("usage"), Mapping) else None
    if isinstance(st, Mapping):
        for k, v in st.items():
            if isinstance(v, int) and v > 0:
                found.add(k.replace("_requests", ""))
    for item in data.get("output") or []:                                        # OpenAI Responses-style
        if isinstance(item, Mapping) and str(item.get("type", "")).endswith("_call") and item.get("type") != "function_call":
            found.add(str(item["type"])[: -len("_call")])
    for ch in data.get("choices") or []:                                         # OpenAI chat completions
        msg = ch.get("message") if isinstance(ch, Mapping) else None
        if isinstance(msg, Mapping):
            if any(isinstance(a, Mapping) and a.get("type") == "url_citation" for a in msg.get("annotations") or []):
                found.add("web_search")
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, Mapping) and tc.get("type") not in CLIENT_TOOL_TYPES:
                    found.add(str(tc.get("type")))
    return sorted(found)


def client_tool_calls(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Calls the model asks the CALLER to make (``{"id", "name", "arguments"}``). Each must be run through the AsOf tool proxy; the gateway never runs a tool itself."""
    out: list[dict[str, Any]] = []
    for b in data.get("content") or []:                                          # Anthropic
        if isinstance(b, Mapping) and b.get("type") == "tool_use":
            out.append({"id": b.get("id"), "name": b.get("name"), "arguments": b.get("input") if isinstance(b.get("input"), Mapping) else {}})
    for ch in data.get("choices") or []:                                         # OpenAI chat completions
        msg = ch.get("message") if isinstance(ch, Mapping) else None
        for tc in (msg.get("tool_calls") or []) if isinstance(msg, Mapping) else []:
            fn = tc.get("function") if isinstance(tc, Mapping) else None
            if isinstance(fn, Mapping) and tc.get("type", "function") == "function":
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {"_unparsed": args}
                out.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args if isinstance(args, Mapping) else {}})
    return out
