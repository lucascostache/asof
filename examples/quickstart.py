"""AsOf in five minutes. Run:  python examples/quickstart.py   (uses a temporary ledger, touches nothing in ~/.asof)"""
import tempfile
from pathlib import Path

from asof import ModelResponse, Session, sources

work = Path(tempfile.mkdtemp())
(work / "prices.csv").write_text(
    "symbol,date,close,available_at\n"
    "ACME-SYN,2023-01-19,148.00,2023-01-19T21:00:00Z\n"
    "ACME-SYN,2023-01-20,150.00,2023-01-20T21:00:00Z\n"
    "ACME-SYN,2023-01-23,155.00,2023-01-23T21:00:00Z\n"          # happens after the time we are pretending it is
)

s = Session(as_of="2023-01-21T14:00:00Z", ledger_path=work / "ledger.db")
s.add_source(sources.CSV(work / "prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))

rows = s.query("prices", symbol="ACME-SYN", lookback="30d")
print("closes the agent can see on Jan 21:", [r.payload["close"] for r in rows])
print(s.receipt(), end="\n\n")

s.advance_to("2023-01-24T14:00:00Z")                           # the clock only moves forward
rows = s.query("prices", symbol="ACME-SYN", lookback="30d")
print("closes the agent can see on Jan 24:", [r.payload["close"] for r in rows], end="\n\n")

# a tool: anything dated after the clock is dropped before the agent sees it
s.add_tool("news_search", lambda query: [{"title": "ACME-SYN raises guidance", "published_at": "2023-01-20T12:00:00Z"},
                                         {"title": "ACME-SYN misses (a later story)", "published_at": "2023-02-22T21:00:00Z"}], timestamp_field="published_at")
print("news:", [n["title"] for n in s.tool("news_search", query="ACME-SYN guidance")])

# a model call (a stand-in provider here; use "anthropic:...", "openai:..." or "local:..." with a key or a local server)
s.register_provider("demo", lambda req: ModelResponse("(model reply)"))
s.model("demo:model", messages=[{"role": "user", "content": "Summarise ACME-SYN."}])
print(s.receipt(), end="\n\n")                                  # note the model memory warning: AsOf does not control what a model remembers

print("ledger verifies:", bool(s.verify_ledger()), f"({s.verify_ledger().n_events} events)")
s.close()
