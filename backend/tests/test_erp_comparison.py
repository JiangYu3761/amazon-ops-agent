import os
import unittest
from datetime import date, datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from app.analytics_engine import calculate_product_metrics
from app.erp_comparison import compare_reports, period_bounds


class PeriodComparisonTest(unittest.TestCase):
    def report(self, day, *, revenue=100, units=1, cost=20, ad_spend=10, ad_type="SP", grain="advertised_product",
               complete=True, has_sale=True, currency="USD", sale_currency="USD", has_ad=True, ad_days=7, sessions=5):
        product = {"id": "p", "model": "P", "currency": currency, "business_timezone": "America/Los_Angeles"}
        sale = {"product_id": "p", "sale_date": day, "sku": "a", "currency": sale_currency, "ordered_revenue": revenue,
                "units_ordered": units, "orders": 1, "sessions": sessions, "page_views": 15}
        ad = {"product_id": "p", "report_date": day, "campaign_name": "c", "entity_name": "a", "currency": currency,
              "ad_type": ad_type, "report_grain": grain, "attribution_days": ad_days,
              "impressions": 12, "clicks": 3, "spend": ad_spend, "attributed_sales": 20, "attributed_orders": 1}
        card = {"sku": "a", "effective_date": "2026-09-01", "product_cost": cost, "first_mile_cost": 0, "fba_fee": 0,
                "referral_fee": 0, "other_cost": 0, "currency": currency}
        records = {"sales": [sale] if has_sale else [], "costs": [card], "advertising": [ad] if has_ad else []}
        coverage = {dataset: {"complete_days": [day] if complete else [], "source": "api",
                              "last_synced_at": "2026-11-20T09:00:00Z"} for dataset in ("sales", "advertising")}
        coverage["advertising"]["ad_types_complete"] = [ad_type]
        return calculate_product_metrics(product, records, day, day, coverage=coverage,
                                         now=datetime(2026, 11, 24, 18, tzinfo=timezone.utc))

    def rows(self, previous=None, current=None):
        result = compare_reports(self.report("2026-10-01", **(previous or {})), self.report("2026-10-02", **(current or {})))
        self.assertEqual(result["periods"]["previous"]["start_date"], "2026-10-01")
        self.assertEqual(result["periods"]["current"]["end_date"], "2026-10-02")
        self.assertEqual(result["product"]["currency"], "USD")
        return {r["key"]: r for r in result["rows"]}

    def test_closed_adjacent_days_including_leap_day(self):
        previous, current = period_bounds(date(2024, 3, 2), 2, None)
        self.assertEqual(previous, (date(2024, 2, 27), date(2024, 2, 28)))
        self.assertEqual(current, (date(2024, 2, 29), date(2024, 3, 1)))
        for days, end in [(0, None), (367, None), (1, date(2024, 3, 2)), (2, date.min)]:
            with self.subTest(days=days, end=end), self.assertRaises(HTTPException):
                period_bounds(date(2024, 3, 2), days, end)

    def test_ready_source_and_negative_contribution(self):
        rows = self.rows({"cost": 200}, {"cost": 200, "revenue": 120, "ad_spend": 15})
        self.assertEqual(rows["ordered_revenue"]["change_percent"], 20)
        self.assertEqual(rows["sessions"]["current"]["value"], 5)
        self.assertEqual(rows["page_views"]["current"]["sources"][0]["complete_days"], 1)
        row = rows["contribution_before_ads"]
        self.assertEqual((row["previous"]["value"], row["current"]["value"], row["difference"], row["change_percent"], row["status"]),
                         (-100, -80, 20, 20, "estimated"))
        self.assertEqual(rows["spend"]["change_percent"], 50)

    def test_zero_base_retains_difference_and_missing_traffic_is_unknown(self):
        rows = self.rows({"has_sale": False, "has_ad": False})
        self.assertEqual((rows["ordered_revenue"]["difference"], rows["ordered_revenue"]["change_percent"],
                          rows["ordered_revenue"]["status"]), (100, None, "zero_base"))
        self.assertEqual(rows["spend"]["previous"]["value"], 0)
        self.assertIsNone(rows["sessions"]["previous"]["value"])
        self.assertIsNone(rows["orders"]["change_percent"])

    def test_partial_period_keeps_observations_but_withholds_comparisons(self):
        row = self.rows(current={"revenue": 12, "complete": False})["ordered_revenue"]
        self.assertEqual((row["current"]["value"], row["previous"]["value"], row["difference"], row["status"]),
                         (12, 100, None, "partial"))
        for key in ("ordered_revenue", "spend", "sessions"):
            r = self.rows(current={"has_sale": False, "has_ad": False, "complete": False})[key]
            self.assertIsNone(r["current"]["value"])
            self.assertIsNone(r["change_percent"])

    def test_wrong_currency_and_cross_product_reports(self):
        row = self.rows(current={"sale_currency": "EUR"})["ordered_revenue"]
        self.assertIsNone(row["current"]["value"])
        self.assertIsNone(row["change_percent"])
        before, after = self.report("2026-10-01"), self.report("2026-10-02")
        for field, value in (("id", "other"), ("currency", "JPY"), ("business_timezone", "UTC")):
            with self.subTest(field=field), self.assertRaises(HTTPException):
                compare_reports(before, dict(after, product=dict(after["product"], **{field: value})))

    def test_sp_grain_channel_and_window_must_match(self):
        for kwargs in ({"ad_type": "SB"}, {"grain": "campaign"}, {"ad_days": 14}):
            with self.subTest(kwargs=kwargs):
                row = self.rows(current=kwargs)["spend"]
                self.assertIsNone(row["current"]["value"])
                self.assertIsNone(row["change_percent"])


