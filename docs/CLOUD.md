# Likelyread Cloud

The hosted version of Likelyread at `https://app.likelyread.com`. It is the same server
as a self-hosted install, started with `MODE=cloud`: many accounts, Google sign-in, a Free
and a Pro plan, and billing through [Stripe](https://stripe.com), with Stripe Tax for VAT and
sales tax. Likelyread is the seller. Self-hosting stays free with every feature.

## Plans

| | Free | Pro | Self-hosted |
|---|---|---|---|
| Price (excluding VAT) | €0 | €5 / month, €36 / year, or €59 once (first 500) | €0, your server |
| Tracked emails | 500 a day | 5,000 a day | Unlimited |
| Every open, with time and verdict | Yes | Yes | Yes |
| "Sent with" signature in your emails | Never | Never | Never |
| Pick a campaign in Gmail's compose window | Yes | Yes | Yes |
| Agent MCP, CLI and API | Yes | Yes | Yes |
| Claude and ChatGPT connect with a sign-in | Yes | Yes | No: they use the server's token |
| History | 30 days | Unlimited | Unlimited |
| Campaign analytics, charts, filters by campaign and sender | No | Yes | Yes |
| Sender health: SPF/DKIM/DMARC checks, daily pace guide, open-rate drop alerts | No | Yes | Yes |

### Why these prices

What Mailsuite (Mailtrack) charges, from [their pricing page](https://mailsuite.com/en/pricing/), checked 20 Oct 2026:

- **Free:** shows the first open only, and adds their promotional signature to your emails.
- **Mailtrack:** $6.99 per user per month, with a 7-day tracking history.
- **Mailsuite:** $19.99 per user per month for full history.

How Likelyread beats them:

- **A more generous free plan:** every open, no signature in your emails, and 30 days of history.
- **A cheaper Pro with more in it:** €5 a month, or €3 a month paid yearly, plus VAT. It includes the full history that Mailsuite keeps for its $19.99 plan.
- **Reasons no competitor shows:** every hit gets a verdict that explains why it counted. Gmail proxy, Apple prefetch and your own views are told apart.
- **Trust:** the code is open source, and self-hosting is free.

### Why it is profitable

**Running costs.** One €5 to €10 VPS serves thousands of accounts. A pixel hit is one SQLite insert, and a tracked email is about 1 KB.

**Payment fees.** Likelyread sells through its own Stripe account, so it is the seller. Prices are in euros and exclude VAT: Stripe Tax calculates and adds it at checkout, and the tax returns are filed from Stripe Tax's reports. While the company's cross-border EU sales to consumers stay under €10,000 a year, French VAT applies to every EU consumer (Stripe's *small seller* scheme); above that, register for the EU's OSS. Stripe tracks that threshold per Stripe account, so add up every account the company sells through. Stripe's prices for an account in France ([stripe.com/en-fr/pricing](https://stripe.com/en-fr/pricing), checked October 2026):

- **Cards:** 1.5% + €0.25 for standard EEA cards. Premium and international cards cost more, and so does a payment that has to be converted to the account's currency.
- **Stripe Billing:** from 0.7% of subscription revenue.
- **Stripe Tax:** 0.5% per transaction where you are registered to collect tax.

**Why yearly is the default.** The fixed part of the card fee hurts small payments most, so the dashboard puts the yearly plan first.

**Break-even.** About 3 yearly subscribers pay for the server.

**Lifetime offer.** It is capped at 500 because it trades future revenue for cash and word of mouth at launch. A lifetime user costs cents per year to host.

**Free tier limits.** The 500-a-day limit and 30-day history keep free accounts cheap to run. Anyone who sends campaigns needs the analytics, which pushes them to Pro.

## Run it

Everything below is a one-time setup on the cloud server.

1. **DNS.** Add an `A` record `app` → the server's IP in Cloudflare. Set it to **DNS only** (grey cloud), so Caddy can get its certificate.
2. **Google sign-in.** In [Google Cloud Console](https://console.cloud.google.com/):
   - create a project, then go to *APIs & Services* and open the *OAuth consent screen*;
   - user type: External; app name: Likelyread; support email; authorised domain `likelyread.com`;
   - privacy policy: `https://likelyread.com/privacy`;
   - scopes: only `openid` and `email`. These are non-sensitive, so Google needs no security review;
   - publish the app.

   Then create the credentials: *Credentials*, then *Create credentials*, then *OAuth client ID*.
   - type: Web application;
   - authorised redirect URI: `https://app.likelyread.com/auth/google/callback`;
   - copy the client id and secret.
3. **Stripe.** Use a Stripe account that belongs to Likelyread alone, not one shared with another business: a webhook receives every event of its account, and the API key reaches every customer in it. Its public details carry Likelyread's name, the statement descriptor `LIKELYREAD`, `hello@likelyread.com`, `https://likelyread.com`, and the links to `https://likelyread.com/privacy` and `https://likelyread.com/terms`. Checkout asks buyers to accept those terms and refuses to open without the terms link. In the [Stripe Dashboard](https://dashboard.stripe.com):
   - Product catalogue: *Likelyread Pro* with two recurring prices, €5 a month and €36 a year, and *Likelyread Pro Lifetime* with a one-time price of €59, all with tax behaviour *Exclusive* (VAT added on top). Copy the three price ids.
   - Tax: set up Stripe Tax with the company's address, the default tax behaviour *Exclusive* and its registrations (for a French company under the threshold above: France, *standard*, *small seller* scheme), and give both products the *Software as a service (SaaS)* tax code.
   - Customer portal (*Settings*, then *Billing*, then *Customer portal*): allow cancelling at the end of the period, switching between the monthly and yearly prices, updating the payment method and viewing invoices, then save. The dashboard's *Manage subscription and invoices* button opens it.
   - API key: a restricted key with *Write* on Customers, Checkout Sessions, Customer portal and Subscriptions.
   - Webhook: an endpoint `https://app.likelyread.com/webhooks/stripe` with the events listed in `deploy/cloud.env.example`. Copy its signing secret.
   - Test everything in test mode first, with test-mode prices, key and webhook.
4. **Server.**
   ```bash
   git clone https://github.com/Hugo-SEQUIER/likelyread.git /opt/likelyread
   cd /opt/likelyread/deploy
   cp cloud.env.example .env    # fill in every value
   docker compose up -d --build
   curl -fsS https://app.likelyread.com/api/config
   ```
   Without the Stripe values the cloud runs with billing switched off, and everyone is on Free. You can launch sign-ups before payments are ready.
5. **Backups.** Add the nightly job. It keeps 30 days, which the privacy policy promises:
   ```
   17 3 * * * root bash /opt/likelyread/deploy/backup.sh --dir /opt/likelyread >> /var/log/likelyread-backup.log 2>&1
   ```
   Copy `deploy/data/backups/` off the machine as well, for example with `rclone` to Cloudflare R2.
6. **Extension id.** Uploading the draft to the Chrome Web Store assigns the extension its id. Put that id in `EXTENSION_IDS` and restart the server. Only listed extensions receive the token after sign-in. Until then, the sign-in page asks the user to paste their setup link instead.
7. **Admin.** Put your email in `ADMIN_EMAILS` (separate several with commas) and run `docker compose up -d` again, so the server reads it. Signed in with that address, the dashboard shows an **Admin** tab: accounts and paying users, new and active accounts with 30-day charts, tracked emails and pixel loads, what people use (the Chrome extension, the dashboard, agent tokens, each app connected with a sign-in, campaigns), and every account with its plan, sign-up, last activity and email counts. It never shows the subjects, recipients or opens of anyone's emails. The page is as safe as the Google account of each address listed, so turn on 2-Step Verification for those accounts.

## How it works

- **Accounts and sign-in:**
  - *Sign-in.* The dashboard and the extension use Google. PKCE and a signed 10-minute cookie protect the round trip.
  - *Tokens.* Each sign-in creates a bearer token, and the Connect page creates agent tokens on request. The server stores only the SHA-256 hash of each. Per account it keeps the 20 newest sign-in tokens and the 5 newest agent tokens, in separate pools: a sign-in never pushes out an agent token, and a sixth agent token replaces the oldest agent token, never a sign-in. Apps connected with a sign-in (below) are a third pool of 10 connections, where an eleventh replaces the least recently used. *Sign out everywhere* revokes them all, connected apps included; *Revoke agent tokens* (Account, Security) revokes only the agent tokens.
  - *Extension handoff.* The extension opens `/auth/google?ext=<extension id>`. After sign-in, the page hands the token to the extension with `chrome.runtime.sendMessage`. The manifest lists `https://app.likelyread.com/*` under `externally_connectable`, and the extension accepts the message only from that origin.
  - *Agents.* AI agents connect over MCP at `https://app.likelyread.com/mcp`, with the token in an `Authorization: Bearer` header. On the dashboard's **Connect** page, *Create agent token* makes a token labelled `Agent` (`POST /api/me/agent-token`) when clicked, never on page load. It fills in the setup for Claude Code, Cursor, VS Code, Codex and other MCP clients, and a prompt that an agent such as Hermes follows to set itself up. The instructions for agents without MCP (they install the CLI and the agent skill from GitHub and run `likelyread setup`) use the same token. The server returns the token once. The page shows it masked, next to *Copy token* and *Show*; the setups and prompts carry it only when copied.
  - *What an agent token can do.* Track emails and read results, through MCP or the API. Account actions answer 403 with it: deleting or exporting the account, signing out everywhere, checkout and the billing portal, and creating or revoking agent tokens. An agent misled by something it read cannot touch the account.
  - *Apps that sign in (OAuth).* Claude (claude.ai, the desktop and mobile apps, Claude Code), ChatGPT and other MCP apps connect without a token to copy. `server/oauth.py` makes the server an OAuth 2.1 authorization server for `/mcp`, following the MCP authorization spec:
    - *Discovery.* An unauthenticated `/mcp` request answers 401 with `WWW-Authenticate: Bearer resource_metadata="https://app.likelyread.com/.well-known/oauth-protected-resource/mcp"`. That document (RFC 9728) names `https://app.likelyread.com/mcp` as the resource and `https://app.likelyread.com` as its authorization server, whose metadata is at `/.well-known/oauth-authorization-server` (RFC 8414).
    - *Which app is asking.* Apps identify themselves with a client metadata document: the `client_id` is an https URL to a JSON document with the app's name and redirect URIs (Claude's is `https://claude.ai/oauth/mcp-oauth-client-metadata`, ChatGPT's `https://chatgpt.com/oauth/client.json`). The server fetches it with guards, because anyone can name any URL: https on port 443 and a host name only, public internet addresses only (and it connects to the address it checked), no redirects, 16 KB and 5 seconds at most, cached as its `Cache-Control` says, 20 fetches a minute per client IP. A redirect URI must be listed in the document exactly; a loopback one (`http://127.0.0.1/...`, `http://localhost/...`, as Claude Code uses) matches on any port. There is no dynamic client registration, and there are no client secrets: apps are public clients, and PKCE (S256) ties each code to the app that asked.
    - *Sign-in and consent.* `/oauth/authorize` checks the request, then sends the user to Google (the same sign-in and redirect URI as the dashboard; the checked request travels in the signed sign-in cookie). After Google, the user sees which app asks (its name and the host of its document), the account, what the app can do, and where they go back to, with a warning when that is an app on this computer. Every connection needs its own **Approve**; nothing is remembered. Errors after the app is checked go back to its redirect URI, every answer carries `iss` (RFC 9207), and errors before go to a page, never to an unchecked address.
    - *Tokens.* A code is redeemed once, within 5 minutes, at `/oauth/token`. The app gets an access token that works only at `/mcp`, for an hour, and acts like an agent token (no account actions); the API answers 401 to it. Its refresh token rotates at every refresh, and the last one spent still works until the app uses its replacement, for an app that lost an answer. A connection that gets no new token for 180 days ends. `/oauth/revoke` ends a connection from the app's side; **Connected apps** on the Account page lists them, each with **Disconnect** (`GET /api/me/apps`, `DELETE /api/me/apps/{id}`, sign-in tokens only).
    - *Links.* The Connect page's **Add to Claude** opens `https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Likelyread&connectorUrl=https%3A%2F%2Fapp.likelyread.com%2Fmcp`, Claude's dialog with both filled in; the user still confirms there. Nothing needs setting up for any of this beyond the Google sign-in already configured.
    - *Sending.* In Claude and ChatGPT, Likelyread is for reading results. Claude's Gmail connector removes images from the emails it drafts and sends, the pixel included, so tracked emails go out from Gmail with the Chrome extension, or from an agent that sends the HTML as written, such as Hermes. The server's instructions tell agents to check that a draft or sent email still contains the pixel before registering it, and to tell the user when it is gone.
- **Data isolation:**
  - *Scoping.* Every query is scoped to the account.
  - *Pixel ids.* An id registered by one account can never be claimed by another; the server answers 409.
  - *Self-view pings.* Suppress pings count only for the account that sent them, so nobody can hide someone else's opens.
- **Plans:**
  - *Locked features.* Locked endpoints answer 402 `{"error": "pro_required"}`, and the dashboard shows the upgrade card instead. The MCP tools return a sentence that names the Pro feature, for the agent to pass on.
  - *Daily limit.* It answers 429 `{"error": "daily_limit"}`. The extension keeps the email in its retry queue; the MCP tool `track_email` tells the agent to stop the batch and register the sent emails later.
- **Billing:**
  - *Checkout.* An account's first checkout creates its Stripe customer, and the server keeps the customer id. Each Checkout Session carries the account id in `client_reference_id` and in metadata, so webhooks map straight to the account. Stripe Tax adds the tax, a business can enter its VAT number, and the buyer accepts the terms of sale before paying.
  - *Webhooks.* They are verified with the `Stripe-Signature` header (HMAC-SHA256 with a 5-minute tolerance; while a signing secret is rolled, either signature is accepted) and deduplicated by event id.
  - *Cancellation.* Pro stays active until the paid period ends, plus 3 days of grace. A full refund of the lifetime payment goes back to Free.
  - *Lifetime.* It is a one-time payment, with an invoice. It is granted once the payment is confirmed, including a delayed method such as a bank debit.
- **Hardening:**
  - *Daily limit.* It counts the server's registration time, so a client-supplied `age` cannot dodge it.
  - *Idle tokens.* An extension or agent token unused for 180 days stops working; a dashboard token (kept in the browser) after 30 days. Agents connected from the Connect page have an agent token of their own, so they no longer lapse with the browser's. An app connected with a sign-in ends after 180 days without a refresh.
  - *Subscription events.* An event for a subscription other than the current live one never changes the plan.
  - *One subscription at a time.* Checkout refuses a second subscription while one is live.
  - *Deleting an account.* It cancels the subscription in Stripe first. A tombstone records the deleted account, so late events are recognised.
  - *Webhooks.* An event id is recorded only after the event is applied, so Stripe's retry of a failed delivery works.
  - *Lifetime cap.* It is enforced at checkout. A purchase that slips past it in a race is still honoured and logged.
- **Data rights:**
  - *Export.* `GET /api/me/export` returns everything held for the account, including emails older than the Free history window. Data portability covers all of the user's data, so the export is not gated by plan.
  - *Deletion.* `DELETE /api/me` removes the account and all of its data.
- **Admin page:**
  - *Who sees it.* Only a sign-in whose address is in `ADMIN_EMAILS`: `GET /api/me` then says `"admin": true`, and the dashboard shows the **Admin** tab. The address is the one Google verified at the account's latest sign-in. It must match a listed address in full (case and surrounding spaces aside), and the server checks it on every request. `GET /api/admin/stats` and `GET /api/admin/accounts` answer 404 to anyone else, the admin's own agent tokens included. Apps connected with a sign-in get 401, as everywhere in the API.
  - *What it counts.* `GET /api/admin/stats?tz=` returns accounts, paying accounts (subscriptions and lifetime), lifetime deals left, sign-ups, active accounts (they sent a tracked email), tracked emails, pixel loads, and a 30-day timeline in the viewer's time zone (`tz`, minutes east of UTC). *What they use* covers the last 30 days. It counts the accounts that used each kind of sign-in token (Chrome extension, Dashboard, Agent token), each connected app (its name and the host of its client document, since any app can call itself Claude), and campaigns. Each count is a share of the accounts seen: they signed in, used a token or an app, or sent a tracked email.
  - *Accounts.* `GET /api/admin/accounts?q=&sort=new|seen|emails&limit=&offset=` lists 50 accounts at a time, with the total in `X-Total-Count`. Each row has the address, plan, sign-up, last seen, emails in the last 30 days and in all, and what the account used in the last 30 days. `q` matches part of the address.
  - *Limits.* The page reads only what the server already keeps. Tokens and app connections disappear when the user signs out everywhere or revokes them, and when they lapse, so an account's channels can drop out of the counts. The server never sends the page the subjects, recipients or opens of anyone's emails.

## Tests

```bash
cd server && python -m pytest -q                      # includes tests/test_cloud.py and tests/test_oauth.py
cd extension && CHROME_PATH=... PYTHON=... npm run e2e:cloud
npx e2e run --config e2e.cloud.config.ts   # from the repository root: every test in tests/cloud/
```

`tests/test_oauth.py` runs the OAuth flow for Claude, Claude Code and ChatGPT with copies of their client metadata documents, every refusal, refresh and revocation, the guards on fetching documents, and the MCP SDK's own OAuth client against a live server. The tests in `tests/cloud/` use the `e2e` package, with fake Google and a test app from `tests/support/cloud-server.mjs`. Fake Google signs in Ann, the admin, or Bob when the browser carries the cookie `e2e_person=bob`. `connect-apps.e2e.ts` drives the Connect page, an app's sign-in through the consent page to a working `/mcp` token, and **Disconnect** on the Account page. `admin.e2e.ts` checks the Admin page's counts, sort and search for Ann, and that Bob and Ann's agent token get no Admin tab and a 404.

`e2e:cloud` runs real Chromium with the extension built for a local cloud, plus fake Google and fake Stripe servers. It covers the following:

- Google sign-in from the extension's settings, and the token handoff.
- Tracking on Free, and the locked analytics.
- Checkout, the webhook, and the Pro unlock.
- Connect: an agent token made only on click, the Claude Code command copied with it, that token listing the tools on `/mcp`, the tokens masked on screen until *Show*, *Copy token*, and the *Any agent* prompt carrying the address, the token and the skill.
