import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trackr_app.emailing import send_email, send_magic_link


class SmtpTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            smtp_server="smtp.gmail.com",
            smtp_port=587,
            smtp_user="sender@example.com",
            smtp_password="app-password",
            smtp_from="Trackr Alerts <sender@example.com>",
        )

    @patch("trackr_app.emailing.smtplib.SMTP")
    def test_send_email_uses_starttls_login_html_and_stable_message_id(self, smtp):
        client = MagicMock()
        smtp.return_value.__enter__.return_value = client
        with patch("trackr_app.emailing.settings", self.settings):
            first = send_email("person@example.com", "Subject", "<b>Hello</b>", "stable-key")
            second = send_email("person@example.com", "Subject", "<b>Hello</b>", "stable-key")
        self.assertEqual(first, second)
        smtp.assert_called_with("smtp.gmail.com", 587, timeout=30)
        client.starttls.assert_called()
        client.login.assert_called_with("sender@example.com", "app-password")
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "person@example.com")
        self.assertEqual(message["From"], "Trackr Alerts <sender@example.com>")
        self.assertIn("text/html", str(message))

    @patch("trackr_app.emailing.send_email")
    def test_magic_link_uses_smtp_email_adapter(self, send):
        send.return_value = "message-id"
        result = send_magic_link("person@example.com", "https://trackr-alerts.vercel.app/auth/consume/token")
        self.assertEqual(result, "message-id")
        self.assertIn("Sign in", send.call_args.args[2])

    def test_missing_smtp_configuration_fails(self):
        with patch("trackr_app.emailing.settings", SimpleNamespace(smtp_server="", smtp_user="", smtp_password="", smtp_from="", smtp_port=587)):
            with self.assertRaisesRegex(RuntimeError, "SMTP"):
                send_email("person@example.com", "Subject", "<b>Hello</b>", "key")


if __name__ == "__main__":
    unittest.main()
