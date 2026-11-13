"""Privacy of the data-quality widget: row ids are keyed with a per-client secret (HMAC-SHA256, not a plain hash), the three views (internal / shared / showcase) hold back what they say they hold back,
the secret never appears anywhere, and the external report defaults to the shared view. The JS side (what is drawn) is in test_widgets_js.py."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from asof import Session
from asof.cli import main as cli_main
from asof.errors import AsOfError
from asof.privacy import digest
from asof.quality import THRESHOLDS
from asof.records import Record
from asof.widgets import element, redact, report

UTC = timezone.utc
SECRET_A = "client-a-secret-0123456789"
SECRET_B = "client-b-secret-9876543210"
VENDOR = "acme_vendor_bars"                    # a distinctive name: the shared view must not contain it anywhere


class Bars:
    """40 daily rows, one of them published three months late (a backfill the quality check flags with example row ids)."""
    def __init__(self, name):
        self.name = name

    def records(self):
        d0 = datetime(2023, 1, 2, tzinfo=UTC)
        return iter([Record(self.name, f"p{i}", d0 + timedelta(days=i), d0 + timedelta(days=i, hours=21) + (timedelta(days=90) if i == 5 else timedelta(0)), {"close": 100 + i}, order=i) for i in range(40)])


@pytest.fixture
def session(tmp_path):
    s = Session("2023-02-15T00:00:00Z", ledger_path=tmp_path / "l.db")
    s.add_source(Bars(VENDOR))
    s.add_source(Bars("second_feed"))
    yield s
    s.close()


@pytest.fixture
def merged(session):
    return report._merge(session.quality())                 # the unredacted merge: what quality.py gives, plain hashes and all


# ---------------------------------------------------------------- 1. row ids: HMAC with a per-client secret
def test_a_row_id_is_hmac_sha256_over_the_plain_hash_computed_independently():
    plain = digest("p5")
    expect = hmac.new(SECRET_A.encode(), b"asof.quality.row-id.v1\x00" + plain.encode(), hashlib.sha256).hexdigest()
    assert redact.row_id(plain, SECRET_A.encode()) == expect and len(expect) == 64
    assert expect not in (plain, hashlib.sha256(plain.encode()).hexdigest())                                  # not the plain hash, and not a hash of it either


def test_the_same_row_gets_different_ids_for_different_clients_and_the_same_id_for_one_client(merged):
    a1, a2, b = (redact.apply(merged, "internal", s)["issues"][0]["example_row_ids_hmac"] for s in (SECRET_A, SECRET_A, SECRET_B))
    assert a1 == a2 and a1 != b and a1                                                                          # one client can still link an issue to its rows; two clients cannot be joined
    assert not set(a1) & set(b)


def test_no_plain_hash_survives_in_any_view(merged):
    plains = {h for i in merged["issues"] for h in i["example_record_ids_sha256"]}
    assert plains
    for view in redact.VIEWS:
        blob = json.dumps(redact.apply(merged, view, SECRET_A))
        assert not any(h in blob for h in plains), view
        assert "example_record_ids_sha256" not in blob


def test_with_no_secret_ids_are_withheld_not_shown_unkeyed(merged, monkeypatch):
    monkeypatch.delenv(redact.ENV_SECRET, raising=False)
    d = redact.apply(merged, "internal")
    assert d["row_ids"] == "no_secret" and all("example_row_ids_hmac" not in i and "example_record_ids_sha256" not in i for i in d["issues"])


def test_the_secret_can_come_from_the_environment(merged, monkeypatch):
    monkeypatch.setenv(redact.ENV_SECRET, SECRET_A)
    assert redact.apply(merged, "internal") == redact.apply(merged, "internal", SECRET_A)
    monkeypatch.setenv(redact.ENV_SECRET, SECRET_B)
    assert redact.apply(merged, "internal") != redact.apply(merged, "internal", SECRET_A)


def test_a_short_secret_is_refused_and_the_error_does_not_contain_it(merged):
    with pytest.raises(AsOfError) as e:
        redact.apply(merged, "internal", "short-secret")
    assert "short-secret" not in str(e.value) and "at least 16" in str(e.value)


def test_the_secret_is_in_no_output_no_log_and_no_repr(session, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    w = session.show("quality-panel", secret=SECRET_A)
    out = [w.html(), repr(w), str(w.data), element("asof-quality-panel", w.data)]
    p = tmp_path / "w.html"
    w.save(p)
    out.append(p.read_text())
    assert all(SECRET_A not in o for o in out) and SECRET_A not in caplog.text
    assert any("example_row_ids_hmac" in o for o in out)                                                         # the keyed ids are there; the key is not


def test_element_scrubs_an_unkeyed_hash_list_even_if_the_caller_passes_one(merged):
    plains = [h for i in merged["issues"] for h in i["example_record_ids_sha256"]]
    h = element("asof-quality-panel", merged)
    assert plains and not any(p in h for p in plains) and "example_record_ids_sha256" not in h


# ---------------------------------------------------------------- 2. the shared view
def test_shared_aliases_sources_and_hides_lag_row_ids_and_custom_thresholds(merged):
    custom = copy.deepcopy(merged)
    for s in custom["sources"]:
        s["thresholds"] = {**THRESHOLDS, "gap_factor": 99}                                                      # a client's own tuning
    d = redact.apply(custom, "shared", SECRET_A)
    assert [s["name"] for s in d["sources"]] == ["Source A", "Source B"]                                          # sorted by real name: acme_vendor_bars, second_feed
    assert all(s["lag_seconds"] is None and s["thresholds"] == THRESHOLDS for s in d["sources"])
    assert all(i["flag"].startswith(("Source A: ", "Source B: ")) for i in d["issues"])
    assert d["view"] == "shared" and d["row_ids"] == "hidden" and d["thresholds_view"] == "defaults"
    blob = json.dumps(d)
    assert VENDOR not in blob and "second_feed" not in blob and "example_row_ids_hmac" not in blob and "lag_seconds\": {" not in blob and "99" not in json.dumps(d["sources"])


def test_shared_keeps_the_counts_and_severities(merged):
    d = redact.apply(merged, "shared")
    assert d["n_records"] == merged["n_records"] and d["by_severity"] == merged["by_severity"]
    assert [(i["severity"], i["basis"], i["n"]) for i in d["issues"]] == [(i["severity"], i["basis"], i["n"]) for i in merged["issues"]]


def test_shared_aliases_are_stable_and_run_past_z():
    assert redact.alias(0) == "Source A" and redact.alias(25) == "Source Z" and redact.alias(26) == "Source AA" and redact.alias(27) == "Source AB"
    assert redact.aliases(["b", "a", "c"]) == redact.aliases(["c", "b", "a"]) == {"a": "Source A", "b": "Source B", "c": "Source C"}


def test_internal_keeps_everything_apart_from_the_keyed_ids(merged):
    d = redact.apply(merged, "internal", SECRET_A)
    assert [s["name"] for s in d["sources"]] == [VENDOR, "second_feed"] and d["sources"][0]["lag_seconds"] == merged["sources"][0]["lag_seconds"] and d["view"] == "internal"
    strip = lambda x: [{k: v for k, v in i.items() if not k.startswith("example_")} for i in x]                      # noqa: E731
    assert strip(d["issues"]) == strip(merged["issues"])


def test_apply_does_not_change_its_input(merged):
    before = copy.deepcopy(merged)
    for view in redact.VIEWS:
        redact.apply(merged, view, SECRET_A)
    assert merged == before


def test_showcase_keeps_the_content_but_shows_only_the_generic_default_thresholds(merged):
    custom = copy.deepcopy(merged)
    custom["sources"][0]["thresholds"] = {**THRESHOLDS, "gap_factor": 99}
    d = redact.apply(custom, "showcase", SECRET_A)
    assert [s["name"] for s in d["sources"]] == [VENDOR, "second_feed"] and d["sources"][0]["lag_seconds"] is not None
    assert all(s["thresholds"] == THRESHOLDS for s in d["sources"]) and d["thresholds_view"] == "defaults" and d["row_ids"] == "hmac"


def test_an_unknown_view_is_refused(merged):
    with pytest.raises(ValueError, match="view must be one of"):
        redact.apply(merged, "public")


def test_not_recorded_stays_not_recorded_in_every_view():
    for view in redact.VIEWS:
        assert redact.apply({"recorded": False}, view) == {"recorded": False}


# ---------------------------------------------------------------- 3. the shared view of the performance widgets and the receipt (payload level: what is embedded, not only what is drawn)
FIXDIR = Path(__file__).parent / "fixtures" / "widgets"
TAGS = {"receipt": "asof-receipt", "rolling-sharpe": "asof-rolling-sharpe", "leak-overlay": "asof-leak-overlay"}


def fx(name):
    return json.loads((FIXDIR / f"{name}.json").read_text())


@pytest.mark.parametrize("name", ["receipt", "rolling-sharpe", "leak-overlay"])
def test_internal_and_showcase_return_the_data_unchanged(name):
    d = fx(name)
    for view in ("internal", "showcase"):
        assert redact.widget(TAGS[name], d, view, SECRET_A) == d


def test_internal_gap_breakdown_is_unchanged_too():
    from asof.widgets import compute
    d = compute.gap_breakdown(fx("attribution"))
    assert redact.widget("asof-gap-breakdown", d, "internal") == d and redact.widget("asof-gap-breakdown", d, "showcase") == d


def test_shared_rolling_sharpe_carries_no_raw_value_but_keeps_the_shape():
    d, s = fx("rolling-sharpe"), redact.widget("asof-rolling-sharpe", fx("rolling-sharpe"), "shared")
    raw = [p["v"] for p in d["points"] if p["v"] is not None]
    idx = [p["v"] for p in s["points"] if p["v"] is not None]
    assert len(idx) == len(raw) and min(idx) == 0.0 and max(idx) == 100.0 and s["view"] == "shared"
    blob = json.dumps(s)
    assert str(max(raw)) not in blob and str(min(raw)) not in blob
    assert [p["t"] for p in s["points"]] == [p["t"] for p in d["points"]] and [p["n"] for p in s["points"]] == [p["n"] for p in d["points"]]      # the times and the counts stay


def test_shared_sharpe_index_is_monotone_in_the_raw_value():
    d, s = fx("rolling-sharpe"), redact.widget("asof-rolling-sharpe", fx("rolling-sharpe"), "shared")
    pairs = sorted((a["v"], b["v"]) for a, b in zip(d["points"], s["points"]) if a["v"] is not None)
    assert all(y1 <= y2 for (_, y1), (_, y2) in zip(pairs, pairs[1:]))                                      # a higher raw value never gets a lower index


def test_shared_leak_overlay_keeps_the_gap_in_percent_of_range_and_no_raw_value():
    d, s = fx("leak-overlay"), redact.widget("asof-leak-overlay", fx("leak-overlay"), "shared")
    lo, hi = zip(*[(a["v"], b["v"]) for a, b in zip(d["locked"], d["unlocked"]) if a["v"] is not None and b["v"] is not None])
    vals = [*lo, *hi]
    rng = max(vals) - min(vals)
    assert s["summary"]["max_abs_gap"] == round(d["summary"]["max_abs_gap"] * 100 / rng, 1) and s["summary"]["n_points"] == d["summary"]["n_points"]
    assert all(0 <= p["v"] <= 100 for p in s["locked"] + s["unlocked"] if p["v"] is not None)
    assert [(a["v"] is None) for a in s["locked"]] == [(a["v"] is None) for a in d["locked"]]
    assert str(max(vals)) not in json.dumps(s) and str(d["summary"]["max_abs_gap"]) not in json.dumps(s)
    clear = [(a, b, ia, ib) for (a, b), (ia, ib) in zip(zip(d["locked"], d["unlocked"]), zip(s["locked"], s["unlocked"])) if a["v"] is not None and b["v"] is not None and abs(a["v"] - b["v"]) > 0.02 * rng]
    assert clear and all((a["v"] < b["v"]) == (ia["v"] < ib["v"]) for a, b, ia, ib in clear)                  # wherever the lines are clearly apart, the same one is still on top


def test_shared_gap_breakdown_has_shares_and_no_values_or_total():
    from asof.widgets import compute
    d = compute.gap_breakdown(fx("attribution"))
    s = redact.widget("asof-gap-breakdown", d, "shared")
    assert s["gap"] is None and all("value" not in c and "share" in c for c in s["components"]) and "unit" not in s
    assert abs(sum(c["share"] for c in s["components"]) - 100) < 0.5                                         # shares of the gap add up to 100 (to rounding)
    blob = json.dumps(s)
    for c in d["components"]:
        assert str(c["value"]) not in blob, c["id"]
    assert str(d["gap"]) not in blob
    assert [c["label"] for c in s["components"]] == [c["label"] for c in d["components"]] and s["reconciles"] is True


def test_shared_receipt_hides_T_and_keys_the_served_ids():
    r = fx("receipt")
    s = redact.widget("asof-receipt", r, "shared", SECRET_A)
    assert "as_of" not in s and "served_ids" not in s and s["row_ids"] == "hmac"
    assert r["as_of"] not in json.dumps(s) and not any(i in json.dumps(s) for i in r["served_ids"][:20] if i.startswith("r"))   # neither T nor a raw id is anywhere in it
    assert s["served_ids_hmac"] == [redact.row_id(digest(i), SECRET_A.encode()) for i in r["served_ids"]]
    other = redact.widget("asof-receipt", r, "shared", SECRET_B)
    assert not set(s["served_ids_hmac"]) & set(other["served_ids_hmac"])                                        # a different client gets different ids for the same rows
    assert s["served"] == r["served"] and s["blocked"] == r["blocked"] and s["ledger_hash"] == r["ledger_hash"]   # the counts and the hash stay


def test_every_keyed_id_in_the_shared_receipt_is_hmac_sha256_computed_by_hand_without_the_librarys_helpers():
    """Recomputed from the raw ids with hashlib and hmac only (the row hash is the SHA-256 of the canonical JSON of the id), for ALL served ids, for two clients."""
    r = fx("receipt")
    assert len(r["served_ids"]) == r["served"] > 100
    for secret in (SECRET_A, SECRET_B):
        shared = redact.widget("asof-receipt", r, "shared", secret)
        by_hand = [hmac.new(secret.encode(), b"asof.quality.row-id.v1\x00" + hashlib.sha256(json.dumps(i, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest().encode(), hashlib.sha256).hexdigest()
                   for i in r["served_ids"]]
        assert shared["served_ids_hmac"] == by_hand and len(set(by_hand)) == len(by_hand)
    a, b = (redact.widget("asof-receipt", r, "shared", s)["served_ids_hmac"] for s in (SECRET_A, SECRET_B))
    assert all(x != y for x, y in zip(a, b, strict=True))                                                         # the same row, a different id for every client


def test_a_receipt_id_equals_the_data_quality_panels_id_for_the_same_row():
    """One client can follow a row from the panel to the receipt, nobody else can."""
    q = redact.apply(report._merge({"returns": _fixture_source_quality()}), "internal", SECRET_A)
    key = redact.row_id(digest("r3"), SECRET_A.encode())                                           # one of the rows the fixture publishes 120 days late (every third row: LATE_EVERY in generate.py), among the report's examples
    r = redact.widget("asof-receipt", fx("receipt"), "shared", SECRET_A)
    late = next(i for i in q["issues"] if i["flag"].endswith("late_availability"))
    assert key in r["served_ids_hmac"] and key in late["example_row_ids_hmac"]


def _fixture_source_quality():
    """The real quality summary of the shared fixture's source, from the real check."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fx_generate", FIXDIR / "generate.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    from asof.quality import analyse_source
    return analyse_source(list(g.Returns().records()), now=datetime(2024, 3, 1, tzinfo=UTC))


