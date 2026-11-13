"""The MCP server's stop handling at the moments CI found: a stop that arrives while the server is still being built (GitHub Actions, Python 3.10 on macOS: a SIGHUP landed inside
pydantic and printed a traceback; elsewhere the default action killed the process before the handlers were installed) must still close the session, with exit 0 and no traceback.
These run `main()` in this process and send the signal at an exact point, so they do not depend on timing."""
import json
import os
import signal
import sys
import time

import pytest

pytest.importorskip("mcp")
from asof import mcp_server                                      # noqa: E402
from asof.ledger import Ledger                                  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP")


@pytest.fixture(autouse=True)
def keep_this_processs_handlers():
    saved = {n: signal.getsignal(getattr(signal, n)) for n in SIGNALS}
    yield
    for n, h in saved.items():
        signal.signal(getattr(signal, n), h)


def cfg(tmp_path):
    (tmp_path / "prices.csv").write_text("symbol,date,close,available_at\nACME-SYN,2023-01-19,148.0,2023-01-19T21:00:00Z\n")
    p = tmp_path / "asof.config.json"
    p.write_text(json.dumps({"as_of": "2023-01-21T14:00:00Z", "ledger_path": "ledger.db", "sources": [
        {"type": "csv", "path": "prices.csv", "event_time": "date", "available_at": "available_at", "key": ["symbol", "date"], "assume_tz": "UTC"}]}))
    return p


def types(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    try:
        assert led.verify().ok
        return [e.type for e in led.events()]
    finally:
        led.close()


@pytest.mark.parametrize("name", SIGNALS)
def test_a_stop_while_the_server_is_being_built_closes_the_session_and_never_starts_serving(tmp_path, monkeypatch, name):
    real, ran = mcp_server.build_server, []

    def building(session, policy):
        os.kill(os.getpid(), getattr(signal, name))             # the stop lands mid-setup, as on CI
        server = real(session, policy)
        monkeypatch.setattr(server, "run", lambda *a, **k: ran.append(a))
        return server
    monkeypatch.setattr(mcp_server, "build_server", building)
    assert mcp_server.main(["--config", str(cfg(tmp_path))]) == 0
    assert ran == []                                             # the remembered stop is honoured where serving would begin
    assert types(tmp_path) == ["session_start", "source_added", "source_loaded", "session_end"]


@pytest.mark.parametrize("name", SIGNALS)
def test_a_stop_while_serving_closes_the_session_and_a_second_stop_cannot_interrupt_the_close(tmp_path, monkeypatch, name):
    real = mcp_server.build_server

    def building(session, policy):
        server = real(session, policy)
        close = session.close

        def closing():
            os.kill(os.getpid(), getattr(signal, name))         # a second stop, while session_end is being written
            close()
        monkeypatch.setattr(session, "close", closing)

        def serving(*a, **k):
            os.kill(os.getpid(), getattr(signal, name))
            time.sleep(5)                                        # the handler's SystemExit ends this
            raise AssertionError("the stop did not end serving")
        monkeypatch.setattr(server, "run", serving)
        return server
    monkeypatch.setattr(mcp_server, "build_server", building)
    t = time.monotonic()
    assert mcp_server.main(["--config", str(cfg(tmp_path))]) == 0
    assert time.monotonic() - t < 4
    assert types(tmp_path) == ["session_start", "source_added", "source_loaded", "session_end"]
