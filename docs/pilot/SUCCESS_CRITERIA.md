# Pilot success criteria (template)

Fill this in **with the customer, before the pilot starts**. The baselines are the customer's own statements, recorded now, so the result cannot be flattered afterwards.
Nothing below is pre-filled: any number in a finished copy must come from the customer or from a linked piece of evidence.

| | |
|---|---|
| Customer | ‹name› |
| Pilot period | ‹start› to ‹end› |
| Agent(s) in scope | ‹names, versions› |
| Data sources / tools / models wrapped | ‹list› |
| What is sampled | ‹which sessions or decision logs, how many, how chosen› |
| Agreed by (customer / us) and date | ‹›  /  ‹› |

## The four measures

### 1. Leaks found

* **Definition.** Decisions in which an input or tool result was **not yet available** when the decision was made, or whose availability is **unknown**, counted separately.
* **Two different numbers, never added together:**
  * *Found in existing runs:* run the customer's past decision logs through Verumark; count the decisions flagged by the **Data timing** and **Tool integrity** checks, and the claims labelled Unknown.
  * *Prevented going forward:* from AsOf receipts, the totals of `blocked_future` and `blocked_untimestamped` (what the lock withheld from the agent).
* **Evidence per finding:** a Verumark report link and decision id, or a receipt id and ledger event, in the pilot report.
* Baseline (what the customer believes today, e.g. "we have never checked"): ‹›   Target: ‹›   Result: ‹›

### 2. Share of the live-versus-backtest gap explained

* **Definition.** From the Verumark attribution: `(|gap| − |unexplained|) / |gap|`, where `gap` is live net PnL minus expected net PnL and `unexplained` is the component no check could attribute.
* **Only computable** with live fills, expected trades and candles for the same period. If any is missing the result is written **"not computable: ‹what is missing›"**, never 0 and never a guess. Components labelled Unknown are reported next to the percentage.
* Baseline (share the customer can explain today): ‹›   Target: ‹›   Result: ‹›   Evidence: ‹report link›

### 3. Hours saved against manual review

* **Baseline, asked upfront:** how many hours does your team spend today reviewing an agent's decisions for look-ahead, per review and per month? ‹hours› ‹per›   Who does it: ‹role›   How you know: ‹estimate / timesheet›.
* **Measured:** time a reviewer takes to reach an answer on the same sample ‹n› decisions (a) the usual way and (b) from the Verumark report and receipts. Record both, with the reviewer's start and end times.
* **Result** = (usual time − time with the report) × the number of reviews the customer does in a period. Show the inputs, not just the product.
* Baseline: ‹›   Target: ‹›   Result: ‹›   Evidence: ‹the two timings›

### 4. Decisions changed

* **Definition.** A strategy, an agent version, a limit or a deployment the **customer** changed, paused or declined because of a finding. Only the customer's statement counts.
* List each one: ‹what changed› · ‹finding it came from (link)› · ‹who decided› · ‹date›.
* Baseline: not applicable   Target: ‹›   Result: ‹count and list›

## Ground rules for the final report

1. **No number without a link.** Every figure in the pilot report points at a Verumark report, a receipt, a ledger event or a timing the customer supplied.
2. **"We found nothing" is a result.** Report it, with the sample size and what was not covered.
3. **Denominators always.** "4 flagged" means "4 of ‹n› decisions in ‹sessions›".
4. **Unknown stays Unknown.** A claim that could not be checked is not counted as clean.
5. **False positives are recorded.** If the customer says a flagged decision was fine, write down why.
6. **Say what the pilot cannot show** (see `LIMITATIONS.md`): that source timestamps are true, that a closed model forgot anything, that nothing was left unsent.

## Sign-off

| | Name | Date |
|---|---|---|
| Customer | ‹› | ‹› |
| Us | ‹› | ‹› |
