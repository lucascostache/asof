"""What AsOf knows about a model's memory (rule R8).

AsOf **cannot** remove or verify what a model already knows. A model trained on data past T may "remember" the future whatever the
data layer serves. So receipts never call a model clean:

* ``declared_cutoff``: the date the model's training data is *said* to end, and **who said so**: ``"provider"`` (the vendor) or ``"user"`` (you).
  Either way it is a statement, not something AsOf measured. Unregistered models show ``"unknown"`` for both.
* ``contamination_score``: a number from an evaluation, if one exists. **Unknown unless you register one.**
* the label is ``"unknown"`` or at most ``"observed"`` (a score exists). There is no ``"clean"`` or ``"verified"`` label, by design.

This module ships **empty**: no model names, cutoffs or scores are bundled, because a wrong date in a time-lock tool silently becomes a leak.
Register what you have checked::

    from asof import contam
    contam.register("anthropic:some-model", declared_cutoff="2024-04-01T00:00:00Z", declared_by="provider")

Optional external scores (for example from the Strathora platform) plug in through :func:`set_score_provider`; no network client is bundled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .clock import parse_time

LABELS = ("unknown", "observed")        # deliberately no stronger label
DECLARED_BY = ("user", "provider")      # who stated the cutoff: you (user), or the model's vendor (provider)
SCORE_SOURCES = ("user", "strathora")   # who produced the contamination score


@dataclass(frozen=True)
class ModelProfile:
    model: str
    declared_cutoff: Optional[datetime] = None
    declared_by: str = "unknown"                # "user" | "provider" | "unknown"
    contamination_score: Optional[float] = None
    score_source: str = "unknown"               # "user" | "strathora" | "unknown"

    @property
    def label(self) -> str:
        """``"observed"`` only when a contamination score exists; otherwise ``"unknown"``. Never anything stronger (R8)."""
        return "observed" if self.contamination_score is not None else "unknown"


_TABLE: dict[str, tuple[ModelProfile, bool]] = {}           # key -> (profile, prefix_match)
_provider: Optional[Callable[[str], Optional[float]]] = None


def register(model: str, *, declared_cutoff: str | datetime | None = None, declared_by: str = "user", contamination_score: float | None = None,
             score_source: str = "user", prefix: bool = False) -> ModelProfile:
    """Record what you know about ``model`` (``"provider:name"``). ``declared_by`` says who stated the cutoff (``"user"`` or ``"provider"``).
    ``prefix=True`` makes the entry apply to every model id that starts with it."""
    if declared_by not in DECLARED_BY:
        raise ValueError(f"declared_by must be one of {DECLARED_BY}")
    if score_source not in SCORE_SOURCES:
        raise ValueError(f"score_source must be one of {SCORE_SOURCES}")
    if contamination_score is not None and (not isinstance(contamination_score, (int, float)) or not math.isfinite(contamination_score)):
        raise ValueError("contamination_score must be a finite number")
    cutoff = parse_time(declared_cutoff, what="declared_cutoff") if declared_cutoff is not None else None
    prof = ModelProfile(model, cutoff, declared_by if cutoff is not None else "unknown",
                        float(contamination_score) if contamination_score is not None else None, score_source if contamination_score is not None else "unknown")
    _TABLE[model] = (prof, prefix)
    return prof


def clear() -> None:
    _TABLE.clear()


def set_score_provider(fn: Optional[Callable[[str], Optional[float]]]) -> None:
    """Install ``fn(model) -> score | None`` to fill in scores for models without a registered one (the score is labelled ``source='strathora'``)."""
    global _provider
    _provider = fn


def lookup(model: str) -> ModelProfile:
    """The registered profile, else an all-unknown one. Matches the full id, then the id without its provider, then the longest ``prefix=True`` entry."""
    bare = model.split(":", 1)[-1]
    for key in (model, bare):
        if key in _TABLE:
            return _TABLE[key][0]
    hits = [(k, p) for k, (p, pre) in _TABLE.items() if pre and (model.startswith(k) or bare.startswith(k))]
    if hits:
        return max(hits, key=lambda kv: len(kv[0]))[1]
    if _provider is not None:
        try:
            score = _provider(model)
        except Exception:  # noqa: BLE001 - a failing provider must never break a model call
            score = None
        if score is not None and math.isfinite(score):
            return ModelProfile(model, None, "unknown", float(score), "strathora")
    return ModelProfile(model)
