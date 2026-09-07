from html import escape
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import smtplib

from .config import settings
from .models import Offer


def offer_email_html(offers: list[Offer], title: str) -> str:
        rows = "".join(
                f'''<tr>
                    <td style="padding:18px 0;border-bottom:1px solid #dfe7e2;vertical-align:top;">
                        <div style="font-size:16px;line-height:22px;font-weight:700;color:#17352b;">{escape(o.name)}</div>
                        <div style="margin-top:4px;font-size:14px;line-height:20px;color:#5b7067;">{escape(o.company)}</div>
                    </td>
                    <td style="padding:18px 10px;border-bottom:1px solid #dfe7e2;vertical-align:top;font-size:13px;line-height:20px;color:#5b7067;">{escape(o.region_label)}</td>
                    <td style="padding:18px 10px;border-bottom:1px solid #dfe7e2;vertical-align:top;font-size:13px;line-height:20px;color:#5b7067;">{escape(o.start_term or "Not specified")}</td>
                    <td style="padding:18px 0;border-bottom:1px solid #dfe7e2;vertical-align:top;text-align:right;">
                        <a href="{escape(o.offer_url, quote=True)}" style="color:#19765b;font-size:13px;font-weight:700;text-decoration:none;white-space:nowrap;">View offer&nbsp;→</a>
                    </td>
                </tr>'''
                for o in offers
        )
        count_label = "opportunity" if len(offers) == 1 else "opportunities"
        return f"""<!doctype html>
<html lang="en">
    <body style="margin:0;background:#edf3ef;font-family:Arial,Helvetica,sans-serif;color:#17352b;">
        <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{len(offers)} new {count_label} matching your Trackr Alerts preferences.</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#edf3ef;">
            <tr><td align="center" style="padding:28px 14px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;background:#ffffff;border:1px solid #d9e4dd;border-radius:12px;overflow:hidden;">
                    <tr><td style="padding:25px 28px;background:#17352b;color:#ffffff;">
                        <div style="font-size:12px;line-height:16px;letter-spacing:1.5px;text-transform:uppercase;color:#a9d7c0;font-weight:700;">Trackr Alerts</div>
                        <h1 style="margin:12px 0 0;font-size:28px;line-height:34px;font-weight:700;color:#ffffff;">{escape(title)}</h1>
                    </td></tr>
                    <tr><td style="padding:28px;">
                        <p style="margin:0;font-size:16px;line-height:25px;color:#365247;">We found <strong style="color:#17352b;">{len(offers)} new {count_label}</strong> that match your preferences.</p>
                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:22px;border-collapse:collapse;">
                            <thead><tr>
                                <th align="left" style="padding:0 0 10px;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:#789087;">Opportunity</th>
                                <th align="left" style="padding:0 10px 10px;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:#789087;">Region</th>
                                <th align="left" style="padding:0 10px 10px;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:#789087;">Start</th>
                                <th style="padding:0 0 10px;"></th>
                            </tr></thead>
                            <tbody>{rows}</tbody>
                        </table>
                    </td></tr>
                    <tr><td style="padding:18px 28px;background:#f5f8f6;border-top:1px solid #e1e9e4;font-size:12px;line-height:19px;color:#71847b;">You are receiving this because these opportunities match your Trackr Alerts preferences.</td></tr>
                </table>
            </td></tr>
        </table>
    </body>
</html>"""


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
        refused = client.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    return message_id


def send_magic_link(to: str, url: str) -> str:
    return send_email(
        to,
        "Your Trackr Alerts sign-in link",
                f'''<!doctype html><html lang="en"><body style="margin:0;background:#edf3ef;font-family:Arial,Helvetica,sans-serif;color:#17352b;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#edf3ef;"><tr><td align="center" style="padding:28px 14px;">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border:1px solid #d9e4dd;border-radius:12px;overflow:hidden;">
                        <tr><td style="padding:22px 26px;background:#17352b;color:#ffffff;font-size:12px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;">Trackr Alerts</td></tr>
                        <tr><td style="padding:30px 26px 34px;"><h1 style="margin:0;font-size:26px;line-height:32px;color:#17352b;">Welcome back</h1>
                            <p style="margin:14px 0 24px;font-size:16px;line-height:25px;color:#536b60;">Use the secure button below to sign in. This link expires in 15 minutes.</p>
                            <a href="{escape(url, quote=True)}" style="display:inline-block;padding:13px 20px;background:#19765b;border-radius:7px;color:#ffffff;font-size:15px;font-weight:700;text-decoration:none;">Sign in to Trackr Alerts</a>
                            <p style="margin:26px 0 0;font-size:12px;line-height:19px;color:#81938a;">If you did not request this email, you can safely ignore it.</p>
                        </td></tr>
                    </table>
                </td></tr></table></body></html>''',
        f"magic-{to}-{url.rsplit('/', 1)[-1]}",
    )
