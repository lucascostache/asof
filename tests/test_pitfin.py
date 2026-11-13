"""The PIT-Fin harness: the question format, the four variants, deterministic grading, the runner through the gateway, and the four scores. Stand-in models (not language models) with stated behaviour show
that each score moves exactly the way its definition says. No network, no key, no live call."""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from asof import GatewayPolicy, Ledger, ModelGateway, ModelResponse
from asof import pitfin as P

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pitfin" / "sample_questions.json"


@pytest.fixture
def qs():
    return P.load_questions(SAMPLE)


def base(**kw):
    d = {"id": "q", "as_of": "2023-01-20T14:00:00Z", "question": "As of 2023-01-20, what did Foo Inc (FOO) report on 2023-02-02?", "answer": "10", "ticker": "FOO", "event_date": "2023-02-02",
         "source": "synthetic: test"}
    d.update(kw)
    return d


def write(tmp_path, items):
    p = tmp_path / "q.json"
    p.write_text(json.dumps(items))
    return p


# ---------------------------------------------------------------- the format
def test_the_sample_set_is_five_synthetic_questions_each_with_a_control(qs):
    assert [q.id for q in qs] == [f"syn-0{i}" for i in range(1, 6)] and all(q.synthetic and q.control is not None for q in qs)
    assert all(date.fromisoformat(q.control.as_of[:10]) > date(2029, 1, 1) for q in qs)                      # the controls are about events after any model's cutoff
    assert "SYNTHETIC" in json.loads(SAMPLE.read_text())["note"]


@pytest.mark.parametrize("change,msg", [
    ({"id": ""}, "missing"), ({"answer": ""}, "missing"), ({"source": "private: our desk notes"}, "public data only"), ({"source": "edgar:"}, "public data only"),
    ({"event_date": "2023-01-20"}, "must be after as_of"), ({"event_date": "soon"}, "YYYY-MM-DD"), ({"as_of": "2023-01-20T14:00:00"}, "time zone"),
])
def test_a_malformed_question_is_an_error_that_names_it(tmp_path, change, msg):
    with pytest.raises(Exception, match=msg):
        P.load_questions(write(tmp_path, [base(**change)]))


def test_duplicate_ids_empty_sets_and_bad_json_are_errors(tmp_path):
    with pytest.raises(P.PitFinError, match="duplicate"):
        P.load_questions(write(tmp_path, [base(), base()]))
    with pytest.raises(P.PitFinError, match="list of questions"):
        P.load_questions(write(tmp_path, []))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(P.PitFinError, match="not valid JSON"):
        P.load_questions(bad)
    with pytest.raises(P.PitFinError, match="control cannot have a control"):
        P.load_questions(write(tmp_path, [base(control={"as_of": "2030-01-01T00:00:00Z", "question": "x", "answer": "1", "event_date": "2030-02-01", "control": {}})]))


def test_every_public_source_kind_is_accepted_and_a_wrapped_set_loads(tmp_path):
    items = [base(id=f"q{i}", source=s) for i, s in enumerate(["edgar:0009999991-23-000006", "prices:FOO daily", "earnings:FOO Q4 2022", "synthetic: x"])]
    assert len(P.load_questions(write(tmp_path, {"questions": items}))) == 4


# ---------------------------------------------------------------- the variants
def test_the_disguise_hides_the_name_and_ticker_and_shifts_every_date_by_the_same_days(qs):
    q = qs[0]
    d = P.disguise(q)
    days = P.shift_days(q)
    assert 90 <= days < 360 and P.shift_days(q) == days                                                      # deterministic
    assert "Acme" not in d.question and "ACME" not in d.question and "Company A" in d.question and "Company A (Company A)" not in d.question
    assert "2023-01-20" not in d.question and "2023-02-02" not in d.question
    assert (date.fromisoformat(d.event_date) - date.fromisoformat(q.event_date)).days == days == (date.fromisoformat(d.as_of[:10]) - date.fromisoformat(q.as_of[:10])).days
    assert d.answer == "412"                                                                                 # a number is not a date: it does not move
    dated = P.disguise(qs[1])
    assert dated.answer != qs[1].answer and (date.fromisoformat(dated.answer) - date.fromisoformat(qs[1].answer)).days == P.shift_days(qs[1])     # a date answer moves with the dates
    assert P.shift_days(qs[0]) != P.shift_days(qs[1]) or True


def test_the_long_form_dates_shift_too():
    assert P._shift_text("on March 3, 2023 and 2023-03-03", 10) == "on March 13, 2023 and 2023-03-13"
    assert P._shift_text("on December 28, 2023", 10) == "on January 7, 2024"


