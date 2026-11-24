from unittest import TestCase

from app.importer import parse_csv


HEAD = "date,campaign_name,impressions,clicks,spend,sales,orders,currency,report_grain,profile_id,ad_type,campaign_id,ad_group_id,keyword_id,match_type,search_term,attribution_days"
BASE = dict(zip(HEAD.split(","), "2026-01-02,c,10,2,5,10,1,USD,search_term,US1,SP,C,G,K,EXACT,cold brew,7".split(",")))


def report(*changes):
    lines = [",".join({**BASE, **values}[k] for k in HEAD.split(",")) for values in changes]
    return (HEAD + "\n" + "\n".join(lines)).encode()


class SearchTermImportTest(TestCase):
    def test_stable_source_key_and_distinct_dimensions(self):
        first = parse_csv("advertising", report({}))[0][0]
        corrected = parse_csv("advertising", report(dict(spend="99", campaign_name="renamed")))[0][0]
        self.assertEqual(first["business_key"], corrected["business_key"])
        for field, value in dict(search_term="Cold Brew",match_type="PHRASE",keyword_id="new",campaign_id="other",
                                 ad_group_id="other",profile_id="different",ad_type="SB",currency="CAD",attribution_days="14").items():
            self.assertNotEqual(first["business_key"],parse_csv("advertising",report({field:value}))[0][0]["business_key"])
        self.assertEqual(first["source_line"],2)

    def test_missing_dimensions_unsupported_types_and_duplicate_rows_fail_whole_file(self):
        for field in ("search_term","currency","attribution_days","keyword_id","profile_id","campaign_id","ad_group_id","report_grain"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,"第 3 行"):
                parse_csv("advertising",report({}, {field:""}))
        for edits in ({"ad_type":"SD"},{"attribution_days":"0"},{"match_type":"AUTO"},{}):
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                parse_csv("advertising",report({}, edits))

    def test_legacy_campaign_entity_is_not_a_search_term(self):
        legacy = b"date,campaign_name,entity_name,impressions,clicks,spend,sales,orders,currency\n2026-01-02,c,cold brew,10,2,5,10,1,USD"
        row = parse_csv("advertising",legacy)[0][0]
        self.assertNotIn("search_term",row)
