"""The colour rule of the widget kit, read from the stylesheet: red only for errors, leaks and blocked; amber for warnings and OBSERVED; no pink left over for warnings."""
from __future__ import annotations

import re

import pytest

from asof.widgets import css

RULES = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css(), flags=re.S))
RED = re.compile(r"var\(--r2?\)|rgba\((?:215, 122, 120|189, 90, 88)")
SOFT = re.compile(r"var\(--soft\)|rgba\(217, 163, 161")
AMBER = re.compile(r"var\(--a2?\)|rgba\(214, 161, 75")
# every selector that may be red: errors, look-ahead leaks, blocked / knowable-after-T, broken chains
RED_OK = ("asof-chain-status.asof-broken", "asof-leak-overlay::before", "asof-error", "asof-b-flagged", "asof-tile-hot", "asof-s-error", "asof-c-bad", "asof-bad", "asof-band", "asof-gbar-on", "asof-bandsw",
          "asof-gaplabel", "asof-late", "asof-bl-flagged", "asof-bl-leak", "asof-sw-l")


def rules():
    for sel, body in RULES:
        yield " ".join(sel.split()), body


def test_red_is_used_only_for_errors_leaks_and_blocked():
    bad = [sel for sel, body in rules() if RED.search(re.sub(r"--(?:r2?|soft)\s*:[^;]*;?", "", body)) and not any(ok in sel for ok in RED_OK)]       # defining the token is not using it
    assert not bad, f"red outside error / leak / blocked: {bad}"


@pytest.mark.parametrize("selector", [".asof-s-warning", ".asof-b-observed", ".asof-bl-observed", ".asof-tile-warm", ".asof-warn", ".asof-sw.asof-sw-o", ".asof-fill.asof-fill-warn", ".asof-neg"])
def test_warnings_and_observed_are_amber(selector):
    hit = [body for sel, body in rules() if any(s.strip() == selector or s.strip().startswith(selector + " ") for s in sel.split(","))]      # the rule itself and its descendants (".asof-tile-warm b")
    assert hit and any(AMBER.search(b) for b in hit), selector
    assert not any(SOFT.search(b) or RED.search(b) for b in hit), f"{selector} still uses the red / pink family"


def test_no_warning_class_uses_the_old_pink():
    assert not [sel for sel, body in rules() if SOFT.search(body) and "error" not in sel and "--soft" not in body.split(":")[0]]


def test_the_light_theme_defines_amber_too():
    light = [body for sel, body in rules() if sel.startswith(".asof-light .asof-w")][0]
    assert "--a:" in light and "--a2:" in light
