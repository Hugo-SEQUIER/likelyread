# Likelyread Cloud

The hosted version of Likelyread at `https://app.likelyread.com`. It is the same server
as a self-hosted install, started with `MODE=cloud`: many accounts, Google sign-in, a Free
and a Pro plan, and billing through [Stripe](https://stripe.com), with Stripe Tax for VAT and
sales tax. Likelyread is the seller. Self-hosting stays free with every feature.

## Plans

| | Free | Pro | Self-hosted |
|---|---|---|---|
| Price | $0 | $5 / month, $36 / year, or $59 once (first 500) | $0, your server |
| Tracked emails | 500 a day | 5,000 a day | Unlimited |
| Every open, with time and verdict | Yes | Yes | Yes |
| "Sent with" signature in your emails | Never | Never | Never |
| Pick a campaign in Gmail's compose window | Yes | Yes | Yes |
| Agent CLI and API | Yes | Yes | Yes |
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
- **A cheaper Pro with more in it:** $5 a month, or $3 a month paid yearly. It includes the full history that Mailsuite keeps for its $19.99 plan.
- **Reasons no competitor shows:** every hit gets a verdict that explains why it counted. Gmail proxy, Apple prefetch and your own views are told apart.
- **Trust:** the code is open source, and self-hosting is free.

### Why it is profitable

**Running costs.** One €5 to €10 VPS serves thousands of accounts. A pixel hit is one SQLite insert, and a tracked email is about 1 KB.

**Payment fees.** Likelyread sells through its own Stripe account, so it is the seller. Stripe Tax calculates and collects VAT and sales tax at checkout; the tax returns (the EU's OSS return, and any other registration) are filed from Stripe Tax's reports. Stripe's prices for an account in France ([stripe.com/en-fr/pricing](https://stripe.com/en-fr/pricing), checked October 2026):

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
3. **Stripe.** In the [Stripe Dashboard](https://dashboard.stripe.com):
   - Product catalogue: *Likelyread Pro* with two recurring prices, $5 a month and $36 a year, and *Likelyread Pro Lifetime* with a one-time price of $59. Copy the three price ids.
   - Tax: set up Stripe Tax (your origin address and your registrations, such as the EU's OSS) and give both products a *Software as a service (SaaS)* tax code.
   - Customer portal (*Settings*, then *Billing*, then *Customer portal*): allow cancelling at the end of the period, updating the payment method and viewing invoices, then save. The dashboard's *Manage subscription and invoices* button opens it.
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
7. **Admin.** Put your email in `ADMIN_EMAILS`. Your Account page then shows accounts, paying users, weekly actives, and emails and hits in the last 24 h.

## How it works

- **Accounts and sign-in:**
  - *Sign-in.* The dashboard and the extension use Google. PKCE and a signed 10-minute cookie protect the round trip.
  - *Tokens.* Each sign-in creates a bearer token. The server stores only its SHA-256 hash and keeps the 20 newest per account. *Sign out everywhere* revokes them all.
  - *Extension handoff.* The extension opens `/auth/google?ext=<extension id>`. After sign-in, the page hands the token to the extension with `chrome.runtime.sendMessage`. The manifest lists `https://app.likelyread.com/*` under `externally_connectable`, and the extension accepts the message only from that origin.
- **Data isolation:**
  - *Scoping.* Every query is scoped to the account.
  - *Pixel ids.* An id registered by one account can never be claimed by another; the server answers 409.
  - *Self-view pings.* Suppress pings count only for the account that sent them, so nobody can hide someone else's opens.
- **Plans:**
  - *Locked features.* Locked endpoints answer 402 `{"error": "pro_required"}`, and the dashboard shows the upgrade card instead.
  - *Daily limit.* It answers 429 `{"error": "daily_limit"}`. The extension keeps the email in its retry queue.
- **Billing:**
  - *Checkout.* An account's first checkout creates its Stripe customer, and the server keeps the customer id. Each Checkout Session carries the account id in `client_reference_id` and in metadata, so webhooks map straight to the account. Stripe Tax adds the tax, and a business can enter its VAT number.
  - *Webhooks.* They are verified with the `Stripe-Signature` header (HMAC-SHA256 with a 5-minute tolerance; while a signing secret is rolled, either signature is accepted) and deduplicated by event id.
  - *Cancellation.* Pro stays active until the paid period ends, plus 3 days of grace. A full refund of the lifetime payment goes back to Free.
  - *Lifetime.* It is a one-time payment, with an invoice. It is granted once the payment is confirmed, including a delayed method such as a bank debit.
- **Hardening:**
  - *Daily limit.* It counts the server's registration time, so a client-supplied `age` cannot dodge it.
  - *Idle tokens.* An extension or agent token unused for 180 days stops working; a dashboard token (kept in the browser) after 30 days.
  - *Subscription events.* An event for a subscription other than the current live one never changes the plan.
  - *One subscription at a time.* Checkout refuses a second subscription while one is live.
  - *Deleting an account.* It cancels the subscription in Stripe first. A tombstone records the deleted account, so late events are recognised.
  - *Webhooks.* An event id is recorded only after the event is applied, so Stripe's retry of a failed delivery works.
  - *Lifetime cap.* It is enforced at checkout. A purchase that slips past it in a race is still honoured and logged.
- **Data rights:**
  - *Export.* `GET /api/me/export` returns everything held for the account, including emails older than the Free history window. Data portability covers all of the user's data, so the export is not gated by plan.
  - *Deletion.* `DELETE /api/me` removes the account and all of its data.

## Tests

```bash
cd server && python -m pytest -q                      # includes tests/test_cloud.py
cd extension && CHROME_PATH=... PYTHON=... npm run e2e:cloud
```

`e2e:cloud` runs real Chromium with the extension built for a local cloud, plus fake Google and fake Stripe servers. It covers the following:

- Google sign-in from the extension's settings, and the token handoff.
- Tracking on Free, and the locked analytics.
- Checkout, the webhook, and the Pro unlock.
