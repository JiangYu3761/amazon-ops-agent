"""Integer largest-remainder allocation and append-only reversals of local expense attribution."""
from decimal import Decimal

from psycopg.types.json import Jsonb

from .erp_refunds import currency_digits, money_text


def distribute(amount, currency, targets):
    """Allocate a positive expense by integer arithmetic, independent of input order."""
    scale = 10 ** currency_digits(currency)
    units = int(amount * scale)
    weighted = []
    for target in targets:
        key = (str(target['product_id']),target['sku'] or '')
        weighted.append((key,int(Decimal(str(target['weight'])) * 1_000_000),target))
    weighted.sort(key=lambda value:value[0])
    total_weight = sum(value[1] for value in weighted)
    if not units or not weighted or any(weight<=0 for _,weight,_ in weighted):
        raise ValueError('费用和各项权重必须大于零')
    rows = []
    for key,weight,target in weighted:
        floor,remainder = divmod(units*weight,total_weight)
        rows.append([key,floor,remainder,target])
    leftover = units-sum(row[1] for row in rows)
    for row in sorted(rows,key=lambda row:(-row[2],row[0]))[:leftover]:
        row[1]+=1
    return [{**target,'expense':money_text(Decimal(count)/scale,currency)}
            for _,count,_,target in rows]


def last_event(conn, entry_id):
    return conn.execute('SELECT * FROM erp_fee_allocations WHERE entry_id=%s ORDER BY version DESC LIMIT 1',
                        (entry_id,)).fetchone()


def reverse_active(conn, entry_id, actor, reason, request_id=None):
    event=last_event(conn,entry_id)
    if not event or event['action']!='post':
        return None
    targets=[{**t,'expense':money_text(-Decimal(t['expense']),event['currency'])} for t in event['targets']]
    return conn.execute('''INSERT INTO erp_fee_allocations(entry_id,version,action,reverses_id,actor_id,
            request_id,payout_date,currency,expense,basis,reason,targets)
            VALUES(%s,%s,'void',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (entry_id,event['version']+1,event['id'],actor['id'],request_id,event['payout_date'],event['currency'],
             -event['expense'],event['basis'],reason,Jsonb(targets))).fetchone()


def reverse_on_date_change(conn, statement_id, actor):
    active=conn.execute('''SELECT a.entry_id FROM erp_fee_allocations a JOIN erp_settlement_entries e ON e.id=a.entry_id
        WHERE e.statement_id=%s AND a.action='post' AND NOT EXISTS
        (SELECT 1 FROM erp_fee_allocations v WHERE v.reverses_id=a.id)''',(statement_id,)).fetchall()
    for row in active:
        reverse_active(conn,row['entry_id'],actor,'结算发放日已修订，原期间的费用分摊冲正')
