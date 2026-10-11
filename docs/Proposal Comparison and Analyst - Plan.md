# Proposal comparison and analyst: plan

Pick a finalised RFQ, see every proposal received for it, and run the notebook's Phase 6 pipeline on them: extract, normalise, check compliance, then compare single-source against cherry-pick. The comparison stays on the left and a chat with an analyst agent sits on the right. The analyst answers questions from the saved tables and can add new comparison parameters. Any proposal file opens in a zoomable viewer.

- Engine source: Phase 6 cell (cell 5) in `Build/rfx_copilot.ipynb`
- Builds on: Live RFQ (`app/live_rfx.py`); RFQs and uploads live in `state/live/rfqs/<RFQ-ID>/`
- Design: same tokens and components as Live RFQ

## Layout

```
┌ Nav ┬───────────── Comparison (left) ───────────────┬──── Analyst chat (right) ────┐
│     │ RFQ-L-004 ▾   9 proposals · run R-003 · [Run] │ messages…                    │
│     │ [Apex][Best Tech][Orange City (5 files)]…     │                              │
│     │ Summary | Lines | Cherry-pick | Q&A | Custom  │                              │
│     │ ┌──────────────────────────────────────────┐  │                              │
│     │ │ table for the selected tab               │  │ [ ask about the quotes ]     │
│     │ └──────────────────────────────────────────┘  │                              │
└─────┴───────────────────────────────────────────────┴──────────────────────────────┘
```

Opened from a finalised RFQ (a **Compare quotes** button in Live RFQ and in History) at `#/compare-live?id=RFQ-L-004`.

## 0. What changes from the notebook's Phase 6

| Phase 6 today | Change | Why |
| --- | --- | --- |
| Re-reads `RFQ.pdf` with the LLM to rebuild the RFQ | Use the RFQ's stored lines and questions directly | Exact, free, no extraction errors |
| One file = one vendor; vendor name from the file name | One **proposal = one file per organisation**; name from the upload form. A new upload from the same organisation replaces the earlier file | Orange City's 5 JPGs were counted as 5 vendors |
| Lines matched on the vendor's item text | Model returns the **RFQ line number** for each quoted row; tables join on it | Cherry-pick and coverage group the same item correctly |
| Shortfall = total RFQ qty − total quoted qty | Shortfall **per line**, then summed; missing lines listed | Over-quoting one item no longer hides a missing one |
| USD→INR fixed at 84 | A setting per run (default 84) | Rate shown and changeable |
| Vendors with no questionnaire answers pass (score NaN) | **No answers → FAIL.** One blanket answer for all questions ("we accept all terms") → PASS, flagged. Answered one by one → scored as in Phase 6 | NaN quietly passed 4 of 9 vendors |
| Tables printed and lost | Saved to disk per run and reloaded by the analyst | Requirement |

Extraction schema, prompts, confidence scoring, the discount rule, knock-outs and both scenarios stay as in Phase 6.

## 1. RFQ ↔ proposal mapping

- A proposal is one uploaded file, stored in the RFQ's folder with its organisation name and source (`link`, `email` or `buyer`). The RFQ's upload list is the mapping.
- **One file per organisation:** the vendor page accepts a single file. Uploading again from the same organisation replaces the earlier file; the old one is kept, marked superseded, and is left out of comparisons.
- **Buyer can add proposals** on the comparison page: **Add proposal** (organisation name + one file) for quotes that arrived by email or WhatsApp. The buyer can also remove one.
- **One-time import for RFQ-L-004:** copy `~/Downloads/Proposals/` into RFQ-L-004 as `buyer` uploads, naming organisations from file names. **Orange City Infotech is skipped** (8 proposals).

## 2. Extraction and comparison engine (`app/live_compare.py`, new)

- **Reading files:** Phase 6's `read_file_to_llm_parts` (PDF text via pymupdf4llm, else page images; images; DOCX; XLSX; TXT). Add HTML (text) and CSV.
  - New dependencies in `requirements.txt`: `pymupdf`, `pymupdf4llm`, `python-docx`, `tabulate`.
- **Extraction:** one `llm.call` per proposal, using Phase 6's `VendorProposal` schema plus `rfq_line` on each line item and `questionnaire_style` (`individual`, `blanket` or `none`).
  - The context lists the RFQ lines with their numbers, locations and quantities, plus the questions.
  - `llm.call` caches by input hash, so re-running unchanged proposals costs nothing.
- **Math engine:** Phase 6's normalisation (currency, "box of N"), audits, knock-outs, single-source and cherry-pick, joined on `rfq_line`.
- **Runs:** **Run comparison** creates run `R-001`, `R-002`, … in the background (4 proposals at a time). The page shows each proposal as queued, extracting, done or failed, like email sending.
  - A failed proposal shows the error and can be retried alone.
  - Settings stored with the run: FX rate (84) and QA threshold (6.0).

## 3. Saved dataframes

