import unittest
from unittest.mock import Mock, patch

import mailer


def _record(state="Pending", minutes=30):
    return {
        "case_number": "SR-1",
        "case_owner": "Owner <One>",
        "pod": "Integration",
        "support_region": "India",
        "sla_due_utc": "2026-08-27T12:00:00+00:00",
        "minutes_remaining": minutes,
        "sla_state": state,
        "active_status": "Troubleshooting",
        "transfer_reason": "Misroute - Technical",
    }


class EmailSelectionTests(unittest.TestCase):
    def test_selects_pending_inside_window(self):
        self.assertEqual(mailer.select_pending([_record()], 60)[0]["case_number"], "SR-1")
        self.assertEqual(mailer.select_pending([_record(minutes=90)], 60), [])

    def test_pending_excludes_zero_and_negative_time(self):
        self.assertEqual(mailer.select_pending([_record(minutes=0)], 60), [])
        self.assertEqual(mailer.select_pending([_record(minutes=-0.1)], 60), [])

    def test_selects_pending_inside_non_overlapping_stage(self):
        self.assertEqual(mailer.select_pending([_record(minutes=59.2)], 60, 30)[0]["case_number"], "SR-1")
        self.assertEqual(mailer.select_pending([_record(minutes=30)], 60, 30), [])

    def test_selects_missed(self):
        self.assertEqual(mailer.select_missed([_record("Missed", -5)])[0]["case_number"], "SR-1")

    def test_selects_met_with_one_minute_or_less_remaining(self):
        inside = _record("Complete", 0.8)
        self.assertEqual(mailer.select_met([inside], 1), [inside])
        self.assertEqual(mailer.select_met([_record("Met", 1.1)], 1), [])
        self.assertEqual(mailer.select_met([_record("Met", -0.1)], 1), [])

    def test_met_message_is_labelled_clearly(self):
        payload = mailer.build_message([_record("Complete", 0.8)])
        self.assertIn("SLA met within 1 minute", payload["message"]["subject"])

    def test_message_contains_key_details_and_escapes_html(self):
        payload = mailer.build_message([_record("Missed", -5)])
        content = payload["message"]["body"]["content"]
        self.assertIn("Owner &lt;One&gt;", content)
        self.assertIn("Misroute - Technical", content)
        self.assertIn("5.0 minutes overdue", content)


class EmailSendTests(unittest.TestCase):
    @patch("mailer._get_token", return_value="token")
    @patch("mailer.requests.post")
    def test_posts_graph_send_mail_payload(self, post, _get_token):
        post.return_value = Mock(status_code=202, text="")

        count = mailer.send_alert_email(
            [_record()],
            "tenant",
            "client",
            "secret",
            "sender@example.com",
            ["recipient@example.com"],
        )

        self.assertEqual(count, 1)
        self.assertEqual(
            post.call_args.args[0],
            "https://graph.microsoft.com/v1.0/users/sender%40example.com/sendMail",
        )
        recipients = post.call_args.kwargs["json"]["message"]["toRecipients"]
        self.assertEqual(recipients[0]["emailAddress"]["address"], "recipient@example.com")

    @patch("mailer.time.sleep")
    @patch("mailer._get_token", return_value="token")
    @patch("mailer.requests.post")
    def test_retries_graph_throttling(self, post, _get_token, sleep):
        post.side_effect = [
            Mock(status_code=429, text="throttled", headers={"Retry-After": "1"}),
            Mock(status_code=202, text="", headers={}),
        ]

        count = mailer.send_alert_email(
            [_record()], "tenant", "client", "secret",
            "sender@example.com", ["recipient@example.com"]
        )

        self.assertEqual(count, 1)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()