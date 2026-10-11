# Weighted scorecard: plan

Replace the cost-only ranking with a **weighted scorecard** that the buyer can reshape in the analyst chat at run time. The model proposes a standard scorecard from the RFQ, the buyer adjusts weights (as numbers or as relations), adds or changes criteria, and sets minimum scores that can fail a vendor. Rescoring that needs the vendors' documents re-read happens only when a criterion changes, and only after the buyer approves it.

Builds on: the comparison engine (`app/live_compare.py`), the analyst (`app/live_analyst.py`) and the Compare quotes page (`web/views/compare-live.js`).

## Decisions taken

| Question | Decision |
| --- | --- |
| How the score is used | A full scorecard (price, lead time and criteria, weighted) replaces the cost-only ranking. A criterion can carry a minimum score that fails a vendor |
| Who sets weights | The model proposes a standard scorecard from the RFQ. Weights or relations the buyer gives in chat ("delivery twice as important as warranty") take precedence |
| Re-reading documents | Only when a criterion's rubric is new or changed, and only after the buyer approves. Weight and minimum-score changes never re-read anything |

## 1. How it works for the buyer

```
Run comparison  ->  scorecard v1 proposed from the RFQ (weights + one-line reason each)
                        |
Buyer in chat:  "Price matters most, delivery plan twice as much as warranty"
                        |  weights only: recomputed instantly, no model calls  ->  v2
Buyer in chat:  "Score delivery plans 10 only if they name the origin and a date per site; fail anyone below 5"
                        |  rubric changed: Ana asks "This re-reads 8 proposals (about 30 s). Proceed?"  [Proceed] [Cancel]
                        |  approved: re-read, score, recompute  ->  v3
Summary and Scorecard tabs show v3; earlier versions stay comparable
```

## 2. The scorecard

Each criterion produces a score from 0 to 10 per vendor. The vendor's total is the weighted average, shown out of 100:

`Score = Σ (weightᵢ × scoreᵢ) / Σ weightᵢ × 10`

| Criterion type | How the 0–10 score is produced | Model calls |
| --- | --- | --- |
| **Price** | `10 × lowest landed cost ÷ vendor's landed cost`, among vendors that quoted every line | None |
| **Lead time** | `10 × shortest lead time ÷ vendor's lead time`; not stated scores 0 and is flagged | None |
| **Questionnaire question** (one per RFQ question) | The answer score already produced during extraction (default rubric) | None, unless the buyer changes the rubric |
| **Custom criterion** (for example delivery-plan detail, warranty length, after-sales support) | The model reads the vendor's document against the criterion's rubric and returns a score, a reason, a verbatim quote and a confidence | One per proposal, cached |

**Defaults (to confirm):**
- A missing value scores 0 and is flagged "not stated".
- A blanket questionnaire answer counts for each question at its own score, with the existing flag.
- Weight 0 keeps a criterion visible but uncounted.

**Ranking:**
1. Vendors that pass all gates come first, then by score (highest first), then by cost on ties.
2. The cost-only view stays available as a toggle.
3. Cherry-pick is unchanged: it is per line and price-based (see section 8).

## 3. Gates (pass or fail)

| Gate | Source |
| --- | --- |
| Quantity shortfall, items not in the RFQ, no questionnaire answers | Existing rules, unchanged |
| Questionnaire average below the threshold | Existing rule, still adjustable. It becomes redundant once per-question minimums are set, and the plan keeps it for compatibility |
| **Criterion minimum** (new) | Any criterion may carry `min_score`. Below it, the vendor fails with "Below minimum on Delivery plan (4 < 5)" |

## 4. The standard scorecard (version 1)

- **When:** after the first successful run of an RFQ, if it has no scorecard yet.
- **Input:** one model call (job `live_scorecard`) with the RFQ (scope, lines, questions) and the candidate criteria: price, lead time and one per question.
- **Output:** a weight for each criterion, summing to 100, with a one-line reason each.
- **Checks in code:**
  - every candidate criterion is present;
  - weights are between 0 and 100 and are normalised to 100;
  - no unknown criteria.
  - If the check fails, the call is retried once and then falls back to equal weights with a notice.
- **Cost:** v1 uses the existing answer scores, so the documents aren't re-read.
- **Visibility:** Ana announces it in the chat ("I've set a starting scorecard: price 40, delivery 20 …. Tell me what matters more").

## 5. Changing the scorecard in chat

New analyst tools. Every change creates a new version.

| Tool | Does | Model calls | Asks first? |
| --- | --- | --- | --- |
| `get_scorecard` | Current version, criteria, weights, minimums, reasons | None | No |
| `update_weights(weights, relations, note)` | Sets weights from explicit numbers and/or relations such as `{a: delivery, b: warranty, factor: 2}`. Code checks that every stated relation holds (within 1%), normalises to 100, and rejects contradictions with an error Ana must resolve with the buyer | None | No |
| `set_minimum(criterion, min_score or null)` | Adds or removes a gate | None | No |
| `propose_criterion(name, rubric, question or custom, weight, min_score)` | New criterion, or a changed rubric for an existing one. If the rubric is unchanged it is applied at once. If it is new or changed, it creates a **pending change** and returns the cost (proposals to re-read, rough time) for Ana to put to the buyer | None at this point | **Yes** |
| `apply_pending(id)` | Re-reads and scores. Code refuses unless the buyer approved that pending change (section 6) | One per proposal | Approval required |
| `remove_criterion(name)` | Removes a criterion; its weight is redistributed proportionally | None | No |

