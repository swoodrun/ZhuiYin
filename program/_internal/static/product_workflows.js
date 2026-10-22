(() => {
  'use strict';
  const $=id=>document.getElementById(id), api=window.Zhuiyin;
  if(!api)return;
  let templates, comparison, activeTemplate=null, applying=false, historyRevision=0;
  const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!=null)e.textContent=String(text);if(cls)e.className=cls;return e;};
  const button=(text,action)=>{const e=node('button',text,'button button-light button-small');e.type='button';e.addEventListener('click',action);return e;};
  const clone=value=>value==null?null:JSON.parse(JSON.stringify(value));
  const strategyNames={baseline:'原始',fixed:'幂等＋核验',verified:'幂等＋核验',idempotency_only:'仅幂等',verification_only:'仅核验'};
  function selectTemplate(value){
    activeTemplate=clone(value);
    $('active-template-note').textContent=value?'本次绑定：'+value.name+' · 第 '+value.version+' 版；运行报告会保存模板及独立业务规则检查。':'未绑定任务模板；可以保存当前参数，或载入已有模板。';
  }
  function detach(){if(activeTemplate){selectTemplate(null);templates?.clear?.();}}
  function readConfig(){
    const value=api.getConfig({rulesOnly:true});
    delete value.template_snapshot;
    return {...value,agent_mode:'llm',...api.getBillingSettings()};
  }
  function applyConfig(value,label='历史任务配置'){
    if(api.isBusy())throw new Error('请先等待当前测试完成或停止测试。');
    if(value.agent_mode==='rules'&&value.template_snapshot){
      value={...value,max_requests:value.template_snapshot.config.max_requests,
        billing:value.template_snapshot.config.billing||{}};
      label+='（已恢复模板原定预算）';
    }
    applying=true;
    try{
      if(!api.applyScenario(value,label))return false;
      $('max-requests').value=String(value.max_requests??400);
      const billing=value.billing||{};
      for(const [id,key]of [['billing-max-cost','max_cost_cny'],['billing-cache-hit-price','input_cache_hit_price_per_million'],['billing-cache-miss-price','input_cache_miss_price_per_million'],['billing-output-price','output_price_per_million']])$(id).value=billing[key]==null?'':String(billing[key]);
      $('max-requests').dispatchEvent(new Event('change',{bubbles:true}));
      selectTemplate(value.template_snapshot||null);
      templates?.setSnapshot?.(value.template_snapshot||null);
      return true;
    }finally{applying=false;}
  }
  api.readReusableConfig=readConfig;api.applyReusableConfig=applyConfig;
  window.ZhuiyinProduct={getTemplateSnapshot:()=>clone(activeTemplate)};
  templates=window.ZhuiyinTemplates?.mount($('task-templates-host'),{readConfig,applyConfig,isBusy:api.isBusy,onSelect:selectTemplate});
  comparison=window.ZhuiyinCompare?.mount($('history-compare-host'));
  $('experiment-form').addEventListener('input',event=>{
    if(!applying&&!['base-url','model','api-key','agent-variant'].includes(event.target.id))detach();
  });
  $('experiment-form').addEventListener('change',event=>{
    if(!applying&&!['base-url','model','api-key','agent-variant'].includes(event.target.id))detach();
  });
  $('reset-btn').addEventListener('click',detach);
  window.addEventListener('zhuiyin:config',()=>{if(!applying)detach();});
  window.addEventListener('zhuiyin:busy',()=>{
    if(api.isBusy())$('diagnostics-workspace').classList.add('hidden');
    if(!api.isBusy())comparison?.refresh?.();
    document.querySelectorAll('[data-product-run-control]').forEach(e=>e.disabled=api.isBusy());
  });
  function contractView(host,report){
    if(!report?.template_snapshot)return;
    const box=node('details',null,'product-panel');box.append(node('summary','本次任务模板与业务规则检查'));
    box.append(node('p',report.template_snapshot.name+' · 第 '+report.template_snapshot.version+' 版。此检查单列，不改变原数据库裁判。'));
    const checks=[];
    for(const [name,value]of [['原始',report.baseline],['幂等＋核验',report.fixed]])if(value?.template_contract)checks.push({name,...value.template_contract});
    for(const [i,value]of (report.samples||[]).entries())if(value?.template_contract)checks.push({name:'第 '+(i+1)+' 轮',...value.template_contract});
    for(const item of checks){const row=node('details',null,'product-contract');row.append(node('summary',item.name+'：'+({pass:'符合规则',fail:'不符合规则',unknown:'证据不足，无法判断'}[item.status]||'未记录')),node('pre',JSON.stringify(item.checks,null,2)));box.append(row);}
    const raw=node('details');raw.append(node('summary','查看当时的模板快照'),node('pre',JSON.stringify(report.template_snapshot,null,2)));box.append(raw);host.append(box);
  }
  function diagnostics(host,report){host.replaceChildren();window.ZhuiyinDiagnostics?.mount(host,report);contractView(host,report);}
  window.addEventListener('zhuiyin:result',event=>{
    const host=$('diagnostics-workspace');$('result-overview').after(host);host.classList.remove('hidden');diagnostics(host,event.detail);
  });
  document.querySelectorAll('[data-workbench-home]').forEach(e=>e.addEventListener('click',()=>{$('diagnostics-workspace').classList.add('hidden');}));
  window.addEventListener('zhuiyin:history',event=>{
    const job=event.detail, revision=++historyRevision, host=node('section',null,'product-history-actions');
    const report=job.result||job.partial_result;
    $('history-detail-content').prepend(host);
    if(report){const diag=node('details',null,'product-panel');diag.append(node('summary','打开诊断工作台'));const body=node('div');diag.append(body);let drawn=false;diag.addEventListener('toggle',()=>{if(diag.open&&!drawn){drawn=true;diagnostics(body,report);}});host.append(diag);}
    const controls=node('div',null,'product-action-row'),status=node('p','正在读取可复用配置…','subtle-label');host.append(controls,status);
    api.request('/api/runs/'+encodeURIComponent(job.run_id)+'/retest-plan').then(plan=>{
      if(revision!==historyRevision||!host.isConnected)return;
      status.textContent='复制配置和生成重测预览不会执行任务；开始测试时才使用当前工作台的模型、指令与预算。';
      if(plan.config&&Object.keys(plan.config).length){const copy=button('复制配置到工作台',()=>{try{applyConfig(plan.config,job.label||job.run_id);}catch(error){status.textContent=error.message;}});copy.dataset.productRunControl='true';copy.disabled=api.isBusy();controls.append(copy);}
      const eligible=(plan.candidates||[]).filter(item=>item.eligible);
      if(!eligible.length){status.append(node('span',' 这份记录没有可重建的业务任务。'));return;}
      const picker=node('details',null,'product-panel');picker.append(node('summary','选择失败或需关注的任务重测'));
      picker.append(node('p','同一任务的不同策略和重复轮次会合并为一个任务，在全新环境中运行四种防护配置。','subtle-label'));
      const choices=node('div',null,'product-retest-choices'),inputs=[];
      const renderItems=page=>{choices.replaceChildren();for(const item of page){const label=node('label',null,'product-retest-option'),input=node('input');input.type='checkbox';input.value=item.candidate_id;input.checked=Boolean(item.failed||item.attention);input.dataset.productRunControl='true';input.disabled=api.isBusy();inputs.push(input);label.append(input,node('span',(item.name||item.label||((item.config?.request_id||'任务')+' · '+(item.config?.scenario==='ticket'?item.config.ticket_title:item.config?.equipment||item.source||item.candidate_id)))+' · '+(item.failed?'有失败':item.attention?'需关注':'无已知失败')+' · '+(strategyNames[item.strategy]||(item.strategy?'其他策略':'任务'))));choices.append(label);}};
      // Keep every selection in the DOM; cap the server plan at its explicit limit.
      renderItems(eligible.slice(0,500));picker.append(choices);
      const prepare=button('生成选中任务的重测预览',async()=>{
        try{
          prepare.disabled=true;const chosen=inputs.filter(e=>e.checked).map(e=>e.value);if(!chosen.length)throw new Error('请先选择需要重测的任务。');
          const preview=await api.post('/api/retest/prepare',{run_id:job.run_id,candidate_ids:chosen});
          if(!window.ZhuiyinTestCenter?.showPrepared)throw new Error('批量测试页面尚未就绪，请刷新后重试。');
          window.ZhuiyinTestCenter.showPrepared(preview);status.textContent='重测预览已生成；尚未执行。';
        }catch(error){status.textContent=error.message;}finally{prepare.disabled=api.isBusy();}
      });prepare.dataset.productRunControl='true';prepare.disabled=api.isBusy();picker.append(prepare);host.append(picker);
    }).catch(error=>{if(revision===historyRevision)status.textContent=error.message;});
  });
  window.addEventListener('zhuiyin:page',event=>{if(event.detail?.page!=='history')historyRevision++;});
})();
