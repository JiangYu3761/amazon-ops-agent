"""Buildless static assets for the hosted preview; Python API stays self-hosted."""
from pathlib import Path
import shutil
root=Path(__file__).resolve().parents[1]
out=root/'dist'
out.mkdir(exist_ok=True)
for p in out.iterdir():
    if p.is_dir():shutil.rmtree(p)
    else:p.unlink()
for name in ('index.html','styles.css','app.js','analytics.js','analytics.css','deliverables.js','process.js','process.css','approvals.js','approvals.css','approval-templates.json','catalog.json'):
    shutil.copy2(root/'frontend'/name,out/name)
shutil.copytree(root/'frontend'/'templates',out/'templates')
print('Static workspace prepared in dist/')
