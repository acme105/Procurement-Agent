# Login page: plan

> **Status: built, in a simpler form.** Decisions taken: one shared password and no username (the only role is the analyst/buyer); no lockout for now; a long-lived session with no idle timeout, ending only at **Sign out**; the open health check returns only `{"ok": true}` and the details moved to `/api/health/details` behind sign-in. Kept from this plan: the sign-in page and its errors, session cookie flags, hashed token storage, return-to-page, safe `next`, cross-site request check, auth log, draft kept across an expired session. Left out: named accounts, `scripts/users`, lockout, the idle and 30-day lifetimes. A changed password signs everyone out (added).

Replace the browser's HTTP Basic pop-up with a proper sign-in page, a session, sign-out, and clear handling of every way signing in can fail. Vendors are unaffected: the upload page stays open behind its link token.

**Today** (`app/main.py`): a middleware checks an `Authorization: Basic …` header against one password from `AERCHAIN_BUYER_PASSWORD`, and returns `401` with `WWW-Authenticate`. The browser shows its own grey dialog. As a result:
- there is no way to sign out, and no message for a wrong password;
- there is no protection against guessing;
- a password typed into that dialog is resent with every request.

## 1. What the buyer sees

```
https://…/            (not signed in)
      │
      ▼
┌─────────────────────────────────────┐
│ [logo] Procurement                  │
│                                     │
│ Sign in                             │
│ Username  [ buyer              ]    │
│ Password  [ ••••••••     ] [Show]   │
│ [x] Keep me signed in on this device│
│                                     │
│ [        Sign in        ]           │
│                                     │
│ ⚠ That username or password isn't   │  ← inline, announced to screen readers
│   right. 4 attempts left.           │
└─────────────────────────────────────┘
      │ success
      ▼
the page they asked for (deep links such as #/compare-live?id=RFQ-L-002 survive)
```

- **Top bar:** the signed-in name and a **Sign out** button.
- **After sign-out or an expired session:** the sign-in page explains why ("You've signed out." / "Your session expired. Sign in again to continue where you left off.").

## 2. Error handling

| Situation | What the buyer sees | Server behaviour |
| --- | --- | --- |
| Empty username or password | "Enter your username." / "Enter your password." under the field; nothing is sent | Not called |
| Wrong username or password | "That username or password isn't right." The same message for both, so usernames can't be probed. Attempts left are shown after 3 failures | `401`, failure logged |
| Too many failures (5 in 15 min from one address, or for one username) | "Too many attempts. Try again in 12 minutes." The button is disabled with a countdown | `429` with `Retry-After`; lockout recorded |
| Caps Lock on | "Caps Lock is on" hint next to the password | Client only |
| Server unreachable or timed out | "Can't reach the server. Check your connection and try again." Typed values kept | — |
| Server error | "Something went wrong on our side. Try again in a moment." | `500`, logged |
| Session expired while working | Taken to sign-in with the expiry message and returned to the same page after; an unsent chat message is kept and restored | API answers `401` JSON; the page redirects |
| Signed out in another tab | The next action leads to sign-in with "You were signed out" | `401` |
| A tampered or foreign `next` link (`?next=https://evil…`) | Ignored; goes to the home page | Only same-site relative paths are accepted |
| Already signed in and opening `/login` | Goes straight to the app | `302` |
| JavaScript disabled | The form still posts and the server redirects | Plain form fallback |

## 3. Design