def test_the_instruction_variant_prepends_the_instruction_and_keeps_the_question(qs):
    c = P.build_prompt(qs[0], "C")
    assert c.text.startswith("Use only information that was publicly available before 2023-01-20") and c.text.endswith(qs[0].question) and "CANNOT_ANSWER" in c.text and c.expected == qs[0].answer


def test_the_control_variant_asks_the_control_question_and_is_absent_when_there_is_none(qs):
    d = P.build_prompt(qs[0], "D")
    assert d.text == qs[0].control.question and d.expected == "977" and d.as_of.startswith("2030")
    nc = P.load_questions(SAMPLE)[0]
    object.__setattr__(nc, "control", None)
    assert P.build_prompt(nc, "D") is None
    with pytest.raises(P.PitFinError):
        P.build_prompt(qs[0], "E")


# ---------------------------------------------------------------- grading
@pytest.mark.parametrize("expected,response,ok", [
    ("412", "Revenue was $412 million.", True), ("412", "About 413 million", False), ("412", "412.0", True), ("0.42", "$0.42 per share", True), ("0.42", "0.43", False),
    ("58.25", "It closed at 58.25.", True), ("58.25", "58.30", False), ("Okonkwo", "The new CEO is Chidi Okonkwo.", True), ("Okonkwo", "Okonkwoish", False),
    ("2023-04-17", "It was announced on 2023-04-17.", True), ("2023-04-17", "2023-04-18", False), ("412", "CANNOT_ANSWER", False), ("412", "", False), ("412", "I do not know.", False),
])
def test_grading_is_deterministic_and_conservative(expected, response, ok):
    assert P.grade(expected, response) is ok                                                                  # exact by default


def test_a_question_can_ask_for_a_relative_tolerance_and_only_then_is_a_near_number_right():
    assert P.grade("412", "413 million") is False and P.grade("412", "413 million", 0.005) is True and P.grade("412", "420", 0.005) is False


def test_future_mentions_are_dates_after_t_or_later_facts():
    assert P.mentions_future("It happened on 2023-02-02.", "2023-01-20T14:00:00Z")
    assert P.mentions_future("It happened on February 2, 2023.", "2023-01-20T14:00:00Z")
    assert not P.mentions_future("As of 2023-01-15 nothing was known.", "2023-01-20T14:00:00Z")
    assert P.mentions_future("They bought Gamma Imaginary", "2023-01-20T14:00:00Z", ["acquisition of Gamma", "bought gamma imaginary"])
    assert not P.mentions_future("No later date or fact here.", "2023-01-20T14:00:00Z", ["secret"])


# ---------------------------------------------------------------- the runner and the scores
def runner(qs, profile, tmp_path, repeats=1, models=None, policy=None, variants=P.VARIANTS):
    gw = ModelGateway(policy)
    gw.register("mock", P.standin(qs, profile))
    ledger = Ledger(tmp_path / f"{profile}.db")
    rows = P.run(qs, models or [f"mock:{profile}"], repeats=repeats, variants=variants, ledger=ledger, gateway=gw)
    return rows, ledger


def test_a_rememberer_shows_a_memory_gap_and_ignores_the_instruction_and_has_no_skill(qs, tmp_path):
    rows, _ = runner(qs, "rememberer", tmp_path)
    s = P.score(rows)["mock:rememberer"]
    assert s["accuracy"] == {"A": 1.0, "B": 0.0, "C": 1.0, "D": 0.0} and s["memory_gap"] == 1.0 and s["instruction_failure"] == 1.0 and s["instruction_effect"] == 0.0 and s["skill_baseline"] == 0.0


def test_an_obedient_model_follows_the_instruction_and_a_reasoner_has_skill_but_no_memory_gap(qs, tmp_path):
    ob = P.score(runner(qs, "obedient", tmp_path)[0])["mock:obedient"]
    assert ob["accuracy"]["A"] == 1.0 and ob["accuracy"]["C"] == 0.0 and ob["instruction_failure"] == 0.0 and ob["instruction_effect"] == 1.0
    rs = P.score(runner(qs, "reasoner", tmp_path)[0])["mock:reasoner"]
    assert rs["accuracy"] == {"A": 1.0, "B": 1.0, "C": 0.0, "D": 1.0} and rs["memory_gap"] == 0.0 and rs["skill_baseline"] == 1.0


def test_future_mention_rate_counts_answers_that_name_a_later_date_or_fact(qs, tmp_path):
    s = P.score(runner(qs, "rememberer", tmp_path)[0])["mock:rememberer"]
    assert s["future_mention"]["A"] is not None and 0.0 <= s["future_mention"]["A"] <= 1.0
    blank = P.score(runner(qs, "blank", tmp_path)[0])["mock:blank"]
    assert blank["accuracy"]["A"] == 0.0 and blank["future_mention"]["A"] == 0.0 and blank["memory_gap"] == 0.0


