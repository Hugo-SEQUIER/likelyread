---
name: likelyread
description: Track whether emails you send get opened, with Likelyread (Likelyread Cloud or a self-hosted server). Use when sending email that should carry open tracking, or when asked which sent emails or campaigns were opened.
license: MIT
---

# Likelyread: open tracking for emails an agent sends

Likelyread is an email open tracker, hosted (Likelyread Cloud) or on the user's own server. A tiny invisible image (a "pixel") goes into the HTML part of an email. When the recipient's mail app loads it, the server records a hit, and classifies it as an open, a self-view or a prefetch. This skill tells you how to add the pixel to emails you send, register them, and read the results back.

Everything goes through the `likelyread` command. It is a single Python 3.9+ file with no dependencies. Every command prints JSON, except `html --raw`, which prints the HTML itself.

## When to use

- You are about to send an email (or create a draft that will be sent) and the user wants to know whether it gets opened.
- The user asks which emails or campaigns were opened, or wants open rates.

Do not use it for emails the user sends by hand from Gmail in Chrome: the Likelyread Chrome extension handles those.

## Setup (once)

The user needs a **setup link** like `https://app.likelyread.com/#connect=TOKEN`. It is on their dashboard under **Connect**, on Likelyread Cloud or their own server, and the *Instructions for your agent* there already include it. With no account or server yet, they can sign up at `https://app.likelyread.com`, or self-host with one command on their server (`curl -fsSL https://raw.githubusercontent.com/Hugo-SEQUIER/likelyread/main/deploy/install.sh | sudo bash`, see the README), which prints the link. Then:

```bash
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/Hugo-SEQUIER/likelyread/main/cli/likelyread.py -o ~/.local/bin/likelyread
chmod +x ~/.local/bin/likelyread   # or link cli/likelyread.py from a clone (the installer clones to /opt/likelyread)
likelyread setup 'https://app.likelyread.com/#connect=TOKEN'   # tests the connection, saves the config (mode 600)
likelyread ping
```

Ask the user for the setup link; never invent one. On the server itself, add `--api-url http://127.0.0.1:PORT` only if the API is reachable locally.

- `url` is the public address recipients load the pixel from. It must be stable: every sent email keeps it forever. Do not use a temporary tunnel address for real email.
- Optional `api_url` (for example `http://127.0.0.1:8100`) if the agent runs on the same machine as the server.
- Environment variables `LIKELYREAD_URL`, `LIKELYREAD_API_URL` and `LIKELYREAD_TOKEN` override the file.
- The token is a secret. Never print it, log it, put it in an email, or paste it into chat.

## Plans (Likelyread Cloud)

`likelyread ping` shows the plan as `server.plan`: `free`, `pro` or `selfhost`. Pro and self-hosted servers have every command. On Free:

- `ls` covers the last 30 days. `ls --campaign`, `ls --from`, `campaigns`, `stats`, `health`, and `show` or `set-campaign` on an older email answer 402 `pro_required`. Tracking and per-email results still work: tell the user the rest needs Likelyread Pro.
- There is no pace guide: before a batch, ask the user how many emails to send today.

Free tracks 500 emails in 24 hours, Pro 5,000. Past that, `track` answers 429 `daily_limit`: stop the batch and tell the user. An email already sent with its pixel still records opens; rerun its `track` command once the limit frees up (it accepts sends up to 30 days old).

## Before tracking real email

1. **Ask first.** Adding a pixel changes the email (it needs an HTML part). Get the user's approval per campaign. Never add a pixel to a draft the user has already approved word for word: that changes what they approved.
2. **Consent.** In the EU, tracking pixels fall under the ePrivacy rules and the GDPR. France's CNIL (2026) says measuring opens of campaign emails needs the recipient's prior consent, B2B included. Unless the user decides otherwise, do not track recipients in the EU.
3. **Plain text cannot be tracked.** If the user's emails are plain text, send `multipart/alternative` with the text part unchanged and an HTML part that shows the same words plus the pixel.

## Sending

**Email sent right now:**

```bash
likelyread track --subject "Proposal" --to maya@example.com --campaign "Q4 outreach" --from me@example.com
# -> {"id": "...", "registered": true, "img": "<img src=... >"}
```

Put the returned `img` at the end of the HTML body, then send.

**Draft now, send later (recommended for reviewed outreach):**

```bash
likelyread pixel                                      # {"id", "url", "img"}; save the id with the draft
likelyread html --raw --id ID --file body.txt > body.html   # plain text to HTML, pixel appended
# ... create the draft / send multipart/alternative: text part = body.txt, HTML part = body.html ...
likelyread track --id ID --subject "Proposal" --to maya@example.com \
  --campaign "Q4 outreach" --from me@example.com --sent-at 1792251301849
```

- Run `track` once the send is confirmed. `--sent-at` takes unix seconds, unix milliseconds (Gmail's `internalDate`) or ISO 8601 with a timezone. It accepts sends up to 30 days old and is idempotent, so it is safe to rerun.
- Hits before the send time count as a draft preview, never as an open, so reviewing the draft is harmless.
- Send one email per recipient if you need to know who opened. Several recipients on one email share one pixel.
- `--to` accepts display names (`Maya H <maya@example.com>`); only the addresses are kept. `--id` must be the UUID printed by `likelyread pixel`.
- `likelyread html` escapes the text, keeps paragraphs and line breaks, and turns bare URLs into real links. Check that the HTML part shows the same words as the text part.

## Pace before a batch

Check the sender's health before sending a batch, and never send more than `ramp.remaining`:

```bash
likelyread health --from me@example.com --no-dns   # ramp: {sent_24h, limit, remaining}; status ok|warn|bad; issues
likelyread health --from me@example.com            # also checks SPF, DKIM and DMARC (slower)
```

- `ramp.remaining` is how many more emails the address should send in the next 24 hours. `null` means the address is marked established: no guide.
- If `status` is `bad`, or an issue mentions an open-rate drop, stop the batch and tell the human. Do not work around it by switching addresses.
- On Likelyread Cloud's Free plan, `health` answers 402: see *Plans* above.

## Reading results

```bash
likelyread ls --campaign "Q4 outreach" --status opened     # also: not_opened, prefetch_only; --from, --q, --limit
likelyread show ID                                         # every hit with kind, via and the reason
likelyread campaigns                                       # open rate per campaign
likelyread stats --campaign "Q4 outreach" --days 30    # buckets use this machine's timezone; --tz to override
likelyread set-campaign ID "Another campaign"
```

## How to interpret opens

- An open means the tracking image loaded on the recipient's side. Report it as "opened", never as "read".
- Gmail loads images through its proxy and caches them, so repeat opens by Gmail users often do not show up.
- Apple Mail Privacy Protection loads images on delivery from Apple's network. Those hits are `prefetch`, not opens. Apple Mail without Privacy Protection loads on the device and counts as an open.
- Some mail apps block images, so `not_opened` does not prove the email was ignored.
- Use opens to time a follow-up, not to conclude someone is not interested.
- Never mention tracking or opens to the recipient ("I saw you opened my email").

## Troubleshooting

- `{"error": "not configured ..."}`: the config file or environment variables are missing.
- `token rejected (401)`: the token is wrong or has expired; ask the user for a fresh setup link (dashboard, **Connect**).
- `failed: 402 ... pro_required` or `failed: 429 ... daily_limit`: see *Plans*.
- Opens not showing: run `likelyread show ID`. No hits at all usually means the pixel is not in the HTML part, the HTML part is missing, or the recipient's app blocks images. Hits marked `self` show the reason.
