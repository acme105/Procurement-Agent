# Procurement: Technical Requirements Document

Describes the system as deployed (release `9514b60`). Product context, personas and business rules are in [PRD.md](PRD.md). Where this document says "not built", it means it does not exist in the deployed release.

## 1. Summary

A single-process Python web app (FastAPI) with a no-build JavaScript front end. State is files on local disk. Two model jobs do the heavy reading (proposal extraction and one-parameter extraction); two chat agents (the co-pilot and the analyst) call fixed tools. It runs as one systemd service on a shared Hetzner VM behind an existing nginx reverse proxy.

| Concern | Choice |
| --- | --- |
| Back end | Python 3.12, FastAPI, uvicorn (one worker) |
| Front end | ES modules, no framework, no bundler; styles are the design-system tokens |
| Model | OpenAI API, default model `gpt-6-luna` (setting `AERCHAIN_MODEL`) |
| Storage | JSON and Parquet files under one state directory |
| Documents | PyMuPDF and pymupdf4llm (PDF), python-docx (Word), pandas and openpyxl (Excel, CSV) |
| PDF output | Headless Google Chrome (A4) |
| Email | SMTP over STARTTLS (Gmail) |
| Hosting | One Hetzner VM (Ubuntu 24.04, 2 vCPU, 4 GB RAM, 40 GB disk) shared with other products |

## 2. Architecture

```
Browser (buyer)                                   Browser (vendor)
   |  HTTPS, Basic auth                              |  HTTPS, no login (token in URL)
   v                                                 v
 nginx container (existing, shared; TLS, host routing, 25 MB body limit, no SSE buffering)
   |  proxy_pass to the VM's Docker bridge address, port 8085 (not reachable from the internet)
   v
 uvicorn (1 worker)  <-- systemd service "aerchain", user aerchain, MemoryMax 1.2 GB
   |
   +-- app/main.py         buyer login middleware, routers, static files
   +-- app/live_rfx.py     RFQ state, co-pilot chat (SSE), finalise, PDF, send, vendor page and upload
   +-- app/live_compare.py proposals, extraction runs, math engine, tables, viewer pages, RFQ viewer
   +-- app/live_analyst.py analyst chat (SSE) and its tools
   +-- app/llm.py          model wrapper: structured output, cache, run log
   +-- app/printing.py     HTML to PDF with headless Chrome
   +-- web/                views/live.js, views/compare-live.js, viewer.js, components.js, tokens.css
   |
   +--> OpenAI API (extraction, chat)      +--> SMTP (Gmail)      +--> Wikipedia (fact lookup tool)
   +--> state/  (local disk, section 4)    +--> google-chrome (subprocess)
```

The app is stateless apart from the state directory; all long work (extraction, email) runs in background threads inside the one process.

## 3. Modules and responsibilities

| Module | Responsibility |
| --- | --- |
| `live_rfx.py` | Create, load, save and list RFQs. Co-pilot: loop of model call, tool call, state update, and an SSE `rfx` event after every tool call. Finalise (validate, print PDF, lock, create the shareable token). Send (validate addresses, create per-recipient tokens, deliver in background). Vendor page and upload (`store_upload`: type and size checks, one file per organisation). |
| `live_compare.py` | File reading to model input; extraction run orchestration; the math engine (`compute`); parameter extraction; table storage and download; proposal and RFQ viewer data and page images. |
| `live_scorecard.py` | Scorecard versions, formulas, weight and relation arithmetic, the standard proposal, criterion scoring, pending changes with the approval guard, and its API. |
| `live_analyst.py` | System prompt with the run's context and scorecard; thirteen tools; SSE stream; saved chat. |
| `llm.py` | `call()`: structured output through the Responses API, cached by input hash; appends a record per call to the run log. `client()` for the chat agents. |
| `printing.py` | Writes HTML to a temp file, runs Chrome, waits for a stable PDF, kills a lingering process. |
| `main.py` | Buyer login middleware (exempts `/q/` and `/api/q/`), router registration, health, redirect to the app. |

## 4. Data model

Everything lives under `STATE_DIR` (default `./state`, on the server `/opt/aerchain/state`). No database.