@unittest.skipUnless(os.getenv("WORKFLOW_INTEGRATION") == "1", "requires disposable migrated PostgreSQL")
class ComparisonEndpointTest(unittest.TestCase):
    def test_auth_product_timezone_and_real_analytics_loader(self):
        from unittest.mock import patch
        from urllib.parse import urlsplit
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import COOKIE, password_hash

        self.assertTrue(urlsplit(os.environ["DATABASE_URL"]).path.startswith("/homa_workflow_test_"))
        password = uuid4().hex
        email = uuid4().hex + "@comparison.example"
        client = TestClient(app)
        with connection() as conn:
            user = conn.execute("INSERT INTO app_users(email,display_name) VALUES (%s,'环比测试') RETURNING id", (email,)).fetchone()["id"]
            conn.execute("INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES (%s,%s,false)",
                         (user, password_hash(password)))
            product = conn.execute("""INSERT INTO products(model,name,marketplace,currency,business_timezone,sku)
                VALUES (%s,'ERP 经营环比测试','US','USD','America/Los_Angeles',%s) RETURNING id""",
                                   (uuid4().hex[:12], uuid4().hex[:10])).fetchone()["id"]
        path = f"/api/products/{product}/analytics/comparison"
        try:
            self.assertEqual(client.get(path).status_code, 401)
            response = client.post("/api/workflow-auth/login", json={"email": email, "password": password},
                                   headers={"X-Workflow-Request": "1"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(COOKIE, client.cookies)
            # 01:00 UTC on November 1 is still October 31 in this product's business timezone.
            with patch("app.data_automation.now_utc", return_value=datetime(2026, 11, 1, 1, tzinfo=timezone.utc)):
                r = client.get(path, params={"days": 7})
                self.assertEqual(r.status_code, 200, r.text)
                data = r.json()
                self.assertEqual(data["periods"]["current"]["end_date"], "2026-10-30")
                self.assertEqual(data["periods"]["previous"]["end_date"], "2026-10-23")
                self.assertTrue(all(row["current"]["value"] is None for row in data["rows"]))
                self.assertEqual(client.get(path, params={"end_date": "2026-10-31"}).status_code, 422)
                self.assertEqual(client.get(path.replace(str(product), str(uuid4()))).status_code, 404)
        finally:
            client.close()
            with connection() as conn:
                conn.execute("DELETE FROM products WHERE id=%s", (product,))
                conn.execute("DELETE FROM app_users WHERE id=%s", (user,))
