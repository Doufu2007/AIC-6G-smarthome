const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const PAGE = path.resolve(__dirname, '../../../openvela_smart_home/dashboard/web_panel.html');
const script = fs.readFileSync(PAGE, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

function element(tag='div') {
  return {tagName:tag.toUpperCase(), style:{}, value:'', textContent:'', innerHTML:'', className:'',
    children:[], appendChild(child){this.children.push(child);}, addEventListener(){}};
}
function fixture() {
  const elements = new Map();
  const context = vm.createContext({
    document:{getElementById(id){if(!elements.has(id))elements.set(id, element());return elements.get(id);}, createElement:element},
    fetch:async()=>{throw new Error('not connected');}, setTimeout(){}, setInterval(){}, Date, console,
  });
  vm.runInContext(script, context);
  return {context, elements, async settle(){await Promise.resolve();await Promise.resolve();},
    logs(){return elements.get('logContainer').children.map(x=>({type:x.className, html:x.innerHTML}));}};
}
function response(value, status=200){return {ok:status>=200&&status<300,status,json:async()=>value};}
function validState(){const rooms=['客厅','卧室','厨房','浴室','书房','室外'];return {
  lights:Object.fromEntries(rooms.map(r=>[r,{is_on:false,brightness:0}])),
  curtains:Object.fromEntries(rooms.map(r=>[r,{is_open:false,position:0}])),
  temperature:Object.fromEntries(rooms.map(r=>[r,{temp:25,humidity:60}])), sensors:{}, security:{},
};}

for (const [name, failing] of [
  ['fetch rejection', async()=>{throw new TypeError('Failed to fetch');}],
  ['HTTP 503 with success-looking body', async()=>response({answer:'已自动开灯80%',actions:[{description:'已开启80%'}],semantic_complete:true},503)],
  ['malformed JSON', async()=>({ok:true,status:200,json:async()=>{throw new SyntaxError('bad json');}})],
]) test(name+' does not fabricate an action', async()=>{
  const f=fixture();await f.settle();f.context.fetch=failing;
  await f.context.sendCmd('太暗了');
  const logs=f.logs();assert.equal(logs.filter(x=>x.type.includes('action')).length,0);
  assert.match(logs.map(x=>x.html).join('\n'),/执行未确认/);
  assert.doesNotMatch(logs.map(x=>x.html).join('\n'),/光照1200|自动开灯80|\[Light\]|✅/);
  assert.equal(JSON.stringify(vm.runInContext('state',f.context)),'{}');
});

test('business failure remains unconfirmed and does not label its answer success',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response({semantic_complete:false,interface_ok:true,answer:'请指定房间',actions:[]});
  await f.context.sendCmd('有点暗');const logs=f.logs().map(x=>x.html).join('\n');
  assert.match(logs,/请指定房间/);assert.match(logs,/未完成/);assert.doesNotMatch(logs,/✅/);
});

test('only structured successful gateway result can report completion; output is escaped',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response({semantic_complete:true,interface_ok:true,backend:'local-rules',reason:'<script>bad</script>',actions:[{description:'<img src=x onerror=bad()>'}],answer:'<b>已完成</b>'});
  await f.context.sendCmd('<img onerror=evil()>');const logs=f.logs().map(x=>x.html).join('\n');
  assert.match(logs,/网关报告完成/);assert.doesNotMatch(logs,/<img|<script>|<b>已完成/);
  assert.match(logs,/&lt;img/);
});

test('HTTP200 invalid result is not trusted',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response({answer:'成功',semantic_complete:'true',actions:'fake'});
  await f.context.sendCmd('开灯');assert.match(f.logs().map(x=>x.html).join('\n'),/执行未确认/);
  assert.equal(f.logs().filter(x=>x.type.includes('action')).length,0);
});

test('state HTTP failure and invalid schema clear confirmation rather than invent defaults',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response(validState());await f.context.fetchState();
  assert.equal(vm.runInContext('Object.keys(state.lights).length',f.context),6);
  f.context.fetch=async()=>response({error:'not ready'},503);await f.context.fetchState();
  assert.equal(JSON.stringify(vm.runInContext('state',f.context)),'{}');
  assert.match(f.elements.get('stateStatus').textContent,/未知/);
  f.context.fetch=async()=>response({lights:{},temperature:{},curtains:{}});await f.context.fetchState();
  assert.equal(JSON.stringify(vm.runInContext('state',f.context)),'{}');
  assert.match(f.elements.get('roomGrid').children.map(x=>x.innerHTML).join('\n'),/未知/);
});

