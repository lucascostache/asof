"""The model gateway: one call shape for any provider, with everything recorded except secrets.

Model ids are ``"provider:name"``: ``anthropic:...``, ``openai:...`` or ``local:...`` (any OpenAI-compatible server, default
``http://localhost:11434/v1``, which is Ollama's address; set ``base_url=`` or ``ASOF_LOCAL_BASE_URL``). Your own provider can be added with
:meth:`ModelGateway.register`.

API keys come from the ``api_key=`` argument or the environment (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``ASOF_LOCAL_API_KEY``). They are
sent to the provider and nowhere else: never to the ledger, never in a receipt, never in an error message (R7).

The gateway cannot stop a model from using what it already knows. See :mod:`asof.contam`.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from . import modelrules
from .errors import GatewayError, GatewayRuleError
from .redaction import REDACTED, redact, register_secret, scrub

# A provider's own "masked" echo of the key in an error body, e.g. OpenAI's 401: "Incorrect API key provided: sk-proj-************wxyz" (seen live: the first 8 and the last 4
# characters of whatever was sent). The ends are real characters of the key, so the whole token is removed before the message is raised or logged (R7). This pattern needs no
# knowledge of the key; `scrub` (asof.redaction) then removes the key itself in whatever form it was repeated, because the gateway registers every key it sends.
_MASKED_KEY = re.compile(r"[A-Za-z0-9_\-]*\*{4,}[A-Za-z0-9_\-]*")
_ERROR_BODY_READ = 65536               # how much of an error body is read and cleaned
_ERROR_BODY_SHOWN = 500                # how much of it is shown, AFTER cleaning (cutting first could leave half a key that no longer looks like one)

DEFAULT_TIMEOUT = 120.0
ANTHROPIC_VERSION = "2023-06-01"
RESERVED = ("api_key", "base_url", "timeout")


@dataclass(frozen=True)
class ModelRequest:
    provider: str
    model: str                                  # without the provider prefix
    messages: tuple[Mapping[str, Any], ...]
    params: Mapping[str, Any]
    api_key: str | None = field(default=None, repr=False)         # never shown in repr, never logged
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model_version: str | None = None            # the model name the provider reports having served, if it says
    finish_reason: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    server_tools_used: tuple[str, ...] = ()     # server-side tools the PROVIDER says it ran (web search ...): they ran outside the AsOf proxy
    tool_calls: tuple[Mapping[str, Any], ...] = ()   # calls the model asks the CALLER to make: run them with Session.run_tool_calls, which sends each through the AsOf proxy
    raw_message: Mapping[str, Any] | None = None     # the assistant turn exactly as the provider returned it ({"role": "assistant", "content": ...}): in a multi-turn tool loop, append THIS
                                                     # to `messages`, unchanged. A turn rebuilt from `text` and `tool_calls` has lost its thinking blocks, which providers ask to get back with the tool results


Provider = Callable[[ModelRequest], ModelResponse]


@dataclass(frozen=True)
class GatewayPolicy:
    """What the gateway allows. Everything that can let information in from outside the AsOf clock is OFF unless an operator turns it on, and turning it on is recorded and flagged.

    ``allow_server_tools``       provider-side tools (web search, fetch, code execution, remote MCP). If allowed, every call that uses them is FLAGGED and the run cannot be called clean.
    ``allow_contributor_models`` models whose name contains "contributor" (they train on what they are sent). If allowed, the call is FLAGGED as a policy exception.
    ``allow_provider_state``     requests that rely on state the provider keeps (``previous_response_id``, stored threads, cached contexts). If allowed, the receipt cannot claim to show everything the model saw.
    ``pins``                     ``{"provider:model": "version prefix"}``: the version the provider reports having served must start with it, or the call is FLAGGED.
    """
    allow_server_tools: bool = False
    allow_contributor_models: bool = False
    allow_provider_state: bool = False
    pins: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSpec:
    """A provider that speaks the OpenAI chat-completions shape, as configuration: adding one is this plus nothing else (no adapter code)."""
    base_url: str
    key_env: str
    base_url_env: str | None = None             # an environment variable that may override ``base_url``
    verified: bool = False                      # True only once someone has run it against the live endpoint; mocked tests do not count
    note: str = ""


# Meta's Model API is OpenAI-compatible. The base URL and the model name (for example ``meta:muse-spark-1.3``) are taken from the operator's configuration and have NOT been confirmed against
# a live endpoint by this project: ``verified`` stays False, and every call to it records ``provider_verified: false`` until someone who has run it live says otherwise.
META_SPEC = ProviderSpec("https://api.meta.ai/v1", "MODEL_API_KEY", base_url_env="META_API_BASE_URL", verified=False,
                         note="UNVERIFIED: the endpoint and model names are from configuration, not from a live call made by this project")
VERIFIED_PROVIDERS = {"anthropic": False, "openai": False, "local": False}      # the tests use a local mock server. Anthropic was run live once with one model (docs/LIVE_TEST.md); one run is recorded there, not in every receipt, so the flag stays False. Kept explicit so it can be changed in one place.


def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            parsed = json.loads(r.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise GatewayError(f"the provider returned JSON that is not an object (a {type(parsed).__name__}): not a reply this gateway can read")
        return parsed
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(_ERROR_BODY_READ).decode("utf-8", "replace") if e.fp else ""
        except (OSError, ValueError):
            detail = ""
        wait = (e.headers.get("retry-after") or "").strip() if e.headers else ""           # what the provider says about when to try again (rate limits, overload)
        hint = f" (the provider says: retry after {wait} seconds)" if wait.replace(".", "", 1).isdigit() else ""
        raise GatewayError(f"the provider returned HTTP {e.code}{hint}: {_MASKED_KEY.sub(REDACTED, scrub(detail))[:_ERROR_BODY_SHOWN]}") from None
    except urllib.error.URLError as e:
        raise GatewayError(f"could not reach the provider at {_safe_url(url)}: {scrub(str(e.reason))}") from None
    except TimeoutError:
        raise GatewayError(f"the provider at {_safe_url(url)} did not answer within {timeout:g} seconds") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GatewayError(f"the provider returned something that is not JSON ({type(e).__name__} at character {getattr(e, 'pos', getattr(e, 'start', '?'))})") from None
    except (OSError, http.client.HTTPException) as e:                      # the connection dropped part-way, or the reply was not HTTP
        raise GatewayError(f"the connection to the provider at {_safe_url(url)} failed ({type(e).__name__}: {scrub(str(e))})") from None


def _clean_values(obj: Any) -> Any:
    """`scrub` every string VALUE in a provider's message, leaving its keys and structure exactly as they were (a signature under a key that merely sounds secret must survive, or the
    message could not be sent back)."""
    if isinstance(obj, Mapping):
        return {k: (v if k in _OPAQUE_FIELDS else _clean_values(v)) for k, v in obj.items()}      # opaque provider blobs are passed through byte for byte: changing one would make the turn unusable
    if isinstance(obj, (list, tuple)):
        return [_clean_values(v) for v in obj]
    return scrub(obj) if isinstance(obj, str) else obj


_OPAQUE_FIELDS = frozenset({"signature", "data", "encrypted_content", "encrypted_index"})


def _safe_url(url: str) -> str:
    p = urlsplit(url)
    host = p.hostname or ""
    return urlunsplit((p.scheme, f"{host}:{p.port}" if p.port else host, p.path, "", ""))      # no credentials, no query string


def _need_key(req: ModelRequest, env: str) -> str:
    key = req.api_key or os.environ.get(env)
    if not key:
        raise GatewayError(f"no API key for {req.provider}: pass api_key= or set {env}")
    register_secret(key)                                                    # from here on it is removed from anything written or returned, in whatever form it comes back
    return key


def anthropic(req: ModelRequest) -> ModelResponse:
    key = _need_key(req, "ANTHROPIC_API_KEY")
    system = "\n\n".join(str(m["content"]) for m in req.messages if m.get("role") == "system")
    msgs = [dict(m) for m in req.messages if m.get("role") != "system"]
    body: dict[str, Any] = {"model": req.model, "max_tokens": 1024, **req.params, "messages": msgs}      # max_tokens is required by this API
    if system:
        body["system"] = system
    data = _post((req.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages",
                 {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, body, req.timeout)
    blocks = data.get("content")
    blocks = blocks if isinstance(blocks, list) else []                    # a reply with no usable content is an empty answer, not a crash
    data = {**data, "content": blocks, "usage": data.get("usage") if isinstance(data.get("usage"), Mapping) else {}}
    text = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    return ModelResponse(text, _text_or_none(data.get("model")), _text_or_none(data.get("stop_reason")), data["usage"],
                         tuple(modelrules.server_tools_used(data)), tuple(modelrules.client_tool_calls(data)),
                         raw_message={"role": "assistant", "content": blocks})


def _text_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _openai_like(req: ModelRequest, base: str, key: str | None) -> ModelResponse:
    body = {"model": req.model, **req.params, "messages": [dict(m) for m in req.messages]}
    data = _post(base.rstrip("/") + "/chat/completions", {"Authorization": f"Bearer {key}"} if key else {}, body, req.timeout)
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
    m = choice.get("message")
    message: Mapping[str, Any] = m if isinstance(m, Mapping) else {}
    data = {**data, "choices": [choice] if choice else [], "usage": data.get("usage") if isinstance(data.get("usage"), Mapping) else {}}
    content = message.get("content")
    return ModelResponse(content if isinstance(content, str) else "", _text_or_none(data.get("model")), _text_or_none(choice.get("finish_reason")), data["usage"],
                         tuple(modelrules.server_tools_used(data)), tuple(modelrules.client_tool_calls(data)),
                         raw_message=dict(message) if message else None)


def openai(req: ModelRequest) -> ModelResponse:
    return _openai_like(req, req.base_url or "https://api.openai.com/v1", _need_key(req, "OPENAI_API_KEY"))


def local(req: ModelRequest) -> ModelResponse:
    base = req.base_url or os.environ.get("ASOF_LOCAL_BASE_URL") or "http://localhost:11434/v1"
    key = req.api_key or os.environ.get("ASOF_LOCAL_API_KEY")
    register_secret(key)
    return _openai_like(req, base, key)


def openai_compatible(spec: ProviderSpec) -> Provider:
    """An adapter for a provider that speaks the OpenAI chat-completions shape, built from configuration only. The key comes from ``spec.key_env`` (or ``api_key=``), never from a file."""
    def call(req: ModelRequest) -> ModelResponse:
        base = req.base_url or (os.environ.get(spec.base_url_env) if spec.base_url_env else None) or spec.base_url
        return _openai_like(req, base, _need_key(req, spec.key_env))
    return call


@dataclass(frozen=True)
class Inspection:
    """What a request asks for, before anything is sent (pure: no network)."""
    provider: str
    name: str
    server_tools: tuple[str, ...]
    state_params: tuple[str, ...]
    contributor: bool


class ModelGateway:
    def __init__(self, policy: GatewayPolicy | None = None) -> None:
        self.policy = policy or GatewayPolicy()
        self._providers: dict[str, Provider] = {"anthropic": anthropic, "openai": openai, "local": local, "openai_compatible": local, "meta": openai_compatible(META_SPEC)}
        self._verified: dict[str, bool] = {**VERIFIED_PROVIDERS, "openai_compatible": False, "meta": META_SPEC.verified}

    def register(self, provider: str, fn: Provider, *, verified: bool = False) -> None:
        """Add or replace a provider. ``fn(ModelRequest) -> ModelResponse``. ``verified`` is False unless someone has run it against the live endpoint."""
        self._providers[provider] = fn
        self._verified[provider] = bool(verified)

    def register_openai_compatible(self, provider: str, spec: ProviderSpec) -> None:
        """Add a provider that speaks the OpenAI chat-completions shape from configuration alone (base URL, key variable)."""
        self.register(provider, openai_compatible(spec), verified=spec.verified)

    def provider_verified(self, provider: str) -> bool:
        """Has this provider been confirmed against a live endpoint? Mocked tests do not count, so this is False until the operator says otherwise."""
        return self._verified.get(provider, False)

    def inspect(self, model: str, params: Mapping[str, Any]) -> Inspection:
        provider, name = self.split(model)
        return Inspection(provider, name, tuple(modelrules.requested_server_tools(name, params)), tuple(modelrules.state_params(params)), modelrules.is_contributor(name))

    def enforce(self, ins: Inspection) -> None:
        """Refuse, before anything is sent, what the policy does not allow."""
        p = self.policy
        if ins.contributor and not p.allow_contributor_models:
            raise GatewayRuleError("contributor_model", f"{ins.provider}:{ins.name} is refused: a model whose name contains 'contributor' trains on what it is sent. "
                                   "Set GatewayPolicy(allow_contributor_models=True) to allow it (the call will then be flagged).")
        if ins.server_tools and not p.allow_server_tools:
            raise GatewayRuleError("server_tools_off", f"server-side tools ({', '.join(ins.server_tools)}) are off: they run at the provider, outside the AsOf tool proxy, and can bring in "
                                   "information from after the clock. Route the tool through Session.add_tool, or set GatewayPolicy(allow_server_tools=True) (the run will then be flagged and cannot be called clean).")
        if ins.state_params and not p.allow_provider_state:
            raise GatewayRuleError("provider_state", f"the request relies on state kept by the provider ({', '.join(ins.state_params)}), so the receipt could not show everything the model saw. "
                                   "The gateway sends the full conversation every time; remove the parameter, or set GatewayPolicy(allow_provider_state=True).")

    def assess(self, model: str, ins: Inspection, resp: ModelResponse) -> list[dict[str, Any]]:
        """The flags a completed call earns. Each is metadata only (names and kinds), so it is safe in a hashes_only ledger."""
        flags: list[dict[str, Any]] = []
        used = list(resp.server_tools_used)
        if ins.server_tools or used:
            flags.append({"kind": "SERVER_TOOL", "requested": list(ins.server_tools), "reported_by_provider": used, "unrequested": bool(used and not ins.server_tools)})
        if ins.contributor:
            flags.append({"kind": "POLICY", "rule": "contributor_model_allowed"})
        if ins.state_params:
            flags.append({"kind": "POLICY", "rule": "provider_state_allowed", "params": list(ins.state_params)})
        pin = self.policy.pins.get(model)
        if pin is not None and not (resp.model_version or "").startswith(pin):
            flags.append({"kind": "POLICY", "rule": "version_pin", "pinned": pin, "reported": resp.model_version})
        return flags

    def providers(self) -> list[str]:
        return sorted(self._providers)

    @staticmethod
    def split(model: str) -> tuple[str, str]:
        provider, sep, name = model.partition(":")
        if not sep or not provider or not name:
            raise GatewayError(f"model must look like 'provider:name' (for example 'anthropic:claude-x'), not {model!r}")
        return provider, name

    def call(self, model: str, messages: Sequence[Mapping[str, Any]], params: Mapping[str, Any]) -> tuple[ModelResponse, ModelRequest]:
        provider, name = self.split(model)
        if provider not in self._providers:
            raise GatewayError(f"unknown provider {provider!r}; available: {self.providers()} (add one with register())")
        if not messages or not all(isinstance(m, Mapping) and "role" in m and "content" in m for m in messages):
            raise GatewayError("messages must be a non-empty list of {'role': ..., 'content': ...}")
        self.enforce(self.inspect(model, params))                      # the rules run on EVERY call, before anything leaves the process
        if params.get("stream"):                                       # refused before sending: a streamed reply arrives in pieces this gateway does not assemble, so the call would be made (and
            raise GatewayRuleError("streaming_unsupported",            # billed) and then fail to parse, with no complete response to hash for the receipt
                                   "streaming is not supported: the gateway records one whole response per call, so that its receipt covers everything the model said. Remove stream=True.")
        p = dict(params)
        register_secret(p.get("api_key"))
        req = ModelRequest(provider, name, tuple(dict(m) for m in messages), {k: v for k, v in p.items() if k not in RESERVED},
                           api_key=p.get("api_key"), base_url=p.get("base_url"), timeout=float(p.get("timeout", DEFAULT_TIMEOUT)))
        resp = self._providers[provider](req)
        if not isinstance(resp, ModelResponse):
            raise GatewayError(f"provider {provider!r} must return a ModelResponse")
        # A provider that repeats the key in its ANSWER (it should never, but nothing stops it): the caller, and through MCP the calling model, must not get it back.
        clean = replace(resp, text=scrub(resp.text), model_version=scrub(resp.model_version), finish_reason=scrub(resp.finish_reason),
                        tool_calls=tuple(redact(dict(c)) for c in resp.tool_calls), raw_message=None if resp.raw_message is None else _clean_values(resp.raw_message))
        return (clean if clean != resp else resp), req
