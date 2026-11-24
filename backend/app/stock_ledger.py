"""Serialize receipt and transfer postings against the same local warehouse balances."""
from fastapi import HTTPException


def check_postings(conn, changes):
    """Each (scope UUID, SKU) appears once, with signed (pending, good, bad) deltas."""
    for scope, sku in sorted(changes):
        key = f'stock:{scope}:{sku}'
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
    # Separate SELECTs after the locks use a fresh READ COMMITTED snapshot.
    for (scope, sku), delta in changes.items():
        if min(delta) >= 0:
            continue
        row = conn.execute('''SELECT COALESCE(sum(pending_delta),0) AS pending,
                                     COALESCE(sum(good_delta),0) AS good,
                                     COALESCE(sum(bad_delta),0) AS bad
                              FROM erp_stock_postings WHERE scope_id=%s AND sku=%s''', (scope, sku)).fetchone()
        if any(row[bucket] + change < 0 for bucket, change in zip(('pending', 'good', 'bad'), delta)):
            raise HTTPException(409, f'{sku} 的本地库存余额不足，请核对现有调拨或质检流水')
