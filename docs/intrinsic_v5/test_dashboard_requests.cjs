const test=require('node:test'); const assert=require('node:assert/strict');
const fs=require('node:fs'); const vm=require('node:vm'); const path=require('node:path');
const script=fs.readFileSync(path.resolve(__dirname,'../../openvela_smart_home/dashboard/web_panel.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
function fixture(storage=new Map()) {
 const elements=new Map(); const el=()=>({style:{},value:'',textContent:'',innerHTML:'',children:[],appendChild(c){this.children.push(c)},addEventListener(){}});
 const context=vm.createContext({document:{getElementById(id){if(!elements.has(id))elements.set(id,el());return elements.get(id)},createElement:el},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  fetch:async()=>{throw Error('offline')},setTimeout(){},setInterval(){},Date,Math,console});
 vm.runInContext(script,context); return {context,elements,storage};
}
const response=d=>({ok:true,status:200,json:async()=>d});
test('lost response keeps client ID; repeat queries original instead of posting again',async()=>{
 const f=fixture(); let posts=[]; let gets=[];
 f.context.fetch=async(url,options)=>{if(options?.method==='POST'){posts.push(JSON.parse(options.body));throw Error('lost')} gets.push(url);return response({command_id:posts[0]?.context.command_id,request_state:'processing',semantic_complete:false,interface_ok:false,actions:[]})};
 await f.context.sendCmd('打开客厅的灯'); await f.context.sendCmd('打开客厅的灯');
 assert.equal(posts.length,1); assert.match(posts[0].context.command_id,/^[A-Za-z0-9_.-]+$/);
 assert.ok(posts[0].context.session_id); assert.ok(gets.some(u=>u.includes('/api/requests/'+posts[0].context.command_id)));
 assert.ok(f.storage.get('sensehome.pendingRequest'));
});
test('reload restores pending identity and recovery only performs GET',async()=>{
 const storage=new Map([['sensehome.pendingRequest',JSON.stringify({id:'fixed-a',command:'打开客厅的灯',kind:'command'})]]);
 const f=fixture(storage); const calls=[];
 f.context.fetch=async(url,options)=>{calls.push([url,options]);return response({command_id:'fixed-a',request_state:'finished',semantic_complete:true,task_complete:true,interface_ok:true,actions:[]})};
 await f.context.recoverPendingRequest();
 assert.ok(calls.some(([u,o])=>u==='/api/requests/fixed-a'&&!o?.method));
 assert.equal(calls.filter(([,o])=>o?.method==='POST').length,0);
 assert.equal(storage.has('sensehome.pendingRequest'),false);
});
test('disabled patrol report never displays completion',async()=>{
 const f=fixture();f.context.fetch=async()=>response({status:'ok',report:{active:false,error:'DISABLED',actions:[],anomalies:[],auto_decisions:0}});
 await f.context.triggerPatrol();
 const logs=f.elements.get('logContainer').children.map(c=>c.innerHTML).join('\n');
 assert.doesNotMatch(logs,/网关报告巡检完成/);assert.match(logs,/未完成|未确认|不可用/);
});

test('not-found request can be archived explicitly and later queried without blocking new intent',async()=>{
 const storage=new Map([['sensehome.pendingRequest',JSON.stringify({id:'missing-a',command:'打开客厅的灯',kind:'command'})]]);
 const f=fixture(storage); const calls=[];
 f.context.fetch=async(url,options)=>{calls.push([url,options]);return response({command_id:'missing-a',request_state:'not_found',error:'REQUEST_NOT_FOUND',semantic_complete:false,interface_ok:false,actions:[]})};
 await f.context.recoverPendingRequest();
 assert.ok(storage.has('sensehome.pendingRequest'));
 f.context.archivePendingRequest();
 assert.equal(storage.has('sensehome.pendingRequest'),false);
 const archived=JSON.parse(storage.get('sensehome.requestHistory'));
 assert.equal(archived[0].id,'missing-a');
 assert.equal(archived[0].command,'打开客厅的灯');
 const next=await f.context.beginClientRequest('关闭书房的灯');
 assert.notEqual(next.id,'missing-a');
 await f.context.recoverArchivedRequest('missing-a');
 assert.equal(JSON.parse(storage.get('sensehome.pendingRequest')).id,next.id);
 assert.equal(calls.filter(([,o])=>o?.method==='POST').length,0);
 const reload=fixture(storage);
 assert.match(reload.elements.get('requestHistorySelect').innerHTML,/missing-a/);
});

test('late old response cannot clear the newer pending request',async()=>{
 const f=fixture(); let release; const oldReply=new Promise(resolve=>{release=resolve});
 f.context.fetch=async(url,options)=> options?.method==='POST' ? oldReply : response({});
 const first=f.context.sendCmd('打开客厅的灯');
 await new Promise(resolve=>setImmediate(resolve));
 const old=JSON.parse(f.storage.get('sensehome.pendingRequest'));
 f.context.archivePendingRequest();
 const next=await f.context.beginClientRequest('关闭书房的灯');
 release(response({command_id:old.id,request_state:'finished',semantic_complete:true,interface_ok:true,actions:[]}));
 await first;
 assert.equal(JSON.parse(f.storage.get('sensehome.pendingRequest')).id,next.id);
});