`run_scenario` gains `scorecard_version`, so earlier versions can be compared ("rank with v2's weights").

**Analyst rules** added to the system prompt:
- Translate relations into weights only through `update_weights`.
- State the weights in the answer.
- Never estimate a score.
- Always ask before re-reading, giving the count and rough time.
- Say "only weights changed; nothing was re-read" when that is so.

## 6. Asking permission before re-reading

- **Pending change:** a proposed rubric change is stored with an id, the criterion, its old and new rubric, the proposals affected and the estimated time.
- **In the chat:** it shows as a confirmation card with **Proceed** and **Cancel**. The buttons call `POST /api/live/rfqs/{id}/analyst/pending/{pid}/approve` (streams progress: "Scored 3 of 8") or `…/cancel`.
- **Typed approval:** "yes, go ahead" also works. Ana calls `apply_pending`, and code accepts it only if that pending change was proposed in Ana's immediately preceding message and nothing else changed since.
- **Never re-read when:**
  - the rubric and the proposal file are unchanged (the cache key is the rubric, the file's hash, the model and the prompt version);
  - only weights or minimums changed;
  - the buyer cancelled.
- **New proposals:** a proposal added after the scorecard exists is scored on all criteria during the next **Run comparison**. The run dialog says so ("also scores 2 custom criteria"), and pressing Run is the approval.

## 7. Storage, tables and screens

**Storage** (per RFQ, alongside the runs):
```
analysis/scorecard/v1.json, v2.json, …   criteria, weights, minimums, reasons, author (model or buyer),
                                          the chat message that caused it, created_at
analysis/scorecard/current.txt
analysis/R-NNN/scorecard.parquet         vendor × criterion: raw value, score, weight, points, reason,
                                          quote, source (formula, answers, document), cached flag
```

**Tables:**
- `vendor_summary` gains `Score / 100`, `Score rank` and `Scorecard version`.
- Scenario runs save `derived/<name>_scorecard`.
- Changing weights recomputes these tables from saved data in under a second.

**Screens:**
- **Scorecard tab:**
  - criteria as rows with weight, minimum and the reason for the weight;
  - vendors as columns, each score with its reason and quote on hover;
  - totals and rank at the bottom;
  - a version picker showing who set it (model or buyer) and when.
- **Summary tab:** Score column; rank by scorecard with a cost toggle; failures from criterion minimums named in Fail Reasons.
- **Chat:** the confirmation card; progress while scoring; a one-line note of what changed ("v3: delivery plan rubric changed, re-read 8 proposals; weights unchanged").

## 8. Out of scope

- Editing weights with sliders or inline fields (chat only).
- Scorecard templates reused across RFQs, or saved per category.
- Per-line scoring in the cherry-pick (it stays price-based per line).
- Pairwise-comparison methods (AHP) and sensitivity charts ("what weight would change the winner").
- Approval of a scorecard by a second person.
- Scoring that mixes in price history or vendor past performance.

## 9. Edge cases

| Case | Handling |
| --- | --- |
| Contradictory relations ("A twice B" and "B twice A") | `update_weights` rejects them; Ana asks which holds |
| Relations plus explicit numbers that conflict | Explicit numbers win; Ana says which relation could not be met |
| All weights 0 | Rejected |
| Vendor did not state lead time or did not answer a question | Scores 0 for that criterion, flagged "not stated" |
| Rubric change declined | Pending change cancelled; the scorecard stays on the current version |
| Same rubric proposed again | Cache hit; applied without asking or re-reading |
| A proposal fails to re-read during scoring | That vendor scores 0 on the criterion and is flagged "could not be scored"; the others complete |
| Buyer asks to rank by an old version | `run_scenario` with `scorecard_version` |

## 10. Metrics

- Scoring calls are logged to the existing model-call log as job `live_criterion` (duration, tokens, cache flag).
- Scorecard versions per RFQ and who authored each.
- Pending changes proposed, approved and cancelled.
- How often the top-ranked vendor changes between the cost view and the scorecard view.

## 11. Tests (no model calls; the model wrapper is stubbed)

- The weight formula and normalisation; relation checks, including a contradiction.
- Price and lead-time scoring, including missing values.
- Minimum-score gates and their fail reasons.
- Ranking order: passing vendors first, then score, then cost.
- Unchanged rubric means no scoring call; a changed rubric creates a pending change.
- `apply_pending` without approval is refused; approval through the button endpoint works.

## 12. Files

- **New:** `app/live_scorecard.py` (model, storage and versions, formulas, standard proposal, criterion scoring, pending changes).
- **Changed:**
  - `app/live_compare.py`: score columns and ranking in `compute`; scoring of existing criteria during a run.
  - `app/live_analyst.py`: the tools, prompt rules, approve and cancel endpoints.
  - `web/views/compare-live.js`: Scorecard tab, rank toggle, confirmation card, progress.
  - `web/tokens.css`, `tests/`.
- **Delivery:** built and tested in `Backup/Build`, pushed to GitHub, then pulled on the server, as before.

## 13. Defaults to confirm

1. A missing value scores 0 (rather than being left out of that vendor's total).
2. Price and lead time are scored relative to the best vendor (rather than against a target from the RFQ).
3. A blanket answer counts at its own score for every question.
4. The questionnaire-average gate stays alongside the new per-criterion minimums.
