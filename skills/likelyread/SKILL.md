---
name: likelyread
description: Track whether emails you send get opened, with a self-hosted Likelyread server. Use when sending email that should carry open tracking, or when asked which sent emails or campaigns were opened.
license: MIT
---

# Likelyread: open tracking for emails an agent sends

Likelyread is a self-hosted email open tracker. A tiny invisible image (a "pixel") goes into the HTML part of an email. When the recipient's mail app loads it, the server records a hit, and classifies it as an open, a self-view or a prefetch. This skill tells you how to add the pixel to emails you send, register them, and read the results back.

Everything goes through the `likelyread` command. It is a single Python 3.9+ file with no dependencies, and every command prints JSON.

## When to use

- You are about to send an email (or create a draft that will be sent) and the user wants to know whether it gets opened.
- The user asks which emails or campaigns were opened, or wants open rates.

Do not use it for emails the user sends by hand from Gmail in Chrome: the Likelyread Chrome extension handles those.

## Setup (once)

The user needs a running Likelyread server (see the repository README: `docker compose up -d` on a VPS with a domain). Then:

```bash
# from a clone of the repository
ln -sf "$PWD/cli/likelyread.py" ~/.local/bin/likelyread
mkdir -p ~/.config/likelyread && chmod 700 ~/.config/likelyread
# write the config; ask the user for the token, never invent one
cat > ~/.config/likelyread/config.json <<'EOF'
{"url": "https://t.example.com", "token": "PASTE_TOKEN_HERE"}
EOF
chmod 600 ~/.config/likelyread/config.json
likelyread ping
```

- `url` is the public address recipients load the pixel from. It must be stable: every sent email keeps it forever. Do not use a temporary tunnel address for real email.
- Optional `api_url` (for example `http://127.0.0.1:8100`) if the agent runs on the same machine as the server.
- Environment variables `LIKELYREAD_URL`, `LIKELYREAD_API_URL` and `LIKELYREAD_TOKEN` override the file.
- The token is a secret. Never print it, log it, put it in an email, or paste it into chat.

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
- `likelyread html` escapes the text, keeps paragraphs and line breaks, and turns bare URLs into real links. Check that the HTML part shows the same words as the text part.

## Reading results

```bash
likelyread ls --campaign "Q4 outreach" --status opened     # also: not_opened, prefetch_only; --from, --q, --limit
likelyread show ID                                         # every hit with kind, via and the reason
likelyread campaigns                                       # open rate per campaign
likelyread stats --campaign "Q4 outreach" --days 30
likelyread set-campaign ID "Another campaign"
```

## How to interpret opens

- An open means the tracking image loaded on the recipient's side. Report it as "opened", never as "read".
- Gmail loads images through its proxy and caches them, so repeat opens by Gmail users often do not show up.
- Apple Mail Privacy Protection loads images on delivery. Those hits are `prefetch`, not opens.
- Some mail apps block images, so `not_opened` does not prove the email was ignored.
- Use opens to time a follow-up, not to conclude someone is not interested.
- Never mention tracking or opens to the recipient ("I saw you opened my email").

## Troubleshooting

- `{"error": "not configured ..."}`: the config file or environment variables are missing.
- `token rejected (401)`: wrong token; ask the user.
- Opens not showing: run `likelyread show ID`. No hits at all usually means the pixel is not in the HTML part, the HTML part is missing, or the recipient's app blocks images. Hits marked `self` show the reason.
