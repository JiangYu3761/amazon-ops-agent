/* Run with scripts/check_workbench.py against its disposable FastAPI/PostgreSQL workspace. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {randomBytes} = require('node:crypto');
const {chromium} = require('playwright');

(async () => {
  const url = new URL(process.env.WORKBENCH_BROWSER_URL);
  assert(['127.0.0.1','localhost'].includes(url.hostname), '需要本机隔离工作区');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(),'homa-pwa-'));
  let ctx, cdp, installed = false;
  try {
    ctx = await chromium.launchPersistentContext(profile, {executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless:!process.env.PWA_HEADED});
    const auth = await ctx.request.post(new URL('api/workflow-auth/setup',url).href,{
      headers:{'X-Workflow-Request':'1'},
      data:{email:'workspace-install@test.example',display_name:'隔离工作区管理员',
            password:randomBytes(24).toString('hex'),setup_code:process.env.WORKFLOW_SETUP_TOKEN}
    });
    assert.equal(auth.status(),200);
    const page = ctx.pages()[0];
    await page.goto(url.href);
    await page.locator('#connectionBadge.live').waitFor();
    cdp = await ctx.newCDPSession(page);
    const {errors, data} = await cdp.send('Page.getAppManifest');
    assert.deepEqual(errors, []);
    const parsed = JSON.parse(data);
    assert.equal(parsed.display,'standalone');
    for (const icon of parsed.icons) {
      const res = await ctx.request.get(new URL(icon.src,url).href);
      assert.equal(res.status(),200);
      const png = await res.body();
      assert.equal(png.subarray(1,4).toString(),'PNG');
      assert.equal(png.readUInt32BE(16)+'x'+png.readUInt32BE(20),icon.sizes);
    }
    const manifestId = new URL(parsed.id,url).href;
    assert.deepEqual((await cdp.send('Page.getInstallabilityErrors')).installabilityErrors, []);
    await cdp.send('PWA.install',{manifestId,installUrlOrBundleUrl:url.href});
    installed = true;
    // The DevTools installation is unattended; set the same “open in a window” preference as a desktop install.
    await cdp.send('PWA.changeAppUserSettings',{manifestId,displayMode:'standalone'});
    const launch = ctx.waitForEvent('page');
    await cdp.send('PWA.launch',{manifestId});
    const app = await launch;
    await app.waitForURL('**/?launch=workspace');
    const child=await ctx.newCDPSession(app);
    assert.notEqual((await child.send('Browser.getWindowForTarget')).windowId,(await cdp.send('Browser.getWindowForTarget')).windowId);
    await child.detach();
    await app.locator('#connectionBadge.live').waitFor();
    await app.waitForFunction(()=>matchMedia('(display-mode: standalone)').matches,null,{timeout:3500});
    assert.equal(await app.locator('#workspaceInstallButton').isVisible(),false);
    assert.equal(await app.locator('#pageTitle').isVisible(),true);
    await app.locator('button.nav-item[data-view="finance"]').click();
    const protectedRead = await app.request.get(new URL('api/erp-settlements/statements',url).href);
    assert.equal(protectedRead.status(),200);
    assert.equal(protectedRead.headers()['cache-control'],'no-store');
    assert.equal((await app.request.get(new URL('api/workspace',url).href)).headers()['cache-control'],'no-store');
    assert.deepEqual(await app.evaluate(async()=>({workers:(await navigator.serviceWorker.getRegistrations()).length,
                                                  caches:await window.caches.keys()})), {workers:0,caches:[]});
    await app.route('**/api/workspace',route=>route.fulfill({status:404,body:''}));
    await app.reload();
    await app.getByText('数据服务异常',{exact:true}).waitFor();
    assert.equal(await app.locator('#connectionBadge.live').count(),0);
    const demo=await ctx.newPage();
    await demo.route('**/api/workspace',route=>route.fulfill({status:404,body:''}));
    await demo.goto(url.href);
    await demo.getByText('示例体验模式',{exact:true}).waitFor();
    assert.equal(await demo.locator('link[rel=manifest]').count(),0);
    console.log('Chromium 正式工作区完成安装及独立窗口启动；财务响应 no-store、演示来源和断连提示通过。');
    await demo.close();
  } finally {
    if(installed)await cdp.send('PWA.uninstall',{manifestId:url.href}).catch(()=>{});
    if(ctx)await ctx.close();
    fs.rmSync(profile,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
