"""Exceptions raised by AsOf. All derive from :class:`AsOfError`."""
from __future__ import annotations


class AsOfError(Exception):
    """Base class for every error AsOf raises on purpose."""


class NaiveTimeError(AsOfError, ValueError):
    """A datetime without a time zone was given where an unambiguous instant is required."""


class ClockError(AsOfError, ValueError):
    """The session clock was asked to do something it must not (move backwards)."""


class SourceError(AsOfError):
    """A source could not be read or is configured incorrectly."""


class ToolError(AsOfError):
    """A tool is unknown, misregistered, or returned something AsOf cannot time-check."""


class GatewayError(AsOfError):
    """A model call could not be made or its provider is unknown."""


class GatewayRuleError(GatewayError):
    """A model call was refused by a gateway rule, before anything was sent to the provider. ``rule`` says which (``server_tools_off``, ``contributor_model``, ``provider_state``)."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


class LedgerError(AsOfError):
    """The ledger could not be written or read."""


class LedgerVersionError(LedgerError):
    """The ledger file was written by a different schema version. Versions are never mixed in one chain."""


class DecisionRequiredError(ClockError):
    """The session requires a logged decision before the clock may advance (``decision_gate``)."""