### Accounts
- **Named accounts** (recommended; see decision 1) in `/etc/aerchain/users.json`, root-only, never in the repository: `{username, display_name, password_hash, created_at}`.
- **Password hashes:** `scrypt` from the Python standard library, with a per-user salt. No plaintext anywhere.
- **Adding users:** `python -m scripts.users add <name>` prompts for the password twice. `remove` and `reset` work the same way.
- **Migration:** if no users file exists but `AERCHAIN_BUYER_PASSWORD` is set, a `buyer` account is created from it at start-up. The env variable is then removed from the server.
- **Local development:** with no users configured at all, the app stays open (today's behaviour), with a red "No login configured" banner.

### Sessions
- **Token:** an opaque 256-bit random token in a cookie named `procurement_session`: `HttpOnly`, `Secure` behind HTTPS, `SameSite=Lax`, `Path=/`.
- **Storage:** server-side in `state/sessions.json`, holding only a hash of each token, so a stolen state file can't be replayed. Sessions survive a restart.
- **Lifetime:**
  - 12 hours idle, refreshed on use;
  - "Keep me signed in" extends the cookie to 30 days, with the same 12-hour idle rule;
  - sign-out deletes the session.
- **Exposure:** the middleware attaches the user to each request so the analyst and logs can use the name.

### What is protected
| Path | Without a session |
| --- | --- |
| `/login`, `/api/auth/login`, `/api/auth/logout` | Open |
| `/q/<token>`, `/api/q/<token>/upload` | Open (vendors; unchanged) |
| `/web/` static files (HTML, JS, CSS, logo) | Open. They hold no data (the code is public on GitHub), and serving the app shell lets it read the full URL, including the `#/…` part that a server redirect would lose |
| `/api/health` | Open, but returns only `{"ok": true}`; the configuration details move behind sign-in |
| Every other `/api/…` | `401` JSON `{"detail": "Sign in required", "reason": "expired" or "missing"}` |
| `/` | Redirect to the app, which redirects to `/login?next=…` if `/api/auth/me` returns 401 |

### Attack resistance
- **Guessing:** per-address and per-username failure counters. The address is the real client address from nginx's `X-Real-IP`, trusted only because the app listens on the Docker bridge, not publicly. Lockout is 15 minutes after 5 failures. Every attempt takes the same time whether or not the username exists.
- **Cross-site requests:** `SameSite=Lax` cookies, plus a check that `Origin` (or `Referer`) matches the site on every non-GET API call. This blocks a forged form or fetch from another site.
- **Session fixation:** a fresh token on every sign-in; old tokens for that browser are discarded.
- **Logging:** an auth log at `state/auth.log` records sign-in successes, failures (with reason), lockouts and sign-outs, by time, username and address. Passwords are never logged.
- **Headers on the login page:** `Cache-Control: no-store`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.

### Endpoints
| Method and path | Purpose |
| --- | --- |
| `GET /login` | The sign-in page (self-contained HTML using the design tokens and logo) |
| `POST /api/auth/login` | `{username, password, remember}` or a plain form post. Sets the cookie; returns `{user, next}`. Errors: `400` validation, `401` wrong credentials (with attempts left), `429` locked (with retry time) |
| `POST /api/auth/logout` | Ends the session, clears the cookie |
| `GET /api/auth/me` | `{username, display_name, expires_at}` or `401` |

## 4. Front-end changes

- **`web/login.html` (new):** the form, inline errors (`aria-live="polite"`), a show/hide password toggle, the Caps Lock hint, a disabled-while-sending button with a spinner, a lockout countdown, and the reason banner from `?reason=`. It uses `autocomplete="username"` / `"current-password"` so password managers work, and the 16px gutter layout that works at phone width.
- **`web/components.js`:** one `authFetch()` used by `api()`, by the chat streams in `live.js` and `compare-live.js`, and by the scorecard approval. On `401` it saves any unsent chat text to `sessionStorage`, then goes to `/login?next=<path+hash>&reason=expired`.
- **`web/app.js`:**
  - on start-up, calls `/api/auth/me` and redirects to sign-in if needed;
  - shows the user's name and **Sign out** in the top bar;
  - restores a saved chat draft after sign-in.
- **Images and downloads** (viewer pages, proposal files, CSV and Excel) are same-origin, so the cookie goes with them and they need no change.

## 5. Server and deployment

- **Remove** the Basic-auth middleware and `AERCHAIN_BUYER_PASSWORD` once the users file exists. The migration creates `buyer` from it, with the same password you use today.
- **Users file:** `/etc/aerchain/users.json` (mode 600, root; the service reads it through a path setting). Created on the server with `scripts.users`, never copied through git.
- **nginx:** no change. The live block already passes `X-Real-IP` and `X-Forwarded-Proto`. The repository's `deploy/nginx.example.conf` gains the `X-Real-IP` line.
- **Rollout:** deploy, check `/login` and a deep link, then remove the old env variable and restart.

## 6. Tests (no browser needed)

- Correct sign-in sets a cookie flagged `HttpOnly`, `SameSite=Lax`, and `Secure` behind HTTPS.
- A protected API answers `401` JSON with no session and `200` with one.
- A wrong password answers `401` with the same message for an unknown user.
- Five failures give `429` with `Retry-After`; the counters reset after the window.
- Expired and idle sessions are refused; "keep me signed in" extends the cookie but not the idle limit.
- Sign-out invalidates the token server-side; the old cookie stops working.
- `next` accepts only relative same-site paths.
- A non-GET API call with a foreign `Origin` is refused.
- Vendor routes stay open; `/api/health` returns only `ok` without a session.
- Hashes verify, with wrong passwords rejected in constant time.
- The plaintext-env migration creates `buyer`.
- Removed code is not referenced (`WWW-Authenticate` gone).
- **Headless-browser checks:** the sign-in page at 1440 px and 390 px wide, the error states, and a deep link that survives sign-in.

## 7. Out of scope

- Single sign-on (Google, Microsoft), two-factor codes, password reset by email.
- Self-service sign-up.
- Roles and per-user permissions: every signed-in user can see every RFQ.
- An admin screen for users (the `scripts.users` command covers it).
- Per-user RFQ ownership and audit of who did what in the app beyond the auth log.

## 8. Files

- **New:** `app/auth.py` (accounts, hashing, sessions, lockout, middleware, endpoints, login page route), `web/login.html`, `scripts/users.py`, `tests/test_auth.py`.
- **Changed:** `app/main.py` (remove Basic auth, mount auth), `app/config.py` (users file, session settings), `web/components.js`, `web/app.js`, `web/views/live.js`, `web/views/compare-live.js`, `web/tokens.css`, `deploy/` (setup script and README: users file; nginx example: `X-Real-IP`), `.env.example`, `docs/PRD.md`, `docs/TRD.md`.

## 9. Decisions needed

1. **Accounts:** named accounts with a username, which is recommended for accountability, or keep one shared password with a password-only form?
2. **Session length:** 12 hours idle and 30 days with "keep me signed in", or something else?
3. **Lockout:** 5 failures in 15 minutes locks for 15 minutes. Is that right?
4. **Health check:** open with only `{"ok": true}`, or fully behind sign-in?