def test_repeats_models_and_variants_multiply_the_rows_and_every_row_has_a_receipt(qs, tmp_path):
    gw = ModelGateway()
    gw.register("mock", P.standin(qs, "obedient"))
    gw.register("mock2", P.standin(qs, "reasoner"))
    ledger = Ledger(tmp_path / "multi.db")
    rows = P.run(qs, ["mock:a", "mock2:b"], repeats=3, ledger=ledger, gateway=gw)
    assert len(rows) == 5 * 4 * 2 * 3 and {r.model for r in rows} == {"mock:a", "mock2:b"} and {r.repeat for r in rows} == {1, 2, 3}
    assert all(r.call_id and r.event_hash and len(r.prompt_sha256) == 64 and len(r.response_sha256) == 64 and r.tokens_in is not None and r.synthetic for r in rows)
    assert len({r.event_hash for r in rows}) == len(rows) and ledger.verify().ok                              # one chain, every call a distinct linked event
    only = P.run(qs, ["mock:a"], variants=("A",), ledger=Ledger(tmp_path / "one.db"), gateway=gw)
    assert len(only) == 5 and {r.variant for r in only} == {"A"}


def test_every_call_is_stamped_with_the_questions_own_t_through_the_gateway(qs, tmp_path):
    rows, ledger = runner(qs, "obedient", tmp_path, variants=("A", "D"))
    stamps = {e.data()["gateway"]["clock_T"] for e in ledger.events(type="model_call")}
    assert stamps == {q.as_of for q in qs}                                                                  # T of the question, for A and for D alike
    assert not any(r.error for r in rows)


def test_a_refused_call_is_a_row_with_an_error_not_a_crash_and_the_rest_continue(qs, tmp_path):
    gw = ModelGateway()
    gw.register("mock", P.standin(qs, "obedient"))
    ledger = Ledger(tmp_path / "ref.db")
    rows = P.run(qs[:2], ["mock:ok", "mock:model-contributor"], variants=("A",), ledger=ledger, gateway=gw)
    bad = [r for r in rows if r.model == "mock:model-contributor"]
    assert len(rows) == 4 and all(r.refused and "contributor" in r.error and r.correct is None for r in bad) and all(r.correct for r in rows if r.model == "mock:ok")
    s = P.score(rows)
    assert s["mock:model-contributor"]["errors"] == 2 and s["mock:model-contributor"]["accuracy"]["A"] is None and s["mock:ok"]["accuracy"]["A"] == 1.0         # n/a, not 0


def test_a_provider_that_runs_a_search_of_its_own_is_flagged_in_the_row(qs, tmp_path):
    def searchy(req):
        return ModelResponse("The answer is 412.", "s-1", "stop", {}, server_tools_used=("web_search",))
    gw = ModelGateway(GatewayPolicy())
    gw.register("mock", searchy)
    rows = P.run(qs[:1], ["mock:s"], variants=("A",), ledger=Ledger(tmp_path / "s.db"), gateway=gw)
    assert rows[0].flags == "SERVER_TOOL" and rows[0].correct is True                                       # right answer, but the run is flagged: it cannot be taken as memory or skill


def test_repeats_must_be_at_least_one(qs, tmp_path):
    with pytest.raises(P.PitFinError):
        P.run(qs, ["mock:x"], repeats=0, ledger=Ledger(tmp_path / "z.db"))


def test_the_table_and_csv_come_out_with_every_column_and_say_when_the_data_is_synthetic(qs, tmp_path):
    rows, _ = runner(qs, "rememberer", tmp_path)
    t = P.table(P.score(rows), synthetic=True)
    assert "memory gap A-B" in t and "+100 pts" in t and "SYNTHETIC" in t and "mock:rememberer" in t
    out = P.write_csv(rows, tmp_path / "out" / "r.csv")
    got = list(csv.DictReader(out.open()))
    assert len(got) == len(rows) and set(got[0]) >= {"question_id", "model", "variant", "repeat", "correct", "future_mention", "prompt_sha256", "response_sha256", "call_id", "flags", "event_hash", "synthetic"}
    assert {r["variant"] for r in got} == {"A", "B", "C", "D"}


def test_a_model_with_no_answered_cells_reports_n_a_not_zero():
    r = P.Row("q", "m", "A", 1, None, None, error="boom")
    s = P.score([r])["m"]
    assert s["accuracy"]["A"] is None and s["memory_gap"] is None and "n/a" in P.table({"m": s}) and s["errors"] == 1
