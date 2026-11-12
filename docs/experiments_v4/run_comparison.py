"""One registered three-arm comparison using frozen production planners."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
FROZEN=HERE/'frozen_source'
sys.path.insert(0,str(FROZEN/'docs/deployment'))
sys.path.insert(0,str(FROZEN/'docs/demo_v3'))
import fused_eval as f
from real_fault_eval_v2 import ControlledProxy
from planning_recovery import PlanningRecoveryBackend
from runtime import LocalRuntime,now,write,digest

ARMS=('local-first','second-first','joint')
MODES={'local-first':'static-local','second-first':'static-cloud','joint':'joint'}
CONDITIONS=('healthy','local_wait700','second_wait700','local_post503','second_post503','both_post503')
COMPLEX=('C01','C02','C03','C04')
EMPTY=('A01','A02','A03','A04','R01','R02','R03','R04')
WARMUP='读取室外温度，然后把浴室窗帘位置设为22%'

def schedule():
    jobs=[]
    for c,condition in enumerate(CONDITIONS):
        for j,case in enumerate(COMPLEX):
            k=(c+j)%3
            jobs.append({'condition':condition,'case_id':case,'cohort':'reviewed_complex_regression',
                         'arms':list(ARMS[k:]+ARMS[:k])})
    for j,case in enumerate(EMPTY):
        k=j%3
        jobs.append({'condition':'healthy','case_id':case,'cohort':'reviewed_empty_plan_regression',
                     'arms':list(ARMS[k:]+ARMS[:k])})
    return sorted(jobs,key=lambda j:CONDITIONS.index(j['condition']))

def counts(records):
    posts=[r for r in records if r['method']=='POST']
    return {'proxy_post':len(posts),'model_post':sum(bool(r['upstream_forwarded']) for r in posts),
            'proxy_503':sum(r['http_status']==503 and not r['upstream_forwarded'] for r in posts)}

def aggregate(rows):
    return {'n':len(rows),'completed':sum(bool(r['task_completed']) for r in rows),
        'median_total_wall_ms_all_requests':statistics.median(r['timing_ms']['total_wall'] for r in rows) if rows else None,
        'executor_calls':sum(r['executor_calls'] for r in rows),
        **{key:sum(counts(r['proxy_records'])[key] for r in rows) for key in ('proxy_post','model_post','proxy_503')}}

def summarize(rows):
    primary=[r for r in rows if r['cohort']=='reviewed_complex_regression']
    result={'schema':'sensehome.fair-recovery-results.v4','n_rows':len(rows),'primary_rows':len(primary),
        'runner_failed':sum(r['status']!='finished' for r in rows),
        'by_arm':{a:aggregate([r for r in primary if r['arm']==a]) for a in ARMS},
        'by_condition':{c:{a:aggregate([r for r in primary if r['condition']==c and r['arm']==a]) for a in ARMS} for c in CONDITIONS},
        'ancillary_empty_plan':{a:{'n':sum(r['arm']==a and r['cohort']!='reviewed_complex_regression' for r in rows),
            'safe_no_action_structure':sum(r['arm']==a and r['cohort']!='reviewed_complex_regression' and r['safe_no_action_structure'] for r in rows)}
            for a in ARMS}}
    eligible=[a for a in ARMS if result['by_arm'][a]['n']]
    chosen=min(eligible,key=lambda a:(-result['by_arm'][a]['completed'],
        result['by_arm'][a]['median_total_wall_ms_all_requests'],ARMS.index(a))) if eligible else None
    result['recommendation']={'arm':chosen,
        'rule':'maximize completed primary rows; then minimize median actual total wall over ALL primary requests including failures; then local-first,second-first,joint order',
        'scope':'provisional default for this one-host four-command reviewed regression and six equally weighted controlled conditions; not generalization or significance'}
    return result

class InventoryQueueBackend(f.DirectModelBackend):
    """Inventory remains healthy after POST faults; queue comes from real wait telemetry."""
    def __init__(self,proxy,model,tier):
        super().__init__(proxy.url,model,tier,timeout_s=15,max_tokens=256)
        self.proxy=proxy
    def health(self):
        value=super().health()
        extra=f.exchange(self.proxy.base_url,'GET','health',None,5)
        value['exchanges'].append(extra)
        value['probe_http_calls']+=1
        value['latency_ms']+=extra['wall_ms']
        telemetry=extra['response']
        value.update(queue_ms=telemetry.get('last_admission_wait_ms'),
            queue_scope='measured_fixed_proxy_wait_not_gpu_queue',
            queue_sample_age_ms=telemetry.get('admission_sample_age_ms'),
            inflight=telemetry.get('inflight'),proxy_telemetry=telemetry)
        return value

class TimedPolicy(f.ResourcePolicyClient):
    def __init__(self,*args,**kwargs):
        self.command_timings=[]
        super().__init__(*args,**kwargs)
    def _cmd(self,line):
        start=time.perf_counter()
        try:
            return super()._cmd(line)
        finally:
            self.command_timings.append({'command':line,'wall_ms':(time.perf_counter()-start)*1000})

def register():
    if (HERE/'registration.json').exists():
        raise FileExistsError('REGISTERED_RUN_CANNOT_BE_OVERWRITTEN')
    dataset=f.read_dataset(f.NET/'backends/eval_commands.reviewed.jsonl')
    cases=[c for c in dataset['cases'] if c['id'] in COMPLEX+EMPTY]
    assert len(cases)==12 and all(c['human_reviewed'] for c in cases)
    assert all(f.OffloadGateway.classify(c['command'])=='complex' and c['expected_actions']
               for c in cases if c['id'] in COMPLEX)
    config=f.load_json(f.NET/'experiments/policy_config.json')
    calibration=f.load_json(FROZEN/'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/calibration.json')
    for tier,value in calibration['inference_cost_ms'].items():
        config[tier+'_infer_ms']=value
    random.seed(202611114)
    initial=f.bridge.default_state()
    write(HERE/'initial_state.json',initial)
    runtime=f.load_json(FROZEN/'docs/demo_v3/local_models.json')
    original_base=ROOT/'docs/demo_v3'
    runtime['runtime']['path']=str((original_base/runtime['runtime']['path']).resolve())
    for model in runtime['models'].values():
        model['path']=str((original_base/model['path']).resolve())
    write(HERE/'runtime_resolved.json',runtime)
    sources=[p for p in FROZEN.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    sources += [HERE/name for name in ('run_comparison.py','verify_run.py','test_comparison.py',
                                      'initial_state.json','runtime_resolved.json')]
    registration={'schema':'sensehome.fair-recovery-registration.v4','registered_at_utc':now(),
        'planned_primary_rows':72,'planned_ancillary_rows':24,'planned_total_rows':96,
        'schedule':schedule(),'schedule_sha256':f.canonical_hash(schedule()),
        'cases':cases,'cases_sha256':f.canonical_hash(cases),'source_dataset_sha256':dataset['sha256'],
        'source_sha256':{p.relative_to(HERE).as_posix():digest(p) for p in sources},
        'policy_config':config,'policy_modes':MODES,'timeout_s':15,'max_tokens':256,
        'temperature':0,'recovery_launch_budget_ms':10000,'initial_state_sha256':f.canonical_hash(initial),
        'warmup_command':WARMUP,
        'warmup':'each tier once per condition in healthy mode with registered fixed POST wait; separate from evaluation',
        'condition_semantics':'700ms actual controlled proxy POST dispatch wait, observed from last measured warmup/POST via health telemetry; not GPU queue. POST503 activated after shared block health; inventory stays healthy; faults never forward to model.',
        'fairness':'all arms identical recovery wrapper, prompt, model pair, timeout, initial JSON state, execution and scoring; fresh C process per arm per block; same frozen measured observation per block; cyclic arm order balanced for 24 primary blocks',
        'timing':'total_wall is actual contiguous row interval immediately before row health through gateway and state read; row health audited but policy gets identical shared block observation. c_decision_ipc is actual C decide command write/read roundtrip; internal C CPU time unknown/null. policy_adapter includes observation marshalling/IPC. No simulated config cost enters measured timing.',
        'primary_scoring':'unchanged frozen fused_eval.score_outcome: nonempty expected action equivalence + target state + observed software execution + semantic complete without error or fallback',
        'empty_scoring':'empty actions and unchanged JSON state only; answer semantics not human-scored; separate from task completion',
        'recommendation_rule':'maximize completed primary rows, then minimize median actual total wall over ALL primary requests including failures, then fixed ARMS order',
        'stop_rule':'single registered execution; no rerun after rows exist; preserve failures; no outcome-driven prompt/model/label/config tuning',
        'scope':'four previously human-reviewed complex commands and eight empty-plan tasks on one RTX4050 host; two logical model candidates; JSON device state; regression not new human holdout, independent cloud, physical device, WAN or 6G',
        'ruling':'frozen 20 cases contain four complex executable tasks; use 72 primary rows rather than inventing twelve complex commands; 24 ancillary rows separate'}
    write(HERE/'registration.json',registration)
    return registration

def assert_sources(registration):
    for name,sha in registration['source_sha256'].items():
        if digest(HERE/name)!=sha:
            raise ValueError('REGISTERED_SOURCE_CHANGED:'+name)

def evaluate(registration,endpoints):
    if (HERE/'rows.jsonl').exists():
        raise FileExistsError('EVALUATION_ALREADY_STARTED_NO_RERUN')
    assert_sources(registration)
    cases={c['id']:c for c in registration['cases']}
    initial=f.load_json(HERE/'initial_state.json')
    old_state,old_random=f.bridge.STATE_FILE,random.getstate()
    rows,blocks,warmups=[],[],[]
    try:
        with tempfile.TemporaryDirectory(prefix='sensehome-v4-state-') as temp:
            f.bridge.STATE_FILE=str(Path(temp)/'state.json')
            for condition in CONDITIONS:
                with ExitStack() as stack:
                    proxies={t:stack.enter_context(ControlledProxy(e['url'],delay_ms=700 if
                        condition==('local_wait700' if t=='local' else 'second_wait700') else 0))
                        for t,e in endpoints.items()}
                    raw={t:InventoryQueueBackend(proxies[t],e['model'],t) for t,e in endpoints.items()}
                    wrapped={t:PlanningRecoveryBackend(raw[t],raw[other],primary_name=t+'-model',
                        secondary_name=other+'-model',primary_url=proxies[t].url,secondary_url=proxies[other].url,
                        launch_budget_ms=registration['recovery_launch_budget_ms'])
                        for t,other in (('local','cloud'),('cloud','local'))}
                    for tier,backend in raw.items():
                        backend.infer(WARMUP)
                        warmups.append({'condition':condition,'tier':tier,'attempt':deepcopy(backend.attempts[-1])})
                    write(HERE/'warmups.json',warmups)
                    for index,job in enumerate(j for j in registration['schedule'] if j['condition']==condition):
                        case=cases[job['case_id']]
                        for p in proxies.values(): p.mode='healthy'
                        start=time.perf_counter()
                        shared={t:b.health() for t,b in raw.items()}
                        shared_wall=round((time.perf_counter()-start)*1000,3)
                        obs=f.make_observation(index,shared,f.default_profile())
                        pair=job['cohort']+':'+condition+':'+case['id']
                        blocks.append({'pair_id':pair,'health':deepcopy(shared),'health_wall_ms':shared_wall,
                            'observation':obs,'observation_sha256':f.canonical_hash(obs),'order':job['arms']})
                        write(HERE/'blocks.json',blocks)
                        if condition in ('local_post503','both_post503'): proxies['local'].mode='http_503'
                        if condition in ('second_post503','both_post503'): proxies['cloud'].mode='http_503'
                        for order,arm in enumerate(job['arms']):
                            f.bridge.save_state(deepcopy(initial))
                            before=deepcopy(f.bridge.load_state())
                            starts={t:len(b.attempts) for t,b in raw.items()}
                            proxy_starts={t:len(p.records) for t,p in proxies.items()}
                            rec_starts={t:len(b.recovery_records) for t,b in wrapped.items()}
                            execution=[]; execution_ms=[]
                            def execute(actions,context=None):
                                begin=time.perf_counter()
                                try:
                                    value=f.bridge._gateway_action_executor(actions,context)
                                    execution.append(deepcopy(value))
                                    return value
                                finally: execution_ms.append((time.perf_counter()-begin)*1000)
                            row={'pair_id':pair,**{k:job[k] for k in ('condition','case_id','cohort')},
                                'arm':arm,'order_index':order,'command':case['command'],
                                'expected_actions':case['expected_actions'],'state_before':before,
                                'observation_sha256':f.canonical_hash(obs),'started_at_utc':now()}
                            policy=TimedPolicy(str(f.NET/'policy_cli.exe'),registration['policy_config'],MODES[arm])
                            total_start=None
                            try:
                                gateway=f.OffloadGateway(policy,{'local-rules':f.RuleBackend(f.bridge._gateway_local_handler),
                                    **{t+'-model':b for t,b in wrapped.items()}},
                                    link_snapshot=f.LinkSnapshot(wan_ok=True,lan_ok=True),action_executor=execute)
                                total_start=time.perf_counter()
                                health_start=time.perf_counter()
                                row['row_health']={t:b.health() for t,b in raw.items()}
                                health_ms=(time.perf_counter()-health_start)*1000
                                gateway_start=time.perf_counter()
                                event=gateway.handle(case['command'],deepcopy(case.get('context',{})),deepcopy(obs))
                                gateway_ms=(time.perf_counter()-gateway_start)*1000
                                after=deepcopy(f.bridge.load_state())
                                total_ms=(time.perf_counter()-total_start)*1000
                                row.update(status='finished',gateway_event=event,state_after=after,
                                    executor_calls=len(execution),executor_results=execution,
                                    **f.score_outcome(event,before,after,case['expected_actions'],execution))
                            except Exception as exc:
                                total_ms=(time.perf_counter()-total_start)*1000 if total_start else None
                                row.update(status='runner_failed',error=type(exc).__name__+':'+str(exc),task_completed=False,
                                    executor_calls=len(execution),executor_results=execution,
                                    state_after=deepcopy(f.bridge.load_state()),safe_no_action_structure=False)
                                event={}; gateway_ms=None; health_ms=None
                            finally: policy.close()
                            recovery=[deepcopy(r) for t,b in wrapped.items() for r in b.recovery_records[rec_starts[t]:]]
                            meta=recovery[0] if recovery else None
                            row['timing_ms']={'c_decision_ipc':round(sum(x['wall_ms'] for x in policy.command_timings if x['command'].startswith('decide ')),6),
                                'c_internal':None,'policy_adapter':event.get('latency',{}).get('policy_ms'),
                                'observation_health':round(health_ms,3) if health_ms is not None else None,
                                'first_model':meta['primary_ms'] if meta else 0.0,
                                'recovery_health':meta['health_ms'] if meta else 0.0,
                                'backup_model':meta['secondary_ms'] if meta else 0.0,
                                'executor':round(sum(execution_ms),3),
                                'gateway_wall':round(gateway_ms,3) if gateway_ms is not None else None,
                                'total_wall':round(total_ms,3) if total_ms is not None else None}
                            row['c_commands']=deepcopy(policy.command_timings)
                            row['recovery_records']=recovery
                            row['backend_attempts']=[{'tier':t,**deepcopy(x)} for t,b in raw.items() for x in b.attempts[starts[t]:]]
                            row['proxy_records']=[{'tier':t,**deepcopy(x)} for t,p in proxies.items() for x in p.records[proxy_starts[t]:]]
                            rows.append(row)
                            with (HERE/'rows.jsonl').open('a',encoding='utf-8') as stream:
                                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
                        print(json.dumps({'pair':pair,'saved_rows':len(rows)},ensure_ascii=True),flush=True)
    finally:
        f.bridge.STATE_FILE=old_state
        random.setstate(old_random)
        write(HERE/'execution_status.json',{'finished_at_utc':now(),'rows_saved':len(rows),'all_planned_rows_saved':len(rows)==96})
    result=summarize(rows)
    result['source_hashes_unchanged']=all(digest(HERE/name)==sha for name,sha in registration['source_sha256'].items())
    write(HERE/'results.json',result)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('register','run'))
    args=parser.parse_args()
    if args.action=='register':
        print(json.dumps(register(),ensure_ascii=True,indent=2))
    else:
        registration=f.load_json(HERE/'registration.json')
        assert_sources(registration)
        with LocalRuntime(HERE,config_path=HERE/'runtime_resolved.json') as runtime:
            result=evaluate(registration,runtime.endpoints())
        print(json.dumps(result,ensure_ascii=True,indent=2))

if __name__=='__main__': main()
