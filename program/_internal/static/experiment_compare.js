/* Saved comparison is read-only and does not share execution form state. */
(() => {
  'use strict';
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const labels = {baseline:'原始',fixed:'幂等＋核验',idempotency_only:'仅幂等',verification_only:'仅核验',verified:'幂等＋核验',neutral:'基础任务指令',recovery:'任务指令＋恢复建议',llm:'真实模型',rules:'规则检查',unknown:'未记录'};
  const metrics = {actual_success:'数据库实际完成',business_failed:'业务违规',report_failed:'报告不一致',uncertain:'报告无法确认',contract_failed:'模板规则不符'};
  const kindNames = {experiment:'单次实验',study:'多轮对照',test_suite:'批量测试',batch:'批量场景',concurrency:'并发对照',import:'数据导入',trace:'日志检查',scenarios:'场景导入'};
  const jobStateNames = {done:'流程已结束',running:'运行中 · 已保存快照',error:'执行异常',interrupted:'已中断',cancelled:'已停止',completed:'流程已结束',finished:'流程已结束',partial:'部分完成',queued:'等待运行',unknown:'状态未记录'};
  const outcomeNames = {success:'成功',failure:'失败',unknown:'无法确认'};
  const protocolNames = {'business-report-v2':'业务与报告检查（第2版）','business-report-v1':'业务与报告检查（第1版）',legacy:'历史评价口径',unknown:'评价协议未记录'};
  const excludedNames = {static_trace:'静态日志',static_import:'静态导入材料',no_primary_execution:'未保存主执行记录',duplicate:'重复用例',conflict:'冲突用例',import_error:'导入错误',completed:'已完成的静态记录',done:'已完成的静态记录'};
  const stateNames = {valid:'有效执行',not_run:'未运行',execution_error:'执行异常',unknown:'状态未记录'};
  const changeNames = {improved:'观察到改善',regressed:'观察到退步',mixed:'有改善也有退步',unchanged:'已知可比指标未变',report_changed:'报告结论变化',unverifiable:'材料不足，无法比较',execution_changed:'执行状态变化'};
  const text = value => value == null ? '未记录' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const node = (tag, value, cls) => { const item=document.createElement(tag); if(value!=null)item.textContent=text(value); if(cls)item.className=cls; return item; };
  const named = value => labels[value] || (value?'其他配置':'未记录');
  const number = value => typeof value==='number' && Number.isFinite(value) ? String(Math.round(value*1000000)/1000000) : '未记录';
  function configValue(field,value){
    const key=String(field||'').split('.').pop().replace(/^instruction_policy$/,'prompt_policy');
    const enums={prompt_policy:{neutral:'基础任务指令',recovery:'任务指令＋恢复建议',unknown:'指令未记录'},agent_mode:{llm:'真实模型',rules:'规则检查',unknown:'执行模式未记录'},mode:{llm:'真实模型',rules:'规则检查',unknown:'执行模式未记录'},scenario:{equipment:'设备借用',ticket:'工单创建'},strategy:labels,evaluation_protocol:protocolNames,order_mode:{balanced:'轮换先后顺序',fixed:'固定先后顺序'},ticket_priority:{low:'低',normal:'普通',high:'高',urgent:'紧急'}};
    const arrayEnums={faults:{commit_response_lost:'提交后响应丢失',stale_read:'读取过期状态',truncate_response:'响应内容缺失',latency:'工具调用延迟',precommit_timeout:'写入前超时',ack_without_commit:'未提交却返回成功',corrupt_payload:'业务内容损坏'},strategies:labels,prompt_policies:enums.prompt_policy,prompt_policy:enums.prompt_policy};
    if(Array.isArray(value)&&arrayEnums[key])return value.map(item=>arrayEnums[key][item]||(key==='faults'?'其他故障':key==='strategies'?'其他策略':'其他指令')).join('、')||(key==='faults'?'无故障':'未选择');
    return typeof value==='string'&&enums[key]?.[value]?enums[key][value]:text(value);
  }
  function table(headers, rows, label) {
    const wrap=node('div',null,'compare-table-wrap'), table=node('table',null,'compare-table');
    table.setAttribute('aria-label',label); const head=node('thead'), tr=node('tr');
    headers.forEach(value=>{const th=node('th',value);th.scope='col';tr.append(th);});head.append(tr);table.append(head);
    const body=node('tbody'); rows.forEach(row=>{const tr=node('tr');row.forEach(value=>{const td=node('td');td.append(value instanceof Node?value:node('span',text(value)));tr.append(td);});body.append(tr);});
    table.append(body);wrap.append(table);return wrap;
  }
  function disclosure(label,value) {const details=node('details',null,'compare-disclosure');details.append(node('summary',label),node('pre',JSON.stringify(value,null,2)??'未记录'));return details;}
  function metric(value) {return object(value)&&typeof value.count==='number' ? value.count+' / '+value.known+'（'+(value.rate*100).toFixed(1)+'%）'+(value.missing?'；另'+value.missing+'条缺失':'') : '未记录';}
  function facts(side) {
    const card=node('article',null,'compare-side');card.append(node('h4',side.label||side.run_id||'保存的实验'),node('p',side.run_id||'记录编号未保存','subtle-label'));
    card.append(node('p','主执行记录 '+number(side.records)+' 条；类型 '+(kindNames[side.kind]||(side.kind?'其他记录类型':'未记录'))+'；状态 '+(jobStateNames[side.status]||'状态未记录')));
    const excluded=object(side.excluded)?Object.entries(side.excluded):[];
    if(excluded.length)card.append(node('p','未计入执行统计：'+excluded.map(([key,n])=>(excludedNames[key]||'其他未计入记录')+' '+n).join('、'),'subtle-label'));
    return card;
  }
  function paged(host, items, renderRows) {
    let page=0; const size=20, rows=node('div'), controls=node('nav',null,'compare-pager');controls.setAttribute('aria-label','任务变化分页');
    function draw(){rows.replaceChildren(renderRows(items.slice(page*size,(page+1)*size)));const prev=node('button','上一页','button button-small'),next=node('button','下一页','button button-small');prev.type=next.type='button';prev.disabled=page===0;next.disabled=(page+1)*size>=items.length;prev.addEventListener('click',()=>{page--;draw();});next.addEventListener('click',()=>{page++;draw();});controls.replaceChildren(prev,node('span','第 '+(page+1)+' / '+Math.max(1,Math.ceil(items.length/size))+' 页 · 共 '+items.length+' 条'),next);controls.hidden=items.length<=size;}
    host.append(rows,controls);draw();
  }
  function renderReport(host,data) {
    if(!host || !object(data))return;
    host.replaceChildren();host.classList.add('experiment-compare-result');
    const notes=node('div',null,'compare-notices');list(data.warnings).forEach(note=>notes.append(node('p',note)));host.append(notes);
    const sides=node('div',null,'compare-sides');sides.append(facts(data.left||{}),facts(data.right||{}));host.append(sides);
    const differences=list(data.configuration_differences),diff=node('details',null,'compare-disclosure');diff.open=false;diff.append(node('summary','配置、模型、提示与版本差异 · '+differences.length+' 项'));
    if(differences.length)diff.append(table(['字段','左侧保存值','右侧保存值'],differences.map(item=>[item.field,item.left_recorded===false?'未保存':configValue(item.field,item.left),item.right_recorded===false?'未保存':configValue(item.field,item.right)]),'两次记录的配置差异'));
    else diff.append(node('p','已记录的配置字段相同；这不证明未保存条件相同。','subtle-label'));
    host.append(diff,node('h4','按模式、策略、提示层与评价协议分别统计'),node('p','每个指标显示“计数 / 已知字段的有效执行数”；缺失字段不按零计算，重复轮次不当作独立任务。','subtle-label'));
    const groupRows=[];
    list(data.groups).forEach(group=>{const key=list(group.key),name=key.slice(0,3).map(named).join(' · ')+' · '+(protocolNames[key[3]]||(key[3]?'其他评价协议':'评价协议未记录'));for(const [side,title] of [['left','左侧'],['right','右侧']]){const value=group[side];groupRows.push(value?[name,title,value.records,value.valid,value.not_run,value.execution_errors,value.execution_unknown,...Object.keys(metrics).map(name=>metric(value.metrics?.[name]))]:[name,title,...Array(5+Object.keys(metrics).length).fill('未记录')]);}});
    host.append(groupRows.length?table(['分组','记录','主记录数','有效','未运行','执行异常','状态未知',...Object.values(metrics)],groupRows,'分组统计'):node('p','没有可统计的主执行记录。','subtle-label'));
    const usage=data.left?.usage||{},other=data.right?.usage||{};
    host.append(node('h4','保存的调用量与耗时'),table(['指标','左侧','右侧'],[['模型请求',number(usage.requests),number(other.requests)],['总 token',number(usage.total_tokens),number(other.total_tokens)],['估算费用',usage.estimated_cost==null?'未记录':number(usage.estimated_cost)+' '+text(usage.currency),other.estimated_cost==null?'未记录':number(other.estimated_cost)+' '+text(other.currency)],['已知部分费用（元）',number(usage.known_cost_cny),number(other.known_cost_cny)],['执行耗时（毫秒）',number(usage.elapsed_ms),number(other.elapsed_ms)]],'调用量和执行耗时'),node('p','费用读取当时保存的估算，不按当前单价重算；不同币种不直接相减。没有保存总耗时的记录保持未知。','subtle-label'));
    const pairs=list(data.paired),counts=data.paired_counts||{};host.append(node('h4','同任务逐条变化'),node('p',Object.entries(changeNames).filter(([key])=>counts[key]).map(([key,label])=>label+' '+counts[key]).join('；')||'没有可唯一对齐的任务。'),node('p',data.matching_note,'subtle-label'));
    if(pairs.length){const target=node('div');host.append(target);paged(target,pairs,values=>table(['任务 / 请求编号','策略 / 轮次','左侧 → 右侧','观察变化','条件说明','保存指标'],values.map(pair=>{const details=disclosure('查看两侧指标与来源',{left:pair.left,right:pair.right,improved_metrics:pair.improved_metrics,regressed_metrics:pair.regressed_metrics});return [pair.name+' / '+text(pair.task?.request_id),named(pair.strategy)+(pair.repetition!=null?' · '+pair.repetition:''),named(pair.left?.prompt_policy)+'：'+(stateNames[pair.left?.state]||'未记录')+' / '+(outcomeNames[pair.left?.outcome]||'未记录')+' → '+named(pair.right?.prompt_policy)+'：'+(stateNames[pair.right?.state]||'未记录')+' / '+(outcomeNames[pair.right?.outcome]||'未记录'),changeNames[pair.status]||'变化类型未记录',pair.condition_changed?'条件不同，仅描述观察变化':'已保存条件相同；不作因果推断',details];}),'同任务逐条变化'));}
    const unmatched=data.unmatched||{};if(list(unmatched.left).length||list(unmatched.right).length)host.append(disclosure('未配对记录 · 左 '+list(unmatched.left).length+' / 右 '+list(unmatched.right).length,unmatched));
  }
  async function request(path,body) {
    if(body!==undefined && window.Zhuiyin?.post)return window.Zhuiyin.post(path,body);
    if(body===undefined && window.Zhuiyin?.request)return window.Zhuiyin.request(path);
    const response=await fetch(path,body===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),value=await response.json();if(!response.ok)throw new Error(value.error||'读取失败');return value;
  }
  function mount(host,options={}) {
    if(!host)return null;
    if(host.__zhuiyinCompare)return host.__zhuiyinCompare;
    host.classList.add('experiment-compare');const panel=node('details',null,'compare-panel');panel.open=options.open===true;panel.append(node('summary','比较两次历史实验'));
    const body=node('div',null,'compare-panel-body');body.append(node('p','选择左侧基准和右侧新记录，只读取已保存结果，不调用模型。','subtle-label'));
    const form=node('form',null,'compare-selection'),left=node('select'),right=node('select'),leftLabel=node('label','左侧 · 修改前'),rightLabel=node('label','右侧 · 修改后');leftLabel.append(left);rightLabel.append(right);
    const submit=node('button','比较保存结果','button button-primary'),refreshButton=node('button','刷新列表','button button-light'),more=node('button','加载更多历史','button button-light');submit.type='submit';refreshButton.type=more.type='button';form.append(leftLabel,rightLabel,submit,refreshButton,more);
    const status=node('p','','compare-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');const result=node('div');body.append(form,status,result);panel.append(body);host.append(panel);
    let items=[],total=0,loading=false,revision=0;
    function optionsFor(select,selected){select.replaceChildren(node('option','请选择记录'));select.firstChild.value='';items.forEach(item=>{const label=(item.label||item.run_id)+' · '+(item.created_at&&!Number.isNaN(new Date(item.created_at).getTime())?new Date(item.created_at).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'时间未知')+' · '+item.run_id;const option=node('option',label);option.value=item.run_id;select.append(option);});select.value=selected||'';}
    async function refresh(append=false){if(loading)return;loading=true;const mine=++revision,oldLeft=left.value,oldRight=right.value;refreshButton.disabled=more.disabled=true;status.textContent='正在读取历史列表…';try{const value=await request('/api/history?limit=200&offset='+(append?items.length:0));if(mine!==revision)return;const incoming=list(value.items);items=append?[...items,...incoming.filter(item=>!items.some(old=>old.run_id===item.run_id))]:incoming;total=typeof value.total==='number'?value.total:items.length;optionsFor(left,oldLeft);optionsFor(right,oldRight);more.hidden=items.length>=total;status.textContent='已载入 '+items.length+' / '+total+' 份记录。';}catch(error){status.textContent=error.message||'历史列表读取失败';}finally{loading=false;refreshButton.disabled=more.disabled=false;}}
    async function select(leftId,rightId){await refresh();for(const id of [leftId,rightId])if(id&&!items.some(item=>item.run_id===id)){try{const value=await request('/api/runs/'+encodeURIComponent(id)+'?lite=1');items.push(value);}catch(error){status.textContent=error.message;}}optionsFor(left,leftId);optionsFor(right,rightId);panel.open=true;}
    form.addEventListener('submit',async event=>{event.preventDefault();if(!left.value||!right.value){status.textContent='请选择两份记录。';return;}submit.disabled=true;status.textContent='正在比较保存结果…';try{const value=await request('/api/compare',{left_run_id:left.value,right_run_id:right.value});renderReport(result,value);status.textContent='比较完成；没有重新运行测试。';}catch(error){status.textContent=error.message||'比较失败';}finally{submit.disabled=false;}});
    refreshButton.addEventListener('click',()=>refresh());more.addEventListener('click',()=>refresh(true));
    const controller={refresh,select,render:data=>renderReport(result,data)};host.__zhuiyinCompare=controller;refresh();return controller;
  }
  window.ZhuiyinCompare={mount,renderReport};
})();