```
state/live/rfqs/RFQ-L-004/analysis/
  latest.txt                     → R-003
  R-003/
    manifest.json                run settings, model and prompt versions, file hashes, row counts, per-proposal status
    extractions/<proposal>.json  raw VendorProposal per proposal (evidence for answers)
    rfq_lines.parquet            n, item, location, qty
    line_items.parquet           df_proposals + rfq_line, proposal_id
    questionnaire.parquet        df_questionnaire
    vendor_summary.parquet       vendor_totals (costs, terms, coverage, shortfall, QA, compliance)
    cherry_pick.parquet          cherry_pick_df
    parameters.parquet           custom parameters added through the analyst (vendor × parameter, value, evidence)
    derived/<name>.parquet       tables the analyst saves while answering questions
```

- Parquet keeps column types (pyarrow is installed). The manifest makes a run reproducible.
- The page and the analyst read the latest run unless the user picks an older one.
- **Re-running** carries the custom parameters forward and re-extracts only proposals whose files changed.

## 4. Analyst chat (right side)

`POST /api/live/rfqs/{id}/analyst/messages` streams Server-Sent Events like Live RFQ. When a tool changes a table, the left side redraws.

**Tools.** The model never does arithmetic itself; every number comes from a tool.

| Tool | Does |
| --- | --- |
| `list_tables` / `describe_table` | Names, columns, types, row counts and a few sample rows |
| `query_table` | Filter, group by, aggregate (sum, mean, min, max, count), sort, limit, with an optional `save_as` into `derived/` |
| `run_scenario` | Re-run single-source or cherry-pick with changed settings: exclude vendors, FX rate, QA threshold, require all locations, max share per vendor |
| `get_evidence` | The raw extraction and source text for a vendor, so answers can quote the proposal |
| `suggest_parameters` | Proposes criteria found in the proposals but not yet compared, e.g. warranty years, on-site support, delivery split, price validity |
| `add_parameter` | Extracts one new criterion from every proposal (value + quote + confidence), adds it to `parameters.parquet` and the **Custom** tab |
| `open_proposal` | Opens the viewer on a vendor's file, with a page number when known |

- **No model-written Python is run.** `query_table` takes a structured spec, so the agent can't read or change anything outside the run folder.
- Chat history is saved per RFQ, next to the run.

## 5. Comparison (left side)

- **Header:** RFQ picker (finalised RFQs only); proposal chips (organisation, file count, extraction status; click to open the viewer); **Add proposal**; **Run comparison**; run picker and settings.
- **Tabs:**
  - **Summary:** `vendor_summary`, ranked, with PASS/FAIL pills and the reason, landed cost, discount, lead time, payment terms, coverage, QA score.
  - **Lines:** RFQ line × vendor unit price (INR); the cheapest compliant price is highlighted and low-confidence cells are marked.
  - **Cherry-pick:** chosen vendor per line, total, and savings against the best single source.
  - **Q&A:** question × vendor answers with scores and reasons.
  - **Custom:** parameters added by the analyst, with the quoted evidence.
- **Click-through:** clicking any cell opens that vendor's proposal in the viewer.
- **Download:** each table downloads as CSV or Excel.

## 6. Proposal viewer (modal)

- **PDFs:** pages rendered to PNG on the server with PyMuPDF at 2× (cached), at `GET /api/live/rfqs/{id}/proposals/{pid}/files/{n}/pages/{p}.png`. PDFs and images then use the same viewer and need no PDF library in the browser.
- **Images:** served as uploaded.
- **DOCX, XLSX, HTML, TXT:** shown as extracted text or a table, with a **Download original** button.
- **Controls:**
  - Zoom: −, +, Fit width, 100%, and ⌘/Ctrl + scroll; 25%–400%.
  - Scrolling: vertical and horizontal, plus click-and-drag to pan when zoomed in.
  - Pages: stacked vertically with a "Page 2 / 5" indicator and page jump.
- Esc closes the viewer.

## 7. Routes

| Route | Purpose |
| --- | --- |
| `GET/POST /api/live/rfqs/{id}/proposals` | List, add (buyer upload) |
| `PATCH /api/live/rfqs/{id}/proposals/{pid}` | Rename, move or remove files |
| `POST /api/live/rfqs/{id}/runs` · `GET …/runs/{run}` | Start a run, read progress and tables |
| `GET …/runs/{run}/tables/{name}` (`?format=csv\|xlsx`) | A table as JSON or a download |
| `GET …/proposals/{pid}/files/{n}` · `…/pages/{p}.png` | Viewer files and pages |
| `POST …/analyst/messages` (SSE) · `GET …/analyst` | Analyst chat |

## 8. Out of scope

- Award decisions, approvals and purchase orders.
- Automatically re-running when a new proposal arrives (the page shows "1 new proposal since this run").
- Editing extracted values by hand (the analyst can show the evidence; corrections come later).
- OCR quality tuning for blurry photos beyond Phase 6's confidence flag.

## Files

- New: `app/live_compare.py` (mapping, engine, runs, tables, viewer pages), `app/live_analyst.py` (agent and tools), `web/views/compare-live.js`, `web/viewer.js` (proposal viewer)
- Changed: `app/main.py` (routers), `web/app.js` (nav), `web/views/live.js` (Compare quotes button), `requirements.txt`, `web/tokens.css`

## Decisions made

1. **Questionnaire:** no answers → FAIL; one blanket answer → PASS with a flag; individual answers → scored, FAIL below the QA threshold.
2. **Files:** one file per organisation per RFQ; Orange City Infotech is not imported.
