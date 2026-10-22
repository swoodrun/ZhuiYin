(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const api = window.Zhuiyin;
  let revision=0, timer, preview;
  const node=(tag,text)=>{const e=document.createElement(tag);e.textContent=text;return e;};
  function draw() {
    if(!preview)return;
    const spec=preview.variants[$('agent-variant').value];
    $('agent-name').textContent=preview.name;
    $('agent-tools').replaceChildren(...spec.tools.map(t=>{const row=node('div',''),description=t.function.description.replace('无法确认时允许报告 unknown。','也可报告“无法确认”。');row.className='agent-tool';row.title=description;row.append(node('code',t.function.name),node('span',description.split('。')[0]+'。'));return row;}));
    $('agent-system-prompt').textContent='系统指令：\n'+spec.system;
    $('agent-task-prompt').textContent='任务消息：\n'+JSON.stringify(spec.task,null,2);
    $('agent-tool-schema').textContent=JSON.stringify(spec.tools,null,2);
    $('agent-preview-state').textContent='与后端当前执行配置一致；预览不调用模型。';
  }
  async function update() {
    const id=++revision;
    $('agent-model-label').textContent=$('model').value.trim()||'尚未配置';
    try{$('agent-origin-label').textContent=new URL($('base-url').value.trim()).origin;}catch{$('agent-origin-label').textContent='尚未配置有效地址';}
    const ticket=$('scenario').value==='ticket';
    $('agent-expected').textContent=ticket?'正常结果：当前请求恰好创建一条标题和优先级正确的有效工单。':Number($('stock').value)>=Number($('quantity').value)?'正常结果：恰好一条正确借用记录，剩余库存 '+(Number($('stock').value)-Number($('quantity').value))+'。':'正常结果：库存不足，不新增借用记录，并如实报告未完成。';
    const config={scenario:$('scenario').value,equipment:$('equipment').value,stock:Number($('stock').value),quantity:Number($('quantity').value),request_id:$('request-id').value,seed:Number($('seed').value),ticket_title:$('ticket-title').value,ticket_priority:$('ticket-priority').value,prompt_policy:$('prompt-policy').value,faults:[]};
    $('agent-preview-state').textContent='正在更新后端配置预览…';
    try{const value=await api.post('/api/agent-preview',{config});if(id!==revision)return;preview=value;draw();}
    catch(error){if(id!==revision)return;preview=null;$('agent-tools').replaceChildren();$('agent-system-prompt').textContent='';$('agent-task-prompt').textContent='';$('agent-tool-schema').textContent='';$('agent-preview-state').textContent='配置尚未有效：'+error.message;}
  }
  $('experiment-form').addEventListener('input',event=>{if(event.target.id==='max-requests'||event.target.id.startsWith('billing-'))return;clearTimeout(timer);timer=setTimeout(update,200);});
  $('experiment-form').addEventListener('change',event=>{if(event.target.id==='max-requests'||event.target.id.startsWith('billing-'))return;clearTimeout(timer);timer=setTimeout(update,0);});
  $('reset-btn').addEventListener('click',()=>{clearTimeout(timer);timer=setTimeout(update,0);});
  $('agent-variant').addEventListener('change',draw);
  $('run-rule-check').addEventListener('click',()=>api.runRuleCheck());
  window.addEventListener('zhuiyin:busy',()=>{$('run-rule-check').disabled=api.isBusy();});
  window.addEventListener('zhuiyin:result',event=>{
    const data=event.detail, target=$('result-overview');
    $('agent-executed-config')?.remove();
    if((data.mode||data.config?.agent_mode)!=='llm')return;
    const box=document.createElement('details');box.id='agent-executed-config';box.className='agent-result-config';
    box.append(node('summary','本次实际使用的 Agent 配置（已保存）'));
    box.append(node('p','配置模型：'+(data.model_identity?.model||'未记录')+'；配置来源由用户填写，不是服务商身份认证。'));
    if(data.agent_specs)box.append(node('pre',JSON.stringify(data.agent_specs,null,2)));
    else box.append(node('p','历史记录未保存完整指令与工具定义，不能用当前配置补填历史。'));
    target.append(box);
  });
  window.addEventListener('zhuiyin:config',update);
  update();
})();