test('missing sensors do not display fake zero or absence',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response(validState());await f.context.fetchState();
  assert.match(f.elements.get('sensorDetail').innerHTML,/未知/);
  assert.doesNotMatch(f.elements.get('sensorDetail').innerHTML,/0\.0 lux|CO2=0|节能关灯/);
});

test('health shows both endpoints and unknown telemetry, including failed transport',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response({backends:{'local-model':{ready:true,reachable:true},'cloud-model':{ready:false,reachable:true}},endpoint_age_ms:{'local-model':12},link_basis:'HTTP endpoint RTT; not WAN'});
  await f.context.fetchHealth();let text=f.elements.get('llmInfo').textContent;
  assert.match(text,/local-model/);assert.match(text,/cloud-model/);assert.match(text,/ready=true/);assert.match(text,/ready=false/);assert.match(text,/未知/);assert.match(text,/HTTP endpoint/);
  f.context.fetch=async()=>{throw new TypeError('offline');};await f.context.fetchHealth();
  assert.match(f.elements.get('llmInfo').textContent,/未知/);
});

test('completed model plan text is attributed separately from gateway completion',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response({
    semantic_complete:true,interface_ok:true,selected_backend:'local-model',backend:'local-model',
    actual_backend:'local-model',source:'local-model:real-model',
    answer:'动作计划已生成，等待网关执行确认。',actions:[{description:'书房窗帘位置=100%'}],
  });
  await f.context.sendCmd('关闭书房的灯并打开书房窗帘');
  const completed=f.logs().filter(x=>x.type==='log-entry answer');
  assert.equal(completed.length,1);
  assert.match(completed[0].html,/网关.*确认.*完成/);
  assert.doesNotMatch(completed[0].html,/等待网关执行确认/);
  assert.match(f.logs().map(x=>x.html).join('\n'),/规划器[\s\S]*等待网关执行确认/);
});

test('planning recovery shows C first choice, actual alternate and primary upstream error',async()=>{
  const f=fixture();await f.settle();const recovery={attempted:true,from:'local-model',to:'cloud-model',
    actual_backend:'cloud-model',primary_error:'HTTP_502',primary_response:{error:'UPSTREAM_HTTP_503'},
    secondary_status:{ok:true,error:null},reason:'secondary_success'};
  f.context.fetch=async()=>response({semantic_complete:true,interface_ok:true,selected_backend:'local-model',
    backend:'local-model',reason:'COST_LOCAL',source:'cloud-model:real-model',actual_backend:'cloud-model',recovery,
    actions:[{description:'书房灯光亮度=0%'},{description:'书房窗帘位置=100%'}],
    answer:'动作计划已生成，等待网关执行确认。',
    gateway_event:{selected_backend:'local-model',source:'cloud-model:real-model',response:{actual_backend:'cloud-model',recovery}},
  });
  await f.context.sendCmd('关闭书房的灯并打开书房窗帘');
  const logs=f.logs().map(x=>x.html).join('\n');
  assert.match(logs,/C初选backend=local-model/);
  assert.match(logs,/实际规划来源=cloud-model/);
  assert.match(logs,/source=cloud-model:real-model/);
  assert.match(logs,/HTTP_502/);assert.match(logs,/UPSTREAM_HTTP_503/);
  assert.match(logs,/secondary_success/);
  assert.equal(f.logs().filter(x=>x.type==='log-entry action').length,2);
});

test('actual planning provenance falls back to real source when explicit actual_backend is absent',async()=>{
  const f=fixture();await f.settle();f.context.fetch=async()=>response({
    semantic_complete:true,interface_ok:true,selected_backend:'local-model',backend:'local-model',
    source:'cloud-model:real-model',answer:'已生成计划',actions:[{description:'书房窗帘位置=100%'}],
  });
  await f.context.sendCmd('关闭书房的灯并打开书房窗帘');
  assert.match(f.logs().map(x=>x.html).join('\n'),/实际规划来源=cloud-model:real-model/);
});
