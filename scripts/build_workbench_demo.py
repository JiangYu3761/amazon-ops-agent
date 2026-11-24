"""Package the actual HOMA browser demo as one self-contained HTML file."""

import argparse
import json
import re
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'

INTRO = '''
<section id="offline-entry" aria-labelledby="offline-title" lang="zh-CN">
 <div class="offline-card">
  <div class="offline-brand" aria-hidden="true">HOMA</div>
  <h1 id="offline-title">亚马逊运营工作台</h1>
  <p>用示例产品体验运营办理、审核和排期。数据保存在当前浏览器，重新打开同一文件可继续填写。</p>
  <p class="offline-limit">离线体验 · 无生产后台、外部模型和平台同步</p>
  <button type="button" id="offline-start">进入本机示例 <span aria-hidden="true">→</span></button>
  <noscript><p>请允许浏览器运行 JavaScript 以进入工作台。</p></noscript>
 </div>
</section>
'''

ENTRY_STYLE = '''
<style>
body.offline-welcome > :not(#offline-entry) { display: none !important }
#offline-entry { min-height: 100vh; width: 100%; display: grid; place-items: center; padding: 24px; box-sizing: border-box; background: #f3f5f9 }
.offline-card { width: min(100%, 480px); padding: clamp(28px, 6vw, 52px); background: white; border: 1px solid #dae1eb; border-radius: 18px; box-sizing: border-box; color: #17223a }
.offline-brand { font-weight: 750; letter-spacing: .16em; color: #3a5488; margin-bottom: 26px }
.offline-card h1 { font-size: clamp(27px, 4vw, 36px); line-height: 1.3; margin: 0 0 18px }
.offline-card p { line-height: 1.75; margin: 0 0 16px }
.offline-limit { font-size: 13px; color: #596677 }
#offline-start { margin-top: 8px; border: 0; border-radius: 8px; padding: 12px 20px; font: inherit; font-weight: 650; color: white; background: #243c69; cursor: pointer }
</style>
'''

BOOT = '''
<script>
(() => {
  const files = new Map(Object.entries(JSON.parse(document.querySelector('#offline-assets').textContent))
    .map(([name, contents]) => [new URL(name, document.baseURI).href, contents]));
  const csv = url => url.pathname.endsWith('.csv');
  const mime = url => csv(url) ? 'text/csv;charset=utf-8' : 'application/json;charset=utf-8';

  // All lookups stay in memory. The real workspace switches to its normal demo mode on HTTP 404.
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, document.baseURI);
    url.search = ''; url.hash = '';
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (method === 'GET' && files.has(url.href)) {
      return new Response(files.get(url.href), {headers: {'Content-Type': mime(url)}});
    }
    const message = url.pathname.startsWith('/api/') ? '离线体验没有连接生产后台。' : '离线包没有此资源。';
    return new Response(JSON.stringify({detail: message}), {status: 404, headers: {'Content-Type': 'application/json'}});
  };

  // The real import page creates these links on each render, including after a product switch.
  function prepareDownload(link) {
    const url = new URL(link.href);
    if (!csv(url) || !files.has(url.href)) return;
    link.download = url.pathname.split('/').pop();
    link.href = 'data:' + mime(url) + ',' + encodeURIComponent(files.get(url.href));
  }
  new MutationObserver(changes => {
    for (const change of changes) for (const node of change.addedNodes) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      if (node.matches('a[download][href]')) prepareDownload(node);
      node.querySelectorAll('a[download][href]').forEach(prepareDownload);
    }
  }).observe(document.body, {childList: true, subtree: true});

  document.querySelector('#offline-start').addEventListener('click', () => {
    document.querySelector('#offline-entry').remove();
    document.body.classList.remove('offline-welcome');
    for (const source of document.querySelectorAll('script[type="application/x-homa-program"]')) {
      const script = document.createElement('script');
      script.textContent = source.textContent;
      source.replaceWith(script);
    }
  }, {once: true});
})();
</script>
'''


def asset_text(relative: str) -> str:
    target = (FRONTEND / relative).resolve()
    if not target.is_relative_to(FRONTEND):
        raise ValueError(f'Asset is outside frontend/: {relative}')
    return target.read_bytes().decode('utf-8')


def inline_tag(match: re.Match, tag: str) -> str:
    path = match.group(1)
    source = asset_text(path)
    if re.search(rf'</{tag}\b', source, re.IGNORECASE):
        raise ValueError(f'Cannot inline a raw HTML closing tag from {path}')
    attrs = ' type="application/x-homa-program"' if tag == 'script' else ''
    return f'<{tag}{attrs}>\n{source}\n</{tag}>'


def build(destination: Path) -> None:
    names = ['catalog.json', 'approval-templates.json']
    names.extend(path.name for path in sorted(FRONTEND.glob('workbench-*.json')))
    names.extend(f'templates/{kind}-template.csv' for kind in ('sales', 'advertising', 'inventory', 'costs'))
    files = {name: asset_text(name) for name in names}
    # Raw text in the HTML script element must never terminate on business text containing markup.
    assets = json.dumps(files, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c')
    page = asset_text('index.html')
    page = re.sub(r'<link rel="stylesheet" href="([^"]+)">', lambda m: inline_tag(m, 'style'), page)
    page = re.sub(r'<script src="([^"]+)" defer></script>', lambda m: inline_tag(m, 'script'), page)
    if re.search(r'<(?:script\b[^>]*\bsrc=|link\b[^>]*\brel="stylesheet")', page, re.IGNORECASE):
        raise ValueError('A linked script or stylesheet was not embedded. Check frontend/index.html.')
    page = page.replace('<body>', '<body class="offline-welcome">' + INTRO, 1)
    csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; form-action 'none'; base-uri 'none'"
    page = page.replace('<head>', f'<head>\n<meta http-equiv="Content-Security-Policy" content="{csp}">', 1)
    page = page.replace('</head>', ENTRY_STYLE + '</head>', 1)
    page = page.replace('</body>', f'<script type="application/json" id="offline-assets">{assets}</script>\n{BOOT}</body>', 1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='Output HTML file')
    args = parser.parse_args()
    build(args.output)
    print(args.output)
