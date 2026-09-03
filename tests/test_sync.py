"""Checks for the incremental sync and the one-query-for-all-businesses pull."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import localdb
import rulesets
import sync


DOCUMENT = {
    "concurrency": {"max_concurrent_runs": 50, "run_timeout_seconds": 20},
    "businesses": [
        {
            "name": "Azure AI",
            "match": {"country": ["India"], "product": ["Azure\\Data Factory"]},
            "rulesets": [{"id": "azure-ai", "match": {}}],
        },
        {
            "name": "FabricBI",
            "match": {"country": ["Japan"]},
            "rulesets": [{"id": "fabricbi", "match": {}}],
        },
    ],
}


def row(case_number, country="India", region="DTP DP Integration - India", minutes=30):
    due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {
        config.COL_ID: f"id-{case_number}",
        config.COL_CASE_NUMBER: case_number,
        config.SLA_COLUMNS[0]: due.isoformat(),
        config.COL_SLA_STATE: "Pending",
        config.COL_COUNTRY: "country-guid",
        f"{config.COL_COUNTRY}@OData.Community.Display.V1.FormattedValue": country,
        config.COL_SUPPORT_REGION: "region-guid",
        f"{config.COL_SUPPORT_REGION}@OData.Community.Display.V1.FormattedValue": region,
        config.COL_PRODUCT: "product-guid",
        f"{config.COL_PRODUCT}@OData.Community.Display.V1.FormattedValue": "Azure\\Data Factory",
        config.COL_POD: "pod-guid",
        f"{config.COL_POD}@OData.Community.Display.V1.FormattedValue": "Azure Integration Services",
        config.SYNC_WATERMARK_COLUMN: datetime.now(timezone.utc).isoformat(),
    }


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.config = rulesets.parse(DOCUMENT)

    def test_all_businesses_are_covered_by_one_filter(self):
        built = sync.build_filter(self.config, base_filter="statecode eq 0")
        self.assertIn("statecode eq 0", built)
        self.assertIn("crmee_sr_country/crmee_name eq 'India'", built)
        self.assertIn("crmee_sr_country/crmee_name eq 'Japan'", built)
        self.assertIn("crmee_product_name/crmee_name eq 'Azure\\Data Factory'", built)
        self.assertIn(" or ", built)

    def test_watermark_is_appended_when_incremental(self):
        built = sync.build_filter(self.config, watermark="2026-01-01T00:00:00Z")
        self.assertIn(f"{config.SYNC_WATERMARK_COLUMN} gt 2026-01-01T00:00:00Z", built)

    def test_support_pod_contains_is_applied_to_pipeline_query(self):
        with patch.object(config, "SUPPORT_POD_CONTAINS", "- Integration"):
            built = sync.build_filter(self.config, base_filter="statecode eq 0")

        self.assertIn(
            "contains(crmee_supportpod/crmee_name,'- Integration')", built
        )

    def test_global_country_and_support_region_filters_are_applied(self):
        with patch.object(config, "COUNTRY_EQUALS", "India"), patch.object(
            config, "SUPPORT_REGION_EQUALS", "DTP DP Integration - India"
        ):
            built = sync.build_filter(self.config, base_filter="statecode eq 0")

        self.assertIn("crmee_sr_country/crmee_name eq 'India'", built)
        self.assertIn(
            "crmee_support_region/crmee_name eq 'DTP DP Integration - India'",
            built,
        )

    def test_watermark_is_absent_on_a_full_pull(self):
        built = sync.build_filter(self.config, watermark="")
        self.assertNotIn(" gt ", built)

    def test_quotes_in_values_are_escaped(self):
        quoted = rulesets.parse(
            {
                "businesses": [
                    {
                        "name": "Odd",
                        "match": {"country": ["O'Brien"]},
                        "rulesets": [{"id": "odd", "match": {}}],
                    }
                ]
            }
        )
        self.assertIn("O''Brien", sync.build_filter(quoted, base_filter=""))


class SyncRunTests(unittest.TestCase):
    def setUp(self):
        self.store = localdb.LocalStore(":memory:")
        self.config = rulesets.parse(DOCUMENT)
        self.rows = [row("A"), row("B", country="Japan")]
        self._original_fetch = sync.fetch
        sync.fetch = lambda token, cfg, watermark="": self.rows

    def tearDown(self):
        sync.fetch = self._original_fetch
        self.store.close()

    def test_first_run_is_a_full_refresh_and_fills_the_cache(self):
        result = sync.run("token", self.store, self.config)
        self.assertTrue(result.full_refresh)
        self.assertEqual(result.pulled, 2)
        self.assertEqual(self.store.count_cases(), 2)

    def test_cases_are_labelled_with_their_business(self):
        sync.run("token", self.store, self.config)
        businesses = sorted(c["business"] for c in self.store.pending_cases())
        self.assertEqual(businesses, ["Azure AI", "FabricBI"])

    def test_completed_cases_are_deleted_on_a_full_refresh(self):
        sync.run("token", self.store, self.config, force_full=True)
        self.assertEqual(self.store.count_cases(), 2)
        self.rows = [row("A")]
        result = sync.run("token", self.store, self.config, force_full=True)
        self.assertEqual(result.purged, 1)
        self.assertEqual(
            [c["case_number"] for c in self.store.pending_cases()], ["A"]
        )

    def test_incremental_run_never_deletes(self):
        sync.run("token", self.store, self.config, force_full=True)
        self.rows = [row("A")]
        later = datetime.now(timezone.utc) + timedelta(minutes=1)
        result = sync.run("token", self.store, self.config, now=later)
        self.assertFalse(result.full_refresh)
        self.assertEqual(result.purged, 0)
        self.assertEqual(self.store.count_cases(), 2)

    def test_watermark_advances_with_an_overlap(self):
        now = datetime.now(timezone.utc)
        sync.run("token", self.store, self.config, now=now)
        stored = self.store.get_watermark("dataverse")
        parsed = datetime.strptime(stored, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLessEqual(parsed, now)
        self.assertGreaterEqual(
            parsed, now - timedelta(seconds=config.SYNC_OVERLAP_SECONDS + 2)
        )

    def test_active_cases_without_an_sla_are_cached_for_the_dashboard(self):
        no_sla = row("C")
        for column in config.SLA_COLUMNS:
            no_sla.pop(column, None)
        no_sla[config.COL_SLA_STATE] = "Achieved"
        self.rows = [no_sla]
        sync.run("token", self.store, self.config, force_full=True)
        self.assertEqual(self.store.count_cases(), 1)

    def test_all_active_completed_cases_are_cached_for_the_dashboard(self):
        now = datetime.now(timezone.utc)
        just_met = row("JUST-MET")
        just_met[config.COL_SLA_STATE] = "Complete"
        just_met[config.SLA_COLUMNS[0]] = (now + timedelta(seconds=48)).isoformat()
        too_early = row("TOO-EARLY")
        too_early[config.COL_SLA_STATE] = "Met"
        too_early[config.SLA_COLUMNS[0]] = (now + timedelta(minutes=2)).isoformat()
        self.rows = [just_met, too_early]

        sync.run("token", self.store, self.config, now=now, force_full=True)

        self.assertEqual(
            sorted(case["case_number"] for case in self.store.pending_cases()),
            ["JUST-MET", "TOO-EARLY"],
        )


if __name__ == "__main__":
    unittest.main()
