import unittest

import alerting
from notification_state import MemoryNotificationState


def _record(case_number, minutes_remaining=30, due="2026-01-01T12:00:00Z"):
    return {
        "id": case_number,
        "case_number": case_number,
        "sla_state": "Pending",
        "minutes_remaining": minutes_remaining,
        "sla_due_utc": due,
    }


class AlertCycleTests(unittest.IsolatedAsyncioTestCase):
    def test_default_fingerprint_remains_backward_compatible(self):
        record = _record("SR-1")

        self.assertEqual(
            alerting.alert_fingerprint(record),
            "13f43fc7f2aecf3d1e9910280e80695eb65e86c07b3566b26042b09eb69d5fa6",
        )
        self.assertNotEqual(
            alerting.alert_fingerprint(record),
            alerting.alert_fingerprint(record, "email:pending"),
        )

    async def test_successful_alert_is_not_sent_twice(self):
        state = MemoryNotificationState()
        sent_batches = []

        async def sender(records):
            sent_batches.append(list(records))
            return 1

        first = await alerting.run_alert_cycle([_record("SR-1")], state, sender)
        second = await alerting.run_alert_cycle([_record("SR-1")], state, sender)

        self.assertEqual(first.sent, 1)
        self.assertEqual(second.sent, 0)
        self.assertEqual(second.skipped_duplicate, 1)
        self.assertEqual(len(sent_batches), 1)

    async def test_changed_due_time_creates_a_new_alert(self):
        state = MemoryNotificationState()
        send_count = 0

        async def sender(records):
            nonlocal send_count
            self.assertEqual(len(records), 1)
            send_count += 1
            return 1

        await alerting.run_alert_cycle([_record("SR-1")], state, sender)
        result = await alerting.run_alert_cycle(
            [_record("SR-1", due="2026-01-01T13:00:00Z")], state, sender
        )

        self.assertEqual(result.sent, 1)
        self.assertEqual(send_count, 2)

    async def test_failed_send_is_retried_on_next_cycle(self):
        state = MemoryNotificationState()

        async def failing_sender(records):
            self.assertEqual(len(records), 1)
            raise RuntimeError("Teams unavailable")

        with self.assertRaisesRegex(RuntimeError, "Teams unavailable"):
            await alerting.run_alert_cycle([_record("SR-1")], state, failing_sender)

        self.assertFalse(state.was_sent(alerting.alert_fingerprint(_record("SR-1"))))

    async def test_only_pending_cases_inside_window_are_sent(self):
        state = MemoryNotificationState()
        sent = []

        async def sender(records):
            sent.extend(records)
            return 1

        closed = _record("SR-closed")
        closed["sla_state"] = "Met"
        result = await alerting.run_alert_cycle(
            [_record("SR-due"), _record("SR-later", 90), closed], state, sender
        )

        self.assertEqual(result.eligible, 1)
        self.assertEqual([record["case_number"] for record in sent], ["SR-due"])

    async def test_channels_have_independent_delivery_state(self):
        state = MemoryNotificationState()
        deliveries = []

        async def sender(records):
            deliveries.append(records[0]["case_number"])
            return 1

        record = _record("SR-1")
        await alerting.run_alert_cycle([record], state, sender, namespace="teams")
        result = await alerting.run_alert_cycle(
            [record], state, sender, namespace="email"
        )

        self.assertEqual(result.sent, 1)
        self.assertEqual(deliveries, ["SR-1", "SR-1"])

    async def test_custom_selector_can_include_missed_cases(self):
        state = MemoryNotificationState()
        sent = []
        missed = _record("SR-missed", minutes_remaining=-5)
        missed["sla_state"] = "Missed"

        def selector(records, max_minutes):
            return [
                record
                for record in records
                if record["sla_state"] == "Missed"
                or 0 <= record["minutes_remaining"] <= max_minutes
            ]

        async def sender(records):
            sent.extend(records)
            return 1

        result = await alerting.run_alert_cycle(
            [missed], state, sender, selector=selector, namespace="email"
        )

        self.assertEqual(result.sent, 1)
        self.assertEqual(sent[0]["case_number"], "SR-missed")


class NotificationStateTests(unittest.TestCase):
    def test_destination_must_be_unique_when_not_configured(self):
        state = MemoryNotificationState()
        state.save_conversation("first", "https://service/one")
        self.assertEqual(state.get_destination()["conversation_id"], "first")

        state.save_conversation("second", "https://service/two")
        self.assertIsNone(state.get_destination())
        self.assertEqual(state.get_destination("second")["service_url"], "https://service/two")


if __name__ == "__main__":
    unittest.main()