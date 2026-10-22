/* History has its own read-only view. Never reuse an execution page or its state. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const text = value => value == null ? '未记录' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const names = {experiment:'单次实验',study:'多轮对照',test_suite:'批量测试',batch:'批量场景',concurrency:'并发对照',import:'数据导入',trace:'日志检查',scenarios:'场景导入'};
  const strategies = {baseline:'原始',idempotency_only:'仅幂等',verification_only:'仅核验',verified:'幂等＋核验',fixed:'幂等＋核验'};
  const states = {done:'流程已结束',running:'运行中 · 已保存快照',error:'执行异常',interrupted:'已中断',cancelled:'已停止'};
  let currentId = null, listScroll = 0;
  const node = (tag, value, cls) => {const e=document.createElement(tag); if(value!=null)e.textContent=text(value); if(cls)e.className=cls; return e;};
  function raw(label, value) {
    const d=node('details',null,'history-disclosure'); d.append(node('summary',label));
    let rendered=false; d.addEventListener('toggle',()=>{if(d.open&&!rendered){rendered=true; d.append(node('pre',JSON.stringify(value,null,2)??'未记录'));}}); return d;
  }
  function section(title, ...children) {const s=node('section',null,'history-section'); s.append(node('h3',title),...children); return s;}
  function facts(rows) {const d=node('dl',null,'history-facts'); rows.forEach(([label,value])=>d.append(node('dt',label),node('dd',text(value)))); return d;}
  function rate(value) {
    if(!object(value) || typeof value.rate!=='number')return '未记录';
    const ci=list(value.ci95), interval=ci.length===2?'；95% 区间 '+ci.map(n=>(n*100).toFixed(1)+'%').join('—'):'';
    return (value.rate*100).toFixed(1)+'%（'+text(value.numerator??value.matched)+' / '+text(value.denominator)+'）'+interval;
  }
  function date(value) {if(!value)return '未记录'; const d=new Date(value); return Number.isNaN(d.getTime())?text(value):d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});}
  function trial(value, title, imported=false) {
    const card=node('article',null,'history-trial'); card.append(node('h4',title));
    if(!object(value)){card.append(node('p','本组未保存结果。','subtle-label')); return card;}
    const view=window.ZhuiyinOutcomes.trial(value);
    card.append(facts([['检查结论',view.badgeLabel],[imported?'静态检查推定完成':'数据库实际完成',view.actualLabel],['业务检查',view.businessLabel],['执行报告',view.reportLabel],['报告核对',view.assessmentLabel],['真实写入次数',value.stats?.physical_writes]]));
    if(imported)card.append(node('p','这是对保存日志的静态检查，未重新执行任务；来源声明及核验范围保留在完整记录中。','subtle-label'));
    if(value.failure_fingerprint)card.append(node('p','错误指纹：'+value.failure_fingerprint,'fingerprint-note'));
    if(list(value.violations).length)card.append(raw('违规记录',value.violations));
    if(value.final_state || value.business_records)card.append(raw('数据库最终记录',value.final_state || value.business_records));
    if(list(value.events).length)card.append(raw('操作轨迹与工具返回 · '+value.events.length+' 条',value.events));
    card.append(raw('本组完整保存结果',value)); return card;
  }
  function comparison(report) {
    const grid=node('div',null,'history-trials');
    Object.entries(strategies).forEach(([key,label])=>{if(object(report[key]))grid.append(trial(report[key],label));});
    return grid;
  }
  function entries(title, items, render) {
    const box=node('div'), host=node('div'), pager=node('nav',null,'pager'); pager.setAttribute('aria-label',title+'分页');
    const pages=Math.max(1,Math.ceil(items.length/10)); let page=0;
    function show() {
      host.replaceChildren(...items.slice(page*10,page*10+10).map((item,i)=>render(item,page*10+i)));
      if(!items.length)host.append(node('p','暂无已保存条目。','empty-state'));
      const prev=node('button','上一页','button button-small'), next=node('button','下一页','button button-small'); prev.type=next.type='button'; prev.disabled=page===0; next.disabled=page>=pages-1;
      prev.addEventListener('click',()=>{page--;show();}); next.addEventListener('click',()=>{page++;show();});
      pager.replaceChildren(prev,node('span','第 '+(page+1)+' / '+pages+' 页 · 共 '+items.length+' 条'),next); pager.hidden=pages===1;
    }
    box.append(host,pager); show(); return section(title,box);
  }
  function itemCard(item, index) {
    if(!object(item))return raw('条目 '+(index+1),item);
    const strategyTitle=item.strategy?(strategies[item.strategy]||item.strategy)+(item.schedule?' · '+({concurrent:'同时提交',sequential:'先后提交'}[item.schedule]||item.schedule):' · 第 '+text(item.trial_index)+' 轮'):null;
    const card=node('details',null,'history-entry'); card.append(node('summary',item.name || strategyTitle || item.case_id || item.request_id || '条目 '+(index+1)));
    let rendered=false; card.addEventListener('toggle',()=>{if(!card.open||rendered)return; rendered=true;
      const body=node('div',null,'history-entry-body');
      body.append(facts([['执行状态',item.status || item.execution_status],['记录编号',item.case_id || item.request_id],['错误说明',item.error]]));
      if(object(item.strategies)){const grid=node('div',null,'history-trials'); Object.entries(item.strategies).forEach(([key,value])=>grid.append(trial(value,strategies[key]||key)));body.append(grid);}
      if(object(item.result))body.append(comparison(item.result));
      if(object(item.analysis))body.append(trial(item.analysis,'静态证据检查',true));
      if(Object.hasOwn(item,'actual_success') || Object.hasOwn(item,'reported_outcome') || Object.hasOwn(item,'failed'))body.append(trial(item,'本条检查'));
      if(item.config)body.append(raw('保存的业务参数',item.config));
      body.append(raw('条目完整记录',item)); card.append(body);
    }); return card;
  }
  function showReport(report) {
    const content=[];
    if(object(report.baseline) || object(report.fixed)) {
      content.push(section('业务实际结果与执行报告',node('p','下面保留当时的判断。流程结束不等于任务完成；无法确认也不自动算作报告正确。','subtle-label'),comparison(report)));
      if(report.minimization){const mini=report.minimization,target=mini.target_fingerprint??report.baseline?.failure_fingerprint;content.push(section('故障复现与精简',facts([['原始故障',report.original_faults || report.config?.faults],['保存的故障组合',report.minimized_faults],['基线检查发现错误',report.baseline?.failed===true?'是':report.baseline?.failed===false?'否':'未记录'],['目标错误指纹',target===''?'空（未建立目标错误）':target],['精简流程',report.baseline?.failed===false?'未复现目标错误，不执行精简':mini.completed===true?'流程已结束，复查结果见记录':mini.completed===false?'未完成':'未记录']]),node('p','保存了故障组合不代表精简成功；具体尝试、同一错误复查及停止原因见下方原始记录。','subtle-label'),raw('精简过程与复查结果',mini)));}
    }
    if(report.kind==='study' || Array.isArray(report.arms)) {
      content.push(section('多轮结果摘要',facts([['已完成 / 计划',text(report.completed)+' / '+text(report.planned)],['提前结束原因',report.stopped_reason]]),node('p','比例及评价均读取已保存报告；缺失字段不按当前规则补算。','subtle-label')));
      content.push(entries('各组统计',list(report.arms),(arm,index)=>{const box=node('article',null,'history-trial'),modern=Object.hasOwn(arm,'business_effect_failure_rate');box.append(node('h4',(strategies[arm.strategy]||arm.label||'组 '+(index+1))+' · '+({neutral:'基础任务指令',recovery:'任务指令＋恢复建议'}[arm.prompt_policy]||'指令未记录')),facts([['有效 / 尝试 / 计划',text(arm.valid_trials)+' / '+text(arm.attempted)+' / '+text(arm.planned)],[modern?'业务违规率':'综合失败率（原口径）',rate(modern?arm.business_effect_failure_rate:arm.business_failure_rate)],['数据库完成率（有效轮次）',rate(arm.task_completion_rate)],['报告不一致率',rate(arm.report_error_rate)],['无法确认占比',rate(arm.uncertain_report_rate)],['同一错误复现比例',rate(arm.reproduction)]]),raw('查看本组统计与区间',arm));return box;}));
      if(Array.isArray(report.samples))content.push(entries('逐轮记录',report.samples,itemCard));
    }
    if(Array.isArray(report.cases) || Array.isArray(report.items)) {
      if(report.counts || report.summary || report.progress){const counts=report.counts||{},progress=report.progress||{};content.push(section('批次摘要',facts([['处理总数',counts.cases??report.total],['场景 / 日志',text(counts.scenarios)+' / '+text(counts.traces)],['重复 / 冲突 / 导入错误',text(counts.duplicates)+' / '+text(counts.conflicts)+' / '+text(counts.import_errors)],['已完成 / 计划',text(progress.completed??report.completed)+' / '+text(progress.total??report.total??report.planned)]]),raw('已保存的数量与进度',{counts:report.counts,summary:report.summary,progress:report.progress,completed:report.completed,total:report.total,planned:report.planned})));}
      content.push(entries('逐项结果',list(report.cases || report.items),itemCard));
    }
    if(Array.isArray(report.analyses))content.push(entries('导入日志的检查结果',report.analyses,(item,index)=>{const box=node('article',null,'history-trial');box.append(trial(item,item.name||'日志 '+(index+1),true),raw('检查发现与来源声明',item));return box;}));
    if(Array.isArray(report.scenarios))content.push(entries('导入场景 · 只读预览',report.scenarios,(item,index)=>{const box=node('article',null,'history-trial');box.append(node('h4',item.name||'场景 '+(index+1)),facts(Object.entries(item.config || item)),raw('完整场景',item));return box;}));
    if(!content.length)content.push(node('p','这份记录使用其他格式。下方可以查看完整保存结果。','subtle-label'));
    if(report.model_usage){const usage=report.model_usage,guard=report.run_budget||usage.run_budget,rows=[['请求次数',usage.requests],['总 token',usage.total_tokens],['估算费用',usage.estimated_cost==null?'未记录':text(usage.estimated_cost)+' '+text(usage.currency)]];
      if(report.config?.billing||usage.billing||Object.hasOwn(usage,'cache_usage_complete'))rows.push(['输入缓存命中 token',usage.prompt_cache_hit_tokens],['输入缓存未命中 token',usage.prompt_cache_miss_tokens],['输出 token',usage.completion_tokens],['已知部分费用（元）',usage.known_cost_cny]);
      if(object(guard))rows.push(['本次费用保护额度（元）',guard.max_cost_cny],['预算已计入（元）',guard.accounted_cost_cny],['当前保守预留（元）',guard.reserved_cost_cny],['剩余费用额度（元）',guard.remaining_cost_cny]);
      const box=section('模型调用与费用',facts(rows),raw('完整调用记录',usage));if(object(guard))box.append(node('p',guard.assumption,'subtle-label'),raw('本次保存的费用保护记录',guard));content.push(box);}
    if(list(report.limitations).length)content.push(section('本次结果的适用范围',...report.limitations.map(value=>node('p',value))));
    content.push(section('保存的配置与来源',raw('业务参数、Agent、工具与指令',{config:report.config,model_identity:report.model_identity,agent_specs:report.agent_specs,options:report.options,provenance:report.provenance})),raw('完整保存结果（JSON）',report));
    return content;
  }
  function reveal(runId) {
    if($('history-detail').hidden)listScroll=window.scrollY || 0;
    currentId=runId; $('history-list').hidden=true; $('history-detail').hidden=false;
  }
  function exportState(available) {
    const link=$('history-detail-export'); link.hidden=!available; link.removeAttribute('href');
    if(available && currentId)link.href='/api/runs/'+encodeURIComponent(currentId)+'/export';
  }
  function loading(runId) {
    reveal(runId); $('history-detail-title').textContent='历史记录详情'; $('history-detail-state').textContent='正在读取';
    $('history-detail-state').className='status-pill'; $('history-detail-content').replaceChildren(node('p','正在读取已保存记录…','empty-state'));
    $('history-detail-refresh').disabled=true; exportState(false);
  }
  function show(job) {
    reveal(job.run_id || currentId); const report=job.result || job.partial_result;
    $('history-detail-title').textContent=job.label || '历史记录详情';
    $('history-detail-state').textContent=states[job.status] || '状态未记录';
    $('history-detail-state').className='status-pill '+(job.status==='running'?'running':['error','interrupted','cancelled'].includes(job.status)?'warn':'');
    const mode=report?.mode || report?.config?.agent_mode || job.config?.agent_mode;
    const content=[node('p','只读查看保存结果。查看和刷新不会重新执行任务，也不会更改工作台或其他页面的配置。','history-readonly-note'),facts([['记录编号',job.run_id],['记录类型',names[job.kind] || job.kind],['创建时间',date(job.created_at)],['更新时间',date(job.updated_at)],['测试方式',report?.profile==='agent' || mode==='llm'?'真实 Agent':mode==='rules' || ['quick','full'].includes(report?.profile)?'规则检查 · 不调用模型':'未记录'],['模型',report?.model_identity?.model],['程序版本',report?.provenance?.app_version],['评价协议',report?.evaluation_protocol || report?.provenance?.evaluation_protocol || '原口径（未记录）']])];
    if(job.status!=='done' || !job.result)content.push(node('p',job.status==='running'?'任务仍在运行；这里仅显示已保存的阶段快照，可手动刷新。':'这份记录没有完整结束，以下保留已有材料；未运行的部分不能按成功解读。','history-notice'));
    if(job.error)content.push(node('p',job.error,'history-notice'));
    if(object(report))content.push(...showReport(report));
    else if(report!=null)content.push(raw('完整保存结果（JSON）',report));
    else content.push(node('p','暂无已保存的结果。记录编号、状态及过程仍可查看。','empty-state'));
    if(list(job.events).length)content.push(raw('任务过程记录',job.events));
    $('history-detail-content').replaceChildren(...content); $('history-detail-refresh').disabled=false;
    exportState(report!=null && (job.status==='done' || Boolean(job.partial_result)));
    window.dispatchEvent(new CustomEvent('zhuiyin:history',{detail:job}));
  }
  function fail(message) {
    $('history-detail-state').textContent='读取失败'; $('history-detail-state').className='status-pill warn';
    $('history-detail-content').replaceChildren(node('p',message || '读取失败，请刷新重试。','history-notice'));
    $('history-detail-refresh').disabled=false; exportState(false);
  }
  function hide() {
    currentId=null; $('history-detail').hidden=true; $('history-list').hidden=false;
    window.scrollTo?.({top:listScroll,behavior:'instant'});
  }
  function initialize() {
    if(!$('history-detail-back') || $('history-detail-back').dataset.bound)return;
    $('history-detail-back').dataset.bound='true';
    $('history-detail-back').addEventListener('click',()=>{window.ZhuiyinResearch?.closeHistory?.();hide();});
    $('history-detail-refresh').addEventListener('click',()=>{if(currentId)window.ZhuiyinResearch?.openHistory(currentId);});
    window.addEventListener('zhuiyin:page',event=>{if(event.detail?.page!=='history')window.ZhuiyinResearch?.invalidateHistoryRead?.();});
  }
  window.ZhuiyinHistory={show,loading,fail,hide,renderReport:showReport};
  document.addEventListener('DOMContentLoaded',initialize,{once:true}); initialize();
})();
