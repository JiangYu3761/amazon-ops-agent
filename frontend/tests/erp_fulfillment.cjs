const assert = require('node:assert/strict');
const {randomBytes, randomUUID} = require('node:crypto');
const {chromium} = require('playwright');

async function run(){
  const origin=process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(origin).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const ctx=await browser.newContext();
    const post=async(path,data)=>{
      const res=await ctx.request.post(origin+path,{headers:{'X-Workflow-Request':'1'},data});
      assert.ok(res.ok(),await res.text());return res.json();
    };
    await post('/api/workflow-auth/setup',{email:'fulfill@local.example',display_name:'仓管验收',password:randomBytes(20).toString('hex'),setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    await post('/api/erp-receiving/scopes',{scope_id:undefined,request_id:randomUUID(),account_ref:'DEMO',site:'US',warehouse:'第三方仓 A',business_timezone:'America/Los_Angeles',member_ids:[]});
    const page=await ctx.newPage();page.setDefaultTimeout(6000);
    await page.goto(origin+'/#service');
    await page.locator('[data-erp-fulfillment-open]').click();
    const desk=page.getByRole('dialog',{name:'自发货履约'});
    await desk.getByRole('button',{name:'登记订单'}).click();
    await desk.getByLabel('订单编号',{exact:true}).fill('PO-FBM-100');
    await desk.getByLabel('订单来源凭证').fill('MO-7788');
    await desk.locator('[name=origin]').selectOption('manual');
    await desk.getByLabel('来源订单行号').fill('L1');
    await desk.getByLabel('SKU',{exact:true}).fill('ITEM-A');
    await desk.getByLabel('购买数量').fill('3');
    await desk.getByRole('button',{name:'保存订单'}).click();
    for(const [ref,qty] of [['P1','1'],['P2','2']]){
      await desk.getByText('分配包裹',{exact:true}).click();
      await desk.getByLabel('包裹编号').fill(ref);
      await desk.locator('[data-allocation]').fill(qty);
      await desk.getByRole('button',{name:'保存包裹'}).click();
      await desk.getByRole('heading',{name:ref+' · 待审核'}).waitFor();
    }
    assert.equal(await desk.locator('option[value=release]').count(),0);
    await desk.getByText('确认收款',{exact:true}).click();
    await desk.getByLabel('收款凭证编号').fill('PAY-01');
    await desk.getByRole('button',{name:'登记收款凭证'}).click();
    async function action(ref,kind,evidence,expected){
      const item=desk.locator('section').filter({has:page.getByRole('heading',{name:new RegExp('^'+ref+' ·')})});
      await item.getByLabel('办理动作').selectOption(kind);
      await item.getByLabel('来源消息、仓库或承运商回执编号').fill(evidence);
      await item.getByRole('button',{name:'登记办理结果'}).click();
      await item.getByRole('heading',{name:ref+' · '+expected,exact:true}).waitFor();
    }
    await action('P1','release','WAVE-42','已放行，待仓库出库');
    await action('P1','dispatch','WMS-SHIP-42','已收到出库回执');
    await action('P2','buyer_cancel','BUYER-13','买家取消，暂停审核');
    await action('P1','buyer_cancel','BUYER-14','出库后取消，待核实包裹');
    await action('P2','confirm_cancel','WMS-HOLD-13','已取消');
    await action('P1','intercept_ok','CARRIER-STOP-42','已取消');
    await desk.getByRole('button',{name:'返回订单'}).click();
    await desk.getByLabel('订单号或包裹号').fill('P2');
    await desk.getByRole('button',{name:'查询'}).click();
    await desk.getByRole('button',{name:'办理',exact:true}).click();
    await desk.getByText(/办理流水（/).click();
    await desk.getByText('CARRIER-STOP-42',{exact:true}).waitFor();
    await desk.getByText('WMS-HOLD-13',{exact:true}).waitFor();
    assert.equal(await desk.getByRole('heading',{name:/^P[12] · 已取消$/}).count(),2);
    await page.reload();await page.locator('[data-erp-fulfillment-open]').click();
    await desk.getByRole('button',{name:'办理',exact:true}).click();
    await desk.getByRole('heading',{name:'P1 · 已取消'}).waitFor();
    console.log('真实履约通过：未付款、分包、按包裹截单、仓库回执、搜索及重载。');
  }finally{await browser.close();}
}
run().catch(err=>{console.error(err);process.exitCode=1;});