def test_shared_receipt_without_a_secret_withholds_ids_instead_of_showing_them(monkeypatch):
    monkeypatch.delenv(redact.ENV_SECRET, raising=False)
    s = redact.widget("asof-receipt", fx("receipt"), "shared")
    assert s["row_ids"] == "no_secret" and "served_ids_hmac" not in s and "served_ids" not in s


def compute_overlay(rows):
    from asof.widgets import compute
    return compute.leak_overlay(rows, window=63)


def test_show_gives_every_widget_a_shared_view_and_leaves_internal_alone(session):
    rows = fx("returns")
    session.query(VENDOR, limit=3)                                                                              # a receipt needs a call
    internal = session.show("leak-overlay", rows=rows, window=63)
    shared = session.show("leak-overlay", rows=rows, window=63, view="shared")
    assert "view" not in internal.data and internal.data == compute_overlay(rows)                              # internal is exactly what compute gives
    assert shared.data["view"] == "shared" and shared.data["summary"]["max_abs_gap"] != internal.data["summary"]["max_abs_gap"]
    sh = session.show("rolling-sharpe", rows=rows, window=63, view="shared").data
    assert sh["view"] == "shared" and max(p["v"] for p in sh["points"] if p["v"] is not None) == 100.0
    r = session.show("receipt", view="shared", secret=SECRET_A)
    assert "as_of" not in r.data and r.data["row_ids"] in ("hmac", "none")
    g = session.show("gap-breakdown", attribution=fx("attribution"), view="shared")
    assert g.data["gap"] is None
    with pytest.raises(ValueError, match="view must be one of"):
        session.show("receipt", view="public")


