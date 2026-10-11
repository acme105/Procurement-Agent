# Procurement: Product Requirements Document

Scope of this document: the product exactly as deployed (release `9514b60`, a single-tenant app on one Hetzner server). Anything not deployed is listed under [Out of scope](#9-out-of-scope). A companion document, [TRD.md](TRD.md), covers the technical design.

## 1. Goal

**Take a buyer from "I need to buy X" to a defensible, side-by-side comparison of vendor quotes without retyping a single quote into a spreadsheet.**

The week this removes: a buyer writes an RFQ, emails vendors, and gets back an Excel that ignores the template, a PDF with the discount in a footnote, a Word document, a photo of a rate card and a one-line email. They retype everything, then a manager asks "what if we split it, cheapest per line, but only among vendors who answered the questionnaire?" and a further day goes.

| Goal | Success looks like |
| --- | --- |
| G1. Draft an RFQ by conversation | A buyer reaches a finalised RFQ (scope, line items by site, supplier questions) in one chat session |
| G2. Collect quotes in any format | Vendors reply with a PDF, photo, Excel, Word, CSV or text file through a link, with no template |
| G3. Compare like with like | Every proposal is read against the RFQ's own lines and shown in one table: same lines, same units, INR |
| G4. Trust the result | Every verdict says why, names what is missing or extra, and links to the vendor's own document |
| G5. Ask instead of click | A buyer asks questions in plain language and every number in the answer comes from a saved table |

**Non-goals:** awarding, approvals, purchase orders and vendor management (see section 9).

## 2. Users and personas

The product has one login (the buyer) and one open page (the vendor upload page). The other two personas are served by what the buyer produces.

| Persona | Role in the product | What they care about |
| --- | --- | --- |
| **Buyer** (category buyer, the only logged-in user) | Drafts the RFQ, finalises, sends, collects, compares, asks questions | Speed; not retyping; not missing a compliance gap; being able to show their working |
| **Vendor contact** (sales or pre-sales at a supplier, no login) | Opens a link, enters an organisation name, uploads one quote file | Replying in the format they already have; not creating an account; knowing the upload landed |
| **Approver** (procurement head or VP, reads the buyer's output, no login) | Reviews the comparison the buyer shares or screen-shares | A clear answer, the savings figure, why a vendor was rejected, whether the numbers can be trusted |
| **Operator** (whoever runs the deployment) | Installs, updates, rotates secrets, watches disk and cost | Safe defaults, no secrets in the repo, restartable, bounded memory |

## 3. Needs and how the product meets them

| Persona | Need | Capability that meets it |
| --- | --- | --- |
| Buyer | Write an RFQ without a form | Ana (co-pilot) asks one step at a time: scope, line items from the catalog or free-text requirements per site, supplier questions. The RFQ redraws live as she works |
| Buyer | Lock the RFQ before it goes out | Finalise: locks content, prints one PDF, disables further editing |
| Buyer | Get the RFQ to vendors by whatever channel | Email to typed addresses (PDF attached, personal upload link), download the PDF, or copy one shareable upload link for WhatsApp or similar |
| Buyer | See who replied | Send log with per-recipient status; "Quotes received" list by organisation with downloadable files |
| Buyer | Read any quote format | Extraction from PDF, image, Excel, Word, CSV, HTML and text against the RFQ's lines and questions |
| Buyer | Know which vendors are compliant | Compliance verdict per vendor with named reasons (see section 6) |
| Buyer | Find the cheapest split | Single-source ranking and cherry-pick (cheapest compliant vendor per line), with saving versus the best single source |
| Buyer / Approver | Trust a number | Click a vendor to open its original document in a zoomable viewer; extraction confidence on every row; the evidence behind each answer |
| Buyer | Answer the manager's what-if | Ana re-runs scenarios: exclude vendors, change the exchange rate or QA threshold, and saves the result as a table |
| Buyer | Compare on something not in the RFQ | Ana reads a new parameter (for example warranty) from every proposal and adds it as a column with a quoted source |
| Vendor | Reply in any format, once | One file per organisation; a later upload replaces the earlier one |
| Operator | Run it safely | Buyer login on everything except the vendor page; secrets only in a root-only file; one process with a memory cap |

## 4. Capabilities (as deployed)

### 4.1 List of RFQs (draft, finalise, send)
- RFQ history (every RFQ with scope, status, lines, sent count, replies); open any RFQ; **New RFQ**.
- Chat with Ana; tools: search the item catalog (269 IT, office and packaging items), set scope, add line items with quantity per site, remove lines, set the supplier questionnaire, look up facts on Wikipedia.
- Finalise (requires a scope and at least one line); finalised RFQs are read-only.
- Send panel (after finalise): recipient chips with validation, editable subject and message, per-recipient upload link, background sending with live status (SENDING, SENT, FAILED), PDF download, shareable link with copy button.

### 4.2 Vendor upload page (`/q/<token>`, no login)
- Shows the RFQ (scope, lines, questions); requires an organisation name; accepts one file (PDF, Excel, Word, CSV, image or text, up to 20 MB); replaces the organisation's earlier file.

### 4.3 Compare quotes
- Proposals are the RFQ's uploads. The buyer can add a proposal (for quotes that arrived by email or WhatsApp) or remove one.
- **Run comparison**: extracts every proposal in the background (progress per proposal), builds the tables, saves them. Re-run reuses unchanged extractions.
- Tabs: Summary (verdict and terms per vendor), Lines (price per line per vendor, cheapest highlighted), Cherry-pick, Q&A (answers and scores per question), Custom (parameters added by Ana), Analyst tables (tables Ana saved). Every table downloads as CSV or Excel.
- Headline figures: best single source, cherry-pick total, saving, number of compliant vendors.
- **View RFQ** and **proposal viewer**: zoom 25–400%, scroll both ways, drag to pan, page jump; PDFs and images natively, spreadsheets as tables, text as text.
- Ana (analyst): query, filter, group and sort any saved table; re-run a scenario; read a vendor's extraction evidence; add a parameter; open a proposal.
- **Weighted scorecard:** vendors are ranked by a score out of 100 over criteria: price, lead time, each RFQ question, and custom ones. The model proposes the starting weights from the RFQ. In chat the buyer can restate weights or relations ("price 50, delivery twice the laptop warranty"), set a minimum score that fails a vendor, and add or change a criterion with a rubric ("10 only if it names the dispatch origin and a date per site"). Weight and minimum changes recompute instantly; a new or changed rubric re-reads the vendors' documents and asks the buyer first (Proceed or Cancel, or a typed yes). Each change saves a new scorecard version; a Scorecard tab shows every criterion, weight, score and the reason, with a version picker (an earlier version is shown read-only with its own scores, and **Make current** restores it as a new version); Summary can rank by score or by cost.

### 4.4 Platform
- HTTPS, buyer login (HTTP Basic, one shared credential), vendor links protected by random 128-bit tokens.

## 5. Primary flow

```
Buyer chats with Ana  ->  Finalise (PDF printed, RFQ locked)  ->  Send by email / share link / download PDF
        |                                                                  |
        v                                                                  v
  RFQ redraws live                                      Vendors upload one file each via /q/<token>
                                                                           |
                                                                           v
   Buyer asks Ana  <-  Compare quotes (extract, normalise, verdicts, tables)  <-  Run comparison
```

## 6. Business rules (the contract the comparison enforces)

| Rule | Behaviour |
| --- | --- |
| Match to the RFQ | Each quoted row is matched to an RFQ line by item and location. Prices are compared per RFQ line, never by the vendor's own wording |
| Units and currency | Currency converted to INR (USD at a configurable rate, default 84). "Box of N" is converted to pieces, and unit prices to per piece |
| Quantity shortfall | A vendor fails if any RFQ line is unquoted or under-quoted. The reason names the missing items and their line numbers |
| Items not in the RFQ | A vendor fails if it quotes an item the RFQ never asked for (for example a laptop bag, or a different laptop size). The reason names the items and row counts |
| Extra locations | Quoting a requested item for locations the RFQ did not list is a flag, not a failure |
| Questionnaire | No answers: FAIL. One blanket answer for all questions ("we accept all terms"): PASS with a flag. Answers question by question: each scored 1–10; FAIL if the average is below the threshold (default 6) |
| Discount and bundle | Overall discount applied to the vendor's total. A vendor with bundle conditions ("discount only if you award all items") stays in the single-source ranking but is excluded from the cherry-pick pool |
| Cherry-pick | Cheapest compliant, non-bundle vendor per RFQ line at the RFQ quantity, before discounts. States its basis |
| One file per vendor | One active proposal per organisation per RFQ; replaced uploads are kept but excluded |
| Extraction confidence | Each row is High, Medium or Low; low-confidence rows are flagged on the vendor |
| Scorecard score | Each criterion scores 0–10 per vendor. Price: lowest landed cost among complete quotes is 10, others in proportion; lead time likewise; a missing value scores 0 and is flagged. A question scores its answer score (a blanket answer scores each question at its own score). A custom criterion is scored by the model against the buyer's rubric, with a reason and a quote. Total = weighted average out of 100 |
| Ranking | Vendors that pass come first, then by score (highest first), then by cost. Cost-only ranking remains a toggle |
| Criterion minimum | A vendor below a criterion's minimum score fails, with the reason named. A criterion that could not be scored (timeout) is flagged, not failed, and is retried |
| Weights | Explicit numbers from the buyer hold exactly ("price 70" is 70 of 100); a relation to a fixed criterion fixes the other; the rest share what is left in proportion; contradictions are refused. The model's own choices are relative and are scaled in |
| Re-reading documents | Only when a rubric is new or changed, and only after the buyer approves. Weight and minimum changes never re-read |

## 7. Edge cases and how they are handled

| Situation | Handling today |
| --- | --- |
| Vendor quotes 27 of 30 lines | Fails on quantity shortfall; the missing items are named; lines it did quote remain comparable |
| Vendor quotes one price for several locations | Rows are split per RFQ location during extraction |
| Vendor quotes in USD, or per "box of 10" | Converted to INR per piece; the raw value, unit and currency are kept next to the normalised one |
| Angled photo or low-quality scan | Read as images; rows the model is unsure about are marked Low confidence and flagged |
| Vendor restricts itself to some locations | Only those rows exist; the rest count as missing |
| Vendor substitutes an item (15-inch instead of 13-inch laptop) | The substitute is caught when it appears as a row that matches no RFQ line, and the vendor fails on "items not in the RFQ". A substitution inside a row that does match is **not** detected (see 10) |
| Vendor gives no questionnaire answers, or a blanket one | FAIL, or PASS with a flag, as in section 6 |
| Vendor uploads twice | The newer file replaces the older; the older is retained but excluded |
| Two uploads under slightly different spellings of one organisation | De-duplicated ignoring case and spacing; other spelling differences create a second proposal (the buyer can remove one) |
| File type not accepted, empty file, over 20 MB | Refused with a plain message; nothing saved |
| Email fails (bad address, SMTP error) | That recipient shows FAILED with the error; the rest still send; the PDF can be downloaded and forwarded manually |
| Email not configured | Send is disabled with a hint; download and shareable link still work |
| PDF printing fails | Finalise is refused with an error; the RFQ stays editable |
| One proposal cannot be read | That proposal shows FAILED with the error; the run completes for the others; it can be re-run |
| Model returns different readings on a re-run | Possible (see 10); extraction is cached by input so an unchanged re-run is stable, and the run records which cache entries it used |
| Analyst asked something the tables do not hold | Ana says so and offers to read it from the proposals as a new parameter |
| Server restarts mid-run | Known issue: the interrupted run stays marked "running" and the app refuses to start another run until the operator marks it failed (see 10) |

## 8. Metrics

The product does not have an analytics dashboard. This table separates what the system records today from what is derivable and what is not tracked.

| Metric | Why it matters | Source | Status |
| --- | --- | --- | --- |
| Time from first chat message to finalised RFQ | G1: RFQ drafting speed | RFQ `created_at` and `finalised_at` | Derivable from stored timestamps |
| Emails sent and failed | Delivery reliability | Per-recipient status, time and error in the RFQ record | Recorded |
| Vendor response rate (proposals received / recipients) | G2: is the channel working | RFQ send log and uploads | Derivable |
| Time from send to first and last quote | Vendor responsiveness | Send and upload timestamps | Derivable |
| Proposals extracted without error, per run | G3: extraction reliability | Run manifest, per-proposal status and error | Recorded |
| Share of rows with Low confidence | G4: how much human checking is needed | Line items table (confidence column) | Recorded per run |
| Compliance pass rate; top fail reasons | Quality of vendor responses | Vendor summary table | Recorded per run |
| Cherry-pick saving versus best single source (₹ and %) | The value the comparison delivers | Run summary | Recorded per run |
| Extraction time per run; model calls, tokens, cache hits | Cost and speed of G3 | Model-call log (duration, tokens, cached, status) | Recorded for extraction and parameter reads |
| Analyst questions per RFQ; share answered from a table query | G5: adoption and grounding | Saved analyst chat | Derivable; tool use is not counted |
| Scorecard versions per RFQ; weight and minimum changes; pending changes proposed, approved, cancelled | Adoption of the scorecard and how often buyers override the model's weights | Scorecard version files and the pending-change file | Recorded |
| How often the top vendor by score differs from the top by cost | Whether the scorecard changes decisions | Run summary (top by score, top by cost) | Recorded per run |
| Model cost for drafting and analyst chat | Cost per RFQ | Not logged: chat calls bypass the model-call log | **Not tracked** |
| Active users, sessions, page views, feature use | Adoption | Nothing | **Not tracked** |
| Application errors, request latency, uptime | Operations | Service log only (no aggregation or alerting) | **Not tracked** |

**Targets** are not set; with one buyer and a demo data set there is no baseline. The aim of this release is to make the metrics above measurable, not to meet a number.

## 9. Out of scope

Not built, not deployed, and not implied by the product:

- **Users and security:** accounts, roles, per-user permissions, single sign-on, password reset, audit trail of who did what, rate limiting, malware scanning of uploads.
- **Sourcing process:** vendor master and eligibility checks, automatic vendor shortlisting and invitations, deadlines and reminders, a response tracker, clarification requests to vendors, reading vendor replies from an inbox, other channels (WhatsApp, SMS) beyond a shareable link.
- **After the comparison:** award decisions and scenarios beyond the two shown, approvals, purchase orders, contracts, exports of an award pack, charts and dashboards.
- **Data:** live exchange rates, tax (GST) normalisation, landed cost with freight, price history, item-master administration (the catalog is a static file), multi-category catalogs.
- **Scorecard:** editing weights with sliders or fields (chat only), scorecard templates reused across RFQs, a per-line scorecard in the cherry-pick (it stays price-based), sensitivity charts, sign-off by a second person.
- **Quality:** detection of spec substitutions inside a matched line, human correction of extracted values, an evaluation set with measured extraction accuracy.
- **Platform:** multiple tenants, multiple servers or workers, high availability, automated backups, monitoring and alerting, CI/CD, data-retention and privacy policies, localisation, a mobile-specific layout, an accessibility audit.

## 10. Known limitations and risks

| Limitation | Effect | Mitigation today |
| --- | --- | --- |
| Extraction is model-based and not deterministic | The same file can be read differently on different runs (observed on one vendor, where a total moved from ₹10.9 crore to ₹8.0 crore) | Cached by input; low-confidence flags; original document one click away. A measured accuracy set is out of scope |
| Custom-criterion scores come from the model reading the document | The same document can score differently between fresh readings; re-reading scans and photos is slow (minutes) and can time out | Scores are cached per rubric and file; each score shows its reason and a quote; a timed-out vendor is flagged and retried, not failed; the buyer is told the time estimate first |
| Substitutions inside a matched line are not detected | A vendor offering a close but different spec could pass | Specs are visible in the document and can be asked of Ana |
| One shared login | No individual accountability | Behind HTTPS; the credential is held only by the operator and buyer |
| No rate limit | Anyone with a vendor link can upload many files | Links are random and per recipient; one active file per organisation |
| Single process and local files | A restart during a run leaves it marked "running", which blocks new runs until cleared by the operator; no failover | The fix is small (mark stale runs failed on start-up) and not yet made; re-running is cheap because extraction is cached |
| Shared server with other products | Disk (88% used) and memory are shared | Memory-capped service; disk monitored by the operator |
| Public vendor page | A leaked link allows uploads to that RFQ | Per-recipient random tokens; there is no revoke button, so a new RFQ is needed to get new links |

## 11. Open questions

1. Should an unrequested item always fail a vendor, or only above a policy threshold?
2. Should a substitution inside a matched line be detected (requires a per-row "differs from the RFQ" field in the extraction)?
3. Who may see which RFQs once more than one buyer uses it?
4. What accuracy is acceptable for a ₹4 crore decision, and who signs off the evaluation set?
