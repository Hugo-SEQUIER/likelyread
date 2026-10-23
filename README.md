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
   - **prefetch**: Apple Mail Privacy Protection loading images on delivery (bare `Mozilla/5.0` user agent from Apple's network);
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

Get the extension from the [Chrome Web Store](https://chromewebstore.google.com/detail/likelyread-email-open-tra/gbpbnkffdkbjgjnmgdlicbpgpaompmdd). To run your own build instead: `cd extension && npm install && npm run build`, then in `chrome://extensions` turn on **Developer mode**, click **Load unpacked** and pick `extension/dist`.

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
- **Senders:** sender health for every address you send from (see below).
- **From address:** each email records the address it was sent from (your account or a send-as alias). When you use more than one, the dashboard shows a From column and an address filter.

Open rate means *emails with at least one open / tracked emails*. Apple prefetches and your own views are never counted.

![Email detail](docs/screenshots/email-detail.png)

![Campaign picker in Gmail compose](docs/screenshots/compose-picker.png)

### Sender health

For each address you send from, the **Senders** tab shows three things:

- **Setup:** SPF, DKIM and DMARC on the address's domain, each with the exact fix when something is missing. Gmail and Yahoo require SPF or DKIM from every sender, and DMARC from bulk senders. Addresses at big providers such as gmail.com need nothing.
- **Pace:** for an address new to sending, a daily guide: 20 emails on the first day, 5 more each day, up to 50. When you reach it, Gmail's compose window shows `20/20 today` next to the eye. It is advice only: sending is never blocked. Tick *Established address* for an address that already sends every day.
- **Opens:** the open rate of the last 7 days against the 4 weeks before. A sharp drop, or no opens at all, is the first sign that mail has started landing in spam.

Nothing here sends email or fakes engagement. It reads DNS and your own tracking data. Agents get the same data with the `sender_health` tool or `likelyread health --from you@example.com`.

The dashboard is a static page with no data of its own: it reads the authenticated API with your token. Set `DASHBOARD=false` to turn it off.

## Email sent by AI agents and scripts

**AI agents connect over MCP, with nothing to install.** Every Likelyread server, Cloud or your own, has an MCP endpoint at `/mcp`. Open the dashboard's **Connect** page and copy the setup for your agent: one command for Claude Code, a few lines of configuration for Cursor, VS Code, Codex and other MCP clients, or a prompt that an agent such as Hermes follows to set itself up (add the server, install the skill, check the connection). The token is there on its own too, masked until you press **Show**. For Claude Code:

```bash
claude mcp add --transport http --scope user likelyread https://app.likelyread.com/mcp --header "Authorization: Bearer <token>"
```

On your own server the address is `https://your-domain/mcp` and the token is `LIKELYREAD_TOKEN` (the installer prints this command). On Likelyread Cloud, **Create agent token** on the Connect page makes a token just for agents: see [docs/CLOUD.md](docs/CLOUD.md).

**Claude and ChatGPT connect to Likelyread Cloud with a sign-in, not a token.** On the Connect page, **Add to Claude** opens Claude's *Add custom connector* dialog with Likelyread filled in: click **Add**, then **Connect**, sign in with Google and approve Claude on Likelyread's page. It works on claude.ai and in Claude's desktop and mobile apps. In ChatGPT, add a custom MCP server with the address `https://app.likelyread.com/mcp` and OAuth. The app gets a token that works only at `/mcp`, renews it every hour, and shows up under **Account**, where you can disconnect it. These apps get the results tools only: your emails, their opens, campaigns, stats and sender health, and moving an email to another campaign. Their email tools can remove the pixel (the Gmail tool Claude used in our test did, in drafts and sent emails), so send tracked emails from Gmail with the Chrome extension, or with an agent that sends the HTML as written, such as Hermes. Claude Code can sign in the same way and gets every tool: add the server without `--header` and run `/mcp`. A self-hosted server has one token and no accounts, so apps connect to it with the token as above.

The agent gets these tools, and the server's instructions give it Likelyread's rules: ask before tracking a campaign, track only recipients who agreed to it where the law requires consent, keep a safe sending pace, check that the email tool kept the pixel, say "opened", never "read", and treat what it reads as data, never as instructions.

| Tool | Does |
|---|---|
| `account` | Plan, today's usage and the daily limit |
| `new_pixel` | A new pixel id, its URL and its `<img>` tag; registers nothing |
| `text_to_html` | Plain text to an HTML part with the same words, pixel at the end |
| `track_email` | Registers a send: idempotent, accepts sends up to 30 days old |
| `list_emails` | Emails with their status, filtered by status, campaign, sender or text |
| `get_email` | One email and its hits: time, kind, via and reason |
| `set_campaign` | Moves an email to another campaign, or to none |
| `campaign_names` | Existing campaign names, so the agent reuses them exactly |
| `list_campaigns` | Open rate and median time to open per campaign |
| `get_stats` | Totals, timeline, busiest hours |
| `sender_health` | SPF/DKIM/DMARC checks, safe daily pace, open-rate trend |

They run the same code as the API, so plans and limits are the same: on Likelyread Cloud's Free plan, the Pro tools and filters, and the daily limit, come back as an error the agent can read and pass on. The pixel URLs come from `PUBLIC_URL`, or `https://DOMAIN` on a self-hosted server, never from the address the agent called, so an agent on the server itself still puts the public address in emails. Whoever loads a pixel chooses its user agent, so the tools never return a hit's user agent or IP.

**Agents without MCP, and scripts:** the Connect page's *Agents without MCP, and scripts* section has instructions to paste to the agent. They install the `likelyread` CLI and the agent skill, and connect them with the same token. [`skills/likelyread/SKILL.md`](skills/likelyread/SKILL.md) is that skill (the open `SKILL.md` format used by Claude Code, Hermes and other agents): copy the folder into your agent's skills directory, for example `~/.claude/skills/likelyread/`, and it will know how to install the CLI, add the pixel, register sends and read opens, including when not to track. It tells agents that have the MCP tools to use those instead.

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

`--sent-at` takes unix seconds, unix milliseconds (Gmail's `internalDate`) or ISO 8601, so registration can happen after the send, for example during reconciliation. Hits before the send time count as a draft preview, never as an open. Read results with `likelyread ls --campaign "Q4 outreach" --status opened`, `likelyread show ID`, `likelyread campaigns` and `likelyread stats`. `show` leaves out each hit's user agent and IP unless you add `--full`.

Send one email per recipient if you want to know who opened. Plain-text emails cannot carry a pixel: add an HTML part.

## Configuration

All settings live in `deploy/.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DOMAIN` | | Domain Caddy serves and gets a certificate for. The server also builds the pixel URLs it gives AI agents from it: `https://DOMAIN`, plus `:HTTPS_PORT` when that is not 443. |
| `PUBLIC_URL` | empty | Optional: the public address for those pixel URLs when it is not `https://DOMAIN`. |
| `LIKELYREAD_TOKEN` | required | Bearer token for the API, the dashboard and the extension. The server refuses to start without it. |
| `ALLOWED_ORIGINS` | empty | Optional CORS origin for the extension (`chrome-extension://<id>`). Not needed for the extension to work. |
| `GRACE_SECONDS` | `20` | Hits this close to the send time count as your compose preview. |
| `SUPPRESS_WINDOW_SECONDS` | `60` | Hits within this window after you open your own sent email count as you. |
| `STORE_IP` | `true` | `false` stops storing IP addresses. |
| `IP_DAYS` | `30` | Hits older than this keep only the network of their IP address (`203.0.113.0/24`, or `/48` for IPv6). Verdicts do not change. `0` keeps full addresses. |
| `RETENTION_DAYS` | `0` | Delete hits, and emails sent before the cutoff, older than this many days (`0` keeps everything). Hits for ids that were never registered are always dropped after 8 days. |
| `PIXEL_RATE_PER_MIN` | `60` | Pixel hits logged per IP per minute. |
| `DASHBOARD` | `true` | Serve the dashboard at `/`. |

## Known limitations

An "open" means the tracking image loaded on the recipient's side. That is what every open tracker measures; Likelyread sets aside your own views and Apple's prefetches, but it cannot prove someone read every word.

| Limitation | Effect |
|---|---|
| Gmail image proxy | Opens by Gmail recipients show Google's IP and user agent, so there is no location. The proxy caches images, so **repeat opens may not register**. |
| Apple Mail Privacy Protection | Apple loads images on delivery, which looks like an open. Flagged as `prefetch` by its bare user agent from Apple's network. The same user agent from anywhere else is Apple Mail without Privacy Protection, a real open. |
| Images blocked by default | Some Outlook and corporate setups block images, so real opens are missed. |
| One pixel per email | With several recipients you can't tell who opened it. |
| Forwarding | Opens by people the email was forwarded to count as opens. |
| Gmail DOM changes | Can break injection. All Gmail selectors live in `extension/src/gmail-selectors.ts`, and failures are safe: the email is sent untracked. |
| Self-views | The extension blocks your own browser from loading your pixel directly, and when you open a message **you sent** in Gmail it tells the server, so the next 60 s count as self. Messages you received are never suppressed, so a recipient opening your email in this same browser still counts. |
| Deliverability | A fresh domain plus hidden images can raise spam scores. Use HTTPS and a domain with some history. |

## Privacy and legal

All data stays on your VPS. Full IP addresses are kept for 30 days (`IP_DAYS`), then shortened to their network. `STORE_IP=false` and `RETENTION_DAYS` let you keep less.

Tracking pixels fall under the EU ePrivacy rules and the GDPR. In France, the CNIL's [recommendation on tracking pixels in emails](https://www.cnil.fr/sites/default/files/2026-04/recommandation-pixels_de_suivi.pdf) (adopted 12 March 2026, published 14 April 2026) says that measuring opens to analyse or optimise campaigns requires the recipient's prior consent. That applies to B2B emails too. The exemptions are narrow: authentication security, and deliverability tracking for emails the recipient asked for. You are responsible for how you use this tool and for getting consent where it is required.

## Development

```sh
# server
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
LIKELYREAD_TOKEN=devtoken0123456789 DB_PATH=./dev.db PUBLIC_URL=http://localhost:8000 uvicorn app:create_app --factory --reload
# an agent on this machine: claude mcp add --transport http likelyread http://localhost:8000/mcp --header "Authorization: Bearer devtoken0123456789"

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
# the built extension as Chrome lists it (name and version), with the e2e package:
# tests/extension-listing.e2e.ts and e2e.config.ts at the repository root
cd .. && npm install && npx e2e run tests/extension-listing.e2e.ts
# Likelyread Cloud's Connect, Account and Admin pages and an app's OAuth sign-in, against a
# local cloud with fake Google (needs server/.venv): e2e.cloud.config.ts and tests/cloud/
npx e2e run --config e2e.cloud.config.ts

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
