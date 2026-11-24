"""Buildless static assets for the hosted preview; Python API stays self-hosted."""
from pathlib import Path
import shutil
root=Path(__file__).resolve().parents[1]
out=root/'dist'
out.mkdir(exist_ok=True)
for p in out.iterdir():
    if p.is_dir():shutil.rmtree(p)
    else:p.unlink()
for name in ('index.html','styles.css','app.js','analytics.js','analytics.css','deliverables.js','process.js','process.css','approvals.js','approvals.css','approval-templates.json','catalog.json',
             'erp-stock.js','erp-fulfillment.js','erp-shop-sales.js','erp-queue.js','erp-search-terms.js','erp-comparison.js','workbench.js','workbench-triage.js','workbench.css','erp-refunds.js','erp-refunds.css','erp-procurement.js','erp-receiving.js','erp-receiving.css','erp-payables.js','erp-payables.css','erp-settlements.js','erp-fee-allocations.js'):
    shutil.copy2(root/'frontend'/name,out/name)
for definition in (root/'frontend').glob('workbench-*.json'):
    shutil.copy2(definition,out/definition.name)
shutil.copytree(root/'frontend'/'templates',out/'templates')
print('Static workspace prepared in dist/')
