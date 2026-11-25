"""Buildless static assets for the hosted preview; Python API stays self-hosted."""
from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path
import re
import shutil
parser = ArgumentParser(description=__doc__)
parser.add_argument('--workspace-package', type=Path, help='Verified ZIP for the matching complete local workspace')
args = parser.parse_args()
root=Path(__file__).resolve().parents[1]
out=root/'dist'
out.mkdir(exist_ok=True)
for p in out.iterdir():
    if p.is_dir():shutil.rmtree(p)
    else:p.unlink()
for name in ('index.html','styles.css','app.js','analytics.js','analytics.css','deliverables.js','process.js','process.css','approvals.js','approvals.css','approval-templates.json','catalog.json',
             'erp-stock.js','erp-fulfillment.js','erp-shop-sales.js','erp-queue.js','erp-search-terms.js','erp-comparison.js','workbench.js','workbench-triage.js','workbench-feishu.js','ads-decision-review.js','workbench.css','erp-refunds.js','erp-refunds.css','erp-contract-pricing.js','erp-procurement.js','erp-receiving.js','erp-receiving.css','erp-payables.js','erp-payables.css','erp-settlements.js','erp-fee-allocations.js'):
    shutil.copy2(root/'frontend'/name,out/name)
for definition in (root/'frontend').glob('workbench-*.json'):
    shutil.copy2(definition,out/definition.name)
for name in ('ads-review-training.html', 'workspace-install.js', 'manifest.webmanifest'):
    shutil.copy2(root/'frontend'/name,out/name)
shutil.copytree(root/'frontend'/'icons',out/'icons')
shutil.copytree(root/'frontend'/'templates',out/'templates')
# Link each script and stylesheet to its content so a saved bookmark can load a new release.
index = out / 'index.html'
page = index.read_text(encoding='utf-8')
if args.workspace_package:
    package_name = 'homa-local-workspace.zip'
    shutil.copyfile(args.workspace_package, out / package_name)
    page = page.replace('<body>', f'<body data-workspace-download="{package_name}">', 1)
    if 'data-workspace-download=' not in page:
        raise ValueError('Cannot attach the local workspace download to this page')

def asset_url(match):
    attribute, name = match.groups()
    fingerprint = sha256((out / name).read_bytes()).hexdigest()[:12]
    return f'{attribute}="{name}?v={fingerprint}"'
page = re.sub(r'(src|href)="([\w.-]+\.(?:js|css))"', asset_url, page)
release = sha256()
for asset in sorted(out.rglob('*')):
    if asset.is_file():
        release.update(asset.relative_to(out).as_posix().encode() + b'\0' + sha256(asset.read_bytes()).digest())
page = page.replace('<span id="frontendBuild">HOMA · 运营工作台</span>',
                    f'<span id="frontendBuild">前台版本 {release.hexdigest()[:8]}</span>')
index.write_text(page, encoding='utf-8')
print('Static workspace prepared in dist/')
