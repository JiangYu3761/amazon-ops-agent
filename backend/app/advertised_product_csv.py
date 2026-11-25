"""Manual SP advertised-product report rows. Import time is not a report-refresh timestamp."""
from fastapi import HTTPException


def validate_scope(conn, product_id, product, rows):
    # A configured local profile mapping works even when API synchronization is disabled.
    mappings = conn.execute("""SELECT c.ads_profile_id,c.account_key,c.marketplace,c.currency,
        m.sku,m.asin FROM operating_product_mappings m JOIN operating_connections c ON c.id=m.connection_id
        WHERE m.product_id=%s AND c.provider='ads_sp'""", (product_id,)).fetchall()
    mapped = {(m['ads_profile_id'],m['sku']):m for m in mappings}
    profiles = {r['profile_id'] for r in rows}
    skus = {r['sku'] for r in rows}
    registered = conn.execute("""SELECT c.ads_profile_id,c.marketplace,c.account_key,c.currency,m.product_id,m.sku
        FROM operating_connections c LEFT JOIN operating_product_mappings m ON m.connection_id=c.id AND m.sku=ANY(%s)
        WHERE c.provider='ads_sp' AND c.ads_profile_id=ANY(%s)""", (list(skus),list(profiles))).fetchall()
    account = product['data_account_key']
    foreign_profiles = {m['ads_profile_id'] for m in registered if
                        (m['account_key'],m['marketplace'],m['currency']) != (account,product['marketplace'],product['currency'])}
    foreign_skus = {(m['ads_profile_id'],m['sku']) for m in registered if m['product_id'] and m['product_id'] != product_id}
    for row in rows:
        if ((row.get('account_key') and row['account_key'] != account)
                or (row.get('marketplace') and row['marketplace'] != product['marketplace'])):
            raise HTTPException(422, f"第 {row['source_line']} 行的店铺或站点与所选产品不一致")
        if mappings:
            mapping = mapped.get((row['profile_id'],row['sku']))
            if (not mapping or mapping['account_key'] != account or mapping['marketplace'] != product['marketplace']
                    or mapping['currency'] != product['currency']):
                raise HTTPException(422, f"第 {row['source_line']} 行的广告账户或 SKU 与产品的 SP 映射不一致")
            asin = mapping['asin'] or (product['asin'] if product['sku'] == row['sku'] else None)
        else:
            if (not account or row.get('account_key') != account or row.get('marketplace') != product['marketplace']
                    or row['sku'] != product['sku']):
                raise HTTPException(422, f"第 {row['source_line']} 行缺少可核验的广告映射；手工报表请填写与产品相同的 account_key、marketplace、sku")
            asin = product['asin']
        if row.get('asin') and asin and row['asin'] != asin:
            raise HTTPException(422, f"第 {row['source_line']} 行的 ASIN 与此 SKU 映射不一致")
        if row['profile_id'] in foreign_profiles or (row['profile_id'],row['sku']) in foreign_skus:
            raise HTTPException(422, f"第 {row['source_line']} 行的广告 profile 或 SKU 已登记在其他范围")
        row.update(account_key=account, marketplace=product['marketplace'])


def store_source(conn, product_id, row, import_id):
    params = dict(row, product=product_id, batch=import_id)
    prior = conn.execute("""SELECT * FROM erp_advertised_product_rows WHERE product_id=%(product)s
        AND account_key=%(account_key)s AND marketplace=%(marketplace)s AND business_key=%(business_key)s FOR UPDATE""", params).fetchone()
    conn.execute("""INSERT INTO erp_advertised_product_rows(product_id,business_key,report_date,profile_id,account_key,marketplace,
        currency,attribution_days,ad_type,report_grain,campaign_id,campaign_name,ad_group_id,entity_name,
        ad_id,sku,asin,impressions,clicks,spend,attributed_sales,attributed_orders,import_id,source_line)
        VALUES(%(product)s,%(business_key)s,%(date)s,%(profile_id)s,%(account_key)s,%(marketplace)s,%(currency)s,%(attribution_days)s,
               %(ad_type)s,%(report_grain)s,%(campaign_id)s,%(campaign_name)s,%(ad_group_id)s,
               %(entity_name)s,%(ad_id)s,%(sku)s,%(asin)s,%(impressions)s,%(clicks)s,%(spend)s,
               %(sales)s,%(orders)s,%(batch)s,%(source_line)s)
        ON CONFLICT(product_id,account_key,marketplace,business_key) DO UPDATE SET campaign_name=excluded.campaign_name,
          entity_name=excluded.entity_name,asin=excluded.asin,impressions=excluded.impressions,
          clicks=excluded.clicks,spend=excluded.spend,attributed_sales=excluded.attributed_sales,
          attributed_orders=excluded.attributed_orders,import_id=excluded.import_id,source_line=excluded.source_line""", params)
    return prior
