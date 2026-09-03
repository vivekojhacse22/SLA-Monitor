import unittest
from unittest.mock import patch

import app


class PendingDashboardTests(unittest.TestCase):
    def test_index_includes_support_region_filter(self):
        response = app.app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="support-region"', response.data)
        self.assertIn(b"All Support Regions", response.data)

    def test_pending_requires_positive_remaining_time(self):
        self.assertTrue(
            app._is_active_pending(
                {"sla_state": "Pending", "minutes_remaining": 0.1}
            )
        )
        self.assertFalse(
            app._is_active_pending(
                {"sla_state": "Pending", "minutes_remaining": 0}
            )
        )
        self.assertFalse(
            app._is_active_pending(
                {"sla_state": "Pending", "minutes_remaining": -0.1}
            )
        )

    def test_non_pending_state_is_not_active_pending(self):
        self.assertFalse(
            app._is_active_pending(
                {"sla_state": "Missed", "minutes_remaining": 10}
            )
        )

    def test_dashboard_state_separates_pending_and_overdue(self):
        self.assertEqual(
            app._dashboard_state(
                {"sla_state": "Pending", "minutes_remaining": 0.1}
            ),
            "Pending",
        )
        self.assertEqual(
            app._dashboard_state(
                {"sla_state": "Pending", "minutes_remaining": 0}
            ),
            "Overdue",
        )
        self.assertEqual(
            app._dashboard_state(
                {"sla_state": "Pending", "minutes_remaining": -0.1}
            ),
            "Overdue",
        )

    def test_api_excludes_overdue_record_from_pending_count(self):
        records = [
            {
                "case_number": "POSITIVE",
                "sla_state": "Pending",
                "minutes_remaining": 10,
                "status": "Expiring soon",
            },
            {
                "case_number": "OVERDUE",
                "sla_state": "Pending",
                "minutes_remaining": -10,
                "status": "Breached",
            },
        ]

        with patch.object(
            app.dataverse, "refresh_timing", side_effect=lambda rows: rows
        ), patch.object(app._store, "pending_cases", return_value=records):
            response = app.app.test_client().get("/api/service-requests")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["sla_state_counts"]["Pending"], 1)
        self.assertEqual(payload["sla_state_counts"]["Overdue"], 1)
        by_case = {record["case_number"]: record for record in payload["records"]}
        self.assertTrue(by_case["POSITIVE"]["pending_active"])
        self.assertFalse(by_case["OVERDUE"]["pending_active"])
        self.assertEqual(by_case["POSITIVE"]["dashboard_state"], "Pending")
        self.assertEqual(by_case["OVERDUE"]["dashboard_state"], "Overdue")


if __name__ == "__main__":
    unittest.main()