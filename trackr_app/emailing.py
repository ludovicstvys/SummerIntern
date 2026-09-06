from html import escape
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import smtplib

from .config import settings
from .models import Offer


def offer_email_html(offers: list[Offer], title: str) -> str:
    rows = "".join(
        f'<tr><td><strong>{escape(o.company)}</strong><br>{escape(o.name)}</td>'
        f'<td>{escape(o.region)}</td><td>{escape(o.start_term or "—")}</td>'
        f'<td><a href="{escape(o.offer_url, quote=True)}">View offer</a></td></tr>'
        for o in offers
    )
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#18221d">
    <div style="max-width:760px;margin:auto"><h1>{escape(title)}</h1>
    <p>{len(offers)} new matching opportunity{'ies' if len(offers) != 1 else ''}.</p>
    <table style="width:100%;border-collapse:collapse" cellpadding="10"><thead><tr>
    <th align="left">Opportunity</th><th align="left">Region</th><th align="left">Start</th><th></th>
    </tr></thead><tbody>{rows}</tbody></table></div></body></html>"""


def send_email(to: str, subject: str, html: str, idempotency_key: str) -> str:
    if not all((settings.smtp_server, settings.smtp_user, settings.smtp_password, settings.smtp_from)):
        raise RuntimeError("SMTP is not fully configured")
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    domain = settings.smtp_user.rsplit("@", 1)[-1] if "@" in settings.smtp_user else "trackr.local"
    message_id = f"<{digest}@{domain}>"
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = message_id
    message.set_content("This message contains HTML. Open it in an HTML-capable email client.")
    message.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=30) as client:
        client.ehlo()
        client.starttls()
        client.ehlo()
        client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)
    return message_id


def send_magic_link(to: str, url: str) -> str:
    return send_email(
        to,
        "Your Trackr Alerts sign-in link",
        f'<p>Use this secure link to sign in. It expires in 15 minutes.</p><p><a href="{escape(url, quote=True)}">Sign in to Trackr Alerts</a></p>',
        f"magic-{to}-{url.rsplit('/', 1)[-1]}",
    )
