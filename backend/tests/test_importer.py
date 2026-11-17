import unittest

from app.importer import parse_csv


class ImporterTests(unittest.TestCase):
    def test_parses_sales_template(self):
        rows, errors = parse_csv(
            "sales",
            b"date,sku,asin,units_ordered,ordered_revenue,currency\n2026-11-14,G11-US,B0GXZ848Q1,3,299.97,USD\n",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["units_ordered"], 3)
        self.assertEqual(errors, [])

    def test_rejects_missing_required_column(self):
        with self.assertRaisesRegex(ValueError, "missing columns"):
            parse_csv("inventory", b"date,sku\n2026-11-14,G11-US\n")

    def test_keeps_valid_rows_and_reports_bad_rows(self):
        rows, errors = parse_csv(
            "advertising",
            b"date,campaign_name,impressions,clicks,spend,sales,orders\n2026-11-14,C1,10,2,3.00,8.00,1\n2026-11-14,C2,bad,2,3,8,1\n",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors[0]["line"], 3)


if __name__ == "__main__":
    unittest.main()

class StrictImportTests(unittest.TestCase):
    def test_blank_is_not_zero(self):
        rows,errors=parse_csv('sales',b'date,sku,units_ordered,ordered_revenue\n2026-11-14,G11,,100\n')
        self.assertEqual(rows,[])
        self.assertIn('blank',errors[0]['error'])

    def test_non_finite_cost_is_rejected(self):
        rows,errors=parse_csv('costs',b'date,sku,product_cost,first_mile_cost,fba_fee,referral_fee,other_cost\n2026-11-14,G11,NaN,1,2,3,4\n')
        self.assertEqual(rows,[])
        self.assertIn('finite',errors[0]['error'])

    def test_duplicate_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'duplicate'):
            parse_csv('sales',b'date,sku,units_ordered,ordered_revenue,sku\n')