```
state/
  runs.jsonl                         one JSON line per model call made through llm.call
  cache/<job>/<input_hash>.json      cached structured model results
  live/
    current.txt                      id of the RFQ the buyer last opened
    rfqs/RFQ-L-NNN/
      state.json                     the RFQ record (below)
      rfq.pdf                        the finalised PDF (the exact file vendors receive)
      uploads/NNN-<name>             vendor files; .pages/ holds cached page images
      .rfq-pages/                    cached page images of rfq.pdf
      analysis/
        latest.txt                   id of the latest completed run
        chat.json                    analyst chat history
        scorecard/
          v1.json, v2.json, …        versions: criteria, weights, minimums, rubrics, author, note
          current.txt  pending.json  current version; changes awaiting approval
          scores/<criterion>.json    per-vendor scores for rubric-scored criteria
        R-NNN/
          manifest.json              run record (below)
          extractions/P*.json        one structured extraction per proposal
          vendor_summary.parquet  scorecard.parquet  line_matrix.parquet  cherry_pick.parquet  line_items.parquet
          questionnaire.parquet   rfq_lines.parquet    parameters.parquet
          derived/<name>.parquet     tables saved by the analyst
```

**`state.json`:** `id`, `status` (DRAFT or FINAL), `created_at`, `finalised_at`, `rfx` (`scope`, `items[]` of item codes, requirement text, location, quantity, and `questionnaire[]`), `messages` (model conversation), `chat` (what the buyer sees), `sent[]` (id, email, time, status, error), `links` (email to token), `share_token`, `uploads[]` (n, organisation, email, source, filename, size, time, stored name, `superseded`).

**`manifest.json`:** `run_id`, `status` (running, done, failed), `created_at`, `finished_at`, `settings` (exchange rate, QA threshold), `model`, `prompt_version`, `parameters[]`, `proposals[]` (id, upload number, organisation, filename, SHA-256, status, error, cache flag, model-call id), `summary` (headline figures), `error`.

**Identifiers:** RFQs are `RFQ-L-NNN`, proposals `PNNN` (from the upload number), runs `R-NNN`; vendor and share links are 128-bit random URL-safe tokens.

**Writes:** whole-file JSON writes under a process-wide lock; no transactions and no schema migrations. Tables are Parquet so column types survive.

## 5. API

