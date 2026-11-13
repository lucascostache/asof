"""Emit every event type once (in both privacy modes) and return {type: sorted payload keys}. SPEC.md's schema block is checked against this."""
import tempfile
from pathlib import Path

from asof import Intent, ModelResponse, Session
from asof.errors import AsOfError
from asof.gate import Policy as GatePolicy
from asof.sources import Universe


def probe(privacy):
    d = Path(tempfile.mkdtemp())
    from asof import Record
    from datetime import datetime, timezone
    U = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)      # noqa: E731

    class Src:
        name = "d"

        def records(self):
            yield Record("d", "a", U("2023-01-01T00:00:00Z"), U("2023-01-02T00:00:00Z"), {"v": 1})
            yield Record("d", "f", None, U("2030-01-01T00:00:00Z"), {"v": 2})
            yield Record("d", "u", None, None, {"v": 3}, flags=("missing_available_at",))

    s = Session("2023-01-21T14:00:00Z", ledger_path=d / "ledger.db", privacy=privacy, allow_untimestamped=False, max_as_of="2023-03-01T00:00:00Z", decision_gate=True)
    s.add_source(Src())
    s.add_tool("t", lambda: [{"timestamp": "2023-01-01T00:00:00Z"}, {"timestamp": "2030-01-01T00:00:00Z"}, {"x": 1}])
    s.add_tool("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    s.register_provider("fake", lambda req: ModelResponse("ok"))
    s.register_provider("bad", lambda req: (_ for _ in ()).throw(RuntimeError("x")))
    s.add_source(Universe([{"symbol": 1, "action": "add", "effective": "2022-01-01", "announced": "2022-01-01"}], name="u", assume_tz="UTC"))
    s.query("d", v=1)
    s.query("d", universe=("u", "v"))                                                  # a query restricted to a universe carries the optional `universe` key (SPEC 4.5)
    s.tool("t")
    for fn in (lambda: s.tool("boom"), lambda: s.model("bad:m", messages=[{"role": "user", "content": "q"}]),
               lambda: s.model("fake:contributor-x", messages=[{"role": "user", "content": "q"}])):          # a gateway rule refusal: its model_error carries the `gateway` block (SPEC 4.8)
        try:
            fn()
        except Exception:
            pass
    s.model("fake:m", messages=[{"role": "user", "content": "q"}], temperature=0)
    for fn in (lambda: s.advance_to("2023-01-22T00:00:00Z"), lambda: s.decision("hold", why="x"), lambda: s.advance_to("2023-01-22T00:00:00Z"),
               lambda: s.advance_to("2023-01-01T00:00:00Z"), lambda: s.advance_to("2031-01-01T00:00:00Z"), lambda: s.refuse_clock_move("policy")):
        try:
            fn()
        except AsOfError:
            pass
    s.query("d")
    g = s.gate(Intent("i1", "x", "buy", 1, price=1.0, model="fake:m"), GatePolicy())                  # an intent and its gate decision (SPEC 4.9): the events `intent` and `gate_decision`
    s.order("o1", symbol="x", side="buy", size=1, expected_price=1.0, intent=g.call_id)               # execution events (SPEC 4.6), linked to the approved intent
    s.fill("o1", price=1.0, size=1)
    s.mark("x", size=1, price=1.0)
    try:
        s.advance_to("2023-01-23T00:00:00Z")
    except AsOfError:
        pass
    # a failing source
    class Bad:
        name = "bad"

        def records(self):
            raise RuntimeError("boom")
    try:
        s.add_source(Bad())
    except RuntimeError:
        pass
    s.close()
    keys = {}
    for e in s.events():
        keys.setdefault(e.type, set()).update(e.data().keys())
    return {k: sorted(v) for k, v in sorted(keys.items())}


if __name__ == "__main__":
    import json
    print(json.dumps({"full": probe("full"), "hashes_only": probe("hashes_only")}, indent=1))
