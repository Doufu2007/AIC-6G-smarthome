"""Measure rule coverage and opt-in model proposals without device dispatch.

Gold actions are supplied independently in JSONL. Endpoint output is measured,
never treated as ground truth. A source label records fixtures explicitly.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import time

from catalog_planner import catalog_rules,guard_model_proposal,clarification
from proposal import canonical,validate_payload


def evaluate_cases(planner,catalog,states,cases,*,source_label):
    if not isinstance(source_label,str) or not source_label.strip():raise ValueError('source label required')
    if not isinstance(cases,list) or not 1<=len(cases)<=1000:raise ValueError('require 1..1000 cases')
    ids=set();rows=[]
    for case in cases:
        if not isinstance(case,dict) or set(case)!={'id','request','expected_actions'}:raise ValueError('invalid case fields')
        identifier=case['id'];text=case['request'];gold=case['expected_actions']
        if not isinstance(identifier,str) or not identifier or identifier in ids:raise ValueError('case IDs must be unique strings')
        if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError('invalid case request')
        ids.add(identifier)
        expected_kind='proposal' if gold else 'clarification'
        validate_payload(dict(result_kind=expected_kind,actions=gold,reason='independent gold'),catalog,states=states)
        view=dict(request=text,catalog=copy.deepcopy(catalog),states=copy.deepcopy(states))
        rules=catalog_rules(copy.deepcopy(view));started=time.perf_counter();error=None;raw=None
        try:
            raw=json.loads(canonical(planner(copy.deepcopy(view))))
            guarded=guard_model_proposal(raw,view)
        except Exception as exc:
            error=type(exc).__name__;guarded=clarification('端点调用或输出失败')
        elapsed=(time.perf_counter()-started)*1000
        def correct(payload):
            return isinstance(payload,dict) and payload.get('result_kind')==expected_kind and canonical(payload.get('actions'))==canonical(gold)
        rows.append(dict(id=identifier,request=text,expected_actions=gold,
                         rule_payload=rules,model_payload=raw,guarded_payload=guarded,
                         rule_correct=correct(rules),raw_correct=error is None and correct(raw),
                         guarded_correct=error is None and correct(guarded),error_type=error,elapsed_ms=elapsed,
                         model_metrics=copy.deepcopy(getattr(planner,'last_metrics',{}))))
    summary=dict(cases=len(rows),action_cases=sum(bool(row['expected_actions']) for row in rows),
                 rule_correct=sum(row['rule_correct'] for row in rows),raw_correct=sum(row['raw_correct'] for row in rows),
                 guarded_correct=sum(row['guarded_correct'] for row in rows),
                 additional_action_cases_correct=sum(bool(row['expected_actions']) and not row['rule_correct'] and row['guarded_correct'] for row in rows),
                 wrong_action_proposals=sum(row['guarded_payload']['result_kind']=='proposal' and not row['guarded_correct'] for row in rows),
                 endpoint_errors=sum(row['error_type'] is not None for row in rows),
                 total_elapsed_ms=sum(row['elapsed_ms'] for row in rows))
    return dict(schema='sensehome.semantic_coverage.v1',source_label=source_label,summary=summary,rows=rows,service_calls=0,
                scope='proposal accuracy and latency only; no confirmation or service execution; endpoint identity not independently verified')


def main(argv=None):
    parser=argparse.ArgumentParser(description='评测严格规则与模型语义提案；全程不派发设备动作')
    parser.add_argument('--endpoint',required=True);parser.add_argument('--model',required=True)
    parser.add_argument('--cases',type=Path,default=Path(__file__).parent/'examples/semantic_cases.jsonl')
    parser.add_argument('--catalog',type=Path);parser.add_argument('--states',type=Path)
    parser.add_argument('--source-label',required=True,help='真实模型/端点版本或明确的fixture标签')
    parser.add_argument('--timeout',type=float,default=5);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if bool(args.catalog)!=bool(args.states):raise ValueError('provide both --catalog and --states')
    from controller import ModelPlanner,SoftwareClient
    from cli import DEFAULT_CATALOG,DEFAULT_STATES
    catalog=json.loads(args.catalog.read_text(encoding='utf8')) if args.catalog else copy.deepcopy(DEFAULT_CATALOG)
    seed=json.loads(args.states.read_text(encoding='utf8')) if args.states else copy.deepcopy(DEFAULT_STATES)
    client=SoftwareClient(seed);states={entry['entity_id']:client.get_state(entry['entity_id']) for entry in catalog}
    data=args.cases.read_bytes();cases=[json.loads(line) for line in data.decode('utf-8-sig').splitlines() if line.strip()]
    model=ModelPlanner(args.endpoint,args.model,api_key=os.environ.get('SENSEHOME_MODEL_KEY'),timeout=args.timeout,allow_model_proposals=True)
    report=evaluate_cases(model,catalog,states,cases,source_label=args.source_label)
    report.update(cases_sha256=hashlib.sha256(data).hexdigest(),model=args.model,
                  catalog_sha256=hashlib.sha256(canonical(catalog).encode('utf8')).hexdigest(),
                  states_sha256=hashlib.sha256(canonical(states).encode('utf8')).hexdigest())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    print(json.dumps(report['summary'],ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
