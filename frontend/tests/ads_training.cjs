/* Run against the actual static build, with no API service attached. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const {chromium, firefox} = require('playwright');

const dist = path.resolve(__dirname, '../../dist');
const contentTypes = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json'
};
const trainingKey = 'homa.ads.offline.review.v1';

async function testSingleFile(browser, diskFile, directFile) {
  const context = await browser.newContext({offline: true});
  try {
    const page = await context.newPage();
    page.setDefaultTimeout(7000);
    const url = directFile ? pathToFileURL(diskFile).href : 'https://offline.example/workspace.html';
    const requests = [];
    page.on('request', req => requests.push(req.url().split('#')[0]));
    if (!directFile) {
      await page.route(url, route => route.fulfill({contentType:'text/html; charset=utf-8', body:fs.readFileSync(diskFile)}));
    }
    await page.goto(url + '#ads');
    await page.locator('#offline-start').click();
    await page.locator('button[data-wb-section="verification"]').click();
    const open = page.getByRole('button', {name:'用示例练习广告复盘'});
    const close = page.getByRole('button', {name:'关闭演练'});
    const frame = page.frameLocator('.ar-training-dialog iframe');
    await open.click();
    await frame.locator('[data-act=rv-match]').click();
    await frame.locator('[data-act=rv-toweek]').click();
    await frame.locator('#rvSample').selectOption('clean');
    await frame.locator('#rvSign').check();
    await frame.locator('[data-act=rv-close]').click();
    await close.click();
    await open.click();
    await frame.getByText('已签署结论', {exact:false}).first().waitFor();
    assert.equal(await frame.locator('html').evaluate(() => location.href),'about:srcdoc');
    assert(requests.every(item => item === url), '单文件演练请求了其他资源：' + requests.join(', '));
  } finally {
    await context.close();
  }
}

async function run() {
  assert(fs.existsSync(path.join(dist, 'ads-review-training.html')), '请先运行 python3 scripts/build_frontend.py');
  const requests = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    requests.push({method: req.method, path: url.pathname});
    const filename = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!filename.startsWith(dist + path.sep) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, {'Content-Type': contentTypes[path.extname(filename)] || 'application/octet-stream'});
    fs.createReadStream(filename).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium'});
    const page = await browser.newPage({viewport: {width: 1130, height: 830}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(7000);
    const url = 'http://127.0.0.1:' + server.address().port;
    await page.goto(url + '/#ads');
    await page.locator('button[data-wb-section="verification"]').click();
    const launch = page.getByRole('button', {name: '用示例练习广告复盘'});
    await launch.waitFor();
    assert.equal(await page.locator('#connectionBadge').innerText(),'示例体验模式');
    assert.equal(await page.locator('.ar-training-dialog iframe').count(),0);
    const keys = () => page.evaluate(key => Object.fromEntries(Object.entries(localStorage).filter(([k]) => k !== key)), trainingKey);
    const before = await keys();
    const since = requests.length;
    const popup = page.getByRole('dialog', {name: '广告复盘示例演练'});
    const frame = page.frameLocator('.ar-training-dialog iframe');
    const action = name => frame.locator('[data-act="' + name + '"]');
    await launch.click();
    await frame.getByRole('heading', {name:'复盘中心', exact: true}).waitFor();
    assert.match(await frame.locator('.mode').innerText(),/示例演练.*假设数据/);
    await action('rv-match').click();
    await action('rv-toweek').click();
    await frame.locator('#rvSample').selectOption('clean');
    assert.match(await frame.locator('#content').innerText(),/18.00/);
    await frame.locator('#rvSign').check();
    await action('rv-close').click();
    await frame.getByText('已签署结论', {exact: false}).first().waitFor();
    await popup.getByRole('button', {name: '关闭演练'}).click();
    await popup.waitFor({state:'detached'});
    assert.equal(await launch.evaluate(button => button === document.activeElement),true);
    assert.deepEqual(await keys(),before);
    await page.reload();
    await page.locator('#connectionBadge').filter({hasText:'示例体验模式'}).waitFor().catch(async error => {
      throw new Error(page.url() + ' ' + await page.locator('body').innerText() + ' ' + errors.join(';'), {cause:error});
    });
    await page.locator('button[data-wb-section="verification"]').click();
    await page.setViewportSize({width:390,height:740});
    await launch.click();
    await frame.getByRole('heading', {name:'复盘中心', exact: true}).waitFor();
    await frame.getByText('已签署结论', {exact: false}).first().waitFor();
    const box = await popup.boundingBox();
    assert(box && box.width <= 391 && box.height <= 741);
    await action('reset').click();
    await action('cancelReset').click();
    await frame.getByText('已签署结论', {exact: false}).first().waitFor();
    await action('reset').click();
    await action('doReset').click();
    await action('rv-match').waitFor();
    assert.equal(await frame.locator('#pageTitle').innerText(),'复盘中心');
    await popup.getByRole('button', {name: '关闭演练'}).click();
    assert.deepEqual(await keys(),before);
    assert.equal(new URL(page.url()).hash,'#ads');
    assert.equal(requests.slice(since).filter(r=>r.method!=='GET'||r.path.includes('ads-reviews')).length,0);
    assert.deepEqual(errors,[]);
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(),'homa-ads-training-'));
    try {
      const diskFile = path.join(scratch,'workbench.html');
      execFileSync('python3',[path.join(dist,'../scripts/build_workbench_demo.py'),diskFile]);
      await testSingleFile(browser,diskFile,false);
      if(process.env.FIREFOX_PATH) {
        const ff = await firefox.launch({executablePath:process.env.FIREFOX_PATH});
        try { await testSingleFile(ff,diskFile,true); } finally { await ff.close(); }
      }
    } finally {
      fs.rmSync(scratch,{recursive:true,force:true});
    }
    console.log('静态广告演练通过：实际导航、模拟对账和签署、浏览器持久化、移动端、重置隔离、无业务写入。');
    console.log('单文件离线导出通过：直接嵌入文档，可复盘签署并恢复，无附加资源请求。');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
run().catch(error => {console.error(error);process.exitCode=1;});
