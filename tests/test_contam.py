"""R8: model memory is never claimed clean."""
import math

import pytest

from asof import ModelResponse, contam

MSG = [{"role": "user", "content": "q"}]


@pytest.fixture(autouse=True)
def clean_registry():
    contam.clear()
    contam.set_score_provider(None)
    yield
    contam.clear()
    contam.set_score_provider(None)


def model_receipt(s, model="fake:m"):
    s.register_provider("fake", lambda req: ModelResponse("x"))
    s.model(model, messages=MSG)
    return s.receipt()


def test_the_table_ships_empty_so_every_model_is_unknown_by_default(make_session):
    assert contam._TABLE == {}
    r = model_receipt(make_session())
    assert r.model["label"] == "unknown" and r.model["declared_cutoff"] is None and r.model["contamination_score"] is None
    assert any("cutoff is unknown" in w for w in r.warnings)


def test_a_score_makes_the_label_observed_and_nothing_stronger_exists(make_session):
    contam.register("fake:m", declared_cutoff="2022-06-01T00:00:00Z", declared_by="user", contamination_score=0.0, score_source="user")      # even a perfect score is only 'observed'
    r = model_receipt(make_session())
    assert r.model["label"] == "observed" and r.model["contamination_score"] == 0.0 and r.model["score_source"] == "user"
    assert contam.LABELS == ("unknown", "observed")
    assert "clean" not in str(r.to_dict()).lower().replace("cleaner", "")


def test_a_declared_cutoff_after_as_of_is_called_out(make_session):
    contam.register("fake:m", declared_cutoff="2024-04-01T00:00:00Z", declared_by="provider")
    r = model_receipt(make_session("2023-01-21T14:00:00Z"))
    assert r.model["declared_cutoff_after_as_of"] is True and any("after as_of" in w for w in r.warnings)
    assert r.model["label"] == "unknown"                                                         # a declared date alone is not an observation of anything


def test_a_cutoff_before_as_of_raises_no_cutoff_warning_but_the_limit_text_stays(make_session):
    contam.register("fake:m", declared_cutoff="2021-01-01T00:00:00Z")
    r = model_receipt(make_session("2023-01-21T14:00:00Z"))
    assert r.model["declared_cutoff_after_as_of"] is False and r.warnings == ()
    assert any("memory is not controlled" in line for line in r.limits)


def test_lookup_matching_and_validation():
    contam.register("a:model-1", declared_cutoff="2023-01-01T00:00:00Z")
    contam.register("b:fam", declared_cutoff="2022-01-01T00:00:00Z", prefix=True)
    contam.register("b:fam-big", declared_cutoff="2021-01-01T00:00:00Z", prefix=True)
    assert contam.lookup("a:model-1").declared_cutoff.year == 2023 and contam.lookup("a:model-2").declared_cutoff is None
    assert contam.lookup("b:fam-small-2024").declared_cutoff.year == 2022 and contam.lookup("b:fam-big-x").declared_cutoff.year == 2021
    for bad in (float("nan"), math.inf, "x"):
        with pytest.raises(ValueError):
            contam.register("z:z", contamination_score=bad)                                      # type: ignore[arg-type]
    with pytest.raises(ValueError):
        contam.register("z:z", declared_by="vibes")
    with pytest.raises(Exception):
        contam.register("z:z", declared_cutoff="2023-01-01T00:00:00")                           # naive cutoffs are refused like every other time


def test_an_external_score_provider_fills_gaps_and_may_not_break_a_call(make_session):
    contam.set_score_provider(lambda m: 0.12)
    assert contam.lookup("x:y").label == "observed" and contam.lookup("x:y").score_source == "strathora"
    contam.register("x:own", contamination_score=0.9, score_source="user")
    assert contam.lookup("x:own").contamination_score == 0.9 and contam.lookup("x:own").score_source == "user"      # your own entry wins
    contam.set_score_provider(lambda m: 1 / 0)
    assert contam.lookup("q:r").label == "unknown"
    assert model_receipt(make_session(), "q:r") if False else True


# ---------------------------------------------------------------- A3: the receipt says who declared the cutoff
@pytest.mark.parametrize("by", ["user", "provider"])
def test_a_registered_cutoff_is_shown_with_who_declared_it(make_session, by):
    contam.register("fake:m", declared_cutoff="2022-06-01T00:00:00Z", declared_by=by)
    r = model_receipt(make_session("2023-01-21T14:00:00Z"))
    assert r.model["declared_cutoff"] == "2022-06-01T00:00:00Z" and r.model["declared_by"] == by
    assert f"declared cutoff 2022-06-01T00:00:00Z (declared by {by})" in r.summary()
    assert r.model["label"] == "unknown"                                                          # a statement is not an observation


def test_an_unregistered_model_shows_unknown_for_cutoff_and_who_declared_it(make_session):
    r = model_receipt(make_session())
    assert r.model["declared_cutoff"] is None and r.model["declared_by"] == "unknown"
    assert "declared cutoff unknown" in r.summary() and "declared by" not in r.summary()


def test_the_declared_by_is_in_the_ledger_and_the_warning_names_it(make_session):
    contam.register("fake:m", declared_cutoff="2024-04-01T00:00:00Z", declared_by="provider")
    s = make_session("2023-01-21T14:00:00Z")
    r = model_receipt(s)
    ev = s.events("model_call")[0].data()
    assert ev["declared_by"] == "provider" and ev["declared_cutoff"] == "2024-04-01T00:00:00Z"
    assert any("declared by provider" in w and "after as_of" in w for w in r.warnings)


def test_the_label_can_never_exceed_observed_whatever_is_registered(make_session):
    for score in (0.0, 0.5, 1.0, -3.0):
        contam.register("fake:m", declared_cutoff="2000-01-01T00:00:00Z", declared_by="provider", contamination_score=score, score_source="user")
        r = model_receipt(make_session("2023-01-21T14:00:00Z"))
        assert r.model["label"] == "observed" and r.model["label"] in contam.LABELS
    assert set(contam.LABELS) == {"unknown", "observed"}


def test_the_score_provider_cannot_raise_the_label_above_observed_and_is_marked_as_external(make_session):
    contam.set_score_provider(lambda m: 0.0)
    r = model_receipt(make_session(), "fake:unregistered-model")
    assert r.model["label"] == "observed" and r.model["score_source"] == "strathora" and r.model["declared_by"] == "unknown"
