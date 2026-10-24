"""The welcome email Likelyread Cloud sends once, when a Google sign-in creates an account.

It points the new user to the dashboard and says how to check that tracking works.
Sent over SMTP with the standard library, and only while SMTP_HOST and MAIL_FROM are
set (CloudSettings), so self-hosted servers, tests and the e2e cloud server send
nothing. No tracking pixel and no remote images: Likelyread does not track its own
emails.
"""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

SUBJECT = "Welcome to Likelyread: your dashboard is ready"
TIMEOUT_SECONDS = 20


def message(to: str, mail_from: str, public_url: str) -> EmailMessage:
    """The welcome email to `to`, as plain text with an HTML alternative."""
    dashboard = f"{public_url}/"
    account = f"{public_url}/#/account"
    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to
    msg["Subject"] = SUBJECT
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=parseaddr(mail_from)[1].rpartition("@")[2] or None)
    # An automatic message (RFC 3834): out-of-office replies should not answer it.
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(
        f"""Welcome to Likelyread.

Your dashboard is ready:
{dashboard}

It lists every email you track, each open with the reason it counted, and your campaigns. Pro adds open rates per campaign, a 30-day timeline and sender health.

Check that it works:
- In Gmail, the red eye next to Send means the email is tracked.
- The LikelyRead icon in Chrome's toolbar lists your tracked emails and their opens. If you don't see it, pin it from the puzzle-piece menu.
- To test, send an email to another address you own and open it there. Your own views never count, so an email to yourself never shows as opened.

An open is the moment your email's image loads for the recipient. Apple Mail's prefetches and your own views are set aside.

This is the only email we send when you sign up. To export or delete your data, open your Account page:
{account}

Likelyread
"""
    )
    d, a = html.escape(dashboard), html.escape(account)
    p = 'style="margin:0 0 16px"'
    msg.add_alternative(
        f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#ffffff;color:#0a0a0a;font:16px/1.5 -apple-system,'Segoe UI',Roboto,Arial,sans-serif">
<div style="max-width:520px">
<p {p}>Welcome to Likelyread.</p>
<p {p}><a href="{d}" style="display:inline-block;padding:12px 20px;background:#d92b21;color:#ffffff;text-decoration:none;font-weight:600;border-radius:4px">Open your dashboard</a></p>
<p {p}>It lists every email you track, each open with the reason it counted, and your campaigns. Pro adds open rates per campaign, a 30-day timeline and sender health.</p>
<p style="margin:0 0 8px;font-weight:600">Check that it works</p>
<ul style="margin:0 0 16px;padding-left:20px">
<li>In Gmail, the red eye next to Send means the email is tracked.</li>
<li>The LikelyRead icon in Chrome's toolbar lists your tracked emails and their opens. If you don't see it, pin it from the puzzle-piece menu.</li>
<li>To test, send an email to another address you own and open it there. Your own views never count, so an email to yourself never shows as opened.</li>
</ul>
<p {p}>An open is the moment your email's image loads for the recipient. Apple Mail's prefetches and your own views are set aside.</p>
<p style="margin:0 0 16px;color:#5f5f5f;font-size:14px">This is the only email we send when you sign up. To export or delete your data, open your <a href="{a}" style="color:#5f5f5f">Account page</a>.</p>
<p style="margin:0">Likelyread<br><a href="{d}" style="color:#0a0a0a">{d}</a></p>
</div>
</body></html>
""",
        subtype="html",
    )
    return msg


def send_smtp(cfg: Any, msg: EmailMessage) -> None:
    """Send one message with cfg's SMTP settings: implicit TLS on port 465, STARTTLS on
    any other port. Never sends in clear text."""
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT_SECONDS)
    with server:
        if cfg.smtp_port != 465:
            server.starttls(context=context)
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


def failure(exc: BaseException) -> str:
    """What to log when sending failed: the error and the server's answer, never the
    recipient's address or the password."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTPRecipientsRefused"
    if isinstance(exc, smtplib.SMTPResponseException):
        answer = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{type(exc).__name__} {exc.smtp_code} {answer[:200]}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"
