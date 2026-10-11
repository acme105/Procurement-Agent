# Live RFQ Builder: plan

Build the RFQ by chatting with the agent ("Pro"). The RFQ updates live in the centre while the chat runs on the right. Instead of typing "END", the buyer clicks **Finalise RFQ**. Only after that can the RFQ be emailed or downloaded.

- Agent source: `GuidedCopilot` in `Build/rfx_copilot.ipynb`
- Design: `Documents/Designs/Design System.dc.html` (its colours and fonts are already in `Build/web/tokens.css`)
- Host: the existing FastAPI app in `Build/`

## Layout

```
┌ ShellNav ┬──────── RFQ (live) ─────────┬──── Chat with Pro ────┐
│          │ Title / scope               │ messages…             │
│          │ Line items table            │                       │
│          │ Questionnaire               │ [ type a message ]    │
│          │              [Finalise RFQ] │                       │
└──────────┴─────────────────────────────┴───────────────────────┘
```

## 1. Backend: `app/live_rfx.py` (new)

- Move `GuidedCopilot`, its tools and `search_catalog` out of the notebook. Every model call goes through `app/llm.py`.
- Save `rfx_state` and the message history to the store (`S`) after each turn, so a refresh keeps the draft.
- Routes:
  - `POST /api/live/messages` streams Server-Sent Events:
    - an `rfx` event with the full state after **each** tool call (`set_rfx_scope`, `add_to_rfx`, `remove_from_rfx`, `set_questionnaire`), so the RFQ changes while the agent is still working
    - a `reply` event with Pro's text at the end of the turn
  - `GET /api/live/rfx`: current state, used on page load
  - `POST /api/live/reset`: start a new draft

## 2. Frontend: `web/views/live.js` (new) and a nav entry

- **Chat (right):** message bubbles in the Design System style, with a typing indicator while the stream is open.
- **RFQ (centre):** scope, line items (number, description, site, quantity) and the questionnaire, redrawn on each `rfx` event. Rows that changed get a brief highlight.

## 3. Finalise, then send

### 3a. Finalise

- The **Finalise RFQ** button sits under the RFQ. It stays disabled until there is a scope and at least one line item.
- After the buyer confirms, `POST /api/live/finalise`:
  - locks the RFQ as `FINAL` and stores the time it was finalised
  - creates the PDF once from `templates/rfx.html` and saves it, so every copy sent later is the same file
  - disables the chat
- Any send or download request made before this is refused with HTTP 409.

### 3b. Send (appears only after finalising)

The Finalise button is replaced by a **Send RFQ** panel:

- **Recipients:** the buyer types or pastes email addresses (separated by commas or new lines), and each one becomes a removable chip. Invalid addresses are flagged as they are typed.
- **Subject and message:** pre-filled with the RFQ title, deadline and a short note. Both can be edited.
- **Send:** `POST /api/live/send` with the address list. It sends one email per address with the saved PDF attached, through `smtplib`. Each email is also saved to `state/outbox` and recorded in the audit log.
- **Download PDF:** `GET /api/live/rfx.pdf` downloads the same saved file so the buyer can forward it by hand.
- **Sent so far:** a list of each address, the time it was sent, and whether it was sent or failed. The buyer can send to more addresses later. Each send is added to the list, and the same locked PDF is used every time.
- If the SMTP settings are missing, Send is disabled with a hint, and Download still works.

**New RFQ** resets everything and starts a new draft.

### SMTP settings (environment)

| Variable | Example |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | sender account |
| `SMTP_PASS` | app password (never commit it) |
| `SMTP_FROM` | display name and address |

Before emailing real vendors, test by sending to your own address.

## 4. Out of scope

- Voice input. "Speak" means typing for now. The browser's speech-to-text could be added later with no backend change.
- Editing the RFQ by hand in the centre panel. All changes go through the chat.
- A vendor picker, approval steps and vendor eligibility checks.

## Files

- New: `app/live_rfx.py`, `web/views/live.js`
- Changed: `app/main.py` (one-line router registration), `web/app.js` (nav entry)
