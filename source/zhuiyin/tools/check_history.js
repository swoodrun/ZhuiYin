/* Synthetic DOM contracts for saved-history viewing only.
 * Does not read datasets, run a server, call an engine/model, or render a browser.
 */
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const historyProgram=fs.readFileSync(path.join(root,'static/history.js'),'utf8');
const researchProgram=fs.readFileSync(path.join(root,'static/research.js'),'utf8');
const outcomes=require('../static/outcome.js');
const decode=value=>value.replace(/&(?:amp|lt|gt|quot|#39);/g,value=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[value]));
class Element {
  constructor(tag,attrs={}) {
    this.tagName=tag.toUpperCase();this.nodeType=tag==='#text'?3:1;this.attrs={...attrs};this.children=[];this.listeners={};this.dataset={};this.text='';this.value=attrs.value||'';this.checked='checked'in attrs;this.disabled='disabled'in attrs;this.hidden='hidden'in attrs;this.open=false;this.parentElement=null;this.style={};
    for(const[key,value]of Object.entries(attrs))if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]=value;
    this.classList={contains:name=>(this.attrs.class||'').split(/\s+/).includes(name),add:(...names)=>{this.attrs.class=[...new Set((this.attrs.class||'').split(/\s+/).filter(Boolean).concat(names))].join(' ');},remove:(...names)=>{this.attrs.class=(this.attrs.class||'').split(/\s+/).filter(name=>!names.includes(name)).join(' ');},toggle:(name,force)=>{const next=force===undefined?!this.classList.contains(name):Boolean(force);if(next)this.classList.add(name);else this.classList.remove(name);return next;}};
  }
  set id(value){this.attrs.id=String(value);}get id(){return this.attrs.id||'';}
  set className(value){this.attrs.class=String(value);}get className(){return this.attrs.class||'';}
  set innerHTML(_){throw new Error('History content must not use innerHTML');}
  set outerHTML(_){throw new Error('History content must not use outerHTML');}
  insertAdjacentHTML(){throw new Error('History content must not use insertAdjacentHTML');}
  set textContent(value){this.text=String(value);this.children.forEach(child=>child.parentElement=null);this.children=[];}
  get textContent(){return this.text+this.children.map(node=>node.textContent).join('');}
  append(...nodes){for(const item of nodes){const node=item instanceof Element?item:Object.assign(new Element('#text'),{text:String(item)});if(node.parentElement)node.remove();node.parentElement=this;this.children.push(node);}}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children.forEach(child=>child.parentElement=null);this.children=[];this.text='';this.append(...nodes);}
  remove(){if(this.parentElement){const parent=this.parentElement;parent.children=parent.children.filter(child=>child!==this);this.parentElement=null;}}
  after(node){if(!this.parentElement)return;const parent=this.parentElement,index=parent.children.indexOf(this);node.parentElement=parent;parent.children.splice(index+1,0,node);}
  get nextElementSibling(){if(!this.parentElement)return null;return this.parentElement.children[this.parentElement.children.indexOf(this)+1]||null;}
  setAttribute(key,value){this.attrs[key]=String(value);if(key==='hidden')this.hidden=true;}
  getAttribute(key){return this.attrs[key]??null;}
  removeAttribute(key){delete this.attrs[key];if(key==='hidden')this.hidden=false;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  async emit(name,event={}){for(const fn of this.listeners[name]||[])await fn({...event,target:event.target||this,preventDefault(){},stopPropagation(){}});}
  async click(){if(!this.disabled){await this.emit('click');if(this.onclick)await this.onclick({target:this,preventDefault(){}});}}
  focus(){}scrollIntoView(){}getBoundingClientRect(){return{left:0,right:240,top:0,bottom:40,width:240,height:40};}
  contains(other){return this===other||this.children.some(child=>child.contains(other));}
  matches(selector){
    selector=selector.trim();if(selector.includes(','))return selector.split(',').some(part=>this.matches(part));
    if(selector.includes(' ')){const parts=selector.split(/\s+/),last=parts.pop();if(!this.matches(last))return false;let parent=this.parentElement;while(parent){if(parent.matches(parts.join(' ')))return true;parent=parent.parentElement;}return false;}
    if(selector.endsWith(':checked')){if(!this.checked)return false;selector=selector.slice(0,-8);}
    if(selector.startsWith('#'))return this.id===selector.slice(1);
    if(selector.startsWith('.'))return selector.slice(1).split('.').every(name=>this.classList.contains(name));
    const match=selector.match(/^(\w+)?\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
    if(match)return(!match[1]||this.tagName===match[1].toUpperCase())&&(match[3]===undefined?match[2]in this.attrs:this.attrs[match[2]]===match[3]);
    return selector==='*'||this.tagName===selector.toUpperCase();
  }
  closest(selector){let node=this;while(node){if(node.matches(selector))return node;node=node.parentElement;}return null;}
  querySelectorAll(selector){const result=[];const visit=node=>{for(const child of node.children){if(child.matches(selector))result.push(child);visit(child);}};visit(this);return result;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
function parse(source){
  const tree=new Element('root'),stack=[tree];
  for(const match of source.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/?([\w-]+)([^>]*)>|([^<]+)/g)){
    if(match[0].startsWith('<!'))continue;
    if(match[3]){stack.at(-1).text+=decode(match[3]);continue;}
    if(match[0].startsWith('</')){if(stack.length>1)stack.pop();continue;}
    const attrs={};for(const attr of(match[2]||'').matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)'|=([^\s>]+))?/g))attrs[attr[1]]=decode(attr[2]??attr[3]??attr[4]??'');
    const element=new Element(match[1],attrs);stack.at(-1).append(element);
    if(!['META','LINK','INPUT','BR','HR','IMG','SOURCE','WBR'].includes(element.tagName))stack.push(element);
  }
  return tree;
}
function environment(options={}){
  const tree=parse(html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/g,'')),ids=new Map();
  function walk(node){if(node.id){assert(!ids.has(node.id),'Unique HTML IDs: '+node.id);ids.set(node.id,node);}node.children.forEach(walk);}walk(tree);
  const document={readyState:'complete',getElementById:id=>ids.get(id)||null,querySelectorAll:selector=>tree.querySelectorAll(selector),querySelector:selector=>tree.querySelector(selector),createElement:tag=>new Element(tag),createTextNode:text=>Object.assign(new Element('#text'),{text:String(text)}),addEventListener(){},removeEventListener(){}};
  for(const input of tree.querySelectorAll('input,select,textarea')){if(input.tagName==='SELECT'){const selected=input.querySelectorAll('option').find(option=>'selected'in option.attrs)||input.querySelector('option');if(selected)input.value=selected.attrs.value||selected.textContent;}}
  if(ids.get('model'))ids.get('model').value='CURRENT_UNRELATED_MODEL';
  if(ids.get('api-key'))ids.get('api-key').value='CURRENT_SECRET_NEVER_RENDER';
  const calls=[],pages=[],events={},rendererCalls=[];let busy=Boolean(options.busy);
  const forbidden=name=>(...args)=>{rendererCalls.push(name);throw new Error('History must not invoke '+name);};
  const api={isBusy:()=>busy,getBillingSettings:()=>({max_requests:400,billing:{}}),getBillingSummary:()=> '400 次请求；未设金额上限。',describeUsage:()=> '合成保存用量',getConfig:forbidden('getConfig'),showResult:forbidden('showResult'),setExternalBusy(value){busy=Boolean(value);},request:async url=>{calls.push({method:'GET',url});if(url.startsWith('/api/history'))return options.historyResponse?options.historyResponse(url):{items:[],total:0};return options.request?options.request(url):{};},post:async(url,body)=>{calls.push({method:'POST',url,body});throw new Error('Viewing history must not POST');}};
  const location={origin:'http://127.0.0.1:8766',hash:'#history'};
  const window={Zhuiyin:api,ZhuiyinOutcomes:outcomes,innerWidth:1200,location,ZhuiyinUI:{showPage(name){pages.push(name);location.hash='#'+name;window.dispatchEvent({type:'zhuiyin:page',detail:{page:name}});},setLimitations:forbidden('setLimitations')},ZhuiyinUploads:{showBatch:forbidden('showBatch'),showImport:forbidden('showImport')},ZhuiyinTestCenter:{showSuite:forbidden('showSuite')},ZhuiyinConcurrency:{showResult:forbidden('showConcurrency')},addEventListener(name,fn){(events[name]??=[]).push(fn);},removeEventListener(name,fn){events[name]=(events[name]||[]).filter(item=>item!==fn);},dispatchEvent(event){for(const listener of events[event.type]||[])listener(event);}};
  const context={window,document,Node:Element,URL,console,setTimeout,clearTimeout,location,fetch:forbidden('fetch'),XMLHttpRequest:forbidden('XMLHttpRequest'),localStorage:{getItem:()=>null,setItem:forbidden('localStorage.setItem')},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail;}}};
  vm.createContext(context);vm.runInContext(historyProgram,context,{timeout:1000});vm.runInContext(researchProgram,context,{timeout:1000});
  const formSnapshot=()=>JSON.stringify(tree.querySelectorAll('input,select,textarea').map(element=>({id:element.id,value:element.value,checked:element.checked,disabled:element.disabled})));
  return{window,document,tree,calls,pages,rendererCalls,$:id=>ids.get(id),formSnapshot,initialForm:formSnapshot(),get page(){return pages.at(-1);}};
}
const clone=value=>JSON.parse(JSON.stringify(value));
const drain=async()=>{for(let index=0;index<8;index++)await new Promise(resolve=>setImmediate(resolve));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
async function expandAll(element){const seen=new Set();for(let pass=0;pass<20;pass++){const pending=element.querySelectorAll('details').filter(node=>!seen.has(node));if(!pending.length)return;for(const node of pending){seen.add(node);node.open=true;await node.emit('toggle');}}throw new Error('Synthetic detail expansion did not settle');}
const good={completed:true,failed:false,business_failed:false,actual_success:true,reported_outcome:'success',report_assessment:'consistent',stats:{physical_writes:1},events:[{tool:'synthetic_write',observed:{status:'committed'},actual:{rows:1}}]};
const records=[
  ['rule','experiment',{mode:'rules',config:{agent_mode:'rules',scenario:'ticket'},baseline:clone(good),verified:clone(good),minimal_faults:['synthetic_fault']}],
  ['agent','experiment',{mode:'llm',config:{agent_mode:'llm',scenario:'ticket'},model_identity:{model:'SAVED_MODEL'},agent_specs:{baseline:{system_prompt:'SAVED_SYSTEM_INSTRUCTION',tools:[{name:'synthetic_write'}]}},baseline:clone(good),verified:clone(good)}],
  ['suite','test_suite',{kind:'test_suite',status:'completed',counts:{cases:1},cases:[{case_id:'synthetic-case',name:'SYNTHETIC_BATCH_CASE',status:'completed',strategies:{baseline:clone(good)}}]}],
  ['study','study',{kind:'study',status:'finished',mode:'llm',evaluation_protocol:'business-report-v2',planned:1,completed:1,arms:[{strategy:'baseline',planned:1,attempted:1,valid_trials:1}],samples:[{status:'valid',strategy:'baseline',trial_index:1,result:clone(good)}]}],
  ['concurrency','concurrency',{kind:'concurrency',status:'completed',variants:[{strategy:'baseline',label:'SYNTHETIC_CONCURRENCY',trials:[clone(good)]}],summary:{completed:1}}],
  ['batch','batch',{kind:'batch',cases:[{name:'SYNTHETIC_OLD_BATCH',baseline:clone(good)}],summary:{total:1}}],
  ['trace','import',{kind:'trace',events:[{tool:'SYNTHETIC_IMPORTED_TOOL'}],issues:[],summary:{events:1}}],
  ['scenarios','import',{kind:'scenarios',scenarios:[{name:'SYNTHETIC_IMPORTED_SCENARIO',scenario:'ticket'}],issues:[]}],
  ['unknown','future_kind',{kind:'future_kind',synthetic_future_field:'SYNTHETIC_FUTURE_VALUE'}]
];
function jobFrom(record,label){const[id,kind,result]=record;return{run_id:'synthetic-'+id,kind,label:label||'SYNTHETIC '+id,status:'done',created_at:'2026-10-19T01:00:00Z',result:clone(result)};}
const checks=[];
async function check(name,fn){await fn();checks.push({name,passed:true});}
(async()=>{
  await check('independent_history_region_and_asset',()=>{const env=environment();assert(html.includes('/history.js'));for(const id of['history-list','history-detail','history-detail-content','history-detail-title','history-detail-state','history-detail-back','history-detail-refresh','history-detail-export']){assert(env.$(id),'Missing '+id);assert(env.$('page-history').contains(env.$(id)),id+' belongs to history');}assert(env.$('history-detail').hidden);for(const name of['show','loading','fail','hide'])assert.equal(typeof env.window.ZhuiyinHistory?.[name],'function',name);});
  for(const record of records)await check('open_'+record[0]+'_stays_in_history_without_execution',async()=>{
    const job=jobFrom(record),env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);await drain();
    assert.equal(env.page,'history');assert(env.pages.every(page=>page==='history'));assert.equal(env.window.location.hash,'#history');assert.equal(env.$('history-detail').hidden,false);assert(env.$('history-detail').textContent.includes(job.run_id));assert(env.$('history-detail').textContent.includes(job.label));assert(env.$('history-list').hidden);assert.equal(env.formSnapshot(),env.initialForm);assert.equal(env.rendererCalls.length,0);assert.equal(env.calls.filter(call=>call.method==='POST').length,0);assert.equal(env.calls.filter(call=>!call.url.startsWith('/api/history')).length,1);
  });
  await check('label_changes_do_not_control_navigation',async()=>{for(const label of['规则检查','多轮统计 · 模型模式','批量测试 · 快速检查','使用方法','<img src=x onerror=alert(1)>']){const job=jobFrom(records[0],label),env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);assert.equal(env.page,'history');assert(env.$('history-detail').textContent.includes(label));assert.equal(env.$('history-detail').querySelectorAll('img').length,0);assert.equal(env.rendererCalls.length,0);}});
  await check('saved_payload_and_labels_render_as_text_not_executable_markup',async()=>{const env=environment(),job=jobFrom(records.at(-1),'<script>throw Error("synthetic")</script>');job.result.synthetic_future_field='<svg onload=alert(1)> SYNTHETIC_SAFE_TEXT';env.window.ZhuiyinHistory.show(job);await expandAll(env.$('history-detail'));assert(env.$('history-detail').textContent.includes('<svg onload=alert(1)>'));assert.equal(env.$('history-detail').querySelectorAll('svg').length,0);assert.equal(env.$('history-detail').querySelectorAll('script').length,0);assert.equal(env.calls.filter(call=>call.method==='POST').length,0);});
  await check('partial_result_and_error_are_retained',async()=>{const job=jobFrom(records[2]);job.status='interrupted';job.partial_result={...job.result,synthetic_partial:'SYNTHETIC_SAVED_PARTIAL'};delete job.result;job.error='SYNTHETIC_SAVED_INTERRUPTION';const env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);await expandAll(env.$('history-detail'));assert.equal(env.page,'history');assert(env.$('history-detail').textContent.includes('SYNTHETIC_SAVED_PARTIAL'));assert(env.$('history-detail').textContent.includes(job.error));assert(!env.$('history-detail-state').classList.contains('success'));});
  await check('running_without_checkpoint_is_visible_without_polling_or_execution',async()=>{const job={run_id:'synthetic-pending',kind:'experiment',status:'running',label:'SYNTHETIC_PENDING'},env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);await drain();assert.equal(env.page,'history');assert.equal(env.$('history-detail').hidden,false);assert(env.$('history-detail').textContent.includes(job.run_id));assert.equal(env.calls.filter(call=>!call.url.startsWith('/api/history')).length,1);assert(!env.$('history-detail-state').classList.contains('success'));});
  await check('failed_without_result_preserves_record_in_history',async()=>{const job={run_id:'synthetic-failed',kind:'experiment',status:'error',label:'SYNTHETIC_FAILURE',error:'SYNTHETIC_NO_RESULT_ERROR'},env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);assert.equal(env.page,'history');assert(env.$('history-detail').textContent.includes(job.error));assert(!env.$('history-detail-state').classList.contains('success'));});
  await check('legacy_missing_fields_are_not_backfilled_from_current_settings',async()=>{const job={run_id:'synthetic-legacy',kind:'experiment',status:'done',label:'SYNTHETIC_LEGACY',result:{baseline:{failed:false}}},env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);const text=env.$('history-detail').textContent;assert(text.includes(job.run_id));assert(!text.includes('CURRENT_UNRELATED_MODEL'));assert(!text.includes('CURRENT_SECRET_NEVER_RENDER'));assert.equal(env.formSnapshot(),env.initialForm);});
  await check('back_returns_to_history_list_without_navigation_or_execution',async()=>{const env=environment({request:()=>jobFrom(records[0])});await env.window.ZhuiyinResearch.openHistory('synthetic-rule');await env.$('history-detail-back').click();assert(env.$('history-detail').hidden);assert.equal(env.$('history-list').hidden,false);assert(env.pages.every(page=>page==='history'));assert.equal(env.calls.filter(call=>call.method==='POST').length,0);});
  await check('back_retains_second_page_and_its_existing_rows',async()=>{const jobs=Array.from({length:25},(_,index)=>({...jobFrom(records[index%records.length]),run_id:'synthetic-page-'+index,label:'SYNTHETIC_PAGE_ROW_'+index})),env=environment({request:url=>jobs.find(job=>url.endsWith(job.run_id)),historyResponse:url=>{const offset=Number(new URL(url,'http://localhost').searchParams.get('offset'));return{items:jobs.slice(offset,offset+10),total:jobs.length};}});await drain();await env.$('history-pagination').querySelectorAll('button')[1].click();await drain();assert(env.$('history-pagination').textContent.includes('2 / 3'));assert(env.$('history-content').textContent.includes('SYNTHETIC_PAGE_ROW_10'));const savedRows=env.$('history-content').children[0],savedPagination=env.$('history-pagination').children[0];await env.window.ZhuiyinResearch.openHistory('synthetic-page-10');await env.$('history-detail-back').click();assert.equal(env.$('history-list').hidden,false);assert.equal(env.$('history-content').children[0],savedRows);assert.equal(env.$('history-pagination').children[0],savedPagination);assert(env.$('history-pagination').textContent.includes('2 / 3'));assert(env.$('history-content').textContent.includes('SYNTHETIC_PAGE_ROW_10'));});
  await check('refresh_only_reads_current_saved_record',async()=>{const job=jobFrom(records[0]),env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);await env.$('history-detail-refresh').click();await drain();const gets=env.calls.filter(call=>!call.url.startsWith('/api/history'));assert.equal(gets.length,2);assert(gets.every(call=>call.method==='GET'&&call.url==='/api/runs/'+job.run_id));assert.equal(env.page,'history');assert.equal(env.rendererCalls.length,0);});
  await check('export_uses_current_encoded_run_id_not_payload_url',async()=>{const job=jobFrom(records[8]);job.run_id='synthetic id?x=/';job.result.export_url='javascript:alert(1)';const env=environment({request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);const link=env.$('history-detail-export');assert.equal(link.href||link.getAttribute('href'),'/api/runs/'+encodeURIComponent(job.run_id)+'/export');assert(!link.hidden);});
  await check('rapid_open_ignores_late_success_for_previous_record',async()=>{const first=deferred(),second=deferred(),env=environment({request:url=>url.endsWith('synthetic-old')?first.promise:second.promise});const previous=env.window.ZhuiyinResearch.openHistory('synthetic-old'),latest=env.window.ZhuiyinResearch.openHistory('synthetic-new');second.resolve({...jobFrom(records[1]),run_id:'synthetic-new',label:'SYNTHETIC_NEWEST'});await latest;first.resolve({...jobFrom(records[0]),run_id:'synthetic-old',label:'SYNTHETIC_OUTDATED'});await previous;assert(env.$('history-detail').textContent.includes('SYNTHETIC_NEWEST'));assert(!env.$('history-detail').textContent.includes('SYNTHETIC_OUTDATED'));assert.equal(env.page,'history');});
  await check('rapid_open_ignores_late_failure_for_previous_record',async()=>{const first=deferred(),second=deferred(),env=environment({request:url=>url.endsWith('synthetic-old')?first.promise:second.promise});const previous=env.window.ZhuiyinResearch.openHistory('synthetic-old'),latest=env.window.ZhuiyinResearch.openHistory('synthetic-new');second.resolve({...jobFrom(records[1]),run_id:'synthetic-new',label:'SYNTHETIC_NEWEST'});await latest;first.reject(new Error('SYNTHETIC_OUTDATED_ERROR'));await previous;assert(env.$('history-detail').textContent.includes('SYNTHETIC_NEWEST'));assert(!env.$('history-detail').textContent.includes('SYNTHETIC_OUTDATED_ERROR'));});
  await check('back_while_loading_ignores_late_record',async()=>{const response=deferred(),env=environment({request:()=>response.promise}),pending=env.window.ZhuiyinResearch.openHistory('synthetic-delayed');await env.$('history-detail-back').click();response.resolve({...jobFrom(records[0]),run_id:'synthetic-delayed',label:'SYNTHETIC_LATE_AFTER_BACK'});await pending;assert(env.$('history-detail').hidden);assert.equal(env.$('history-list').hidden,false);assert(!env.$('history-detail').textContent.includes('SYNTHETIC_LATE_AFTER_BACK'));});
  await check('leaving_history_ignores_late_record_without_navigation',async()=>{const response=deferred(),env=environment({request:()=>response.promise}),pending=env.window.ZhuiyinResearch.openHistory('synthetic-delayed');env.window.ZhuiyinUI.showPage('workbench');response.resolve({...jobFrom(records[0]),run_id:'synthetic-delayed',label:'SYNTHETIC_LATE_AFTER_LEAVING'});await pending;assert.equal(env.page,'workbench');assert(!env.$('history-detail').textContent.includes('SYNTHETIC_LATE_AFTER_LEAVING'));assert.equal(env.formSnapshot(),env.initialForm);});
  await check('viewing_saved_history_is_allowed_during_an_unrelated_run',async()=>{const job=jobFrom(records[0]),env=environment({busy:true,request:()=>job});await env.window.ZhuiyinResearch.openHistory(job.run_id);assert.equal(env.page,'history');assert.equal(env.$('history-detail').hidden,false);assert.equal(env.formSnapshot(),env.initialForm);assert.equal(env.rendererCalls.length,0);assert.equal(env.calls.filter(call=>call.method==='POST').length,0);});
  await check('request_failure_shows_read_error_without_rerun',async()=>{const env=environment({request:()=>{throw new Error('SYNTHETIC_READ_ERROR');}});await env.window.ZhuiyinResearch.openHistory('synthetic-unavailable');assert.equal(env.page,'history');assert(env.$('history-detail').textContent.includes('SYNTHETIC_READ_ERROR'));assert.equal(env.calls.filter(call=>call.method==='POST').length,0);assert.equal(env.rendererCalls.length,0);});
  console.log(JSON.stringify({passed:true,checks,scope:'Synthetic DOM contracts only; no dataset reads, browser rendering, server, parser, engine, or model calls.'},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
