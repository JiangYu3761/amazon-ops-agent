/* A work item is sent only after its operator explicitly confirms this panel. */
(() => {
  const base = '/api/workbench/feishu';
  const labels = {
    unconfigured:'尚未配置', not_synced:'尚未同步', synced:'飞书已确认',
    stale:'原单已更新', failed:'同步失败'
  };
  let dialog, active, request = 0;
  function el(tag, value, className) {
    const node = document.createElement(tag);
    if(value !== undefined) node.textContent = value;
    if(className) node.className = className;
    return node;
  }
  function button(text,action,primary=false) {
    const b=el('button',text,primary?'btn primary':'btn');
    b.type='button'; b.dataset.fs=action;
    return b;
  }
  function link(label,href) {
    try {
      const target=new URL(href);
      if(target.protocol!=='https:' || target.username || target.password) return null;
      const a=el('a',label,'btn');
      a.href=target.href; a.target='_blank'; a.rel='noopener noreferrer'; return a;
    } catch { return null; }
  }
  function show(state, busy=false) {
    if(state!==active || !dialog.open) return;
    const head=el('header',undefined,'wb-dialog-head');
    const heading=el('h2',state.id?'同步此工作单':'飞书同步总表');
    heading.id='wbFeishuTitle'; head.append(heading,button('关闭','close'));
    const body=el('div',undefined,'wb-dialog-body'), actions=el('footer');
    const status=el('p',state.notice || '', 'wb-message');
    status.setAttribute('role',state.error?'alert':'status'); status.dataset.fsStatus='';
    if(state.id)body.append(el('strong',state.title));
    const data=state.data;
    if(data){
      if(state.id){
        const text=labels[data.status] || '请刷新同步状态';
        const line=el('p',text);line.dataset.fsResult=data.status;body.append(line);
        if(data.message)body.append(el('p',data.message));
        if(data.version!=null){
          const date=data.last_synced_at?new Date(data.last_synced_at):null;
          const time=date && !Number.isNaN(date.getTime())?' · '+date.toLocaleString('zh-CN'):'';
          body.append(el('p','上次确认工作单版本 '+data.version+time));
        }
      }else{
        body.append(el('p',data.configured?'总表连接已配置。打开一张已提交的工作单，点击「同步到飞书总表」可查看并发送该单。':'请由管理员在服务端配置飞书应用与多维表，再点刷新。'));
      }
      const cols=Array.isArray(data.fields)&&data.fields.every(f=>typeof f==='string')?data.fields:
        ['本地ID','类型','事项','产品','负责人','状态','板块'];
      body.append(el('p','同步字段　'+cols.join('、')));
      if(data.record_url){
        const a=link('打开飞书原记录',data.record_url);if(a){a.dataset.fsRecord='';actions.append(a);}
      }
      if(data.table_url){
        const a=link('打开飞书总表',data.table_url);if(a){a.dataset.fsTable='';actions.append(a);}
      }
      if(state.id && data.configured && data.can_sync && ['not_synced','stale','failed','synced'].includes(data.status)){
        const copy={not_synced:'确认同步此单',stale:'更新飞书原记录',failed:'重试同步',synced:'再次同步并核对'};
        body.append(el('p','点击同步后，工作台将按此单资料更新飞书记录的上述字段，并向飞书核对结果。'));
        const b=button(copy[data.status],'sync',true);b.disabled=busy;actions.append(b);
      }
    }
    body.append(status);
    if(state.server && state.user){
      const refresh=button('刷新状态','refresh');refresh.disabled=busy;actions.append(refresh);
    }else if(state.server){actions.append(button('登录','login',true));}
    dialog.replaceChildren(head,body,actions);
  }
  async function load(state,send=false) {
    if(state!==active || state.busy) return;
    const seq=++request;
    state.busy=true; state.error=false;
    state.notice=send?'正在向飞书提交并核对回执…':'正在读取连接状态…';
    show(state,true);
    const path=base+(state.id?'/'+encodeURIComponent(state.id):'');
    try {
      const data=await api(path+(send?'/sync':''),send?{method:'POST'}:{});
      if(seq!==request || state!==active) return;
      state.data=data; state.notice='';state.error=data.status==='failed';
    }catch(error){
      if(seq!==request || state!==active)return;
      state.error=true;
      state.notice=send?'尚未取得本次同步确认。请先刷新状态。'+error.message:error.message;
      if(send || error.status===403 || error.status===404 || error.status===401)state.data=null;
      if(error.status===401)state.user=null;
    }finally{
      if(seq===request && state===active){state.busy=false;show(state);}
    }
  }
  function open(trigger) {
    if(!dialog){
      dialog=el('dialog',undefined,'wb-dialog');
      dialog.id='wbFeishuSync';dialog.setAttribute('aria-labelledby','wbFeishuTitle');
      document.body.append(dialog);
      dialog.addEventListener('close',()=>{active=null;++request;});
      dialog.addEventListener('click',event=>{
        const action=event.target.closest('[data-fs]')?.dataset.fs;
        if(action==='close')dialog.close();
        if(action==='login'){dialog.close();window.processUI?.login();}
        if(action==='refresh' && active)load(active);
        if(action==='sync' && active)load(active,true);
      });
    }
    const state={id:trigger.dataset.feishuItem||'',title:trigger.dataset.feishuTitle||'',
      server:mode==='server',user:window.processUI?.currentUser(),busy:false,notice:'',data:null};
    active=state;dialog.showModal();
    if(!state.server){
      state.notice='本机示例工作单保存在当前浏览器。连接团队服务器并登录后，可同步已提交的工作单。';show(state);
    }else if(!state.user){state.notice='请登录工作台后查看飞书连接。';show(state);}
    else load(state);
  }
  document.addEventListener('click',event=>{
    const trigger=event.target.closest('[data-feishu-open]');
    if(trigger)open(trigger);
  });
  window.addEventListener('homa-session',()=>{if(dialog?.open)dialog.close();});
})();
