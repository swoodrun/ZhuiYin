'use strict';
// Exercise the editor contract in a small DOM; this is not a browser test.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
  constructor(tag,text=''){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.attributes={};this._text=text;this.value='';this.checked=false;this.disabled=false;this.className='';this.classList={toggle:()=>{},add:()=>{},remove:()=>{}};}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(item=>item.textContent).join('');}
  set value(value){this._value=String(value);}
  get value(){return this._value;}
  set innerHTML(value){throw new Error('Unsafe HTML assignment: '+value);}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this._text='';this.children=[...items];}
  setAttribute(key,value){this.attributes[key]=String(value);}
  addEventListener(type,handler){(this.listeners[type]??=[]).push(handler);}
  async emit(type,event={}){for(const handler of this.listeners[type]||[])await handler(event);}
  focus(){this.focused=true;}
}
const document={createElement:tag=>new Element(tag),createTextNode:text=>new Element('#text',text)};
const listeners={};
const window={addEventListener(type,fn){(listeners[type]??=[]).push(fn);},removeEventListener(type,fn){listeners[type]=(listeners[type]||[]).filter(value=>value!==fn);}};
const clone=value=>JSON.parse(JSON.stringify(value));
const context={window,document,AbortController,setTimeout,clearTimeout};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../static/task_templates.js'),'utf8'),context);
function all(item){return [item,...item.children.flatMap(all)];}
function named(host,text){const found=all(host).find(item=>item.tagName==='BUTTON'&&item.textContent===text);assert.ok(found,'Button '+text);return found;}
function id(host,value){const found=all(host).find(item=>item.id===value);assert.ok(found,'ID '+value);return found;}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  let records=[],calls=[],applied=[],selected=[],busy=false;
  const config={scenario:'equipment',equipment:'会议投影仪',stock:3,quantity:1,request_id:'template-ui-001',faults:[],agent_mode:'llm',prompt_policy:'neutral',max_requests:40,billing:{},model_config:{api_key:'SHOULD_NOT_SAVE'},base_url:'SHOULD_NOT_SAVE'};
  const host=new Element('div');
  const instance=window.ZhuiyinTemplates.mount(host,{readConfig:()=>clone(config),applyConfig:value=>applied.push(clone(value)),onSelect:value=>selected.push(clone(value)),isBusy:()=>busy,
    async request(path,options){calls.push({path,...clone(options)});const body=options.body?JSON.parse(options.body):null;
      if(options.method==='GET')return {templates:clone(records)};
      if(options.method==='POST'){const record={...body,id:'123456789abc',version:1,protocol:'task-template-v1',created_at:'2026-10-21',updated_at:'2026-10-21'};records.push(record);return {template:clone(record)};}
      if(options.method==='PUT'){assert.equal(body.expected_version,records[0].version);const record={...records[0],...body,version:records[0].version+1};delete record.expected_version;records[0]=record;return {template:clone(record)};}
      if(options.method==='DELETE'){records=[];return {deleted:true};}
      throw new Error('Unexpected request');
    }});
  await flush();
  assert.equal(calls.length,1);assert.equal(instance.getSnapshot(),null);assert.equal(applied.length,0);
  id(host,'task-template-name').value='<img src=x onerror=alert(1)>';await id(host,'task-template-name').emit('input');
  await named(host,'将当前配置另存为模板').emit('click');
  assert.equal(records.length,1);assert.equal(instance.getSnapshot().version,1);
  assert.equal(records[0].contract.stock_delta,-1);assert.equal(records[0].contract.expected_fields.equipment,'会议投影仪');
  assert.equal(records[0].config.model_config,undefined);assert.equal(records[0].config.base_url,undefined);
  assert.ok(!JSON.stringify(calls).includes('SHOULD_NOT_SAVE'));assert.ok(host.textContent.includes('<img src=x onerror=alert(1)>'));
  await named(host,'载入模板').emit('click');assert.equal(applied.length,1);assert.equal(applied[0].max_requests,40);
  id(host,'task-template-count').value='2';await id(host,'task-template-count').emit('input');
  assert.equal(instance.getSnapshot(),null);assert.equal(selected.at(-1),null);
  await named(host,'保存为新版本').emit('click');assert.equal(instance.getSnapshot().version,2);assert.equal(records[0].contract.record_count.value,2);
  const historical=clone(records[0]);historical.name='历史复制的模板';historical.contract.record_count.value=3;
  instance.setSnapshot(historical);assert.equal(id(host,'task-template-name').value,historical.name);assert.equal(id(host,'task-template-count').value,'3');
  busy=true;for(const fn of listeners['zhuiyin:busy'])fn({detail:{busy:true}});const count=calls.length;
  await named(host,'删除模板').emit('click');assert.equal(calls.length,count);assert.equal(named(host,'保存为新版本').disabled,true);
  busy=false;for(const fn of listeners['zhuiyin:busy'])fn({detail:{busy:false}});
  await named(host,'删除模板').emit('click');assert.equal(records.length,0);assert.equal(instance.getSnapshot(),null);assert.ok(host.textContent.includes('快照仍保留'));
  assert.equal(applied.length,1);assert.ok(calls.every(call=>call.path.startsWith('/api/task-templates')));
  instance.destroy();assert.equal(host.children.length,0);
  console.log('Task template editor: save/load/version/dirty/busy/delete and inert text checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
