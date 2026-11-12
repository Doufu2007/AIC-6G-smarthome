"""Offline verifier: no runtime imports, requests, or model calls."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

HERE=Path(__file__).resolve().parent
ARMS=('local-first','second-first','joint')
def read(name):
    return json.loads((HERE/name).read_text(encoding='utf-8-sig'))
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def require(value,message):
    if not value: raise AssertionError(message)

def normalized(actions):
    values=[]
    for a in actions:
        p=deepcopy(a['params']); name=a['name']
        # Independent semantic forms, matching the device contract.
        if name=='light_control':
            action=p.get('action')
            if 'brightness' in p: p['action']='on' if p['brightness']>0 else 'off'
            elif action=='on': p['brightness']=100
            elif action=='off': p['brightness']=0
        elif name=='curtain_control':
            action=p.get('action')
            if 'position' in p: p['action']='open' if p['position']>0 else 'close'
            elif action=='open': p['position']=100
            elif action=='close': p['position']=0
        values.append(json.dumps({'name':name,'params':p},sort_keys=True,ensure_ascii=False))
    return sorted(values)

def state_matches(expected,before,after):
    target=deepcopy(before)
    for action in expected:
        p=action['params']; room=p['location']
        if action['name']=='light_control':
            if 'brightness' in p:
                target['lights'][room]={'is_on':p['brightness']>0,'brightness':p['brightness']}
            elif p['action']=='on':
                target['lights'][room]={'is_on':True,'brightness':100}
            else:
                target['lights'][room]={'is_on':False,'brightness':0}
        elif action['name']=='curtain_control':
            position=p.get('position',100 if p.get('action')=='open' else 0)
            target['curtains'][room]={'is_open':position>0,'position':position}
        else:
            raise AssertionError('unexpected primary read action')
    return target==after

def verify():
    registration=read('registration.json'); result=read('results.json')
    rows=[json.loads(line) for line in (HERE/'rows.jsonl').read_text(encoding='utf-8').splitlines()]
    blocks=read('blocks.json'); initial=read('initial_state.json')
    require(len(rows)==96,'all 96 rows required')
    require(len(blocks)==32,'all 32 blocks required')
    require(canonical(registration['schedule'])==registration['schedule_sha256'],'schedule hash')
    require(canonical(registration['cases'])==registration['cases_sha256'],'case hash')
    require(canonical(initial)==registration['initial_state_sha256'],'initial hash')
    for path,expected in registration['source_sha256'].items():
        require(sha(HERE/path)==expected,'source changed: '+path)
    keys=[(j['condition'],j['case_id'],arm) for j in registration['schedule'] for arm in j['arms']]
    require(keys==[(r['condition'],r['case_id'],r['arm']) for r in rows],'registered row sequence changed')
    require(len(keys)==len(set(keys)),'duplicate rows')
    require(all(datetime.fromisoformat(r['started_at_utc'])>datetime.fromisoformat(registration['registered_at_utc']) for r in rows),'row precedes registration')
    blockmap={b['pair_id']:b for b in blocks}; cases={c['id']:c for c in registration['cases']}
    post_count=Counter(); usage=Counter(); chosen=Counter(); timing={}; scores=[]
    for row in rows:
        require(row['state_before']==initial,'initial state drift')
        require(row['command']==cases[row['case_id']]['command'],'command drift')
        require(row['expected_actions']==cases[row['case_id']]['expected_actions'],'label drift')
        block=blockmap[row['pair_id']]
        require(row['observation_sha256']==block['observation_sha256']==canonical(block['observation']),'unpaired observation')
        require(row['arm']==block['order'][row['order_index']],'arm order')
        event=row.get('gateway_event',{})
        expected=row['expected_actions']; actual=event.get('actions',[])
        primary=row['cohort']=='reviewed_complex_regression'
        confirmation=bool(row['executor_results'] and all(x.get('semantic_complete') for x in row['executor_results']))
        complete=bool(expected and normalized(expected)==normalized(actual)
            and state_matches(expected,row['state_before'],row['state_after'])
            and confirmation and event.get('interface_ok') and event.get('semantic_complete')
            and not event.get('error') and not event.get('fallback_from'))
        require(complete==row['task_completed'],'task completion mismatch '+row['pair_id']+row['arm'])
        require(row['executor_calls']==len(row['executor_results']),'executor count')
        require(row['executor_calls']<=1,'duplicate execution')
        if complete: require(row['executor_calls']==1,'success without execution')
        if not primary:
            safe=not actual and row['state_before']==row['state_after']
            require(safe==row['safe_no_action_structure'],'empty-plan score mismatch')
        timing=row['timing_ms']
        require(timing['c_internal'] is None,'internal C falsely measured')
        require(abs(timing['c_decision_ipc']-sum(c['wall_ms'] for c in row['c_commands'] if c['command'].startswith('decide ')))<0.00001,'C timing')
        if row['status']=='finished':
            require(timing['total_wall']+.1>=timing['gateway_wall']+timing['observation_health'],'wall decomposition')
            require(timing['gateway_wall']+.2>=timing['policy_adapter']+timing['first_model']+timing['backup_model']+timing['recovery_health']+timing['executor'],'gateway decomposition')
        recovery=row['recovery_records']
        require(len(recovery)<=1,'multiple recovery wrappers')
        require(len(row['backend_attempts'])<=2,'too many planners')
        proxy={p['trace_id']:p for p in row['proxy_records'] if p['method']=='POST'}
        require(len(proxy)==len(row['backend_attempts']),'POST accounting')
        for attempt in row['backend_attempts']:
            exchanges=[x for x in attempt['upstream_exchanges'] if x['method']=='POST']
            require(len(exchanges)==1,'unexpected upstream count')
            exchange=exchanges[0]; request=exchange['request']
            require(request['temperature']==0 and request['max_tokens']==256,'parameters changed')
            require(request['messages'][1]['content']==json.dumps(attempt['request'],ensure_ascii=False),'input changed')
            trace=exchange['response']['_sensehome_proxy_trace']['trace_id']
            p=proxy[trace]
            require(p['http_status']==exchange['http_status'],'proxy HTTP mismatch')
            post_count['proxy_post']+=1
            post_count['model_post']+=int(p['upstream_forwarded'])
            post_count['proxy_503']+=int(not p['upstream_forwarded'] and p['http_status']==503)
            if p['upstream_forwarded']:
                raw=p['upstream_exchange']['response']
                require(raw['usage']==exchange['response']['usage'],'usage mismatch')
                usage.update({k:raw['usage'].get(k,0) for k in ('prompt_tokens','completion_tokens','total_tokens')})
            if row['condition']=='both_post503':
                require(not p['upstream_forwarded'] and p['http_status']==503,'double fault forwarded')
            if row['condition'] in ('local_wait700','second_wait700') and p['upstream_forwarded']:
                delayed='local' if row['condition']=='local_wait700' else 'cloud'
                require(p['configured_admission_wait_ms']==(700 if p['tier']==delayed else 0),'wait manipulation')
                if p['tier']==delayed: require(p['actual_admission_wait_ms']>=690,'real wait absent')
        if primary: scores.append((row['arm'],complete,timing['total_wall']))
        chosen[event.get('selected_backend','unknown')]+=1
    by_arm={}
    for arm in ARMS:
        selected=[r for r in rows if r['cohort']=='reviewed_complex_regression' and r['arm']==arm]
        by_arm[arm]={'n':len(selected),'completed':sum(r['task_completed'] for r in selected),
            'median_total_wall_ms_all_requests':statistics.median(r['timing_ms']['total_wall'] for r in selected)}
        for k,v in by_arm[arm].items(): require(result['by_arm'][arm][k]==v,'summary mismatch '+arm+k)
    recommended=min(ARMS,key=lambda a:(-by_arm[a]['completed'],by_arm[a]['median_total_wall_ms_all_requests'],ARMS.index(a)))
    require(recommended==result['recommendation']['arm'],'recommendation mismatch')
    runtime=read('runtime_manifest.json')
    require(runtime['status']=='stopped','owned runtime not stopped')
    return {'schema':'sensehome.fair-recovery-verification.v4','status':'PASS','n_rows':len(rows),'primary_rows':72,
        'ancillary_rows':24,'source_hashes_verified':len(registration['source_sha256']),
        'counts':dict(post_count),'usage_evaluation_only':dict(usage),'selected_backend':dict(chosen),
        'recomputed_by_arm':by_arm,'recomputed_recommendation':recommended,
        'runtime_status':runtime['status'],'runtime_pids':[x['pid'] for x in runtime['children']],
        'limits':'offline independent action/state/POST/timing/accounting checks; same host and prior human-reviewed cases; no human answer-semantic scoring'}

if __name__=='__main__':
    report=verify()
    (HERE/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))
