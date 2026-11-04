"""Capture one actual Dashboard/C-policy replay on an isolated simulated state."""
from __future__ import annotations
import datetime, hashlib, json, os, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
DASH=ROOT/'openvela_smart_home'/'dashboard'
sys.path.insert(0,str(DASH))
import bridge_server as bridge
from offload_gateway import LinkSnapshot, OffloadGateway, RuleBackend, SubprocessPolicyClient, append_jsonl
OUT=ROOT/'docs/video/v2'
OUT.mkdir(parents=True,exist_ok=True)
EVIDENCE=OUT/'software_replay.json'
if EVIDENCE.exists(): raise SystemExit('Refusing to overwrite an actual capture; archive first')
original_state=bridge.STATE_FILE
original_gateway=bridge.gateway
original_monitor=bridge.backend_monitor
original_bytes=Path(original_state).read_bytes() if Path(original_state).exists() else None
policy=None
rows=[]
try:
    bridge.backend_monitor=None
    with tempfile.TemporaryDirectory(prefix='sensehome-video-replay-') as temp:
        bridge.STATE_FILE=str(Path(temp)/'isolated_state.json')
        initial=bridge.default_state()
        bridge.save_state(initial)
        policy=SubprocessPolicyClient(str(ROOT/'SenseHome-6G/net_offload/policy_cli.exe'))
        event_path=OUT/'events.jsonl'
        if event_path.exists(): raise RuntimeError('Refusing to overwrite events')
        bridge.gateway=OffloadGateway(policy=policy,backends={'local-rules':RuleBackend(bridge._gateway_local_handler)},link_snapshot=LinkSnapshot(wan_ok=False,lan_ok=False),event_sink=append_jsonl(str(event_path)),action_executor=bridge._gateway_action_executor)
        commands=[('开灯','打开客厅的灯'),('只读查询','查询安防状态'),('复合指令不局部执行','关闭客厅的灯并打开卧室的灯'),('关灯','关闭客厅的灯')]
        for index,(name,command) in enumerate(commands,1):
            before=bridge.load_state()
            began=datetime.datetime.now(datetime.timezone.utc).isoformat()
            start=time.perf_counter()
            result=bridge.process_command(command)
            wall=(time.perf_counter()-start)*1000
            after=bridge.load_state()
            rows.append({'step':index,'operation':name,'command':command,'captured_at_utc':began,'wall_ms':wall,'result':result,'state_before':before,'state_after':after})
        assert rows[0]['state_after']['lights']['客厅']=={'is_on':True,'brightness':100}
        assert rows[1]['state_before']==rows[1]['state_after']
        assert rows[1]['result']['semantic_complete'] is True
        assert rows[2]['state_before']==rows[2]['state_after']
        assert rows[2]['result']['semantic_complete'] is False
        assert rows[2]['result'].get('actions')==[]
        assert rows[3]['state_after']['lights']['客厅']=={'is_on':False,'brightness':0}
        assert all(r['result']['backend']=='local-rules' for r in rows)
finally:
    if policy: policy.close()
    bridge.STATE_FILE=original_state
    bridge.gateway=original_gateway
    bridge.backend_monitor=original_monitor
if original_bytes is not None:
    assert Path(original_state).read_bytes()==original_bytes,'Original state modified'
report={'meta':{'captured_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'actual Dashboard process_command + actual C policy + isolated PC JSON simulated state; no browser/desktop UI','network':'WAN=false/LAN=false injected LinkSnapshot','playback_timing':'edited pacing; not execution timing','sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [DASH/'bridge_server.py',DASH/'device_actions.py',ROOT/'SenseHome-6G/net_offload/offload_gateway.py',ROOT/'SenseHome-6G/net_offload/policy_cli.exe']},'original_state_unchanged':True},'rows':rows}
EVIDENCE.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
for row in rows:
    r=row['result']; print(json.dumps({'step':row['step'],'command':row['command'],'backend':r['backend'],'reason':r['reason'],'semantic_complete':r['semantic_complete'],'wall_ms':round(row['wall_ms'],3),'event_id':r['event_id']},ensure_ascii=True))
