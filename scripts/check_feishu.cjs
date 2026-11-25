'use strict';
const assert=require('node:assert/strict');
const {randomBytes,randomUUID}=require('node:crypto');
const {chromium}=require('playwright');
const origin=process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(origin).hostname,'127.0.0.1');
async function run(){
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const owner=await browser.newContext(),reviewer=await browser.newContext(),outsider=await browser.newContext();
    const api=async(ctx,method,p,body,status=200)=>{
      const r=await ctx.request.fetch(origin+p,{method,headers:{'X-Workflow-Request':'1'},data:body});
      assert.equal(r.status(),status,await r.text());return r.json();
    };
    const password=randomBytes(20).toString('hex'),post=(ctx,path,b,s)=>api(ctx,'POST',path,b,s);
    await post(owner,'/api/workflow-auth/setup',{email:'feishu-owner@local.example',password,display_name:'飞书验收经办人',setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const page=await owner.newPage();page.setDefaultTimeout(7000);
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin+'/#overview');await page.locator('[data-feishu-open]:visible').first().click();
    const panel=page.locator('#wbFeishuSync');
    if(process.env.FEISHU_BROWSER_CASE==='unconfigured'){
      await panel.getByText(/请由管理员在服务端配置/).waitFor();
      assert.equal(await panel.locator('a').count(),0);
      const exported=[];
      await page.route('**/api/**',route=>{
        if(route.request().url().includes('/workbench/feishu'))exported.push(route.request().method());
        return route.fulfill({status:404,contentType:'application/json',body:'{}'});
      });
      await page.reload();await page.locator('[data-feishu-open]:visible').first().click();
      await panel.getByText(/本机示例工作单保存在当前浏览器/).waitFor();
      assert.deepEqual(exported,[]);assert.equal(await panel.locator('[data-fs=sync]').count(),0);
      console.log('未配置服务器与纯静态模式保留飞书总表入口，并给出配置方式；静态入口没有发起飞书请求。');
      return;
    }
    await panel.locator('[data-fs-table]').waitFor();
    assert.match(await panel.locator('[data-fs-table]').getAttribute('href'),/fixture_BASE_TOKEN/);
    await panel.locator('[data-fs=close]').click();
    const member=async(email,name)=>post(owner,'/api/workflow-auth/members',{email,password,display_name:name,department:'运营'},201);
    const peer=await member('feishu-peer@local.example','飞书验收审核人');
    await member('feishu-other@local.example','其他人');
    await post(reviewer,'/api/workflow-auth/login',{email:'feishu-peer@local.example',password});
    await post(outsider,'/api/workflow-auth/login',{email:'feishu-other@local.example',password});
    const product=await post(owner,'/api/products',{model:'FEISHU-UI-TEST',name:'同步验收型号',marketplace:'US'},201);
    const root='/api/workbench/items';
    const item=await post(owner,root,{product_id:product.id,page_id:'ads',section_id:'change',site:'US',object_ref:'同步测试对象',title:'飞书验收工作单',reviewer_id:peer.id,request_id:randomUUID(),
      source_note:'工作台私人原文，不进入飞书总表',
      values:{entity_id:'profile:123/campaign:31',change_kind:'调整预算',previous_setting:'每日20 USD',proposed_setting:'每日22 USD',change_basis:'以成熟报告复核',stop_condition:'达到止损线即停',affected_reach:'仅本活动'}},201);
    async function openItem(){
      await page.goto(origin+'/#ads');await page.reload();await page.locator('#productSelect').selectOption(product.id);
      const row=page.locator('[data-wb-id="'+item.id+'"]');await row.click();
      await page.locator('[data-feishu-item="'+item.id+'"]').click();
    }
    await openItem();
    await panel.getByText(/草稿保留在工作台/).waitFor();
    assert.equal(await panel.locator('[data-fs=sync]').count(),0);
    const submitted=await post(owner,root+'/'+item.id+'/actions',{action:'submit',expected_version:item.version,request_id:randomUUID()});
    await panel.locator('[data-fs=refresh]').click();await panel.getByRole('button',{name:'确认同步此单'}).click();
    await panel.locator('[data-fs-result=failed]').waitFor();assert.equal(await panel.locator('[data-fs-record]').count(),0);
    await panel.getByRole('button',{name:'重试同步'}).click();await panel.locator('[data-fs-result=synced]').waitFor();
    const record=await panel.locator('[data-fs-record]').getAttribute('href');
    assert.match(record,/record=recBrowserFixture/);
    assert(!await panel.innerText().then(t=>t.includes('工作台私人原文')));
    const approved=await post(reviewer,root+'/'+item.id+'/actions',{action:'approve',expected_version:submitted.version,request_id:randomUUID()});
    await openItem();await panel.locator('[data-fs-result=stale]').waitFor();
    await panel.getByRole('button',{name:'更新飞书原记录'}).click();
    await panel.locator('[data-fs-result=synced]').waitFor();
    assert.equal(await panel.locator('[data-fs-record]').getAttribute('href'),record);
    const recheck=page.waitForResponse(r=>r.url().endsWith('/sync') && r.request().method()==='POST');
    await panel.getByRole('button',{name:'再次同步并核对'}).click();
    assert.equal((await (await recheck).json()).status,'synced');
    assert.equal(await panel.locator('[data-fs-record]').getAttribute('href'),record);
    const fb='/api/workbench/feishu/'+item.id;
    assert.equal((await api(owner,'GET',fb)).version,approved.version);
    await api(outsider,'GET',fb,undefined,404);await post(outsider,fb+'/sync',{},404);
    assert.deepEqual(errors,[]);
    console.log('飞书总表配置、工作单入口、草稿阻断、丢失回执重试不重复、版本更新同一原记录、私有原文和第三人权限均通过（外部飞书模拟）。');
  }finally{if(browser.isConnected())await browser.close();}
}
run().catch(err=>{console.error(err);process.exitCode=1;});
