"""The one label set: VERIFIED / OBSERVED / UNKNOWN / FLAGGED, and the finding types (LEAK and others) that map to FLAGGED. Every path is tested."""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from asof import contam
from asof.labels import FINDING_LABEL, FindingType, Label, evidence_of, finding, finding_label, label_of, model_label, worst

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "labels_vector.json").read_text())


def test_there_are_exactly_four_labels():
    assert [l.value for l in Label] == VECTOR["labels"] == ["VERIFIED", "OBSERVED", "UNKNOWN", "FLAGGED"]


def test_every_finding_type_including_leak_maps_to_flagged():
    assert [t.value for t in FindingType] == VECTOR["finding_types"]
    assert set(FINDING_LABEL) == set(FindingType)
    for t in FindingType:
        assert finding_label(t) is Label.FLAGGED
        assert finding_label(t.value.lower()) is Label.FLAGGED          # strings are read case-insensitively
    assert finding_label("LEAK") is Label.FLAGGED
    with pytest.raises(ValueError):
        finding_label("NOT_A_TYPE")


@pytest.mark.parametrize("case", VECTOR["worst"], ids=lambda c: "+".join(c["in"]) or "empty")
def test_combining_labels_takes_the_worst_and_matches_the_shared_vector(case):
    assert worst(*case["in"]).value == case["out"]


def test_worst_is_order_free_and_flagged_always_wins():
    for combo in itertools.permutations([Label.VERIFIED, Label.OBSERVED, Label.UNKNOWN, Label.FLAGGED]):
        assert worst(*combo) is Label.FLAGGED
    for combo in itertools.permutations([Label.VERIFIED, Label.OBSERVED, Label.UNKNOWN]):
        assert worst(*combo) is Label.UNKNOWN
    assert worst() is Label.UNKNOWN                                      # nothing checked is unknown, never verified


def test_a_typo_is_an_error_not_a_quiet_default():
    assert label_of(" verified ") is Label.VERIFIED and label_of(Label.FLAGGED) is Label.FLAGGED
    for bad in ("CLEAN", "verfied", "", None, 3):
        with pytest.raises(ValueError):
            label_of(bad)                                                # type: ignore[arg-type]


def test_evidence_is_verified_or_observed_only():
    assert evidence_of("verified") is Label.VERIFIED and evidence_of("OBSERVED") is Label.OBSERVED
    for bad in ("UNKNOWN", "FLAGGED"):
        with pytest.raises(ValueError):
            evidence_of(bad)


@pytest.mark.parametrize("case", VECTOR["model_label"], ids=lambda c: f"score={c['has_score']},broken={c['rule_broken']}")
def test_model_label_paths_match_the_shared_vector(case):
    assert model_label(has_score=case["has_score"], rule_broken=case["rule_broken"]).value == case["out"]


def test_a_models_memory_is_never_verified():
    for has_score, broken in itertools.product([False, True], repeat=2):
        assert model_label(has_score=has_score, rule_broken=broken) is not Label.VERIFIED


def test_the_contamination_registry_only_ever_yields_unknown_or_observed():
    contam.register("test:unregistered-check", declared_cutoff="2020-01-01T00:00:00Z", declared_by="user")
    assert label_of(contam.lookup("test:unregistered-check").label) is Label.UNKNOWN            # a declared cutoff is a statement, not a measurement
    contam.register("test:scored-check", contamination_score=0.12, score_source="user")
    assert label_of(contam.lookup("test:scored-check").label) is Label.OBSERVED
    assert label_of(contam.lookup("test:never-registered").label) is Label.UNKNOWN
    assert set(contam.LABELS) == {"unknown", "observed"} and "verified" not in contam.LABELS


def test_a_finding_is_a_flagged_claim_that_keeps_how_well_it_is_proven():
    f = finding("LEAK", "1 input was available after its decision.", evidence="VERIFIED", n=1)
    assert f == {"text": "1 input was available after its decision.", "label": "FLAGGED", "kind": "LEAK", "evidence": "VERIFIED", "n": 1}
    g = finding(FindingType.SUSCEPTIBILITY, "a probe hit", evidence=Label.OBSERVED)
    assert g["label"] == "FLAGGED" and g["evidence"] == "OBSERVED"
    with pytest.raises(ValueError):
        finding("LEAK", "x", evidence="UNKNOWN")                          # a finding cannot be "proven" at UNKNOWN
