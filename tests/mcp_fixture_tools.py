"""Tools and a provider the MCP tests load through the config file ('callable': 'mcp_fixture_tools:news')."""
from asof import ModelResponse

SECRET = "FUTURE-SECRET-ACME-SYN-MISS"
CALLS: list[dict] = []


def news(query):
    return [{"title": "ACME-SYN raises guidance", "published_at": "2023-01-20T12:00:00Z", "q": query},
            {"title": SECRET, "published_at": "2023-02-22T21:00:00Z"}]


def clocked(query, as_of):
    CALLS.append({"query": query, "as_of": as_of.isoformat()})
    return [{"title": "x", "published_at": "2023-01-20T12:00:00Z", "as_of_seen": as_of.isoformat()}]


def llm(req):
    return ModelResponse(f"echo:{req.messages[-1]['content']}")


def news_past_only(query):
    """The same tool with nothing after the clock: the control for the 'can the agent tell that later items exist?' tests."""
    return [{"title": "ACME-SYN raises guidance", "published_at": "2023-01-20T12:00:00Z", "q": query}]


SEEN_REQUESTS: list = []


def spy(req):
    """A provider that records the request it was handed, so a test can see whether anything the model sent changed the key, the address or the timeout."""
    SEEN_REQUESTS.append(req)
    return ModelResponse("ok")
