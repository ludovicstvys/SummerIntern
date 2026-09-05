from html import escape

import resend

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
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    resend.api_key = settings.resend_api_key
    result = resend.Emails.send(
        {"from": settings.email_from, "to": [to], "subject": subject, "html": html},
        options={"idempotency_key": idempotency_key[:256]},
    )
    return result["id"]


def send_magic_link(to: str, url: str) -> str:
    return send_email(
        to,
        "Your Trackr Alerts sign-in link",
        f'<p>Use this secure link to sign in. It expires in 15 minutes.</p><p><a href="{escape(url, quote=True)}">Sign in to Trackr Alerts</a></p>',
        f"magic-{to}-{url.rsplit('/', 1)[-1]}",
    )
