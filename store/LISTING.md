# Chrome Web Store submission

Everything to paste into the [Chrome Web Store developer dashboard](https://chrome.google.com/webstore/devconsole), field by field. It follows Google's publishing guides: [Prepare your extension](https://developer.chrome.com/docs/webstore/prepare), [Supplying images](https://developer.chrome.com/docs/webstore/images), [Creating a great listing page](https://developer.chrome.com/docs/webstore/best-listing), [Fill out the privacy fields](https://developer.chrome.com/docs/webstore/cws-dashboard-privacy), [Branding guidelines](https://developer.chrome.com/docs/webstore/branding) and the [Limited Use policy](https://developer.chrome.com/docs/webstore/program-policies/limited-use).

## Before you upload

1. `cd extension && npm ci && npm test && npm run build`, then zip the **contents** of `dist/` (the manifest must be at the root of the zip, not in a folder). From the repository root, `npm install && npx e2e run tests/extension-listing.e2e.ts` checks that Chrome lists the build under the name and version the manifest declares.
2. Every new upload needs a higher `version` in `extension/manifest.json` and `extension/package.json`.
3. The privacy policy URL below must be live: `public/privacy.html` in the `likelyread-site` repo, served at `https://likelyread.com/privacy`.
4. Likelyread Cloud must be live at `https://app.likelyread.com` (reviewers sign in with Google there). The self-hosted reviewer server is the fallback.

## Package tab

Upload the zip. Name, summary, version and icons come from the manifest:

- **Name:** LikelyRead (10 characters, limit 75)
- **Summary:** See when the emails you send from Gmail™ are opened. Free with a Google sign-in, or self-host it on your own server. (116 characters, limit 132)
- **Icon:** `icons/128.png`, 96×96 artwork with 16 px transparent padding.

## Store listing tab

**Description** (overview, then a short feature list, then the trademark attribution the branding guidelines require):

```
Likelyread tells you when the emails you send from Gmail™ are opened, and why each open counted.

Most trackers count every image load as an open. Likelyread sets aside your own views and Apple Mail's automatic downloads, and shows the reason behind every verdict, so the opens you see are the ones that count.

What you get:
- A red eye next to Send: tracking on or off for each email
- A campaign picker in the compose window: choose or create a campaign without leaving Gmail
- The address you sent from, including send-as aliases
- A toolbar popup with every tracked email and its status
- A dashboard: every hit with its reason, and on Pro, open rates per campaign, time to first open and busiest hours
- Sender health on Pro: checks that each address you send from is set up so inboxes trust it, suggests a safe daily pace for new addresses, and warns when opens drop
- An MCP server, a command-line tool and an agent skill, so AI agents and scripts can track the emails they send

Two ways to use it:
- Likelyread Cloud: click Sign in with Google in the extension's settings and you are ready. The Free plan tracks every email with no signature added to it and keeps 30 days of history. Pro (from €3 a month plus VAT, billed yearly) adds campaign analytics, charts, filters and unlimited history.
- Self-hosted: install the open-source server on any small Linux machine with one command and paste the setup link it prints. Every feature, free, and your data never leaves your server.

The extension has no analytics and loads no code from the internet. Open source, MIT licence.

Tracking images can fall under privacy laws such as the EU ePrivacy rules and the GDPR. You are responsible for getting consent where it is required.

Gmail is a trademark of Google LLC. Use of this trademark is subject to Google Permissions. Likelyread is not affiliated with Google.
```

- **Category:** Productivity, then Communication
- **Language:** English
- **Screenshots** (1280×800, in this order): `store/s1-compose.png`, `store/s2-popup.png`, `store/s3-overview.png`, `store/s4-detail.png`, `store/s5-setup.png`
- **Small promo tile** (440×280, required): `store/promo-small.png`
- **Marquee promo tile** (1400×560, optional, needed to be featured): `store/promo-marquee.png`
- **Official URL / Homepage URL:** https://likelyread.com
- **Support URL:** https://github.com/Hugo-SEQUIER/likelyread/issues

The compose window in `s1-compose.png` is a neutral mock-up with the real extension controls injected. If you prefer an unaltered capture of real Gmail (allowed by the branding guidelines), take one at 1280×800 with the campaign picker open and use it instead.

## Privacy practices tab

**Single purpose:**

```
Show the user whether the emails they send from Gmail have been opened, using the Likelyread Cloud service or an open-tracking server that the user hosts themselves.
```

**Permission justifications:**

| Permission | Justification to paste |
|---|---|
| `storage` | Saves the user's own settings on their device: the server address and access token (Likelyread Cloud or their own server), whether tracking is on by default, the default campaign, the addresses they send from, and a queue of emails waiting to be registered when their server is unreachable. |
| `declarativeNetRequest` | Blocks the user's own browser from loading their own tracking images directly, so viewing their own sent email is not counted as an open. The single rule is limited to the user's own server address. |
| `alarms` | Retries registering a sent email with the user's server after a network failure (backoff up to 7 days), even after the service worker has been suspended. |
| Host permission `https://mail.google.com/*` | The content script adds the tracking switch and campaign picker to Gmail's compose window, adds the tracking image to the email when the user clicks Send, and recognises the user's own sent emails so their views are not counted as opens. |
| Host permission `https://app.likelyread.com/*` | The Likelyread Cloud service. The extension registers each tracked email with it and reads the user's tracking results from it, for users who sign in with Google instead of running their own server. The site is also listed under `externally_connectable`, so the sign-in page can hand the account token to the extension; the extension accepts that message only from this origin. |
| Optional host permissions `https://*/*`, `http://localhost/*`, `http://127.0.0.1/*` | The user's server is self-hosted, so its address is unknown when the extension is built. The extension requests access to exactly one origin, the server address the user enters, at the moment they click Save in the settings. No access is requested otherwise. The localhost entries are only used when someone runs the server on their own computer for development. |

**Remote code:** No, I am not using remote code. (All JavaScript is bundled in the package; the extension loads no scripts, fonts or styles from the network.)

**Data usage, what is collected:** tick

- **Personally identifiable information**: the email addresses of recipients and of the sender, and for Likelyread Cloud the user's own Google account email (from sign-in).
- **Personal communications**: the subject line of each tracked email.
- **Authentication information**: the access token for Likelyread Cloud or for the user's own server, stored on the device and sent only to that server.

Leave the others unticked. The extension does not read email bodies, browsing history, location, payment or health data.

All of it goes only to the server the user connects: Likelyread Cloud, which the developer operates solely to show the user their own results, or a server the user runs, in which case nothing is sent to the developer. It is never sold or shared, and never used for advertising.

**Data usage, certifications:** tick all three

- I do not sell or transfer user data to third parties, outside of the approved use cases.
- I do not use or transfer user data for purposes that are unrelated to my item's single purpose.
- I do not use or transfer user data to determine creditworthiness or for lending purposes.

**Privacy policy URL:** https://likelyread.com/privacy (it contains the Limited Use statement the policy requires).

## Test instructions tab

Reviewers can use Likelyread Cloud with their own Google account; nothing else is needed. Paste this:

```
1. Install the extension. Its settings page opens.
2. Click "Sign in with Google" and pick any Google account. The tab that opens says "Extension connected". Close it.
3. Open Gmail and write an email to an address you can read elsewhere (a second account or your phone). A red eye and a campaign menu appear next to Send. Send it.
4. Open that email as the recipient. Within a minute, the extension's toolbar popup shows "Opened" for it. Click the row to see each hit and why it counted.
5. https://app.likelyread.com shows the dashboard (Overview, Campaigns, Emails, Account). Campaign analytics are part of the paid Pro plan; the Free plan shows an upgrade card there. Paid plans are sold on our website through Stripe, not through the extension.

Note: opening the email in the sender's own Gmail account is deliberately not counted as an open; the hit is marked "Self view".
```

To show the self-hosted path too, add a line with the reviewer server's setup link ("Use my own server instead" in the settings). It contains a token, so it stays out of this repository; print it with `sudo bash /root/likelyread-review/deploy/install.sh --show-link --dir /root/likelyread-review`. For a reviewer to see Pro features, set their account to lifetime from the server after they sign in.

## Distribution tab

- **Visibility:** Public
- **Regions:** All regions
- **Pricing:** Free

## After approval

- Add the store link to the README and the landing page.
- Keep the reviewer server running while any submission or update is in review.
