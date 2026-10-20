# likelyread

Open-source email open tracking for Gmail. It tells you when someone opens an email you sent, explains why each open counted, and gives you a small dashboard to follow your campaigns.

Two ways to use it:

- **Likelyread Cloud** at [app.likelyread.com](https://app.likelyread.com): install the Chrome extension, sign in with Google, done. Free plan; Pro adds campaign analytics and full history. See [docs/CLOUD.md](docs/CLOUD.md).
- **Self-hosted**: one small server (FastAPI + SQLite) behind Caddy, installed with one command. One token, every feature, nothing leaves your VPS. Instructions below.

![Dashboard overview](docs/screenshots/overview.png)

## How it works

1. When you click **Send** in Gmail with tracking on, the extension appends a 1x1 invisible image (`https://your-domain/p/<uuid>.gif`) to the email and registers the email with your server. Sending is never blocked or delayed.
2. When the recipient's mail client loads the image, the server logs a hit.
3. Hits are classified when you read them:
   - **self**: your own compose preview or your own view of the sent email (the extension tells the server, and also blocks the request in your browser);
   - **prefetch**: Apple Mail Privacy Protection loading images on delivery;
   - **open**: everything else.

See [SPEC.md](SPEC.md) for the full contract.

## Setup

You need a small Linux server (Ubuntu or Debian, 512 MB of RAM is plenty) with ports 80 and 443 open.

**1. Pick an address.** Use a subdomain of a domain you own, for example `t.yourcompany.com`: add an A record pointing to the server's IP (on Cloudflare, set it to DNS only, grey cloud). No domain yet? The installer can use a free `<your-ip>.sslip.io` address to try it out. For real email use your own domain: spam filters trust it more, and it keeps working if you move servers.

**2. Run one command on the server:**

```sh
curl -fsSL https://raw.githubusercontent.com/Hugo-SEQUIER/likelyread/main/deploy/install.sh | sudo bash
```

It installs Docker if needed, asks for your address, creates a private token, gets an HTTPS certificate and starts everything in `/opt/likelyread`. At the end it prints your **setup link**:

```
https://t.yourcompany.com/#connect=<your token>
```

Treat it like a password. You can print it again any time with `sudo bash /opt/likelyread/deploy/install.sh --show-link`, and it is on the dashboard under **Connect**.

**3. Connect the extension.** Install the Chrome extension, open its settings, paste the setup link and click **Save**. Write an email in Gmail: the red eye next to Send means it is tracked.

Until the extension is on the Chrome Web Store (submission kit: [`store/LISTING.md`](store/LISTING.md)), build and load it yourself: `cd extension && npm install && npm run build`, then in `chrome://extensions` turn on **Developer mode**, click **Load unpacked** and pick `extension/dist`.

**4. Open the dashboard.** Open the setup link in your browser: it signs you in.

Non-interactive install, for example from a provisioning script: `sudo bash install.sh --domain t.yourcompany.com --yes`. If something else on the server already uses port 443 on another address, add `--bind-ip <public IP>`. See `install.sh --help` for all options.

<details>
<summary>Manual setup without the installer</summary>

```sh
git clone https://github.com/Hugo-SEQUIER/likelyread.git
cd likelyread/deploy
cp .env.example .env
# set DOMAIN=t.example.com and LIKELYREAD_TOKEN=$(openssl rand -hex 32)
docker compose up -d
curl https://t.example.com/healthz   # {"ok":true}
```

Your setup link is `https://DOMAIN/#connect=LIKELYREAD_TOKEN`.

</details>

### Backups

Everything is in `/opt/likelyread/deploy/data/likelyread.db` (`deploy/data/` for a manual setup). A safe online copy from cron:

```sh
sqlite3 /path/to/deploy/data/likelyread.db ".backup '/backups/likelyread-$(date +%F).db'"
```

### Updating

Run the install command again. It keeps your address, token and data.

## Campaigns and the dashboard

A campaign is just a label on an email.

- **While writing:** click the campaign name next to the red eye under Gmail's Send button and pick one, or type a new one. Your pick becomes the default for the next emails. The popup and options page set the same default.
- **After sending:** change an email's campaign from its detail page in the dashboard, or rename a whole campaign from the Campaigns tab.

The dashboard (`https://your-domain/`) has:

- **Overview:** tracked emails, open rate, total opens, opens in the last 7 days, median time to first open, a 30-day sent vs opens chart, opens by hour of day (useful to pick a send time), and the latest opens.
- **Campaigns:** open rate, opens and time to first open per campaign, side by side.
- **Emails:** search by subject, recipient or campaign, filter by status, and open any email to see every hit with its time, verdict, the reason for that verdict, user agent and IP.
- **From address:** each email records the address it was sent from (your account or a send-as alias). When you use more than one, the dashboard shows a From column and an address filter.

Open rate means *emails with at least one open / tracked emails*. Apple prefetches and your own views are never counted.

![Email detail](docs/screenshots/email-detail.png)

![Campaign picker in Gmail compose](docs/screenshots/compose-picker.png)

The dashboard is a static page with no data of its own: it reads the authenticated API with your token. Set `DASHBOARD=false` to turn it off.

## Email sent by scripts and AI agents

**Give it to your agent:** [`skills/likelyread/SKILL.md`](skills/likelyread/SKILL.md) is a ready-made agent skill (the open `SKILL.md` format used by Claude Code, Hermes and other agents). Copy the folder into your agent's skills directory, for example `~/.claude/skills/likelyread/`, and it will know how to install the CLI, add the pixel, register sends and read opens, including when not to track.

Anything that sends email itself (a script, a cron job, an AI agent) can use the `likelyread` CLI in `cli/`. It is a single Python file with no dependencies, and every command prints JSON.

```bash
ln -s "$PWD/cli/likelyread.py" ~/.local/bin/likelyread
likelyread setup 'https://t.example.com/#connect=...'   # your setup link; saves ~/.config/likelyread/config.json (mode 600)
```

Draft now, send later:

```bash
likelyread pixel                                   # {"id", "url", "img"}: keep the id with the draft
likelyread html --raw --id ID --file body.txt      # plain text to an HTML part, pixel appended
# ... send multipart/alternative: the text part unchanged, the HTML part above ...
likelyread track --id ID --subject "..." --to maya@example.com \
  --campaign "Q4 outreach" --from me@example.com --sent-at 1792251301849
```

`--sent-at` takes unix seconds, unix milliseconds (Gmail's `internalDate`) or ISO 8601, so registration can happen after the send, for example during reconciliation. Hits before the send time count as a draft preview, never as an open. Read results with `likelyread ls --campaign "Q4 outreach" --status opened`, `likelyread show ID`, `likelyread campaigns` and `likelyread stats`.

Send one email per recipient if you want to know who opened. Plain-text emails cannot carry a pixel: add an HTML part.

## Configuration

All settings live in `deploy/.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DOMAIN` | | Domain Caddy serves and gets a certificate for. |
| `LIKELYREAD_TOKEN` | required | Bearer token for the API, the dashboard and the extension. The server refuses to start without it. |
| `ALLOWED_ORIGINS` | empty | Optional CORS origin for the extension (`chrome-extension://<id>`). Not needed for the extension to work. |
| `GRACE_SECONDS` | `20` | Hits this close to the send time count as your compose preview. |
| `SUPPRESS_WINDOW_SECONDS` | `60` | Hits within this window after you open your own sent email count as you. |
| `STORE_IP` | `true` | `false` stops storing IP addresses. |
| `RETENTION_DAYS` | `0` | Delete hits, and emails sent before the cutoff, older than this many days (`0` keeps everything). Hits for ids that were never registered are always dropped after 8 days. |
| `PIXEL_RATE_PER_MIN` | `60` | Pixel hits logged per IP per minute. |
| `DASHBOARD` | `true` | Serve the dashboard at `/`. |

## Known limitations

An "open" means the tracking image loaded on the recipient's side. That is what every open tracker measures; Likelyread sets aside your own views and Apple's prefetches, but it cannot prove someone read every word.

| Limitation | Effect |
|---|---|
| Gmail image proxy | Opens by Gmail recipients show Google's IP and user agent, so there is no location. The proxy caches images, so **repeat opens may not register**. |
| Apple Mail Privacy Protection | Apple loads images on delivery, which looks like an open. Flagged as `prefetch` by its user agent (heuristic). |
| Images blocked by default | Some Outlook and corporate setups block images, so real opens are missed. |
| One pixel per email | With several recipients you can't tell who opened it. |
| Forwarding | Opens by people the email was forwarded to count as opens. |
| Gmail DOM changes | Can break injection. All Gmail selectors live in `extension/src/gmail-selectors.ts`, and failures are safe: the email is sent untracked. |
| Self-views | The extension blocks your own browser from loading your pixel directly, and when you open a message **you sent** in Gmail it tells the server, so the next 60 s count as self. Messages you received are never suppressed, so a recipient opening your email in this same browser still counts. |
| Deliverability | A fresh domain plus hidden images can raise spam scores. Use HTTPS and a domain with some history. |

## Privacy and legal

All data stays on your VPS. `STORE_IP=false` and `RETENTION_DAYS` let you keep less.

Tracking pixels fall under the EU ePrivacy rules and the GDPR. In France, the CNIL's [recommendation on tracking pixels in emails](https://www.cnil.fr/sites/default/files/2026-04/recommandation-pixels_de_suivi.pdf) (adopted 12 March 2026, published 14 April 2026) says that measuring opens to analyse or optimise campaigns requires the recipient's prior consent. That applies to B2B emails too. The exemptions are narrow: authentication security, and deliverability tracking for emails the recipient asked for. You are responsible for how you use this tool and for getting consent where it is required.

## Development

```sh
# server
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
LIKELYREAD_TOKEN=devtoken0123456789 DB_PATH=./dev.db uvicorn app:create_app --factory --reload

# demo data for the dashboard (never on your real database)
python scripts/seed_demo.py ./demo.db

# extension
cd extension
npm install
npm test          # unit tests + compose tests against a fake Gmail DOM
npm run typecheck
npm run build     # outputs dist/
# end to end: real Chromium with the built extension, a fake Gmail on
# mail.google.com and a real server (send, campaign, sender, server down, bad token)
CHROME_PATH=/path/to/chrome PYTHON=../server/.venv/bin/python npm run e2e

# agent CLI
cd cli
python -m pytest -q tests
```

## Roadmap

- v0.3: status badges in Gmail's Sent list and thread view; per-email "stop tracking".
- v0.4: notifications (ntfy, Telegram, webhook) on first open, opt-in.
- v0.5: link click tracking.
- Later: per-recipient pixels from Gmail web, Firefox.

## License

MIT
