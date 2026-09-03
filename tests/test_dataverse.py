import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import dataverse


class CachedTimingTests(unittest.TestCase):
    def test_refreshes_cached_countdown_from_due_time(self):
        now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
        record = {
            "sla_due_utc": (now + timedelta(minutes=4, seconds=30)).isoformat(),
            "minutes_remaining": 15.0,
            "status": "Expiring soon",
        }

        dataverse.refresh_timing([record], now=now)

        self.assertEqual(record["minutes_remaining"], 4.5)
        self.assertEqual(record["status"], "Expiring soon")

    def test_refreshes_cached_record_after_deadline(self):
        now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
        record = {
            "sla_due_utc": (now - timedelta(seconds=30)).isoformat(),
            "minutes_remaining": 10.0,
            "status": "Expiring soon",
        }

        dataverse.refresh_timing([record], now=now)

        self.assertEqual(record["minutes_remaining"], -0.5)
        self.assertEqual(record["status"], "Breached")


class SlaStateTests(unittest.TestCase):
    def test_boolean_sla_met_field_overrides_pending_state(self):
        row = {
            dataverse.config.COL_CASE_NUMBER: "SR-MET",
            dataverse.config.COL_SLA_STATE: "Pending",
            dataverse.config.COL_SLA_MET: True,
        }

        record = dataverse.classify([row])[0]

        self.assertEqual(record["sla_state"], "Met")
        self.assertEqual(record["status"], "Met")


class ProductTests(unittest.TestCase):
    def test_classify_uses_formatted_product_lookup_name(self):
        row = {
            dataverse.config.COL_CASE_NUMBER: "SR-PRODUCT",
            dataverse.config.COL_PRODUCT: "product-guid",
            f"{dataverse.config.COL_PRODUCT}@OData.Community.Display.V1.FormattedValue": "Azure\\Data Factory",
        }

        record = dataverse.classify([row])[0]

        self.assertEqual(record["product"], "Azure\\Data Factory")

    def test_configured_case_override_marks_known_exception_met(self):
        row = {
            dataverse.config.COL_CASE_NUMBER: "SR-OVERRIDE",
            dataverse.config.COL_SLA_STATE: "Pending",
            dataverse.config.COL_SLA_MET: None,
        }

        with patch.object(
            dataverse.config, "SLA_MET_CASE_NUMBERS", {"SR-OVERRIDE"}
        ):
            record = dataverse.classify([row])[0]

        self.assertEqual(record["sla_state"], "Met")
        self.assertEqual(record["status"], "Met")


class SupportPodFilterTests(unittest.TestCase):
    def test_contains_filter_is_case_insensitive_in_fallback(self):
        display_key = (
            f"{dataverse.config.COL_POD}"
            "@OData.Community.Display.V1.FormattedValue"
        )
        rows = [
            {display_key: "DTP DP - Integration"},
            {display_key: "DTP DP - Analytics"},
        ]

        with patch.object(dataverse.config, "COUNTRY_EQUALS", ""), patch.object(
            dataverse.config, "SUPPORT_REGION_EQUALS", ""
        ), patch.object(
            dataverse.config, "SUPPORT_REGION_CONTAINS", ""
        ), patch.object(
            dataverse.config, "SUPPORT_POD_CONTAINS", "- integration"
        ), patch.object(dataverse.config, "PRODUCT_EQUALS", ""):
            filtered = dataverse.apply_row_filters(rows)

        self.assertEqual(filtered, [rows[0]])

    def test_fetch_pushes_contains_filter_to_dataverse(self):
        captured = {}

        def fake_run(token, url, params):
            captured.update(params)
            return []

        with patch.object(dataverse, "_run", side_effect=fake_run), patch.object(
            dataverse.config, "EXTRA_FILTER", ""
        ), patch.object(dataverse.config, "COUNTRY_EQUALS", ""), patch.object(
            dataverse.config, "SUPPORT_REGION_EQUALS", ""
        ), patch.object(
            dataverse.config, "SUPPORT_REGION_CONTAINS", ""
        ), patch.object(
            dataverse.config, "SUPPORT_POD_CONTAINS", "- Integration"
        ), patch.object(dataverse.config, "PRODUCT_EQUALS", ""), patch.object(
            dataverse.config, "MINE_ONLY", False
        ):
            dataverse.fetch_service_requests("token")

        self.assertEqual(
            captured["$filter"],
            "contains(crmee_supportpod/crmee_name,'- Integration')",
        )


if __name__ == "__main__":
    unittest.main()