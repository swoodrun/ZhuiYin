(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {baseline:'原始',idempotency_only:'仅幂等',verification_only:'仅核验',verified:'幂等＋核验'};
  let activeRun = null, reading = false, startedAt = 0;
  const api = window.Zhuiyin;
  if (!api || !$('concurrency-run')) return;
  const node = (tag, text, className) => {const e=document.createElement(tag);if(text!=null)e.textContent=String(text);if(className)e.className=className;return e;};
  const list = value => Array.isArray(value) ? value : [];
  function status(text, kind='') {$('concurrency-status').className='status-pill '+kind;$('concurrency-status').textContent=text;}
  function message(text, kind='info') {$('concurrency-message').className='import-message '+kind;$('concurrency-message').textContent=text;}
  function updateActions() {$('concurrency-run').disabled=api.isBusy()||reading;$('concurrency-resume').disabled=api.isBusy()||reading;}
  function render(data, runId, partial=false) {
    activeRun=runId;
    const cases=list(data.cases),wrap=node('div',null,'table-wrap'),table=node('table',null,'data-table');table.setAttribute('aria-label','并发重复提交对照');
    const head=node('thead'),headers=node('tr');['提交方式','策略','有效记录','数据库实际完成','业务检查','真实写入','去重次数','独立检查','记录'].forEach(text=>headers.append(node('th',text)));head.append(headers);table.append(head);
    const body=node('tbody');
    cases.forEach(item=>{
      const records=list(item.business_records).filter(row=>row.status==='active'),detail=node('details',null,'research-details');
      detail.append(node('summary','查看详情'));let loaded=false;
      detail.addEventListener('toggle',()=>{if(!detail.open||loaded)return;loaded=true;detail.append(node('pre',JSON.stringify({failure_fingerprint:item.failure_fingerprint,violations:item.violations,clients:item.client_results,events:item.events},null,2)));
        if(typeof item.database_url==='string'){const url=new URL(item.database_url,location.href);if(url.origin===location.origin&&url.pathname.startsWith('/api/runs/')){const link=node('a','下载数据库','text-link');link.href=url.href;detail.append(link);}}
      });
      const check=node('span',item.failed===true?'有违规':item.failed===false?'无违规':'未完成','status-pill '+(item.failed===true?'failed':item.failed===false?'success':'warn'));
      const cells=[item.schedule==='concurrent'?'同时读取后提交':'先后提交',labels[item.strategy]||item.strategy,records.length,item.actual_success===true?'是':item.actual_success===false?'否':'未知',check,item.stats?.physical_writes??'未知',item.stats?.idempotency_hits??'未知',item.stats?.verification_calls??'未知',detail];
      const row=node('tr');cells.forEach(value=>{const cell=node('td');if(value instanceof Node)cell.append(value);else cell.textContent=String(value);row.append(cell);});body.append(row);
    });
    table.append(body);wrap.append(table);
    const contents=[node('p','规则模式 · 两个客户端 · 同一请求','mode-tag'),wrap];
    const verification=cases.find(item=>item.schedule==='concurrent'&&item.strategy==='verification_only'),combined=cases.find(item=>item.schedule==='concurrent'&&item.strategy==='verified');
    if(verification&&combined)contents.push(node('p','本次同时提交：仅核验保留 '+list(verification.business_records).filter(r=>r.status==='active').length+' 条有效记录，幂等＋核验保留 '+list(combined.business_records).filter(r=>r.status==='active').length+' 条。请结合违规与数据库实际完成状态判断。'));
    const source=node('details',null,'source-detail');source.append(node('summary','本次配置与版本'),node('pre',JSON.stringify({config:data.config,provenance:data.provenance},null,2)));contents.push(source);
    $('concurrency-results').replaceChildren(...contents);
    window.ZhuiyinUI?.setLimitations('concurrency',list(data.limitations));
    $('concurrency-export').href='/api/runs/'+encodeURIComponent(runId)+'/export';$('concurrency-export').classList.remove('hidden');
    $('concurrency-progress-text').textContent='已保存 '+(data.completed??cases.length)+' / '+(data.planned??8)+' 组';
    status(partial?'部分结果':'完成',partial?'warn':'success');
  }
  async function poll() {
    reading=true;updateActions();
    while(activeRun){
      if(Date.now()-startedAt>15*60*1000)throw new Error('读取超时，可稍后恢复读取。');
      const path='/api/runs/'+encodeURIComponent(activeRun),state=await api.request(path+'?lite=1');
      let progress=Number(state.progress)||0;if(progress>0&&progress<=1)progress*=100;
      $('concurrency-progress').style.width=Math.min(100,Math.max(0,progress))+'%';
      const event=list(state.events).at(-1);$('concurrency-progress-text').textContent=typeof event==='string'?event:event?.message||'运行中';
      if(['done','error','interrupted'].includes(state.status)){
        const full=await api.request(path),saved=full.result||full.partial_result;
        if(saved?.kind==='concurrency')render(saved,activeRun,full.status!=='done'||saved.status!=='finished');
        else throw new Error(full.error||'没有可读取的并发结果。');
        if(full.status!=='done')message(full.error||'已停止，完成的组合已保存。','warning');
        $('concurrency-cancel').classList.add('hidden');$('concurrency-resume').classList.add('hidden');return;
      }
      await new Promise(resolve=>setTimeout(resolve,700));
    }
  }
  function failed(error) {status('读取中断','warn');message(error.message||'读取失败。','error');$('concurrency-resume').classList.toggle('hidden',!activeRun);}
  async function start() {
    if(api.isBusy()||reading)return;
    let config;try{config=api.getConfig({rulesOnly:true});}catch(error){message(error.message,'error');return;}
    activeRun=null;startedAt=Date.now();api.setExternalBusy(true);$('concurrency-results').replaceChildren();$('concurrency-export').classList.add('hidden');$('concurrency-resume').classList.add('hidden');$('concurrency-cancel').classList.remove('hidden');status('运行中','running');message('每组独立创建数据库，分别执行先后提交与同时提交。');
    try{const result=await api.post('/api/concurrency',{config});if(!result.run_id)throw new Error('服务没有返回实验编号。');activeRun=result.run_id;await poll();}catch(error){failed(error);}finally{reading=false;api.setExternalBusy(false);updateActions();}
  }
  $('concurrency-run').addEventListener('click',start);
  $('concurrency-resume').addEventListener('click',async()=>{if(api.isBusy()||reading||!activeRun)return;startedAt=Date.now();api.setExternalBusy(true);$('concurrency-cancel').classList.remove('hidden');try{await poll();}catch(error){failed(error);}finally{reading=false;api.setExternalBusy(false);updateActions();}});
  $('concurrency-cancel').addEventListener('click',async()=>{if(!activeRun)return;$('concurrency-cancel').disabled=true;try{await api.post('/api/runs/'+encodeURIComponent(activeRun)+'/cancel',{});message('已请求停止，完成的组合会保留。','warning');}catch(error){message(error.message,'error');}finally{$('concurrency-cancel').disabled=false;}});
  window.addEventListener('zhuiyin:busy',updateActions);
  window.ZhuiyinConcurrency={showResult(data,runId,partial=false){render(data,runId,partial);$('concurrency-cancel').classList.add('hidden');$('concurrency-resume').classList.toggle('hidden',!partial||Boolean(data.stopped_reason));window.ZhuiyinUI?.showPage('concurrency');}};
  updateActions();
})();