# ---------------------------------------------------------------- Session.show and the external report
def test_show_passes_the_view_and_the_secret_through(session):
    shared = session.show("quality-panel", view="shared").data
    assert [s["name"] for s in shared["sources"]] == ["Source A", "Source B"]
    internal = session.show("quality-panel", secret=SECRET_A).data
    assert internal["view"] == "internal" and internal["issues"][0]["example_row_ids_hmac"]
    one = session.show("quality-panel", source=VENDOR, secret=SECRET_A).data                                      # one source's own summary is redacted too
    assert "example_record_ids_sha256" not in json.dumps(one) and one["issues"][0]["example_row_ids_hmac"]


def _ledger(tmp_path):
    p = tmp_path / "ledger.db"
    s = Session("2023-02-15T00:00:00Z", ledger_path=p)
    s.add_source(Bars(VENDOR))
    s.query(VENDOR, limit=5)
    s.close()
    return p


def test_the_report_is_internal_by_default_and_shared_when_marked_external(tmp_path, monkeypatch):
    monkeypatch.setenv(redact.ENV_SECRET, SECRET_A)
    led = _ledger(tmp_path)
    internal, external = report.build(led), report.build(led, external=True)
    NOTE = "Shared view: in the data-quality panel source names"                                                     # the report's own note (the inlined script also contains the words "Shared view")
    assert VENDOR in internal and "example_row_ids_hmac" in internal and NOTE not in internal
    quality = external.split("<asof-quality-panel ", 1)[1].split("</asof-quality-panel>", 1)[0]
    assert VENDOR not in quality and "Source A" in quality and "example_row_ids_hmac" not in quality and "lag_seconds\\\":{" not in quality
    assert NOTE in external and "not redacted" in external and "served ids are keyed" in external                                        # the report says that only this panel is redacted
    assert SECRET_A not in internal and SECRET_A not in external
    assert "example_row_ids_hmac" in report.build(led, external=True, view="internal")                           # an explicit view overrides the external default


def test_the_cli_external_flag_writes_the_shared_view(tmp_path):
    out = tmp_path / "r.html"
    assert cli_main(["report", "--ledger", str(_ledger(tmp_path)), "--html", str(out), "--external"]) == 0
    assert "Shared view: in the data-quality panel source names" in out.read_text() and "Source A" in out.read_text() and VENDOR not in out.read_text().split("<asof-quality-panel ", 1)[1].split("</asof-quality-panel>", 1)[0]
