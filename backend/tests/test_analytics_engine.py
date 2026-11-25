"""Formula, temporal, coverage and source-integrity boundaries for the pure engine."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import math
import unittest
from uuid import uuid4

from app.analytics_engine import calculate_product_metrics


class ProductMetricsTests(unittest.TestCase):
    product={'id':'p','model':'G11','currency':'USD','business_timezone':'America/Los_Angeles'}
    now=datetime(2026,11,22,20,tzinfo=timezone.utc)

    def run_engine(self,records=None,days=('2026-11-01','2026-11-02'),**kwargs):
        return calculate_product_metrics(deepcopy(self.product),deepcopy(records or {}),days[0],days[1],now=kwargs.pop('now',self.now),**kwargs)

    def metric(self,result,module,key):
        return next(m for m in result['modules'][module]['metrics'] if m['key']==key)

    def value(self,result,module,key):return self.metric(result,module,key)['value']

    def coverage(self,days=('2026-11-01','2026-11-02')):
        return {k:{'complete_days':list(days),'last_synced_at':'2026-11-22T20:00:00Z','source':'test-canonical-source'} for k in ['sales','advertising']}

    def sale(self,day='2026-11-01',sku='A',units=1,revenue='10',**extra):
        return {'product_id':'p','sale_date':day,'sku':sku,'units_ordered':units,'ordered_revenue':revenue,'currency':'USD',**extra}

    def ad(self,day='2026-11-01',**extra):
        return {'product_id':'p','report_date':day,'campaign_name':'SP-test','entity_name':'','ad_type':'SP','report_grain':'campaign','attribution_days':7,
                'impressions':100,'clicks':10,'spend':'10','attributed_sales':'50','attributed_orders':2,'currency':'USD',**extra}

    def stock(self,day='2026-11-02',sku='A',fulfillable=5,**extra):
        return {'product_id':'p','snapshot_date':day,'sku':sku,'fulfillable':fulfillable,'reserved':0,'inbound':0,'unsellable':0,**extra}

    def cost(self,day='2026-10-01',sku='A',product_cost='2',**extra):
        return {'product_id':'p','effective_date':day,'sku':sku,'product_cost':product_cost,'first_mile_cost':'0','fba_fee':'0','referral_fee':'0','other_cost':'0','currency':'USD',**extra}

    def policy(self,**extra):return {'lead_time_days':1,'safety_days':1,'review_days':1,'moq':1,'pack_size':1,**extra}

    def test_all_nine_modules_have_explicit_metric_boundaries(self):
        r=self.run_engine()
        self.assertEqual(set(r['modules']),{'shop','compliance','listing','ads','inventory','calendar','finance','service','review'})
        for module in r['modules'].values():
            for m in module['metrics']:
                self.assertTrue({'key','label','value','unit','status','formula','reason','missing_inputs','inputs','source_datasets'}<=m.keys())
        self.assertEqual(r['data_quality']['status'],'missing')

    def test_ratios_are_ratios_of_sums_not_means_of_daily_ratios(self):
        r=self.run_engine({'sales':[self.sale(units=1,revenue='100',orders=1,sessions=10),self.sale('2026-11-02',units=9,revenue='300',orders=3,sessions=90)],
                           'advertising':[self.ad(clicks=1,impressions=10,spend='1',attributed_sales='2'),self.ad('2026-11-02',clicks=9,impressions=90,spend='9',attributed_sales='18')]},coverage=self.coverage())
        self.assertEqual(self.value(r,'shop','average_selling_price'),40)
        self.assertEqual(self.value(r,'listing','order_conversion_rate'),4)
        self.assertEqual(self.value(r,'ads','acos'),50)
        self.assertEqual(self.value(r,'ads','ctr'),10)
        self.assertEqual(self.value(r,'ads','roas'),2)

    def test_decimal_amounts_do_not_accumulate_binary_float_error(self):
        r=self.run_engine({'sales':[self.sale(revenue=Decimal('.1')),self.sale('2026-11-02',revenue=Decimal('.2'))]})
        self.assertEqual(self.value(r,'shop','ordered_revenue'),.3)
        r=self.run_engine({'sales':[self.sale(units=3,revenue='1')],'costs':[self.cost(product_cost='.1',first_mile_cost='.2')]},coverage=self.coverage())
        self.assertEqual(self.value(r,'finance','known_unit_cost_total'),.9)
        self.assertEqual(self.value(r,'finance','contribution_before_ads'),.1)

    def test_zero_denominators_are_null_and_explicitly_undefined(self):
        r=self.run_engine({'sales':[self.sale(units=0,revenue='0',orders=0,sessions=0)],'advertising':[self.ad(impressions=0,clicks=0,spend='10',attributed_sales='0')]},coverage=self.coverage())
        for module,key in [('shop','average_selling_price'),('listing','order_conversion_rate'),('ads','acos'),('ads','ctr'),('ads','cpc')]:
            self.assertIsNone(self.value(r,module,key));self.assertEqual(self.metric(r,module,key)['status'],'undefined')
        self.assertEqual(self.value(r,'ads','roas'),0)

    def test_currency_and_product_are_isolated(self):
        r=self.run_engine({'sales':[self.sale(revenue='10'),self.sale('2026-11-02',revenue='1000',currency='EUR'),self.sale('2026-11-02',sku='B',revenue='9999',product_id='other')]})
        self.assertEqual(self.value(r,'shop','ordered_revenue'),10)
        self.assertEqual(self.value(r,'shop','units_ordered'),1)
        self.assertEqual({i['code'] for i in r['data_quality']['issues']} & {'currency_mismatch','foreign_product'},{'currency_mismatch','foreign_product'})

    def test_missing_currency_does_not_assume_a_money_unit(self):
        row=self.sale();row.pop('currency')
        self.assertIsNone(self.value(self.run_engine({'sales':[row]}),'shop','ordered_revenue'))

    def test_product_scoped_rows_may_omit_product_id(self):
        row=self.sale();row.pop('product_id')
        self.assertEqual(self.value(self.run_engine({'sales':[row]}),'shop','ordered_revenue'),10)

    def test_optional_orders_sessions_are_not_inferred_from_units(self):
        r=self.run_engine({'sales':[self.sale(units=20)]})
        self.assertIsNone(self.value(r,'shop','orders'));self.assertIsNone(self.value(r,'listing','sessions'))
        self.assertIsNone(self.value(r,'listing','order_conversion_rate'))

    def test_absent_days_are_not_zero_filled_without_explicit_coverage(self):
        rows={'sales':[self.sale(units=4)],'inventory':[self.stock()]}
        r=self.run_engine(rows,policies=self.policy())
        self.assertIsNone(self.value(r,'inventory','daily_sales_rate'));self.assertIsNone(self.value(r,'inventory','recommended_replenishment'))
        self.assertEqual(self.metric(r,'shop','ordered_revenue')['status'],'partial')
        known=self.run_engine(rows,policies=self.policy(),coverage=self.coverage())
        self.assertEqual(self.value(known,'inventory','daily_sales_rate'),2)

    def test_explicit_complete_empty_sales_days_can_be_zero(self):
        r=self.run_engine({'inventory':[self.stock()]},coverage=self.coverage(),policies=self.policy())
        self.assertEqual(self.value(r,'shop','ordered_revenue'),0)
        self.assertEqual(self.value(r,'inventory','daily_sales_rate'),0)
        self.assertEqual(self.metric(r,'inventory','days_of_supply')['status'],'undefined')
        self.assertEqual(self.value(r,'inventory','recommended_replenishment'),0)

    def test_complete_days_exclude_today_but_allow_closed_day_replenishment(self):
        days=('2026-11-20','2026-11-22');cov=self.coverage(('2026-11-20','2026-11-21'))
        r=self.run_engine({'sales':[self.sale('2026-11-20',units=4),self.sale('2026-11-21',units=6),self.sale('2026-11-22',units=900)],'inventory':[self.stock('2026-11-22',fulfillable=10)]},days,coverage=cov,policies=self.policy())
        self.assertEqual(self.value(r,'inventory','daily_sales_rate'),5)
        self.assertEqual(self.value(r,'inventory','recommended_replenishment'),5)
        self.assertEqual(self.metric(r,'inventory','daily_sales_rate')['inputs']['closed_day_count'],2)
        self.assertEqual(self.metric(r,'shop','ordered_revenue')['status'],'partial')

    def test_complete_day_missing_in_the_middle_prevents_velocity(self):
        r=self.run_engine({'sales':[self.sale(units=4)],'inventory':[self.stock()]},coverage=self.coverage(('2026-11-01',)),policies=self.policy())
        self.assertIsNone(self.value(r,'inventory','daily_sales_rate'))

    def test_effective_cost_is_matched_on_each_sales_day(self):
        r=self.run_engine({'sales':[self.sale(units=2,revenue='20'),self.sale('2026-11-03',units=1,revenue='10')],
                           'costs':[self.cost(product_cost='2',fba_fee='1'),self.cost('2026-11-02',product_cost='4',fba_fee='2')]},('2026-11-01','2026-11-03'),coverage=self.coverage(('2026-11-01','2026-11-02','2026-11-03')))
        self.assertEqual(self.value(r,'finance','known_unit_cost_total'),12)
        self.assertEqual(self.value(r,'finance','contribution_before_ads'),18)
        self.assertEqual([x['effective_date'] for x in self.metric(r,'finance','known_unit_cost_total')['inputs']['lines']],['2026-10-01','2026-11-02'])

    def test_future_cost_does_not_reprice_earlier_sales(self):
        r=self.run_engine({'sales':[self.sale(units=2)],'costs':[self.cost(product_cost='3'),self.cost('2026-11-02',product_cost='999')]})
        self.assertEqual(self.value(r,'finance','known_unit_cost_total'),6)

    def test_missing_cost_for_one_sku_prevents_full_contribution(self):
        r=self.run_engine({'sales':[self.sale(sku='A',units=1),self.sale(sku='B',units=1)],'costs':[self.cost(sku='A')]},coverage=self.coverage())
        self.assertEqual(self.value(r,'finance','known_unit_cost_total'),2)
        self.assertEqual(self.value(r,'finance','cost_coverage_percent'),50)
        self.assertIsNone(self.value(r,'finance','contribution_before_ads'))

    def test_incomplete_latest_cost_does_not_silently_use_an_old_version(self):
        broken=self.cost('2026-11-01',product_cost='9');broken.pop('fba_fee')
        r=self.run_engine({'sales':[self.sale('2026-11-02')],'costs':[self.cost(product_cost='1'),broken]},coverage=self.coverage())
        self.assertIsNone(self.value(r,'finance','contribution_before_ads'))
        self.assertEqual(self.value(r,'finance','known_unit_cost_total'),0)
        self.assertEqual(self.metric(r,'finance','known_unit_cost_total')['inputs']['uncovered_units'],1)

    def test_advertising_sales_are_not_added_to_total_revenue(self):
        r=self.run_engine({'sales':[self.sale(revenue='100')],'costs':[self.cost(product_cost='20')],'advertising':[self.ad(attributed_sales='60',spend='10')]},coverage=self.coverage())
        self.assertEqual(self.value(r,'finance','ordered_revenue'),100)
        self.assertEqual(self.value(r,'finance','contribution_after_ads'),70)
        self.assertIsNone(self.value(r,'finance','net_profit'))
        self.assertIsNone(self.value(r,'finance','settled_revenue'))

    def test_latest_inventory_is_chosen_independently_for_each_sku(self):
        r=self.run_engine({'inventory':[self.stock('2026-11-01','A',10),self.stock('2026-11-03','A',8),self.stock('2026-11-02','B',20),self.stock('2026-11-04','A',999)]},('2026-11-01','2026-11-03'))
        self.assertEqual(self.value(r,'inventory','fulfillable'),28)
        self.assertEqual(self.metric(r,'inventory','fulfillable')['inputs']['snapshot_dates'],{'A':'2026-11-03','B':'2026-11-02'})

    def test_reserved_is_not_deducted_a_second_time(self):
        r=self.run_engine({'sales':[self.sale(units=2),self.sale('2026-11-02',units=2)],'inventory':[self.stock(fulfillable=10,reserved=7,inbound=100)]},coverage=self.coverage(),policies=self.policy())
        self.assertEqual(self.value(r,'inventory','fulfillable'),10)
        self.assertEqual(self.value(r,'inventory','days_of_supply'),5)
        self.assertEqual(self.value(r,'inventory','inventory_position'),10)

    def test_replenishment_rounds_moq_and_pack_size_upward(self):
        r=self.run_engine({'sales':[self.sale(units=2),self.sale('2026-11-02',units=2)],'inventory':[self.stock(fulfillable=1,inbound=1000)]},coverage=self.coverage(),policies=self.policy(moq=7,pack_size=4))
        self.assertEqual(self.value(r,'inventory','reorder_point'),4)
        self.assertEqual(self.value(r,'inventory','recommended_replenishment'),8)
        self.assertEqual(self.value(r,'inventory','inventory_position'),1)

    def test_eta_inbound_only_offsets_a_future_planning_position(self):
        r=self.run_engine({'sales':[self.sale(units=2),self.sale('2026-11-02',units=2)],'inventory':[self.stock(fulfillable=1,inbound=10,inbound_eta='2026-11-03')]},coverage=self.coverage(),policies=self.policy())
        self.assertEqual(self.value(r,'inventory','fulfillable'),1)
        self.assertEqual(self.value(r,'inventory','inventory_position'),11)
        self.assertEqual(self.value(r,'inventory','recommended_replenishment'),0)

    def test_one_sku_surplus_does_not_cancel_another_sku_shortage(self):
        r=self.run_engine({'sales':[self.sale(sku='A',units=10),self.sale(sku='B',units=2)],'inventory':[self.stock(sku='A',fulfillable=0),self.stock(sku='B',fulfillable=100)]},coverage=self.coverage(),policies=self.policy(lead_time_days=1,safety_days=0,review_days=1))
        self.assertEqual(self.value(r,'inventory','recommended_replenishment'),10)

    def test_missing_inventory_sku_or_stale_snapshot_blocks_replenishment(self):
        for stocks in [[self.stock(sku='A')],[self.stock('2026-10-01','A'),self.stock('2026-10-01','B')]]:
            r=self.run_engine({'sales':[self.sale(sku='A'),self.sale(sku='B')],'inventory':stocks},coverage=self.coverage(),policies=self.policy())
            self.assertIsNone(self.value(r,'inventory','recommended_replenishment'))
            self.assertEqual(self.metric(r,'inventory','fulfillable')['status'],'partial')

    def test_lead_time_and_safety_days_are_not_invented(self):
        r=self.run_engine({'sales':[self.sale()],'inventory':[self.stock()]},coverage=self.coverage())
        m=self.metric(r,'inventory','recommended_replenishment')
        self.assertIsNone(m['value']);self.assertTrue({'lead_time_days','safety_days'}<=set(m['missing_inputs']))

    def test_unknown_attribution_window_remains_unknown_even_with_mature_flag(self):
        r=self.run_engine({'advertising':[self.ad(attribution_days=None,mature=True)]},coverage=self.coverage())
        self.assertIsNone(self.value(r,'ads','mature_acos'));self.assertEqual(self.metric(r,'ads','acos')['status'],'partial')
        self.assertEqual(self.metric(r,'ads','spend')['inputs']['groups'][0]['maturity_counts']['unknown'],1)
        self.assertEqual(r['modules']['ads']['findings'],[])

    def test_attribution_window_must_finish_before_the_business_day(self):
        r=self.run_engine({'advertising':[self.ad('2026-11-15',mature=True)]},('2026-11-15','2026-11-15'),coverage=self.coverage(('2026-11-15',)))
        self.assertIsNone(self.value(r,'ads','mature_acos'));self.assertEqual(self.value(r,'ads','immature_spend'),10)
        mature=self.run_engine({'advertising':[self.ad('2026-11-14')]},('2026-11-14','2026-11-14'),coverage=self.coverage(('2026-11-14',)))
        self.assertEqual(self.value(mature,'ads','mature_acos'),20)

    def test_stale_ad_refresh_does_not_become_final_just_because_time_passed(self):
        cov=self.coverage(('2026-11-14',));cov['advertising']['last_synced_at']='2026-11-15T00:00:00Z'
        r=self.run_engine({'advertising':[self.ad('2026-11-14')]},('2026-11-14','2026-11-14'),coverage=cov)
        self.assertIsNone(self.value(r,'ads','mature_acos'))

    def test_overlapping_ad_grains_are_not_summed(self):
        r=self.run_engine({'advertising':[self.ad(),self.ad(report_grain='search_term',entity_name='term')]},
                          coverage=self.coverage(),policies={'target_acos':1})
        self.assertIsNone(self.value(r,'ads','spend'));self.assertIsNone(self.value(r,'ads','acos'))
        self.assertIsNone(self.value(r,'ads','mature_acos'))
        self.assertEqual(r['modules']['ads']['findings'],[])
        self.assertEqual(len(self.metric(r,'ads','spend')['inputs']['groups']),2)

    def test_advertised_product_traffic_keys_ignore_names_and_job_ids(self):
        row = self.ad(report_grain='advertised_product',campaign_id='C',ad_group_id='G',ad_id='AD1',sku='SKU1')
        for second_spend in (10,17):
            records = [row,dict(row,ad_id='AD2',spend=second_spend),dict(row,sku='SKU2',spend=2),
                       dict(row,campaign_name='renamed',entity_name='new group',source_id='next sync')]
            r = self.run_engine({'advertising':records},coverage=self.coverage())
            self.assertEqual(self.value(r,'ads','spend'),12+second_spend)
            self.assertEqual(self.value(r,'ads','clicks'),30)
        # Historic payloads can omit entity IDs while their persisted facts retain an authoritative key.
        old = self.ad(report_grain='advertised_product',source_connection_id='conn',natural_key='key1')
        r=self.run_engine({'advertising':[old,dict(old,natural_key='key2'),dict(old,entity_name='changed')]})
        self.assertEqual(self.value(r,'ads','spend'),20)

    def test_conflicts_mixed_csv_and_incomplete_period_do_not_trigger_target(self):
        row=self.ad(report_grain='advertised_product',profile_id='12',campaign_id='C',ad_group_id='G',ad_id='A',sku='S')
        for other,code in ((dict(row,spend=2),'conflicting_ad_row'),
                           (dict(row,attribution_days=14,campaign_name='renamed'),'overlapping_ad_grain')):
            r=self.run_engine({'advertising':[row,other,dict(row,ad_id='B')]},coverage=self.coverage(),policies={'target_acos':1})
            self.assertIsNone(self.value(r,'ads','mature_acos'))
            self.assertIsNone(self.value(r,'ads','spend'))
            self.assertIn(code,[x['code'] for x in r['data_quality']['issues']])
            self.assertEqual(r['modules']['ads']['findings'],[])
        priced=dict(row,source='advertised_product_csv',maturity_unverified=True)
        r=self.run_engine({'advertising':[priced,self.ad(source='csv',ad_type=None,report_grain=None,attribution_days=None)]})
        self.assertIsNone(self.value(r,'ads','spend'))
        self.assertIn('overlapping_ad_sources',[x['code'] for x in r['data_quality']['issues']])
        r=self.run_engine({'advertising':[priced]},coverage=self.coverage(),policies={'target_acos':1})
        self.assertEqual(self.value(r,'ads','spend'),10)
        self.assertIsNone(self.value(r,'ads','mature_acos'))
        r=self.run_engine({'advertising':[dict(row,mature=True)]},policies={'target_acos':1})
        self.assertEqual(self.metric(r,'ads','mature_acos')['status'],'partial')
        self.assertEqual(r['modules']['ads']['findings'],[])

    def test_distinct_known_windows_are_retained_as_separate_cohorts(self):
        r=self.run_engine({'advertising':[self.ad(),self.ad(ad_type='SB',campaign_name='SB-test',attribution_days=14)]},coverage=self.coverage())
        self.assertEqual(self.value(r,'ads','spend'),20)
        self.assertEqual(self.metric(r,'ads','acos')['status'],'partial')
        self.assertEqual({g['attribution_days'] for g in self.metric(r,'ads','spend')['inputs']['groups']},{7,14})

    def test_target_acos_only_produces_a_review_finding_for_known_mature_rows(self):
        r=self.run_engine({'advertising':[self.ad()]},coverage=self.coverage(),policies={'target_acos':10})
        self.assertEqual(r['modules']['ads']['findings'][0]['type'],'mature_acos_above_target')
        self.assertIn('人工评审',r['modules']['ads']['findings'][0]['action'])
        unverified=self.run_engine({'advertising':[self.ad(attribution_days=None)]},policies={'target_acos':1})
        self.assertEqual(unverified['modules']['ads']['findings'],[])

    def test_business_timezone_controls_today_inventory_cutoff_and_overdue(self):
        r=self.run_engine({'inventory':[self.stock('2026-11-21',fulfillable=3),self.stock('2026-11-22',fulfillable=99)]},('2026-11-21','2026-11-22'),now=datetime(2026,11,22,1,tzinfo=timezone.utc),tasks=[{'id':'t','module_code':'compliance','due_date':'2026-11-21','status':'in_progress'}])
        self.assertEqual(r['period']['as_of'],'2026-11-21');self.assertEqual(self.value(r,'inventory','fulfillable'),3)
        self.assertEqual(self.value(r,'compliance','overdue_tasks'),0)

    def test_context_missing_and_explicit_empty_sources_are_distinct(self):
        absent=self.run_engine();empty=self.run_engine(tasks=[],events=[],rankings=[])
        self.assertIsNone(self.value(absent,'review','open_tasks'));self.assertEqual(self.value(empty,'review','open_tasks'),0)
        self.assertEqual(self.value(empty,'calendar','events_in_period'),0)
        self.assertEqual(self.value(empty,'review','rank_observations'),0)
        self.assertIsNone(self.value(empty,'service','return_rate'))
        self.assertIsNone(self.value(empty,'compliance','verified_certifications'))

    def test_data_and_policy_objects_are_not_modified(self):
        records={'sales':[self.sale()],'advertising':[self.ad()]};original=deepcopy(records);policy=self.policy();before=deepcopy(policy)
        self.run_engine(records,policies=policy,coverage=self.coverage())
        self.assertEqual(records,original);self.assertEqual(policy,before)

    def test_record_permutation_does_not_change_calculated_metrics(self):
        records={'sales':[self.sale(sessions=10),self.sale('2026-11-02',units=3,sessions=40)],'advertising':[self.ad(),self.ad('2026-11-02')],
                 'inventory':[self.stock('2026-11-01'),self.stock('2026-11-02')],'costs':[self.cost(),self.cost('2026-11-02',product_cost='3')]}
        first=self.run_engine(records,coverage=self.coverage(),policies=self.policy())
        second=self.run_engine({k:list(reversed(v)) for k,v in records.items()},coverage=self.coverage(),policies=self.policy())
        self.assertEqual(first,second)

    def test_exact_duplicate_ad_records_are_not_double_counted(self):
        r=self.run_engine({'advertising':[self.ad(),self.ad()]},coverage=self.coverage())
        self.assertEqual(self.value(r,'ads','spend'),10)

    def test_conflicting_cost_and_inventory_records_are_not_chosen_arbitrarily(self):
        r=self.run_engine({'sales':[self.sale()],'costs':[self.cost(product_cost='1'),self.cost(product_cost='9')],
                           'inventory':[self.stock(fulfillable=1),self.stock(fulfillable=99)]},coverage=self.coverage(),policies=self.policy())
        self.assertIsNone(self.value(r,'finance','contribution_before_ads'));self.assertIsNone(self.value(r,'inventory','fulfillable'))

    def test_invalid_numeric_values_are_not_zero_filled(self):
        for value in ['NaN','Infinity','-1',True,None]:
            r=self.run_engine({'sales':[self.sale(revenue=value)]})
            self.assertIsNone(self.value(r,'shop','ordered_revenue'))
            json.dumps(r,allow_nan=False)

    def test_uuid_decimal_and_nonfinite_policy_metadata_are_json_safe(self):
        product={**self.product,'id':uuid4()};row=self.sale(product_id=product['id'],revenue=Decimal('0.30'))
        r=calculate_product_metrics(product,{'sales':[row]},date(2026,11,1),date(2026,11,2),now=self.now,policies={'target_acos':float('nan')})
        json.dumps(r,allow_nan=False)
        self.assertEqual(self.value(r,'shop','ordered_revenue'),.3)

    def test_invalid_period_is_rejected(self):
        with self.assertRaises(ValueError):self.run_engine(days=('2026-11-02','2026-11-01'))

    def test_sources_metadata_does_not_prove_complete_days(self):
        r=self.run_engine({'sales':[self.sale()],'inventory':[self.stock()]},policies=self.policy(),sources=[{'id':'s','provider':'sp_api','name':'Amazon','status':'connected','last_success_at':'2026-11-22T20:00:00Z'}])
        self.assertIsNone(self.value(r,'inventory','daily_sales_rate'))
        self.assertEqual(r['sources'][0]['provider'],'sp_api')
        self.assertIsNone(r['data_quality']['last_updated_at'])

    def test_large_finite_inputs_remain_json_safe(self):
        r=self.run_engine({'sales':[self.sale(units='1e24',revenue='1e24')],'costs':[self.cost(product_cost='1e24')]})
        self.assertIsNotNone(self.value(r,'finance','known_unit_cost_total'))
        json.dumps(r,allow_nan=False)

    def test_future_refresh_timestamp_cannot_prove_mature_attribution(self):
        cov=self.coverage();cov['advertising']['last_synced_at']='2030-01-01T00:00:00Z'
        r=self.run_engine({'advertising':[self.ad()]},coverage=cov)
        self.assertIsNone(self.value(r,'ads','mature_acos'))
        self.assertTrue(any(i['code']=='future_refresh_timestamp' for i in r['data_quality']['issues']))

    def test_conflicting_optional_sales_fields_do_not_depend_on_row_order(self):
        rows=[self.sale(sessions=10),self.sale(sessions=20)]
        a=self.run_engine({'sales':rows});b=self.run_engine({'sales':list(reversed(rows))})
        self.assertEqual(a,b);self.assertIsNone(self.value(a,'listing','sessions'))

    def test_sp_only_cost_rate_is_not_mislabeled_as_all_channel_tacos(self):
        records={'sales':[self.sale(revenue='100')],'advertising':[self.ad(spend='10')],'costs':[self.cost()]}
        cov=self.coverage();cov['advertising']['ad_types_complete']=['SP']
        sp=self.run_engine(records,coverage=cov)
        rate=self.metric(sp,'ads','tacos')
        self.assertEqual(rate['value'],10);self.assertEqual(rate['label'],'SP广告花费率')
        self.assertIn('不包含未接入的 SB、SD',rate['reason']);self.assertIn('SP广告花费',rate['formula'])
        self.assertFalse(sp['modules']['ads']['scope']['all_channels_complete'])
        self.assertIn('SP',self.metric(sp,'ads','spend')['label'])
        self.assertIn('SP',self.metric(sp,'finance','contribution_after_ads')['label'])
        self.assertEqual(self.metric(sp,'finance','contribution_after_ads')['status'],'partial')
        all_cov=deepcopy(cov);all_cov['advertising']['ad_types_complete']=['SP','SB','SD']
        all_channels=self.run_engine(records,coverage=all_cov)
        self.assertEqual(self.metric(all_channels,'ads','tacos')['label'],'全渠道 TACOS')
        self.assertEqual(self.value(all_channels,'ads','tacos'),10)
        unknown=self.run_engine({'sales':[self.sale(revenue='100')],'advertising':[self.ad(ad_type=None)]},coverage=cov)
        self.assertEqual(self.metric(unknown,'ads','tacos')['status'],'partial')
        self.assertNotIn('全渠道 TACOS',self.metric(unknown,'ads','tacos')['label'])
