# Verumark pilot summary (template)

Replace every ‹…› with a fact **and its evidence link**. Delete this paragraph. If a section has nothing to report, write "None found" with the sample size; do not delete the section.
**Do not invent or estimate a number.** A figure with no link is not reported.

| | |
|---|---|
| Customer | ‹name› |
| Period | ‹start› to ‹end› |
| Prepared by / date | ‹› / ‹› |
| AsOf version / Strathora build | ‹strathora --version› / ‹build or commit› |

## 1. What was checked

* Agent(s): ‹names and versions›   Models used: ‹provider:name, declared cutoff and who declared it, or "unknown"›
* Data sources and tools wrapped: ‹list, with the timestamp field each uses›
* Sessions / decision logs: ‹how many, how chosen, which clients in Strathora›   Decisions covered: ‹n› of ‹n total›
* Mode: ‹hashes_only / full›   Sync: ‹on from … / off›

## 2. Headline, against the agreed success criteria

| Measure | Baseline (customer, before) | Target | Result | Evidence |
|---|---|---|---|---|
| Leaks found, in existing runs | ‹› | ‹› | ‹n flagged of n decisions› | ‹Verumark report link› |
| Leaks prevented going forward | n/a | ‹› | ‹blocked_future ‹n›, blocked_untimestamped ‹n›› | ‹receipts / session link› |
| Live-versus-backtest gap explained | ‹› | ‹› | ‹% or "not computable: what is missing"› | ‹report link› |
| Hours saved against manual review | ‹› | ‹› | ‹inputs and result› | ‹the two timings› |
| Decisions changed | n/a | ‹› | ‹count› | ‹list below› |

## 3. Findings

One block per finding.

**F‹n›. ‹one-sentence statement›**
* Where: ‹Strathora link to the run or report, decision id›
* Evidence: ‹receipt call id, ledger event ids and hashes›
* What the agent could see, and when it became available: ‹›
* Label: ‹Verified / Observed / Unknown› and why
* The customer's view (including "this was fine, because …"): ‹›

## 4. What the time lock withheld

‹Totals from the receipts of the sessions in scope: served, blocked_future, blocked_untimestamped, served with unknown timing. Name the biggest source of each.›

## 5. Decisions changed

‹Each: what changed · the finding it came from · who decided · date. Only the customer's statement counts.›

## 6. Evidence index

* Ledger verification of the customer's ledger: ‹`strathora verify` output, date›
* Head stored outside the ledger: ‹where, `{seq, hash, recorded_at}`, date›; Strathora's stored head for each client: ‹seq, hash›
* Verumark reports: ‹links and each report's `report_sha256`›
* Anything that could not be verified: ‹›

## 7. Limits of this pilot

‹Sample size and how chosen; periods not covered; sources not wrapped; decisions after a session's last decision (not in a report); anything a hashes_only client could not supply; what was Unknown and why.›

## 8. What this does not prove

Do not shorten or remove this section.

* That the **source timestamps are true.** AsOf enforces `available_at`; it cannot verify it.
* That a **closed model forgot anything**. Model memory is Unknown unless measured, and a receipt never says "clean".
* That **nothing was left unsent or unrecorded**, or that the ledger was written by the agent it names (it is unsigned). Cut-off ledgers are detectable only against a head stored elsewhere.
* That the agent could **not** reach data another way than through AsOf.
* That results **generalise** beyond the sessions and period examined.

Full list: `LIMITATIONS.md`.

## 9. Next steps

‹agreed actions, owners, dates›

## Appendix: reproduce

‹the exact commands and files needed to re-run a finding, with the as-of times used›
