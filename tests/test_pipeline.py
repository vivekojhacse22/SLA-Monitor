"""End-to-end checks for the sync -> local DB -> ruleset fan-out pipeline.

These run entirely offline: Dataverse is replaced by a stub row source and the
notification senders are replaced by recorders.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import localdb
import rulesets
import runner


CONFIG_DOCUMENT = {
    "version": 1,
    "concurrency": {"max_concurrent_runs": 50, "run_timeout_seconds": 20},
    "businesses": [
        {
            "name": "Azure AI",
            "match": {"country": ["India"]},
            "rulesets": [
                {
                    "id": "azure-ai-emea",
                    "name": "Azure AI / Ruleset EMEA",
                    "warn_minutes": 60,
                    "alert_on": ["pending"],
                    "match": {"support_region_contains": ["EMEA"]},
                },
                {
                    "id": "azure-ai-atz",
                    "name": "Azure AI / Ruleset ATZ",
                    "warn_minutes": 60,
                    "alert_on": ["pending"],
                    "match": {"support_region_contains": ["ATZ"]},
                },
            ],
        },
        {
            "name": "FabricBI",
            "match": {"country": ["India"]},
            "rulesets": [
                {
                    "id": "fabricbi-s500",
                    "name": "FabricBI / Ruleset S500",
                    "warn_minutes": 60,
                    "alert_on": ["missed"],
                    "match": {"pod_contains": ["Fabric"]},
                }
            ],
        },
    ],
}


def record(case_number, region="EMEA", pod="Azure Integration Services",
           minutes=30, state="Pending", country="India"):
    due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {
        "id": f"id-{case_number}",
        "case_number": case_number,
        "case_owner": "Someone",
        "country": country,
        "support_region": f"DTP DP {region} - India",
        "pod": pod,
        "sla_state": state,
        "status": "Expiring soon" if minutes >= 0 else "Breached",
        "sla_due_utc": due.isoformat(),
        "minutes_remaining": float(minutes),
        "active_status": "Active",
        "transfer_reason": "-",
    }


class RulesetConfigTests(unittest.TestCase):
    def test_parses_businesses_and_rulesets(self):
        config = rulesets.parse(CONFIG_DOCUMENT)
        self.assertEqual(len(config.enabled_businesses), 2)
        self.assertEqual(len(config.rulesets), 3)
        self.assertEqual(config.max_concurrent_runs, 50)
        self.assertEqual(config.run_timeout_seconds, 20)

    def test_rejects_unknown_match_field(self):
        broken = {
            "businesses": [
                {"name": "X", "match": {"nonsense": ["a"]}, "rulesets": []}
            ]
        }
        with self.assertRaises(rulesets.RulesetConfigError):
            rulesets.parse(broken)

    def test_rejects_duplicate_ruleset_ids(self):
        broken = {
            "businesses": [
                {
                    "name": "X",
                    "rulesets": [{"id": "same"}, {"id": "same"}],
                }
            ]
        }
        with self.assertRaises(rulesets.RulesetConfigError):
            rulesets.parse(broken)

    def test_ruleset_selects_only_matching_cases(self):
        config = rulesets.parse(CONFIG_DOCUMENT)
        emea = next(r for r in config.rulesets if r.id == "azure-ai-emea")
        records = [record("A", region="EMEA"), record("B", region="ATZ")]
        self.assertEqual([r["case_number"] for r in emea.select(records)], ["A"])

    def test_business_for_labels_records(self):
        config = rulesets.parse(CONFIG_DOCUMENT)
        self.assertEqual(config.business_for(record("A")), "Azure AI")
        self.assertEqual(config.business_for(record("A", country="Japan")), "")


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = localdb.LocalStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_upsert_then_purge_completed(self):
        self.store.upsert_cases([record("A"), record("B")])
        self.assertEqual(self.store.count_cases(), 2)
        removed = self.store.purge_completed({"id-A"})
        self.assertEqual(removed, 1)
        self.assertEqual(
            [c["case_number"] for c in self.store.pending_cases()], ["A"]
        )

    def test_upsert_is_idempotent(self):
        self.store.upsert_cases([record("A", minutes=30)])
        self.store.upsert_cases([record("A", minutes=5)])
        cached = self.store.pending_cases()
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["minutes_remaining"], 5.0)

    def test_notification_state_roundtrip(self):
        self.assertFalse(self.store.was_sent("fp"))
        self.store.mark_sent("fp", "CASE-1", "azure-ai-emea")
        self.assertTrue(self.store.was_sent("fp"))

    def test_configuration_cache(self):
        self.store.save_config("rulesets", CONFIG_DOCUMENT)
        self.assertEqual(self.store.load_config("rulesets"), CONFIG_DOCUMENT)

    def test_watermark_roundtrip(self):
        self.assertEqual(self.store.get_watermark(), "")
        self.store.set_watermark("2026-01-01T00:00:00Z")
        self.assertEqual(self.store.get_watermark(), "2026-01-01T00:00:00Z")


class FanOutTests(unittest.TestCase):
    def setUp(self):
        self.store = localdb.LocalStore(":memory:")
        self.config = rulesets.parse(CONFIG_DOCUMENT)
        self.sent = []

    def tearDown(self):
        self.store.close()

    def _senders(self):
        async def send(ruleset, records):
            self.sent.append((ruleset.id, [r["case_number"] for r in records]))
            return 1

        return {"pending": send, "missed": send}

    def test_each_ruleset_only_sees_its_own_cases(self):
        records = [
            record("EMEA-1", region="EMEA"),
            record("ATZ-1", region="ATZ"),
            record("FAB-1", pod="Fabric Analytics", minutes=-10, state="Missed"),
        ]
        result = asyncio.run(
            runner.run_cycle(self.config, records, self.store, self._senders())
        )
        by_ruleset = dict(self.sent)
        self.assertEqual(by_ruleset["azure-ai-emea"], ["EMEA-1"])
        self.assertEqual(by_ruleset["azure-ai-atz"], ["ATZ-1"])
        self.assertEqual(by_ruleset["fabricbi-s500"], ["FAB-1"])
        self.assertEqual(result.sent, 3)
        self.assertEqual(result.failures, [])

    def test_second_cycle_does_not_resend(self):
        records = [record("EMEA-1", region="EMEA")]
        asyncio.run(runner.run_cycle(self.config, records, self.store, self._senders()))
        self.sent.clear()
        again = asyncio.run(
            runner.run_cycle(self.config, records, self.store, self._senders())
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(again.sent, 0)

    def test_a_failing_ruleset_does_not_stop_the_others(self):
        async def send(ruleset, records):
            if ruleset.id == "azure-ai-emea":
                raise RuntimeError("Teams is down")
            self.sent.append((ruleset.id, [r["case_number"] for r in records]))
            return 1

        records = [record("EMEA-1", region="EMEA"), record("ATZ-1", region="ATZ")]
        result = asyncio.run(
            runner.run_cycle(
                self.config, records, self.store, {"pending": send, "missed": send}
            )
        )
        outcomes = {r.ruleset_id: r.outcome for r in result.results}
        self.assertEqual(outcomes["azure-ai-emea"], "error")
        self.assertEqual(outcomes["azure-ai-atz"], "ok")
        self.assertEqual(dict(self.sent)["azure-ai-atz"], ["ATZ-1"])

    def test_a_slow_ruleset_is_cut_off_at_the_deadline(self):
        slow_config = rulesets.parse(
            {
                "concurrency": {"max_concurrent_runs": 50, "run_timeout_seconds": 1},
                "businesses": CONFIG_DOCUMENT["businesses"],
            }
        )

        async def send(ruleset, records):
            await asyncio.sleep(5)
            return 1

        records = [record("EMEA-1", region="EMEA")]
        result = asyncio.run(
            runner.run_cycle(
                slow_config, records, self.store, {"pending": send, "missed": send}
            )
        )
        timed_out = [r for r in result.results if r.outcome == "timeout"]
        self.assertEqual(len(timed_out), 1)
        self.assertLess(result.duration_ms, 4000)

    def test_runs_are_bounded_by_max_concurrency(self):
        many = {
            "concurrency": {"max_concurrent_runs": 2, "run_timeout_seconds": 20},
            "businesses": [
                {
                    "name": "Bulk",
                    "match": {},
                    "rulesets": [
                        {"id": f"bulk-{i}", "alert_on": ["pending"], "match": {}}
                        for i in range(10)
                    ],
                }
            ],
        }
        config = rulesets.parse(many)
        peak = {"now": 0, "max": 0}

        async def send(ruleset, records):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.01)
            peak["now"] -= 1
            return 1

        records = [record("A")]
        asyncio.run(
            runner.run_cycle(
                config, records, self.store, {"pending": send, "missed": send}
            )
        )
        self.assertLessEqual(peak["max"], 2)

    def test_every_run_is_logged(self):
        records = [record("EMEA-1", region="EMEA")]
        asyncio.run(runner.run_cycle(self.config, records, self.store, self._senders()))
        logged = {entry["ruleset_id"] for entry in self.store.recent_runs()}
        self.assertEqual(logged, {"azure-ai-emea", "azure-ai-atz", "fabricbi-s500"})


if __name__ == "__main__":
    unittest.main()
