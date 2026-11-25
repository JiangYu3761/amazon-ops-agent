"""Live warehouse obligations, computed from the same ledger and access scope as each source screen."""
from fastapi import APIRouter, Depends, Query

from .db import connection
from .workflow_auth import require_user


router = APIRouter(prefix="/api/erp-queue", tags=["Warehouse work queue"])

QUEUE = """
WITH allowed AS MATERIALIZED (
    SELECT s.* FROM erp_receiving_scopes s WHERE %(admin)s OR EXISTS
    (SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=s.id AND m.user_id=%(actor)s)
), queue(kind, id, target_id, scope_id, reference, subject, state, quantity, balance, currency, opened_at, priority) AS (
    SELECT 'cancel',p.id,o.id,s.id,p.package_ref,o.number,p.status,NULL::bigint,NULL::numeric,NULL::text,
           COALESCE((SELECT max(e.created_at) FROM erp_fbm_events e WHERE e.package_id=p.id),p.created_at),
           CASE p.status WHEN 'after_dispatch' THEN 0 WHEN 'stop_failed' THEN 1 WHEN 'intercepting' THEN 1 ELSE 2 END
    FROM allowed s JOIN erp_fbm_orders o ON o.scope_id=s.id JOIN erp_fbm_packages p ON p.order_id=o.id
    WHERE p.status IN ('hold','intercepting','stop_failed','after_dispatch')
    UNION ALL
    SELECT 'receive',n.id,o.id,s.id,n.number,o.number,'waiting',sum(l.qty),NULL,NULL,n.created_at,3
    FROM allowed s JOIN erp_receiving_orders o ON o.scope_id=s.id JOIN erp_receiving_notices n ON n.order_id=o.id
    JOIN erp_receiving_notice_lines l ON l.notice_id=n.id
    WHERE n.status='waiting' GROUP BY n.id,o.id,s.id
    UNION ALL
    SELECT 'quality',n.id,o.id,s.id,n.number,o.number,'pending',sum(g.pending_delta),NULL,NULL,n.created_at,3
    FROM allowed s JOIN erp_receiving_orders o ON o.scope_id=s.id JOIN erp_receiving_notices n ON n.order_id=o.id
    JOIN erp_receiving_ledger g ON g.notice_id=n.id
    WHERE n.status='received' GROUP BY n.id,o.id,s.id HAVING sum(g.pending_delta)>0
    UNION ALL
    SELECT 'payment',b.id,b.id,s.id,b.document_ref,b.supplier,'unpaid',NULL,
           b.amount - COALESCE(pay.paid,0),b.currency,b.created_at,3
    FROM allowed s JOIN erp_payable_bills b ON b.scope_id=s.id
    LEFT JOIN LATERAL (SELECT sum(amount) AS paid FROM erp_payable_payments p WHERE p.bill_id=b.id) pay ON true
    WHERE b.void_at IS NULL AND b.amount > COALESCE(pay.paid,0)
    UNION ALL
    SELECT 'approval',r.id,r.id,s.id,r.number,r.supplier,'pending',NULL,NULL,NULL,r.updated_at,3
    FROM allowed s JOIN erp_procurement_requests r ON r.scope_id=s.id
    WHERE %(admin)s AND r.status='pending' AND r.owner_id<>%(actor)s
)
SELECT q.*,s.account_ref,s.site,s.warehouse FROM queue q JOIN allowed s ON s.id=q.scope_id
ORDER BY q.priority,q.opened_at,q.kind,q.id LIMIT %(limit)s OFFSET %(offset)s
"""


@router.get("")
def warehouse_queue(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                    actor=Depends(require_user)):
    with connection() as conn:
        rows = conn.execute(QUEUE, dict(admin=actor["is_admin"], actor=actor["id"], limit=limit+1, offset=offset)).fetchall()
    return {"items": rows[:limit], "offset": offset, "next_offset": offset+limit if len(rows)>limit else None,
            "scope": "authorized_warehouses"}
