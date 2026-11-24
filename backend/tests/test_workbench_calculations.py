"""Decision aids use form evidence and leave underlying operational records untouched."""
from datetime import date
import unittest

from app.workbench_calculations import calculate_section


def results(page, section, values):
    findings = calculate_section(page, section, values, date(2026, 11, 24))
    return {item["label"]: item for item in findings}


class FormCalculationsTests(unittest.TestCase):
    def stock(self, **edits):
        values = dict(scenario_basis="完整销售日及新鲜快照已核实", sold_qty=140, closed_days=14,
                      available_qty=90, eligible_inbound=0, lead_days=14, safety_days=3,
                      review_days=7, min_order=96, pack_size=24)
        return results("inventory", "replenish", {**values, **edits})

    def test_stock_shortage_and_confirmed_eta(self):
        findings = self.stock()
        self.assertEqual(findings["现货覆盖天数"]["value"], "9")
        self.assertEqual(findings["手工情景补货触发线"]["value"], "170")
        self.assertEqual(findings["手工情景补货量"]["value"], "168")
        self.assertEqual(self.stock(eligible_inbound=48)["手工情景补货量"]["value"], "120")
        self.assertEqual(self.stock(available_qty=240)["手工情景补货量"]["value"], "0")

    def test_stock_unknown_zero_and_unverified(self):
        for edits in [{"closed_days": 0}, {"pack_size": 0}, {"sold_qty": 1.5},
                      {"scenario_basis": "依据不完整"}, {"eligible_inbound": ""}]:
            self.assertEqual(self.stock(**edits)["补货情景"]["level"], "missing")
        zero = self.stock(sold_qty=0)
        self.assertEqual(zero["现货覆盖天数"]["level"], "missing")
        self.assertEqual(zero["手工情景补货量"]["value"], "0")
        self.assertEqual(results("inventory", "replenish", {"requested_qty": 80}), {})

    def test_receiving_short_and_over_delivery(self):
        section = lambda n: results("inventory", "receiving", {"shipped_qty": 120, "received_qty": n})
        self.assertEqual(section(108)["货件短收率"]["value"], "10")
        self.assertEqual(section(121)["货件短收率"]["value"], "0")
        self.assertEqual(section(121)["货件收发核对"]["value"], "1")

    def test_receiving_unknown_base(self):
        self.assertEqual(results("inventory", "receiving", {"shipped_qty": 0, "received_qty": 0})["货件短收率"]["level"], "missing")
        self.assertEqual(results("inventory", "receiving", {"shipped_qty": 120})["货件短收率"]["level"], "missing")

    def test_cash_forecast(self):
        v = {"opening_cash": -20, "incoming_cash": 120, "outgoing_cash": 150, "currency": "USD"}
        item = results("finance", "budget_cash", v)["期末预测现金"]
        self.assertEqual((item["value"], item["level"]), ("-50", "warning"))
        self.assertIn("USD", item["detail"])

    def test_missing_cash_and_currency(self):
        for v in [{"opening_cash": 200, "currency": "USD"},
                  {"opening_cash": 0, "incoming_cash": 0, "outgoing_cash": 0, "currency": "USD/GBP"}]:
            self.assertEqual(results("finance", "budget_cash", v)["期末预测现金"]["level"], "missing")

    def finances(self, **edits):
        v = dict(ordered_revenue=100, matched_cost=56, sold_units=5, matched_units=5, currency="USD",
                 ad_spend=14, expense_scope="同期 SP")
        return results("finance", "contribution", {**v, **edits})

    def test_contribution_and_break_even(self):
        got = self.finances()
        self.assertEqual(got["广告前已知成本贡献"]["value"], "44")
        self.assertEqual(got["已知成本盈亏平衡 ACOS"]["value"], "44")
        self.assertEqual(got["扣已核广告花费贡献"]["value"], "30")
        self.assertEqual(self.finances(ad_spend=0)["扣已核广告花费贡献"]["value"], "44")

    def test_contribution_incomplete_and_zero_revenue(self):
        self.assertEqual(self.finances(matched_units=4)["同期间已知成本贡献"]["level"], "missing")
        self.assertEqual(self.finances(ad_spend=None)["扣已核广告花费贡献"]["level"], "missing")
        self.assertEqual(self.finances(ordered_revenue=0)["已知成本盈亏平衡 ACOS"]["level"], "missing")

    def returns(self, **edits):
        v = dict(cohort_match="已核实同批次", cohort_orders=100, refund_orders=1, cohort_units=20, returned_units=2)
        return results("service", "returns", {**v, **edits})

    def test_cohort_deduped_refunds_and_returned_units(self):
        got = self.returns()
        self.assertEqual(got["批次退款订单率"]["value"], "1")
        self.assertEqual(got["批次退货件数率"]["value"], "10")
        self.assertEqual(self.returns(refund_orders=0)["批次退款订单率"]["value"], "0")

    def test_cohort_out_of_scope_and_unusable_counts(self):
        self.assertEqual(self.returns(cohort_match="范围未对齐")["售后批次比率"]["level"], "missing")
        self.assertEqual(self.returns(cohort_orders=0)["批次退款订单率"]["level"], "missing")
        self.assertEqual(self.returns(returned_units=21)["批次退货件数率"]["level"], "missing")

    def test_classified_single_reason_share(self):
        v = dict(reason_basis="同批去重且单一主因", reason_count=7, classified_count=20)
        self.assertEqual(results("service", "voc", v)["售后主因占比"]["value"], "35")

    def test_overlapping_or_unknown_reason_counts(self):
        for mode, n in [("含重复或未核分类", 8), ("同批去重且单一主因", 0)]:
            v = dict(reason_basis=mode, reason_count=7, classified_count=n)
            self.assertEqual(results("service", "voc", v)["售后主因占比"]["level"], "missing")

    def rankings(self, **edits):
        v = dict(before_series="US-自然-关键词-A", after_series="US-自然-关键词-A",
                 pair_state="连续有效观测", old_rank=42, new_rank=36)
        return results("ranking", "rank_review", {**v, **edits})["可比排名变化"]

    def test_rank_comparison(self):
        self.assertEqual(self.rankings()["value"], "6")
        self.assertEqual(self.rankings(new_rank=45)["value"], "-3")

    def test_rank_gap_and_changed_series(self):
        for edits in [{"pair_state":"中间有缺测"}, {"after_series":"US-广告-关键词-A"}, {"new_rank":0}]:
            self.assertEqual(self.rankings(**edits)["level"], "missing")

    def ads(self, **edits):
        v = dict(maturity="已成熟并刷新", spend=30, attributed_sales=100, target_acos=20)
        return results("ads", "investigation", {**v, **edits})

    def test_mature_ad_target(self):
        got = self.ads()["成熟 ACOS 与目标差额"]
        self.assertEqual((got["value"], got["level"]), ("10", "warning"))
        self.assertEqual(self.ads(spend=0)["成熟 ACOS 与目标差额"]["value"], "-20")

    def test_immature_or_missing_ad_denominator(self):
        for edits in [{"maturity":"窗口内"}, {"target_acos":None}]:
            self.assertEqual(self.ads(**edits)["广告目标调查"]["level"], "missing")
        got = self.ads(attributed_sales=0)["广告目标调查"]
        self.assertEqual(got["level"], "warning")
        self.assertNotIn("value", got)

    def campaign(self, **edits):
        v = dict(forecast_units=100, available_units=80, promo_price=15, unit_cost_total=12, floor_ref="同期含税口径核实")
        return results("calendar", "readiness", {**v, **edits})

    def test_campaign_inventory_and_margin(self):
        self.assertEqual(self.campaign()["现货可承接预测贡献"]["value"], "240")
        self.assertEqual(self.campaign(promo_price=10)["现货可承接预测贡献"]["value"], "-160")
        self.assertEqual(self.campaign(available_units=0)["现货可承接预测贡献"]["value"], "0")

    def test_campaign_missing_basis_or_units(self):
        self.assertEqual(self.campaign(floor_ref="")["活动可承接贡献"]["level"], "missing")
        self.assertEqual(self.campaign(unit_cost_total="")["活动可承接贡献"]["level"], "missing")


    def test_stacked_price_against_approved_floor(self):
        data = {"base_price": 100, "effective_price": 70, "cost_floor": 80}
        got = results("calendar", "pricing_track", data)
        self.assertEqual(got["距已批底价余量"]["value"], "-10")
        self.assertEqual(got["距已批底价余量"]["level"], "warning")
        self.assertEqual(got["参考售价下可折让空间"]["value"], "20")

    def test_price_floor_missing_and_zero_reference(self):
        got = results("calendar", "pricing_track", {"base_price": 0, "effective_price": 0, "cost_floor": 0})
        self.assertEqual(got["参考售价下可折让空间"]["level"], "missing")
        self.assertEqual(results("calendar", "pricing_track", {})["叠加价格底线"]["level"], "missing")


if __name__ == "__main__":
    unittest.main()
