import io
import json
import os
import tempfile
import unittest

import app as dashboard


TEST_CONFIG = {
    "ibkr": {"token": "test", "query_id": "test"},
    "base_currency": "EUR",
    "monthly_contribution": 1000,
    "rebalance_band_pct": 0.05,
    "order_ticket": {
        "max_positions": 2,
        "commission": {},
        "instruments": {},
    },
    "targets": [
        {
            "isin": "IE00BD4TXV59",
            "sleeve": "Core",
            "name": "Core",
            "target_pct": 0.60,
        },
        {
            "isin": "IE00BL25JP72",
            "sleeve": "Momentum",
            "name": "Momentum",
            "target_pct": 0.25,
        },
        {
            "isin": "IE00BL25JL35",
            "sleeve": "Quality",
            "name": "Quality",
            "target_pct": 0.15,
        },
    ],
}


CSV_REPORT = b'''Statement,Data,Period,"August 1, 2026 - August 12, 2026"
Open Positions,Header,ISIN,Quantity,Mark Price,Position Value,Currency,FX Rate To Base,Symbol
Open Positions,Data,IE00BD4TXV59,2,101,202,EUR,1,UETW
Open Positions,Data,US0000000001,3,10,30,USD,0.9,USD
'''


class AllocationTests(unittest.TestCase):
    def test_selects_only_requested_most_underweight_sleeves(self):
        positions = {
            "IE00BD4TXV59": {
                "value_base": 9000,
                "qty": 90,
                "price_base": 100,
                "currency": "EUR",
            }
        }
        view = dashboard.build_view(TEST_CONFIG, positions, 1000)
        self.assertEqual(len(view["buy_summary"]), 2)
        self.assertNotIn(
            "IE00BD4TXV59", {row["isin"] for row in view["buy_summary"]}
        )
        self.assertEqual(sum(row["buy_eur"] for row in view["buy_summary"]), 1000)
        self.assertIsNone(view["leftover_cash"])
        self.assertIn("live ask", view["pricing_error"])

    def test_skip_replaces_sleeve_without_a_sell(self):
        positions = {
            "IE00BD4TXV59": {
                "value_base": 9000,
                "qty": 90,
                "price_base": 100,
                "currency": "EUR",
            }
        }
        view = dashboard.build_view(
            TEST_CONFIG, positions, 1000, 2, {"IE00BL25JP72"}
        )
        self.assertNotIn(
            "IE00BL25JP72", {row["isin"] for row in view["buy_summary"]}
        )
        self.assertTrue(all(row["buy_eur"] >= 0 for row in view["sleeves"]))


class ReportTests(unittest.TestCase):
    def test_csv_uses_report_date_and_fx(self):
        parsed = dashboard.parse_uploaded_csv(CSV_REPORT.decode())
        self.assertEqual(parsed["report_date"], "2026-08-12")
        self.assertEqual(parsed["rows_parsed"], 2)
        self.assertEqual(parsed["positions"]["US0000000001"]["value_base"], 27)

    def test_missing_non_eur_fx_is_explicit(self):
        report = """Report Date,ISIN,Quantity,Mark Price,Currency,FX Rate To Base
2026-08-12,US0000000001,3,10,USD,
"""
        with self.assertRaisesRegex(ValueError, "FX rate"):
            dashboard.parse_uploaded_csv(report)

    def test_preview_does_not_write_and_failed_commit_keeps_cache(self):
        original_config = dashboard.load_config
        original_cache = dashboard.CACHE_PATH
        try:
            dashboard.load_config = lambda: TEST_CONFIG
            with tempfile.TemporaryDirectory() as temp_dir:
                dashboard.CACHE_PATH = os.path.join(temp_dir, "cache.json")
                client = dashboard.app.test_client()
                preview = client.post(
                    "/api/import",
                    data={"file": (io.BytesIO(CSV_REPORT), "report.csv")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(preview.status_code, 200)
                self.assertFalse(os.path.exists(dashboard.CACHE_PATH))

                committed = client.post(
                    "/api/import",
                    data={
                        "confirm": "1",
                        "file": (io.BytesIO(CSV_REPORT), "report.csv"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(committed.status_code, 200)
                with open(dashboard.CACHE_PATH, "rb") as cache_file:
                    before = cache_file.read()
                self.assertEqual(json.loads(before)["as_of"], "2026-08-12")

                bad = b"Report Date,ISIN,Quantity,Mark Price,Currency\n2026-08-12,US0000000001,2,10,USD\n"
                failed = client.post(
                    "/api/import",
                    data={
                        "confirm": "1",
                        "file": (io.BytesIO(bad), "bad.csv"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(failed.status_code, 400)
                with open(dashboard.CACHE_PATH, "rb") as cache_file:
                    self.assertEqual(cache_file.read(), before)
        finally:
            dashboard.load_config = original_config
            dashboard.CACHE_PATH = original_cache


if __name__ == "__main__":
    unittest.main()
