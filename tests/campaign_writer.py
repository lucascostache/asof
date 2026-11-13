"""A ledger writer for the crash and concurrency tests: run as a separate process, it appends until it is killed (or until it has written what it was asked to).

    python campaign_writer.py <ledger> <writer name> [<events to write> [<threads>]]

It writes single events and atomic batches of three in turn. Every event's payload says which writer and which of its writes it is, and a batch's events say how many belong together,
so a reader can tell whether anything is missing, duplicated or half-written. It prints "ready" once the ledger is open.
"""
import sys
import threading
from datetime import datetime, timezone

from asof.ledger import EventSpec, Ledger

path, name = sys.argv[1], sys.argv[2]
limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
n_threads = int(sys.argv[4]) if len(sys.argv) > 4 else 1
T = datetime(2023, 1, 21, 14, tzinfo=timezone.utc)
led = Ledger(path)
print("ready", flush=True)


def run(who: str) -> None:
    n = written = 0
    while limit is None or written < limit:
        n += 1
        if n % 2 and (limit is None or limit - written >= 3):
            led.append_many([EventSpec("crash", "batch", T, T, {"writer": who, "write": n, "part": i, "of": 3, "pad": "x" * 200}) for i in range(3)])
            written += 3
        else:
            led.append("crash", "single", event_time=T, available_at=T, payload={"writer": who, "write": n, "part": 0, "of": 1})
            written += 1


threads = [threading.Thread(target=run, args=(f"{name}.{i}",)) for i in range(n_threads)]
for t in threads:
    t.start()
for t in threads:
    t.join()
led.close()
