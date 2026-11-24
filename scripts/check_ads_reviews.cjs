'use strict';
// Run via scripts/check_workbench.py against an isolated local PostgreSQL.
const assert=require('node:assert/strict');
const {randomUUID,randomBytes}=require('node:crypto');
const {chromium}=require('playwright');
const origin=process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(origin).hostname,'127.0.0.1');
const headers={'X-Workflow-Request':'1'};
async function main(){
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const view={viewport:{width:1440,height:1000},locale:'zh-CN'};
    const owner=await browser.newContext(view), peer=await browser.newContext(view), anon=await browser.newContext(view);
    const api=async(ctx,method,path,body,status)=>{
      const response=await ctx.request.fetch(origin+path,{method,headers,data:body});
      const payload=await response.text();
      assert.equal(response.status(),status,path+' '+payload.slice(0,350));return payload?JSON.parse(payload):null;
    };
    const post=(c,p,b,s=200)=>api(c,'POST',p,b,s);
    const password=randomBytes(16).toString('hex');
    await post(owner,'/api/workflow-auth/setup',{email:'owner@ads.example',display_name:'广告经办',password,setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const reviewer=await post(owner,'/api/workflow-auth/members',{email:'reviewer@ads.example',display_name:'独立复核',department:'广告',password},201);
    const other=await browser.newContext(view);
    await post(owner,'/api/workflow-auth/members',{email:'outside@ads.example',display_name:'其他成员',department:'测试',password},201);
    await post(other,'/api/workflow-auth/login',{email:'outside@ads.example',password});
    await post(peer,'/api/workflow-auth/login',{email:'reviewer@ads.example',password});
    const product=await post(owner,'/api/products',{model:'T-AD-7',name:'测试投放商品',marketplace:'US',sku:'T-AD-7'},201);
    await post(owner,'/api/integrations/connections',{name:'隔离演练',provider:'ads_sp',account_key:'demo-store',
      ads_profile_id:'123',enabled:false,mappings:[{product_id:product.id,sku:'T-AD-7'}]},201);
    const title='预算调整 / 商品测试',wb='/api/workbench/items';
    const newItem=(name,parent_id)=>({product_id:product.id,page_id:'ads',section_id:'change',site:'US',object_ref:'SP-Campaign-31',title:name,
      reviewer_id:reviewer.id,request_id:randomUUID(),...(parent_id?{parent_id}:{}),
      values:{entity_id:'profile:123/campaign:31',change_kind:'调整预算',previous_setting:'每日 20 USD',proposed_setting:'每日 22 USD',change_basis:'以真实七日成熟报告复核',
              stop_condition:'预算触顶即停并复查',affected_reach:'仅本活动'}});
    let source=await post(owner,wb,newItem(title),201);
    const act=async(ctx,action,more={})=>{source=await post(ctx,wb+'/'+source.id+'/actions',{action,expected_version:source.version,request_id:randomUUID(),...more});};
    await act(owner,'submit');await act(peer,'approve');
    const page=await owner.newPage(), independent=await peer.newPage(), loggedOut=await anon.newPage();
    const errors=[];for(const p of [page,independent,loggedOut])p.on('pageerror',e=>errors.push(e.message));
    async function visit(p){
      await p.goto(origin+'/#ads');await p.locator('#productSelect').selectOption(product.id);
      await p.locator('[data-wb-section="verification"]').click();
      return p.locator('#adsReviewRoot');
    }
    const anonymous=await visit(loggedOut);await anonymous.locator('[data-ar="login"]').waitFor();
    const panel=await visit(page), second=await visit(independent);
    const btn=(n,a,id)=>n.locator('[data-ar="'+a+'"]'+(id?'[data-id="'+id+'"]':''));
    const field=(f,name)=>f.locator('[name="'+name+'"]');
    const submit=(f)=>f.locator('button[type="submit"]').click();
    await panel.getByText('当前产品没有待评复盘。').waitFor();
    await btn(panel,'lane','sources').first().click();await btn(panel,'source',source.id).click();
    const f=panel.locator('form[data-ar-form="plan"]'), day=new Intl.DateTimeFormat('sv-SE',{timeZone:'America/Los_Angeles'}).format(new Date());
    for(const [k,v] of Object.entries({expected:'恢复正常曝光',baseline:'22 次点击，来自观察记录',evidence:'模拟报告 A-31',
      review_date:day,observation_end:day,attribution_days:'0'}))await field(f,k).fill(v);
    await field(f,'metric_basis').selectOption('traffic');await field(f,'control_method').selectOption('randomized');await field(f,'control_evidence').fill('按预定时段随机分配，留存分组记录');
    await panel.locator('[data-ar-prior]').getByText(/没有.*前案/).waitFor();
    await submit(f);await panel.getByText('事前计划已登记。').waitFor();
    assert.equal(await f.count(),0);
    await act(owner,'execute',{evidence:'投放后台测试回执 A-31'});
    await btn(panel,'refresh').click();const start=panel.locator('form[data-ar-form="start"]');await start.waitFor();
    await page.route('**/ads-reviews/sources/*/start',async route=>{
      await route.fetch();await route.abort('failed');
    },{times:1});
    await submit(start);await panel.locator('.ar-error').waitFor();
    await submit(start);await panel.getByText('复盘已启动。').waitFor();
    await panel.getByText(/等待 独立复核 提交/).waitFor();
    const B='/api/products/'+product.id+'/ads-reviews',list=await api(owner,'GET',B,undefined,200);
    assert.equal(list.items.length,1);const id=list.items[0].id;
    assert.deepEqual((await api(other,'GET',B,undefined,200)).items,[]);
    await api(other,'GET',B+'/'+id,undefined,404);
    await page.reload();await panel.getByText(/等待 独立复核 提交/).waitFor();
    await btn(second,'refresh').click();await btn(second,'detail',id).click();
    const assess=second.locator('form[data-ar-form="assess"]');await assess.waitFor();
    await field(assess,'incremental').waitFor();
    for(const [k,v] of Object.entries({actual:'成熟测试报告显示同一活动变化',interference:'两组同时段且未逢促销或价格变更',
      guardrails:'预算及库存已复核',evidence:'复核凭证 R-31'}))await field(assess,k).fill(v);
    await field(assess,'as_of').fill(day);await field(assess,'outcome').selectOption('effective');
    assert.equal(await field(assess,'outcome').evaluate(e=>e.checkValidity()),false);
    await field(assess,'refreshed_on').fill(day);await field(assess,'material_interference').selectOption('none');await field(assess,'confidence').selectOption('high');
    await field(assess,'execution_ok').check();await field(assess,'guardrail_ok').check();
    await field(assess,'incremental').check();await field(assess,'effect_direction').selectOption('positive');
    await field(assess,'control_result').fill('冻结分组与回执对照，实验组点击率更高。');
    await submit(assess);await second.getByText('独立评估已保存。').waitFor();
    const child=await post(owner,wb,newItem('下次核对边际花费',source.id),201);
    await second.locator('summary').filter({hasText:'后续办理'}).click();await btn(second,'linkOptions').click();
    const link=second.locator('form[data-ar-form="links"]');await field(link,'item_id').selectOption(child.id);await submit(link);
    await second.getByText('后续子单已关联。').waitFor();
    // A new source reviews the signed earlier case, with human opt-in to the calibration.
    const follow=await post(owner,wb,newItem('同对象预算复验'),201);
    const advance=async(ctx,item,action,more={})=>post(ctx,wb+'/'+item.id+'/actions',{action,expected_version:item.version,request_id:randomUUID(),...more});
    const pending=await advance(owner,follow,'submit');const approved=await advance(peer,pending,'approve');
    await btn(panel,'back').click();await btn(panel,'lane','sources').first().click();
    await btn(panel,'source',approved.id).click();const candidate=panel.locator('[data-ar-calibration]');await candidate.waitFor();
    assert.equal(await candidate.isChecked(),false);await candidate.check();
    const nextPlan=panel.locator('[data-ar-form="plan"]');
    assert.equal(await field(nextPlan,'metric_basis').inputValue(),'traffic');
    assert.equal(await field(nextPlan,'attribution_days').inputValue(),'0');
    assert.equal(await field(nextPlan,'control_method').inputValue(),'randomized');
    for(const [k,v] of Object.entries({expected:'复验点击表现',baseline:'同指标稳定基线',evidence:'候选试验报告 C31',
      review_date:day,observation_end:day,control_evidence:'独立预登记对照组'}))await field(nextPlan,k).fill(v);
    await submit(nextPlan);await panel.getByText('事前计划已登记。').waitFor();
    await advance(owner,approved,'execute',{evidence:'执行新试验的回执 B-32'});
    await btn(panel,'refresh').click();await submit(panel.locator('[data-ar-form="start"]'));await panel.getByText('复盘已启动。').waitFor();
    const nextId=(await api(owner,'GET',B+'?status=open',undefined,200)).items.find(r=>r.source_id===approved.id).id;
    const historyCheck=await api(owner,'GET',B+'/'+nextId,undefined,200);
    assert.equal(historyCheck.plan.feedback.applied,true);
    await btn(second,'back').click();await btn(second,'lane','open').click();await btn(second,'detail',nextId).click();
    const negative=second.locator('[data-ar-form="assess"]');await negative.waitFor();
    for(const [k,v] of Object.entries({as_of:day,refreshed_on:day,actual:'复验表现下降',interference:'无重大干扰',
      guardrails:'超过本次止损线',evidence:'双组复核报告 B-32'}))await field(negative,k).fill(v);
    await field(negative,'material_interference').selectOption('none');await field(negative,'confidence').selectOption('high');
    await field(negative,'outcome').selectOption('ineffective');await field(negative,'execution_ok').check();
    await field(negative,'incremental').check();await field(negative,'effect_direction').selectOption('negative');
    await field(negative,'control_result').fill('随机对照复核显示负向，已登记止损');
    await submit(negative);await second.getByText('独立评估已保存。').waitFor();
    const closedNegative=await api(owner,'GET',B+'/'+nextId,undefined,200);
    assert.equal(closedNegative.assessment.effect_direction,'negative');
    assert.equal(closedNegative.disposition,'owner_pending');
    await second.getByText('等待原经办人安排后续处置').waitFor();
    await btn(second,'back').click();await btn(second,'lane','closed').click();await btn(second,'detail',id).click();
    await second.locator('summary').filter({hasText:'重开复盘'}).click();
    const reopen=second.locator('form[data-ar-form="reopen"]');await field(reopen,'reason').fill('获得后补报告，需要独立复评');await submit(reopen);
    await second.getByText('复盘已重开，历史版本已保留。').waitFor();
    await second.locator('summary').filter({hasText:'办理历史'}).click();
    await second.getByText(/重开原因.*获得后补报告/).waitFor();
    await btn(panel,'refresh').click();await panel.locator('[data-ar-prior]').getByText(/已(?:有)?修订|重新核对/).waitFor();
    await btn(second,'back').click();await btn(second,'lane','period').click();
    const period=second.locator('[data-ar-period]');await period.locator('[name="period"]').selectOption('month');
    const monthResponse=independent.waitForResponse(r=>r.url().includes('/ads-review-periods?')&&new URL(r.url()).searchParams.get('period')==='month');
    await submit(period);assert.equal((await monthResponse).status(),200);
    await second.locator('.ar-rollups dd').first().waitFor();
    await second.getByText(/原经办人待处置/).first().waitFor();
    assert.equal(await period.locator('[name="period"]').inputValue(),'month');
    assert.match(await second.locator('.ar-detail .wb-context').first().textContent(),/\d{4}-\d{2}-01 至/);
    if(process.env.ADS_REVIEW_SCREENSHOT){
      await second.locator('#ar-title').scrollIntoViewIfNeeded();
      await independent.screenshot({path:process.env.ADS_REVIEW_SCREENSHOT});
    }
    await independent.setViewportSize({width:390,height:844});
    assert(await second.locator('select[name="period"]').isVisible());
    assert(await independent.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'复盘不应撑开移动端页面');
    assert.deepEqual(errors,[]);
    console.log('实际 PG + Chromium：经办/独立复核/匿名、计划与来源、未知结果重试、刷新恢复、观察结论、关联跟进、重开和周期、校准候选与负向实验通过。');
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
