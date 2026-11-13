"""One set of labels for every claim, and the finding types that map onto them.

``VERIFIED``  proven by the records themselves (a timestamp, a hash, a number that can be recomputed).
``OBSERVED``  seen, but resting on a heuristic, a sample, or partial coverage.
``UNKNOWN``   could not be checked from what was supplied.
``FLAGGED``   a problem was found. How well the problem is proven is kept separately, as ``evidence`` (``VERIFIED`` or ``OBSERVED``).

A *finding type* says what kind of problem it is (``LEAK``: something was available after the decision it fed; ``SERVER_TOOL``: a provider used its own tools; and so on). Every
finding type maps to ``FLAGGED``. ``LEAK`` is kept as a type, not a label, so a report can say "a leak, proven by timestamps" instead of just "flagged".

Rules that never bend:

* A closed model's memory is never ``VERIFIED``: model labels come from :func:`model_label` and are ``UNKNOWN``, ``OBSERVED`` or (when a rule was broken) ``FLAGGED``.
* Combining labels takes the worst one: ``FLAGGED`` beats ``UNKNOWN`` beats ``OBSERVED`` beats ``VERIFIED``. Nothing is ever rounded up.
"""
from __future__ import annotations

from enum import Enum


class Label(str, Enum):
    VERIFIED = "VERIFIED"
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"
    FLAGGED = "FLAGGED"


class FindingType(str, Enum):
    LEAK = "LEAK"                           # an input, tool result or model answer that was not knowable at the decision time
    SERVER_TOOL = "SERVER_TOOL"             # the provider ran its own tools (web search, fetch) outside the AsOf proxy
    CLOCK_REWIND = "CLOCK_REWIND"           # a request to move the clock backwards
    CHAIN_BREAK = "CHAIN_BREAK"             # the ledger's hash chain does not verify
    EXECUTION = "EXECUTION"                 # an order with no fill, a fill with no order, and so on
    DATA_QUALITY = "DATA_QUALITY"           # a data-quality check found a pattern that honest timestamps do not produce
    SUSCEPTIBILITY = "SUSCEPTIBILITY"       # a probe was answered with a fact from after the as-of date
    POLICY = "POLICY"                       # a configured rule was broken (a blocked model, a gate breach)


# every finding type is a problem, so every one maps to FLAGGED; the table exists so that this is stated, tested, and changed in one place
FINDING_LABEL: dict[FindingType, Label] = {t: Label.FLAGGED for t in FindingType}

# worst first: this is the order used to combine labels
_WORST_FIRST = (Label.FLAGGED, Label.UNKNOWN, Label.OBSERVED, Label.VERIFIED)
_EVIDENCE = (Label.VERIFIED, Label.OBSERVED)


def label_of(value: str | Label) -> Label:
    """Read a label from a string, case-insensitively. Anything else is an error, not ``UNKNOWN``: a typo must not become a quiet default."""
    if isinstance(value, Label):
        return value
    try:
        return Label(str(value).strip().upper())
    except ValueError:
        raise ValueError(f"{value!r} is not a label; use one of {[l.value for l in Label]}") from None


def finding_label(kind: str | FindingType) -> Label:
    """The label a finding of this type carries (always ``FLAGGED``)."""
    t = kind if isinstance(kind, FindingType) else FindingType(str(kind).strip().upper())
    return FINDING_LABEL[t]


def worst(*labels: str | Label) -> Label:
    """The weakest of the labels given (``UNKNOWN`` if none are given). ``FLAGGED`` beats everything: one problem makes the whole thing flagged."""
    got = [label_of(l) for l in labels]
    if not got:
        return Label.UNKNOWN
    return min(got, key=_WORST_FIRST.index)


def evidence_of(value: str | Label) -> Label:
    """How well a finding is proven: ``VERIFIED`` or ``OBSERVED`` only."""
    e = label_of(value)
    if e not in _EVIDENCE:
        raise ValueError(f"evidence is VERIFIED or OBSERVED, not {e.value}")
    return e


def model_label(*, has_score: bool, rule_broken: bool = False) -> Label:
    """The label for a model's memory. ``UNKNOWN`` unless someone measured it (``OBSERVED``); never ``VERIFIED``. ``FLAGGED`` only when a rule about the model was broken."""
    if rule_broken:
        return Label.FLAGGED
    return Label.OBSERVED if has_score else Label.UNKNOWN


def finding(kind: str | FindingType, text: str, *, evidence: str | Label = Label.VERIFIED, **extra: object) -> dict[str, object]:
    """A finding as a claim: ``{"text", "label": "FLAGGED", "kind", "evidence", ...}``."""
    t = kind if isinstance(kind, FindingType) else FindingType(str(kind).strip().upper())
    return {"text": text, "label": finding_label(t).value, "kind": t.value, "evidence": evidence_of(evidence).value, **extra}
