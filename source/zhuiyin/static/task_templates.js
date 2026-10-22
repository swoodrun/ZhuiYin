(() => {
  'use strict';
  const configFields=['equipment','stock','quantity','request_id','seed','faults','agent_mode','scenario','ticket_title','ticket_priority','max_requests','prompt_policy','billing'];
  const payloadFields=[['request_id','请求编号'],['equipment','设备名称'],['quantity','借用数量'],['title','工单标题'],['priority','工单优先级'],['status','有效记录状态']];
  const clone=value=>JSON.parse(JSON.stringify(value));
  function element(tag,text,cls){const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;}
  function button(text,handler){const item=element('button',text,'button button-light button-small');item.type='button';item.addEventListener('click',handler);return item;}
  function option(value,label){const item=element('option',label);item.value=value;return item;}
  function mount(host,options={}){
    if(!host)return null;
    const section=element('section',null,'task-templates'),heading=element('h2','我的任务模板'),intro=element('p','保存任务、故障、指令、预算和业务规则。载入模板只填写配置，不会运行实验，也不保存模型连接或密钥。','subtle-label');
    section.append(heading,intro);host.replaceChildren(section);
    const row=element('div',null,'template-toolbar'),select=document.createElement('select');select.setAttribute('aria-label','选择已保存任务模板');
    const message=element('p',null,'template-message');message.setAttribute('role','status');message.setAttribute('aria-live','polite');
    const active=element('p','当前未绑定模板。','template-active');
    let templates=[],snapshot=null,dirty=false,working=false,externalBusy=Boolean(options.isBusy?.());
    const controls=[],fieldInputs={};
    function notify(text,error=false){message.textContent=text;message.classList.toggle('template-error',error);}
    function setSnapshot(value){snapshot=value?clone(value):null;dirty=false;active.textContent=snapshot?'当前绑定：'+snapshot.name+' · 第'+snapshot.version+'版；本次结果将保存完整快照。':'当前未绑定模板。';options.onSelect?.(snapshot?clone(snapshot):null);sync();}
    function markDirty(){dirty=true;if(snapshot){options.onSelect?.(null);active.textContent='模板规则或名称已修改，保存后才能以新版本绑定实验。';}sync();}
    function sync(){externalBusy=Boolean(options.isBusy?.()||externalBusy);const busy=working||externalBusy;controls.forEach(item=>item.disabled=busy);load.disabled=busy||!select.value;update.disabled=busy||!snapshot;remove.disabled=busy||!snapshot;detach.disabled=busy||!snapshot;}
    async function api(path,method,body){
      if(typeof options.request==='function')return options.request(path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
      if(window.Zhuiyin?.request)return window.Zhuiyin.request(path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
      try{const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},signal:controller.signal,...(body?{body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw new Error(data.error||'模板请求失败');return data;}finally{clearTimeout(timer);}
    }
    async function action(callback){if(working||options.isBusy?.()||externalBusy){notify('实验正在运行，请结束后再调整模板。',true);return;}working=true;sync();try{await callback();}catch(error){notify(error.name==='AbortError'?'模板服务响应超时，请刷新后查看。':error.message||'模板操作未完成。',true);}finally{working=false;sync();}}
    function fillList(preferred){select.replaceChildren(option('','选择已保存模板'));templates.forEach(item=>select.append(option(item.id,item.name+' · 第'+item.version+'版')));if(templates.some(item=>item.id===preferred))select.value=preferred;sync();}
    async function refresh(preferred=select.value){const response=await api('/api/task-templates','GET');templates=Array.isArray(response)?response:response.templates||[];fillList(preferred);}
    function inputField(label,id,type='text'){const wrap=element('label',null,'template-field');wrap.append(element('span',label));const input=document.createElement('input');input.id=id;input.type=type;wrap.append(input);controls.push(input);return {wrap,input};}
    const nameField=inputField('模板名称','task-template-name'),name=nameField.input;name.maxLength=80;
    const descriptionWrap=element('label',null,'template-field');descriptionWrap.append(element('span','说明（可选）'));const description=document.createElement('textarea');description.rows=2;description.maxLength=1000;descriptionWrap.append(description);controls.push(description);
    const editor=document.createElement('details');editor.className='template-editor';editor.append(element('summary','编辑模板名称与业务规则'));
    const body=element('div',null,'template-editor-body');body.append(nameField.wrap,descriptionWrap);editor.append(body);
    const countRow=element('div',null,'template-count-row'),countLabel=element('label',null,'template-field');countLabel.append(element('span','有效记录数量'));
    const countMode=document.createElement('select');countMode.append(option('exactly','恰好'),option('at_most','最多'),option('off','不检查数量'));countLabel.append(countMode);
    const countField=inputField('记录条数','task-template-count','number'),countValue=countField.input;countValue.min='0';countValue.max='20';countValue.value='1';countRow.append(countLabel,countField.wrap);body.append(countRow);controls.push(countMode);
    body.append(element('p','下列字段只检查最终有效记录。勾选需要核对的项目；没有记录或缺少字段时显示无法核对。','subtle-label'));
    const fields=element('div',null,'template-rule-fields');
    payloadFields.forEach(([key,label])=>{const wrap=element('div',null,'template-rule-field'),checkLabel=document.createElement('label'),check=document.createElement('input');check.type='checkbox';checkLabel.append(check,document.createTextNode('检查'+label));let input;
      if(key==='priority'){input=document.createElement('select');[['low','低'],['normal','普通'],['high','高'],['urgent','紧急']].forEach(([value,title])=>input.append(option(value,title)));}
      else if(key==='status'){input=document.createElement('select');input.append(option('active','有效（active）'));}
      else{input=document.createElement('input');input.type=key==='quantity'?'number':'text';if(key==='quantity'){input.min='1';input.max='1000000';}else input.maxLength=key==='title'?200:80;}
      input.setAttribute('aria-label','期望'+label);wrap.append(checkLabel,input);fields.append(wrap);fieldInputs[key]={check,input};controls.push(check,input);
    });body.append(fields);
    const stockRow=element('div',null,'template-stock-row'),stockLabel=document.createElement('label'),stockCheck=document.createElement('input');stockCheck.type='checkbox';stockLabel.append(stockCheck,document.createTextNode('检查库存变化'));
    const stockField=inputField('结束库存减开始库存；借出1台填 -1','task-template-stock-delta','number'),stockValue=stockField.input;stockValue.min='-1000000';stockValue.max='1000000';stockValue.value='-1';stockRow.append(stockLabel,stockField.wrap);body.append(stockRow);controls.push(stockCheck);
    function readConfig(){if(typeof options.readConfig!=='function')throw new Error('当前页面暂不能读取任务配置。');const raw=options.readConfig(),config={};configFields.forEach(key=>{if(Object.hasOwn(raw,key))config[key]=clone(raw[key]);});return config;}
    function integer(input,label){if(input.value.trim()===''||!Number.isInteger(Number(input.value)))throw new Error(label+'需要填写整数。');return Number(input.value);}
    function readContract(){const result={record_count:countMode.value==='off'?null:{mode:countMode.value,value:integer(countValue,'记录条数')},expected_fields:{},stock_delta:stockCheck.checked?integer(stockValue,'库存变化'):null};Object.entries(fieldInputs).forEach(([key,field])=>{if(field.check.checked)result.expected_fields[key]=key==='quantity'?integer(field.input,'借用数量'):field.input.value.trim();});return result;}
    function fillContract(contract={}){countMode.value=contract.record_count?.mode||'off';countValue.value=contract.record_count?.value??1;Object.entries(fieldInputs).forEach(([key,field])=>{field.check.checked=Object.hasOwn(contract.expected_fields||{},key);field.input.value=contract.expected_fields?.[key]??(key==='quantity'?1:key==='priority'?'normal':key==='status'?'active':'');});stockCheck.checked=contract.stock_delta!=null;stockValue.value=contract.stock_delta??-1;}
    function useCurrent(){const config=readConfig(),ticket=config.scenario==='ticket';fillContract({record_count:{mode:'exactly',value:1},expected_fields:{request_id:config.request_id,status:'active',...(ticket?{title:config.ticket_title,priority:config.ticket_priority}:{equipment:config.equipment,quantity:config.quantity})},stock_delta:ticket?null:-config.quantity});markDirty();notify('已按当前任务填入正常业务规则；库存不足等特殊任务可手动修改。');}
    const generate=button('使用当前任务字段生成规则',()=>action(async()=>useCurrent()));controls.push(generate);body.append(generate,element('p','这些规则单独检查，不改变原有数据库裁判；库存变化仅适用于有完整库存记录的任务。','subtle-label'));
    const load=button('载入模板',()=>action(async()=>{const selected=templates.find(item=>item.id===select.value);if(!selected)throw new Error('请先选择模板。');await options.applyConfig?.(clone(selected.config));name.value=selected.name;description.value=selected.description;fillContract(selected.contract);setSnapshot(selected);notify('已载入第'+selected.version+'版，尚未运行实验。');}));
    const refreshButton=button('刷新',()=>action(async()=>{await refresh();notify('模板列表已刷新。');}));row.append(select,load,refreshButton);controls.push(select,load,refreshButton);
    const actions=element('div',null,'template-actions');
    async function save(updating){const payload={name:name.value.trim(),description:description.value.trim(),config:readConfig(),contract:readContract()};if(!payload.name){editor.open=true;name.focus();throw new Error('请填写模板名称。');}if(updating){if(!snapshot)throw new Error('请先载入一个模板。');payload.expected_version=snapshot.version;}
      const response=await api('/api/task-templates'+(updating?'/'+encodeURIComponent(snapshot.id):''),updating?'PUT':'POST',payload),saved=response.template||response;await refresh(saved.id);name.value=saved.name;description.value=saved.description;fillContract(saved.contract);setSnapshot(saved);notify((updating?'已保存新版本：':'已另存模板：')+saved.name+' · 第'+saved.version+'版。');
    }
    const create=button('将当前配置另存为模板',()=>action(async()=>save(false))),update=button('保存为新版本',()=>action(async()=>save(true)));
    const remove=button('删除模板',()=>action(async()=>{if(!snapshot)return;const response=await api('/api/task-templates/'+encodeURIComponent(snapshot.id),'DELETE',{expected_version:snapshot.version});if(response.deleted===false)throw new Error('模板未删除，请刷新后再试。');setSnapshot(null);await refresh();notify('模板已删除；已保存实验中的模板快照仍保留。');}));
    const detach=button('解除本次绑定',()=>{if(!working&&!options.isBusy?.()){setSnapshot(null);notify('已解除本次模板绑定，当前任务参数仍保留。');}});
    actions.append(create,update,remove,detach);controls.push(create,update,remove,detach);section.append(row,active,editor,actions,message);
    [name,description,countMode,countValue,stockCheck,stockValue,...Object.values(fieldInputs).flatMap(field=>[field.check,field.input])].forEach(item=>item.addEventListener('input',markDirty));select.addEventListener('change',sync);
    function busyListener(event){externalBusy=Boolean(event.detail?.busy);sync();}window.addEventListener('zhuiyin:busy',busyListener);
    fillContract({record_count:{mode:'exactly',value:1},expected_fields:{},stock_delta:null});try{useCurrent();}catch{}sync();
    action(async()=>{await refresh();notify('可载入已有模板，或展开规则后保存当前配置。');});
    return {getSnapshot(){return snapshot&&!dirty?clone(snapshot):null;},clear(){setSnapshot(null);},setSnapshot(value){if(value){name.value=value.name||'';description.value=value.description||'';fillContract(value.contract||{});}setSnapshot(value);},refresh(){return action(async()=>refresh());},destroy(){window.removeEventListener('zhuiyin:busy',busyListener);host.replaceChildren();}};
  }
  window.ZhuiyinTemplates={mount};
})();
