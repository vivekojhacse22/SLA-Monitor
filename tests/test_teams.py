import unittest

import teams


def _record(state="Pending", minutes=24.6):
    return {
        "case_number": "SR-1",
        "case_owner": "Owner",
        "sla_state": state,
        "minutes_remaining": minutes,
    }


class TeamsHeadingTests(unittest.TestCase):
    def test_pending_heading_uses_ruleset_threshold(self):
        payload = teams.build_payloads([_record()], max_minutes=30)[0]
        heading = payload["attachments"][0]["content"]["body"][0]["text"]
        self.assertEqual(heading, "Pending SLA cases due within 30 minutes (1)")

    def test_sixty_minute_heading_uses_one_hour(self):
        payload = teams.build_payloads([_record()], max_minutes=60)[0]
        heading = payload["attachments"][0]["content"]["body"][0]["text"]
        self.assertEqual(heading, "Pending SLA cases due within 1 hour (1)")

    def test_met_heading_is_not_replaced_by_threshold(self):
        payload = teams.build_payloads(
            [_record(state="Complete", minutes=0.8)], max_minutes=1
        )[0]
        heading = payload["attachments"][0]["content"]["body"][0]["text"]
        self.assertEqual(heading, "SLA met with 1 minute or less remaining (1)")


if __name__ == "__main__":
    unittest.main()