"""Product-independent account identities used by the shop workbench."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException


def shop_choices(conn):
    # Return operational identity only. Connection credentials/configuration remain administrative.
    return conn.execute('''SELECT DISTINCT ON (account_ref,site,business_timezone) *
        FROM (
            SELECT account_key AS account_ref,marketplace AS site,time_zone AS business_timezone,
                   id AS connection_id FROM operating_connections
            UNION ALL
            SELECT account_ref,site,business_timezone,NULL FROM workbench_shop_scopes
        ) candidates ORDER BY account_ref,site,business_timezone,connection_id NULLS LAST''').fetchall()


def resolve_shop_scope(conn, account_ref, site, business_timezone, connection_id=None, register_by=None):
    account = (account_ref or '').strip()
    market = (site or '').strip().upper()
    zone = (business_timezone or '').strip()
    if connection_id:
        source = conn.execute('''SELECT account_key,marketplace,time_zone FROM operating_connections
                                 WHERE id=%s''', (connection_id,)).fetchone()
        if not source:
            raise HTTPException(422, '所选店铺连接不存在，请重新选择')
        if ((account and account != source['account_key']) or (market and market != source['marketplace'])
                or (zone and zone != source['time_zone'])):
            raise HTTPException(422, '店铺、站点或业务时区与数据连接不一致')
        account, market, zone = source['account_key'], source['marketplace'], source['time_zone']
    if not account or len(account) > 120 or not account.isprintable():
        raise HTTPException(422, '请输入有效的店铺账号标识，最多 120 字')
    if not re.fullmatch('[A-Z]{2,4}', market):
        raise HTTPException(422, '请输入店铺的 2 至 4 位站点代码')
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, '请输入有效的 IANA 业务时区') from None
    if connection_id:
        return account, market, zone

    known = conn.execute('''SELECT id,time_zone FROM operating_connections
                            WHERE account_key=%s AND marketplace=%s ORDER BY id''', (account, market)).fetchall()
    if known:
        if any(source['time_zone'] == zone for source in known):
            return account, market, zone
        raise HTTPException(422, '此店铺站点已有数据连接，请使用已登记的业务时区')
    if register_by:
        conn.execute('''INSERT INTO workbench_shop_scopes(account_ref,site,business_timezone,registered_by)
                        VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (account, market, zone, register_by))
    registered = conn.execute('''SELECT business_timezone FROM workbench_shop_scopes
                                 WHERE account_ref=%s AND site=%s''', (account, market)).fetchone()
    if registered and zone != registered['business_timezone']:
        raise HTTPException(422, '业务时区与该店铺站点的手工登记不一致')
    return account, market, zone
