"""AsOf: a time-lock connector for AI agents.

Show an agent only what was knowable at a time T, route its tools through a time-locked proxy, log every call to a tamper-evident local
ledger, and get a receipt of what it could see. See the README for what it does *not* do.
"""
__version__ = "0.5.0"

from . import contam, gate, sources, tools                # noqa: E402  (after __version__, which submodules import)
from .clock import Clock, parse_duration, parse_time       # noqa: E402
from .errors import (AsOfError, ClockError, DecisionRequiredError, GatewayError, GatewayRuleError, LedgerError, LedgerVersionError, NaiveTimeError,   # noqa: E402
                     SourceError, ToolError)
from .gate import GateResult, Intent                       # noqa: E402  (the policy is asof.gate.Policy: the name Policy alone would be ambiguous next to GatewayPolicy)
from .gateway import GatewayPolicy, ModelGateway, ModelRequest, ModelResponse, ProviderSpec   # noqa: E402
from .ledger import Head, Ledger, LedgerEvent, VerifyResult   # noqa: E402
from .memory import Memory                                 # noqa: E402
from .receipt import Receipt                               # noqa: E402
from .records import Record, canonical_json                # noqa: E402
from .redaction import register_secret                     # noqa: E402
from .session import Session                               # noqa: E402

__all__ = ["Session", "register_secret", "Record", "Receipt", "Ledger", "LedgerEvent", "Head", "VerifyResult", "Clock", "ModelGateway", "ModelRequest", "ModelResponse", "GatewayPolicy", "ProviderSpec", "Intent", "GateResult", "Memory",
           "sources", "tools", "contam", "gate", "canonical_json", "parse_time", "parse_duration",
           "AsOfError", "ClockError", "DecisionRequiredError", "GatewayError", "GatewayRuleError", "LedgerError", "LedgerVersionError", "NaiveTimeError", "SourceError", "ToolError",
           "__version__"]
