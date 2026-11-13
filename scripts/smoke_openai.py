#!/usr/bin/env python3
"""One real call through AsOf to OpenAI (or any OpenAI-compatible endpoint via --base-url), then print the receipt and verify the ledger.

    export OPENAI_API_KEY=...           # yours; it is sent to OpenAI and nowhere else
    python scripts/smoke_openai.py --model <a model name you have access to>

Not run by the author against the live service (no key was used): this is the check you run. Exit 0 only if the call worked, the ledger verifies and the
key is not in the ledger file.
"""
import argparse
import sys

from _smoke_common import add_model_args, run_model_smoke

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
add_model_args(ap)
sys.exit(run_model_smoke("openai", "OPENAI_API_KEY", ap.parse_args()))
