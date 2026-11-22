import csv
import io
from decimal import Decimal
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


class NumericAndCurrencyContractTests(unittest.TestCase):
    @staticmethod
    def sales_row(amount='12.50', units='1', currency=None):
        row={'date':'2026-11-20','sku':'G11-CA','units_ordered':units,'ordered_revenue':amount}
        if currency is not None:
            row['currency']=currency
        output=io.StringIO()
        writer=csv.DictWriter(output,fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        return output.getvalue().encode()

    @staticmethod
    def cost_row(amount):
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['date','sku','product_cost','first_mile_cost','fba_fee','referral_fee','other_cost'])
        writer.writerow(['2026-11-20','G11-CA',amount,'0','0','0','0'])
        return output.getvalue().encode()

    def test_standard_thousands_and_prefix_currency_symbol_are_exact(self):
        rows,errors=parse_csv('sales',self.sales_row('$1,234.50','001,234'),default_currency='cad')
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['ordered_revenue'],Decimal('1234.50'))
        self.assertEqual(rows[0]['units_ordered'],1234)
        self.assertEqual(rows[0]['currency'],'CAD')

    def test_malformed_money_cannot_be_reinterpreted_as_a_different_amount(self):
        for amount in ['1$2','1,2','12,34.56','1,234,56','1 234.00','$ 12.00','1e3','+12','.5','12.','1_000','-1','$-1']:
            with self.subTest(amount=amount):
                rows,errors=parse_csv('sales',self.sales_row(amount))
                self.assertEqual(rows,[])
                self.assertEqual(len(errors),1)
                self.assertIn('ordered_revenue',errors[0]['error'])

    def test_integer_lexical_and_database_limits(self):
        rows,errors=parse_csv('sales',self.sales_row(units='2,147,483,647'))
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['units_ordered'],2147483647)
        for units in ['2147483648','999999999999999999999','1,2','1.0','1e3','+1','1_000','-1']:
            with self.subTest(units=units):
                rows,errors=parse_csv('sales',self.sales_row(units=units))
                self.assertEqual(rows,[])
                self.assertIn('units_ordered',errors[0]['error'])

    def test_money_precision_matches_numeric_14_2_without_silent_rounding(self):
        rows,errors=parse_csv('sales',self.sales_row('999999999999.99'))
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['ordered_revenue'],Decimal('999999999999.99'))
        for amount in ['1000000000000','999999999999.999','1.001','1.000']:
            with self.subTest(amount=amount):
                rows,errors=parse_csv('sales',self.sales_row(amount))
                self.assertEqual(rows,[])
                self.assertIn('ordered_revenue',errors[0]['error'])

    def test_cost_precision_matches_numeric_14_4(self):
        rows,errors=parse_csv('costs',self.cost_row('9,999,999,999.9999'))
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['product_cost'],Decimal('9999999999.9999'))
        for amount in ['10000000000','0.00001','1.00000']:
            with self.subTest(amount=amount):
                rows,errors=parse_csv('costs',self.cost_row(amount))
                self.assertEqual(rows,[])
                self.assertIn('product_cost',errors[0]['error'])

    def test_non_finite_and_blank_amounts_never_become_zero(self):
        for amount in ['NaN','sNaN','Infinity','-Infinity','inf','$NaN','']:
            with self.subTest(amount=amount):
                rows,errors=parse_csv('sales',self.sales_row(amount))
                self.assertEqual(rows,[])
                self.assertTrue(errors)
        rows,errors=parse_csv('sales',self.sales_row('0.00','0'))
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['ordered_revenue'],Decimal('0.00'))
        self.assertEqual(rows[0]['units_ordered'],0)

    def test_missing_or_blank_currency_uses_product_default(self):
        for currency in [None,'','   ']:
            with self.subTest(currency=currency):
                rows,errors=parse_csv('sales',self.sales_row(currency=currency),default_currency='CAD')
                self.assertEqual(errors,[])
                self.assertEqual(rows[0]['currency'],'CAD')

    def test_explicit_currency_is_preserved_and_normalized(self):
        rows,errors=parse_csv('sales',self.sales_row(currency=' eur '),default_currency='CAD')
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['currency'],'EUR')
        # The product/row identity check is the caller's responsibility.
        rows,errors=parse_csv('sales',self.sales_row())
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['currency'],'USD')

    def test_invalid_currency_is_reported_without_importing_row(self):
        for currency in ['US','USDD','US$','123']:
            with self.subTest(currency=currency):
                rows,errors=parse_csv('sales',self.sales_row(currency=currency),default_currency='CAD')
                self.assertEqual(rows,[])
                self.assertIn('currency',errors[0]['error'])
        with self.assertRaisesRegex(ValueError,'default_currency'):
            parse_csv('sales',self.sales_row(),default_currency='')

    def test_advertising_amounts_use_money_scale_and_orders_use_integer_limits(self):
        raw=b'date,campaign_name,impressions,clicks,spend,sales,orders\n2026-11-20,C1,10,1,1.001,10,1\n'
        rows,errors=parse_csv('advertising',raw,default_currency='CAD')
        self.assertEqual(rows,[])
        self.assertIn('spend',errors[0]['error'])
        raw=b'date,campaign_name,impressions,clicks,spend,sales,orders\n2026-11-20,C1,10,1,1,10,2147483648\n'
        rows,errors=parse_csv('advertising',raw)
        self.assertEqual(rows,[])
        self.assertIn('orders',errors[0]['error'])
