'use strict';
// Integration events and explicit actions in a small DOM, not a browser test.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
class Element{
  constructor(tag,text=''){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this._text=text;this._value='';this.dataset={};this.isConnected=true;this.disabled=false;this.checked=false;this.classList={add:()=>{},remove:()=>{},toggle:()=>{}};}
  set value(value){this._value=String(value);}get value(){return this._value;}
  set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(item=>item.textContent).join('');}
  set innerHTML(value){throw new Error('Unsafe HTML '+value);}
  append(...items){this.children.push(...items);}prepend(...items){this.children.unshift(...items);}replaceChildren(...items){this._text='';this.children=[...items];}
  after(...items){this.afterItems=items;}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}setAttribute(){}focus(){}
  dispatchEvent(event){for(const fn of this.listeners[event.type]||[])fn({...event,target:event.target||this});return true;}
  async emit(type,event={}){for(const fn of this.listeners[type]||[])await fn({...event,target:event.target||this});}
}
class Event{constructor(type,options={}){this.type=type;Object.assign(this,options);}}
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value)),ids=new Map(),listeners={};
const id=value=>{if(!ids.has(value))ids.set(value,new Element('div'));return ids.get(value);};
function all(node){return [node,...node.children.flatMap(all)];}
const document={getElementById:id,createElement:tag=>new Element(tag),createTextNode:text=>new Element('#text',text),
  querySelectorAll(selector){if(selector==='[data-workbench-home]')return [id('home')];if(selector==='[data-product-run-control]')return [...ids.values()].flatMap(all).filter(item=>item.dataset.productRunControl);return [];}};
const window={addEventListener(type,fn){(listeners[type]??=[]).push(fn);},dispatchEvent(event){for(const fn of listeners[event.type]||[])fn(event);return true;}};
const snapshot={id:'123456789abc',version:7,name:'历史模板',description:'保留的规则',config:{scenario:'equipment',stock:6,quantity:2,request_id:'saved-001'},contract:{record_count:{mode:'exactly',value:1},expected_fields:{quantity:2},stock_delta:-2}};
let config={scenario:'equipment',equipment:'相机',stock:3,quantity:1,request_id:'current-001',agent_mode:'rules',max_requests:400,faults:[]},busy=false,applied=[],requests=[],posts=[],runs=0,clears=0,setCalls=0,templateOptions,diagnoses=0,previewCount=0;
const savedConfig={scenario:'equipment',stock:6,quantity:2,request_id:'saved-001',max_requests:22,billing:{max_cost_cny:1,input_cache_miss_price_per_million:3,output_price_per_million:12},template_snapshot:snapshot};
window.Zhuiyin={getConfig(){return clone(config);},getBillingSettings(){return {max_requests:50,billing:{input_cache_hit_price_per_million:.3,input_cache_miss_price_per_million:3,output_price_per_million:12}};},isBusy:()=>busy,
  applyScenario(value){if(busy)return false;applied.push(clone(value));config=clone(value);window.dispatchEvent(new Event('zhuiyin:config'));return true;},
  request:async url=>{requests.push(url);return {config:clone(savedConfig),candidates:[{candidate_id:'case-001',eligible:true,failed:true,name:'需重测任务',strategy:'baseline',config:clone(savedConfig)}]};},
  post:async(url,body)=>{posts.push({url,body:clone(body)});return {preview_id:'preview-only'};},runExperiment(){runs++;}};
window.ZhuiyinTemplates={mount(host,options){templateOptions=options;return {clear(){clears++;options.onSelect(null);},setSnapshot(value){setCalls++;options.onSelect(clone(value));}};}};
window.ZhuiyinCompare={mount(){return {refresh(){}};}};
window.ZhuiyinDiagnostics={mount(){diagnoses++;}};
window.ZhuiyinTestCenter={showPrepared(){previewCount++;}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/product_workflows.js'),'utf8'),{window,document,Event,CustomEvent:Event});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function button(text){const found=all(id('history-detail-content')).find(item=>item.tagName==='BUTTON'&&item.textContent===text);assert.ok(found,'Button '+text);return found;}
(async()=>{
  const reusable=window.Zhuiyin.readReusableConfig();assert.equal(reusable.max_requests,50);assert.equal(reusable.agent_mode,'llm');assert.equal(reusable.billing.output_price_per_million,12);
  templateOptions.onSelect(snapshot);assert.equal(window.ZhuiyinProduct.getTemplateSnapshot().version,7);
  id('experiment-form').dispatchEvent(new Event('input',{target:{id:'api-key'}}));assert.equal(clears,0);
  id('experiment-form').dispatchEvent(new Event('change',{target:{id:'stock'}}));assert.equal(clears,1);assert.equal(window.ZhuiyinProduct.getTemplateSnapshot(),null);
  templateOptions.onSelect(snapshot);window.Zhuiyin.applyReusableConfig(savedConfig);assert.equal(clears,1);assert.equal(setCalls,1);assert.equal(window.ZhuiyinProduct.getTemplateSnapshot().version,7);
  assert.equal(id('max-requests').value,'22');assert.equal(id('billing-cache-miss-price').value,'3');assert.equal(runs,0);
  const appliedBefore=applied.length;window.dispatchEvent(new Event('zhuiyin:history',{detail:{run_id:'history-001',result:{config:savedConfig}}}));await flush();
  assert.equal(applied.length,appliedBefore,'Opening history changed configuration');assert.equal(runs,0);assert.equal(posts.length,0);assert.equal(previewCount,0);
  await button('复制配置到工作台').emit('click');assert.equal(applied.length,appliedBefore+1);assert.equal(runs,0);
  await button('生成选中任务的重测预览').emit('click');assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/retest/prepare');assert.equal(previewCount,1);assert.equal(runs,0);
  busy=true;const before=applied.length;await button('复制配置到工作台').emit('click');assert.equal(applied.length,before);
  window.dispatchEvent(new Event('zhuiyin:result',{detail:{template_snapshot:snapshot,baseline:{business_failed:true,template_contract:{status:'fail',checks:[]}},fixed:{template_contract:{status:'pass',checks:[]}}}}));assert.equal(diagnoses,1);assert.equal(runs,0);assert.ok(id('diagnostics-workspace').textContent.includes('原始：不符合规则'));assert.ok(id('diagnostics-workspace').textContent.includes('幂等＋核验：符合规则'));
  assert.ok(requests.every(url=>url.endsWith('/retest-plan')));assert.ok(posts.every(item=>item.url==='/api/retest/prepare'));
  console.log('Product workflow integration: read-only history, explicit copy/preview, template binding, budget restore, busy and no-execution checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
