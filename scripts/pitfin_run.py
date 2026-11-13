"""Run the PIT-Fin pilot harness over a question set and a list of models, through the AsOf gateway.

    python scripts/pitfin_run.py --questions examples/pitfin/sample_questions.json            # offline: stand-in models, SYNTHETIC questions, no network, no key
    python scripts/pitfin_run.py --questions my_50.json --models anthropic:MODEL,openai:MODEL,meta:MODEL --repeats 5 --live

Without --live only the stand-in models (mock:rememberer, mock:obedient, mock:reasoner, mock:blank) can run: they read the answer key and are NOT language models, they exist to show how each score behaves.
With --live the real providers are called, with keys from the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, MODEL_API_KEY for Meta) and provider-side tools OFF (the gateway default). That can cost money.
Output: a results table on screen, and <out>/results.csv (one row per call: hashes, tokens, flags, ledger event hash) and <out>/scores.json. The run is one hash-chained ledger: <out>/pitfin.ledger.db.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from asof import GatewayError, Ledger, ModelGateway
from asof import pitfin as P

STANDINS = ("rememberer", "obedient", "reasoner", "blank")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PIT-Fin pilot harness")
    ap.add_argument("--questions", required=True, help="a JSON (or YAML) question set; see examples/pitfin/sample_questions.json")
    ap.add_argument("--models", default=",".join(f"{s}:standin" for s in STANDINS), help="comma-separated provider:model ids (default: the four offline stand-ins, e.g. rememberer:standin)")
    ap.add_argument("--repeats", type=int, default=1, help="times each question/variant/model is asked (default 1)")
    ap.add_argument("--variants", default="ABCD", help="which variants to run, from ABCD (default all)")
    ap.add_argument("--out", default="pitfin_out", help="where to write results.csv, scores.json and the ledger")
    ap.add_argument("--live", action="store_true", help="call real providers (needs keys in the environment; may cost money). Without it only the mock stand-ins run.")
    ap.add_argument("--max-tokens", type=int, default=None, help="pass max_tokens to the provider")
    a = ap.parse_args(argv)

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    real = [m for m in models if m.partition(":")[0] not in STANDINS]
    if real and not a.live:
        print(f"refused: {real} are real providers. Nothing is called without --live. Run with the offline stand-ins ({', '.join(s + ':standin' for s in STANDINS)}), or pass --live.", file=sys.stderr)
        return 2
    try:
        qs = P.load_questions(a.questions)
    except (P.PitFinError, OSError) as e:
        print(f"cannot read the questions: {e}", file=sys.stderr)
        return 2
    variants = tuple(v for v in a.variants.upper() if v in P.VARIANTS)
    if not variants:
        print("--variants must contain some of A, B, C, D", file=sys.stderr)
        return 2
    gw = ModelGateway()                                                  # provider-side tools OFF, contributor models refused, provider state refused
    for s in STANDINS:
        gw.register(s, P.standin(qs, s))                                 # an offline stand-in is a provider named by its profile: rememberer:standin
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "pitfin.ledger.db")
    params = {"max_tokens": a.max_tokens} if a.max_tokens else {}
    done = 0

    def tick(row: P.Row) -> None:
        nonlocal done
        done += 1
        if done % 20 == 0:
            print(f"  {done} calls...", file=sys.stderr)
    try:
        rows = P.run(qs, models, repeats=a.repeats, variants=variants, ledger=ledger, gateway=gw, params=params, on_row=tick)
    except GatewayError as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 1
    finally:
        ledger.close()
    scores = P.score(rows)
    synthetic = all(q.synthetic for q in qs)
    print(P.table(scores, synthetic=synthetic))
    P.write_csv(rows, out / "results.csv")
    (out / "scores.json").write_text(json.dumps({"synthetic": synthetic, "n_questions": len(qs), "repeats": a.repeats, "variants": variants, "scores": scores}, indent=1, sort_keys=True) + "\n")
    errs = sum(1 for r in rows if r.error)
    print(f"\n{len(rows)} calls, {errs} with errors. Wrote {out / 'results.csv'}, {out / 'scores.json'} and the ledger {out / 'pitfin.ledger.db'} (verify with: strathora verify --ledger {out / 'pitfin.ledger.db'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