All paths are under one origin. "Buyer" means the Basic-auth login is required whenever `AERCHAIN_BUYER_PASSWORD` is set.

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /api/health` | Buyer | Model, whether email, catalog and key are configured |
| `GET /api/live/current`, `GET/POST /api/live/rfqs`, `GET /api/live/rfqs/{id}`, `POST …/open` | Buyer | Current RFQ, history, create, read, switch |
| `POST /api/live/rfqs/{id}/messages` | Buyer | Co-pilot turn; response is SSE (`rfx`, `tool`, `done`) |
| `POST …/finalise`, `GET …/pdf` | Buyer | Lock and print; download the PDF |
| `POST …/send` | Buyer | Start background delivery; returns immediately with each recipient as SENDING |
| `GET /q/{token}` | Open | Vendor page |
| `POST /api/q/{token}/upload` | Open | Vendor upload (organisation name and one file) |
| `GET …/uploads/{n}` | Buyer | Download a vendor file |
| `GET /api/live/compare/rfqs`, `GET …/{id}/compare` | Buyer | Finalised RFQs; proposals and runs for one RFQ |
| `POST …/{id}/proposals`, `DELETE …/proposals/{n}` | Buyer | Add a proposal on a vendor's behalf; remove one |
| `POST …/{id}/runs`, `GET …/runs/{run}`, `GET …/runs/{run}/tables/{name}?format=csv\|xlsx` | Buyer | Start a run; progress and tables; table download |
| `GET …/proposals/{n}/view`, `…/pages/{p}.png`, `GET …/rfq/view`, `…/rfq/pages/{p}.png` | Buyer | Viewer data and page images |
| `GET …/{id}/scorecard` | Buyer | Current version, criteria, versions, pending changes |
| `POST …/analyst/pending/{pid}/approve`, `…/cancel` | Buyer | Approve (streams progress as proposals are re-read) or cancel a pending rubric change |
| `GET …/{id}/analyst`, `POST …/analyst/messages`, `POST …/analyst/reset` | Buyer | Analyst chat; the message response is SSE (`chat`, `tool`, `tables`, `open`, `done`) |

The static front end (`/web/…`) is also behind the buyer login. The vendor page is self-contained HTML, so it needs no static assets.

## 6. Model usage

| Use | API | Settings | Output |
| --- | --- | --- | --- |
| Proposal extraction (`live_extract`) | Responses API with a Pydantic schema | Reasoning effort medium, up to 32,000 output tokens, one call per proposal | `VendorProposal`: line items (RFQ line, item, location, quantity, unit, price, currency, confidence), questionnaire style and answers with a 1–10 score and a reason, lead time, payment terms, freight, bundle conditions, notes, discount |
| Parameter extraction (`live_param`) | Responses API with a schema | Effort low, 2,000 tokens, one call per proposal | Value, a verbatim quote and a confidence |
| Starting scorecard (`live_scorecard`) | Responses API with a schema | Effort low; one call per RFQ, when it first has a run | A weight and reason per criterion; validated (every criterion once, none extra), normalised to 100, equal weights if invalid |
| Custom criterion (`live_criterion`) | Responses API with a schema | Effort low, one call per proposal; only when a rubric is new or changed and the buyer approved; cached by rubric and file | Score 0–10, reason, quote, confidence |
| Co-pilot | Chat Completions with function tools | Reasoning effort none; loops until the model stops calling tools | Tool calls: search catalog, set scope, add and remove lines, set questions, Wikipedia lookup |
| Analyst | Chat Completions with function tools | Reasoning effort none; at most 12 tool rounds per question | Thirteen tools (below) |

**Controls**
- **Structured output:** extraction output is validated against the schema; a refusal or an incomplete response is recorded as a failed call and that proposal is marked FAILED.
- **Context:** the extraction prompt contains the RFQ's own lines (with numbers) and questions, so the model returns RFQ line numbers rather than free text to match later.
- **Cache:** results are cached by a hash of job, prompt version, schema, model, effort, instructions and content. A re-run with unchanged files makes no model calls.
- **Concurrency and limits:** four proposals extract at once; request timeout 180 s; two automatic retries.
- **Analyst tools:** list and describe tables; query a table with a structured filter, group, aggregate and sort spec (no model-written code is run); re-run a scenario (optionally with an earlier scorecard version); read a vendor's extraction and source text; add a parameter; open a proposal; and for the scorecard: get it, update weights (numbers and relations), set a minimum, propose a criterion, apply a pending change, remove a criterion. Arithmetic on weights is done in code; `apply_pending` is refused unless the buyer's latest message is an affirmative reply sent after the proposal (the Proceed button is the other route). Every number in an answer must come from a tool result, and the system prompt requires it.
- **Run log:** `llm.call` appends one line per call with model, prompt and schema versions, input hash, duration, cache flag, status, error and token usage (input, output, reasoning). Chat calls do not go through `llm.call` and are not logged.

## 7. Processing pipeline

1. **Read the file** to model input: PDF to markdown with pymupdf4llm, or to page images (150 dpi) if little text comes out; images as they are; Word paragraphs and tables; Excel and CSV as markdown tables; HTML and text stripped to text.
2. **Extract** (section 6) into `extractions/P*.json`.
3. **Normalise and join** (`compute`): currency to INR, "box of N" to pieces, rows joined to RFQ lines by line number.
4. **Verdicts:** per vendor, coverage by RFQ line (missing and under-quoted lines, named by item and line range); items and locations that match no RFQ line; questionnaire style and score; flags (blanket answer, bundle conditions, low-confidence rows, extra locations).
5. **Scenarios:** single-source ranking after discount; cherry-pick among compliant vendors without bundle conditions.
6. **Save** the tables as Parquet; update `latest.txt`.

A run executes in a background thread. Only one run may be in progress per RFQ.

## 8. Email and documents

- **Sending:** the request validates addresses, assigns each a token, records every recipient as SENDING, and starts a background worker that opens up to three SMTP connections (STARTTLS, port 587, login) and sends one message per recipient with the RFQ PDF attached and a personal upload link. Each result updates the recipient's status and error.
- **Observed timing:** about 5–10 s to connect and log in, then about 3 s per message.
- **RFQ PDF:** HTML to PDF in headless Chrome, A4, Helvetica (system fonts, embedded as TrueType). Chrome is started with `--no-sandbox` on the server (setting `AERCHAIN_CHROME_NO_SANDBOX`), so Chrome's own sandbox is off; the HTML it prints is generated by the app from escaped text.
- **Viewer:** PDF pages are rendered by PyMuPDF at 2x and cached as PNG; the browser viewer implements zoom, pan and paging with no PDF library.

## 9. Security

| Area | Control | Gap |
| --- | --- | --- |
| Buyer access | HTTP Basic login (constant-time compare) on everything except `/q/` and `/api/q/`; enforced only when `AERCHAIN_BUYER_PASSWORD` is set | One shared credential; no lockout or rate limit; the login is off by default on a developer machine |
| Vendor access | 128-bit random per-recipient token and one shareable token per RFQ | No expiry or revoke; anyone holding a link can upload |
| Transport | TLS terminated at nginx (Let's Encrypt, auto-renewed, nginx reloaded by a renewal hook); HTTP redirects to HTTPS | None known |
| Network | The app listens only on the VM's Docker bridge address, so it is reachable from containers and the host, not the internet | Any container on that host can reach the port |
| Secrets | Model key, SMTP credentials and the buyer password live in a root-only file read by systemd; none are in the repository (`.gitignore`, `.env.example`, scan before each push) | Secrets are not rotated automatically |
| Uploads | Type allow-list (pdf, xlsx, xls, csv, docx, doc, png, jpg, jpeg, txt), 20 MB, filename sanitised, stored outside the web root | No malware scan; no per-IP limit |
| Output encoding | Vendor-supplied text is escaped before it is shown; chat markdown is escaped first | None known |
| Prompt injection | Vendor documents are untrusted model input. Output is schema-constrained, and the analyst can only read the run's tables and cannot run code or send mail | A hostile document could still skew what is extracted for that vendor |
| Data sent out | Proposal content goes to the OpenAI API; the co-pilot may send search text to Wikipedia | Needs a policy decision for real vendor data |

## 10. Deployment

**Host.** One Hetzner Cloud VM (Ubuntu 24.04, x86-64, 2 vCPU, 4 GB RAM, 40 GB disk) that also runs other products in Docker.

| Item | Setting |
| --- | --- |
| Code | `/opt/aerchain`, a clone of the GitHub repository (branch `main`), owned by service user `aerchain` |
| Runtime | Virtualenv `/opt/aerchain/.venv`; Python 3.12.3; FastAPI 0.143, pandas 3.0, pyarrow, PyMuPDF 1.28, OpenAI SDK 3.28; Google Chrome 155 |
| Service | systemd unit `aerchain`: `uvicorn app.main:app --host <docker bridge address> --port 8085 --workers 1`, `Restart=always`, `MemoryMax=1200M`, `NoNewPrivileges` |
| Configuration | `/etc/aerchain/aerchain.env` (mode 600, root): model key, SMTP settings, public URL, buyer password, Chrome path, state directory |
| Proxy | Two server blocks added to the existing nginx container: port 80 (ACME challenge and redirect) and 443 (TLS, `proxy_pass`, 25 MB body limit, buffering off, 300 s read timeout). The config was backed up before editing and is tested before every reload |
| Certificate | Let's Encrypt via certbot (webroot), renewal timer, deploy hook that reloads nginx for this certificate |
| DNS | A free DuckDNS subdomain pointing at the VM |
| Update | `sudo -u aerchain git -C /opt/aerchain pull` then `systemctl restart aerchain` (static front-end files need no restart) |
| Observed | Idle memory about 116 MB; demo state about 17 MB; disk 88% used (shared with other products) |

**Repository layout** (public on GitHub): `app/`, `web/`, `data/catalog.csv`, `samples/` (demo RFQ and 8 fabricated proposals), `scripts/seed_demo.py`, `deploy/` (setup script, service unit, nginx example), `docs/`, `tests/`.

## 11. Non-functional characteristics

| Aspect | Today |
| --- | --- |
| Extraction time | About 2 minutes for 8 proposals (4 at a time), dominated by model latency; a cached re-run is near-instant |
| Chat latency | First update after a tool call within a few seconds; updates stream as each tool finishes |
| Email | About 5–10 s setup plus 3 s per message, in the background; the page shows live status |
| Capacity | Designed for one buyer and tens of RFQs; not load-tested |
| Availability | Single process, no failover; systemd restarts it on failure |
| Backup | None; state is on one disk |
| Observability | systemd journal for the service; the model-call log; per-run and per-recipient status in the state files. No metrics endpoint, tracing or alerting |
| Tests | 21 pytest tests, none call a model: compliance rules (including unrequested and missing items), one file per organisation, upload and finalise rules, the analyst's table query tool, route and login tests, a check that the front end holds no references to removed code, and the scorecard (formulas, weights and relations, minimums, ranking, the approval guard, failed-scoring retry, time estimates) |

## 12. Known technical issues and debt

| # | Issue | Impact | Suggested fix |
| --- | --- | --- | --- |
| 1 | A run interrupted by a restart stays `running`, and starting another run is refused while one is "running" | The operator must edit the manifest by hand | Mark stale runs failed at start-up |
| 2 | One process-wide lock is held for the whole of a co-pilot chat turn (including the model call) | Finalise, upload and send for any RFQ wait while another RFQ's chat turn is in progress | A lock per RFQ, and take the lock only around reads and writes |
| 3 | The co-pilot's tool loop has no round limit | A misbehaving model could loop | Cap rounds as the analyst does |
| 4 | Chat model calls are not logged | Chat cost is invisible | Log chat calls through the same run log |
| 5 | Extraction is not deterministic | A total can differ between fresh extractions | An evaluation set, a second pass on low-confidence rows, human correction |
| 6 | Substitutions inside a matched line are not detected | A close but different item can pass | A per-row "differs from the RFQ" field in the extraction |
| 7 | Whole-file JSON writes | A crash mid-write could corrupt one RFQ | Write to a temp file and rename |
| 8 | No rate limit, link revocation or upload scanning | Abuse is possible by anyone with a link | Per-IP limit, expiring links, antivirus scan |
| 9 | Chrome runs without its sandbox | Printing a hostile document's text is the only exposure (the HTML is generated from escaped text) | Run Chrome in a container or with user namespaces |
| 10 | Model cache and run log grow without bound | Disk is shared and at 88% | Retention policy and cache pruning |
| 11 | Re-reading scans and photos for a new rubric is slow and can time out | A vendor is scored 0 and flagged until retried; the time estimate can be off | Estimates are by file type; failed vendors are retried on the next run or an approved retry; a faster document path for scans would help |
| 12 | Custom-criterion scores are a model judgement | Not reproducible across fresh readings | Reason and quote shown; an evaluation set is out of scope |

## 13. Out of scope (technical)

Not built and not deployed: a database, a job queue, multiple workers or servers, high availability, automated backups, monitoring and alerting, CI/CD and automated deployment, user accounts and per-user permissions, an audit log, rate limiting, malware scanning, per-tenant isolation, live exchange rates, a model evaluation harness, and an inbound email integration. Product-level exclusions are listed in [PRD.md](PRD.md#9-out-of-scope).

## 14. Verification

- **Code:** `pytest -q` (21 tests, no network).
- **Deployed release:** `git log -1` in `/opt/aerchain` and `systemctl is-active aerchain`; `GET /api/health` (behind the login) returns the model and whether email, the catalog and the model key are configured.
- **End to end:** `python -m scripts.seed_demo` loads the demo RFQ and proposals; Run comparison on that RFQ should produce 5 of 8 vendors compliant, with headline figures within the model's run-to-run variation.
