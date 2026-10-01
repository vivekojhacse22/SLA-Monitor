import asyncio
import os
import tempfile
import unittest

import alerting
import localdb


RECORD = {
    "id": "case-id",
    "case_number": "2609280030002664",
    "sla_due_utc": "2026-09-28T10:29:07+00:00",
    "sla_state": "Met",
    "minutes_remaining": 0,
}


def select_all(records, max_minutes=60):
    del max_minutes
    return list(records)


class NotificationClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self._temporary_directory.name, "shared.db"
        )
        self.first = localdb.LocalStore(self.database_path)
        self.second = localdb.LocalStore(self.database_path)

    def tearDown(self):
        self.first.close()
        self.second.close()
        self._temporary_directory.cleanup()

    def test_claim_is_atomic_across_store_instances(self):
        fingerprint = alerting.alert_fingerprint(RECORD, "met")

        self.assertTrue(
            self.first.claim_notification(
                fingerprint, RECORD["case_number"], "first-job"
            )
        )
        self.assertFalse(
            self.second.claim_notification(
                fingerprint, RECORD["case_number"], "second-job"
            )
        )

    async def test_overlapping_jobs_send_the_condition_only_once(self):
        first_sender_started = asyncio.Event()
        allow_first_sender_to_finish = asyncio.Event()
        sent_batches = []

        async def first_sender(records):
            sent_batches.append(records)
            first_sender_started.set()
            await allow_first_sender_to_finish.wait()
            return 1

        first_run = asyncio.create_task(
            alerting.run_alert_cycle(
                [RECORD],
                self.first,
                first_sender,
                selector=select_all,
                namespace="met",
            )
        )
        await first_sender_started.wait()

        async def second_sender(records):
            sent_batches.append(records)
            return 1

        second_result = await alerting.run_alert_cycle(
            [RECORD],
            self.second,
            second_sender,
            selector=select_all,
            namespace="met",
        )
        allow_first_sender_to_finish.set()
        first_result = await first_run

        self.assertEqual([[RECORD]], sent_batches)
        self.assertEqual(1, first_result.sent)
        self.assertEqual(0, second_result.sent)
        self.assertEqual(1, second_result.skipped_duplicate)

    async def test_legacy_ruleset_notification_is_adopted_without_sending(self):
        legacy_namespace = "dtp-dp-integration-india-met-1:met"
        legacy_fingerprint = alerting.alert_fingerprint(RECORD, legacy_namespace)
        self.first.mark_sent(
            legacy_fingerprint, RECORD["case_number"], "legacy-ruleset"
        )
        sent_batches = []

        async def sender(records):
            sent_batches.append(records)
            return 1

        result = await alerting.run_alert_cycle(
            [RECORD],
            self.second,
            sender,
            selector=select_all,
            namespace="met",
            legacy_namespaces=(legacy_namespace,),
        )

        self.assertEqual([], sent_batches)
        self.assertEqual(1, result.skipped_duplicate)
        self.assertTrue(
            self.first.was_sent(alerting.alert_fingerprint(RECORD, "met"))
        )

    async def test_definite_send_failure_releases_claim_for_retry(self):
        async def failing_sender(records):
            del records
            raise RuntimeError("webhook rejected the request")

        with self.assertRaisesRegex(RuntimeError, "webhook rejected"):
            await alerting.run_alert_cycle(
                [RECORD],
                self.first,
                failing_sender,
                selector=select_all,
                namespace="met",
            )

        sent_batches = []

        async def successful_sender(records):
            sent_batches.append(records)
            return 1

        result = await alerting.run_alert_cycle(
            [RECORD],
            self.second,
            successful_sender,
            selector=select_all,
            namespace="met",
        )

        self.assertEqual([[RECORD]], sent_batches)
        self.assertEqual(1, result.sent)


if __name__ == "__main__":
    unittest.main()
