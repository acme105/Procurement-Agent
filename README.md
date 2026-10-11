# Role Discovery Sourcing: Live RFQ and Compare quotes

Take-home prototype for "Kill the Quote Spreadsheet". Two parts of one flow:

1. **Live RFQ**: talk an RFQ into existence with an AI co-pilot ("Ana"); it fills in scope, line items and supplier
   questions live. **Finalise** locks it and prints the PDF; then email it to addresses you type, download the PDF, or
   copy a shareable vendor upload link. Vendors upload one quote file under their organisation name.
2. **Compare quotes**: reads every proposal (PDF, photo, Excel, Word, text) against the RFQ's own lines, normalises
   units and currency, applies compliance rules, and shows single-source and cherry-pick comparisons. Chat with an
   analyst ("Ana") over the saved tables, add new comparison parameters, and open any proposal in a zoomable viewer.

The AI loops are real: extraction, scoring and the analyst's answers come from the model, and every number the analyst
states comes from a table query, not from the model's arithmetic.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=...            # or put it in ~/.config/aerchain/openai.env (see .env.example)
uvicorn app.main:app --port 8000     # open http://localhost:8000/
```

Demo data (an 81-line RFQ and 8 fabricated vendor proposals), then press **Run comparison**:

```bash
python -m scripts.seed_demo
```

Tests (no model calls): `pytest -q`

## Settings

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Model access (environment or `~/.config/aerchain/openai.env`) |
| `AERCHAIN_MODEL` | Model for every job (default `gpt-6-luna`) |
| `SMTP_HOST/PORT/USER/PASS/FROM` | Sending email (environment or `~/.config/aerchain/smtp.env`). Optional |
| `AERCHAIN_PUBLIC_URL` | Public address used in vendor upload links |
| `AERCHAIN_CATALOG`, `AERCHAIN_STATE`, `AERCHAIN_CHROME` | Catalog CSV, state folder, Chrome path (PDFs need Chrome) |

Secrets are never read from the repository. `state/` holds RFQs, proposals, upload links and sent-email logs and is
git-ignored.

## Security notes

- There is **no login**. Run it locally. Do not expose it to the internet as is: anyone who can reach it could send
  email from your SMTP account and spend your model credit. Put it behind a password before sharing a public address;
  vendors only need `/q/<token>`.
- Vendor links are random tokens; anyone holding one can upload a file to that RFQ.

## Layout

```
app/          FastAPI: live_rfx (RFQ chat, finalise, send, vendor page), live_compare (extraction and tables),
              live_analyst (analyst agent), llm (model wrapper, cache), printing (HTML to PDF)
web/          Front end: views/live.js, views/compare-live.js, viewer.js, tokens.css (design system)
data/         catalog.csv (IT hardware, office supplies, packaging)
samples/      Demo RFQ and 8 fabricated vendor proposals
scripts/      seed_demo.py
docs/         Plans and decisions
rfx_copilot.ipynb   The original exploration notebook (outputs removed). Reference only; the app does not need it.
tests/        pytest
```
