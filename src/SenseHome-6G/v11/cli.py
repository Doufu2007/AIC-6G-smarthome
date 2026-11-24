"""Local SDK demonstration. Proposal and explicit confirmation are separate."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlparse

from controller import Controller, ModelPlanner, SoftwareClient
from platform_execution import HAClient
from virtual_ha_adapter import VirtualHAAdapter

DEFAULT_CATALOG=[
    dict(entity_id='light.reading',kind='light',room='书房',name='阅读灯',aliases=['桌旁灯'],available=True),
    dict(entity_id='light.living',kind='light',room='客厅',name='客厅顶灯',aliases=['起居灯'],available=True),
    dict(entity_id='cover.study',kind='cover',room='书房',name='书房窗帘',aliases=['阅读区窗帘'],available=True)]
DEFAULT_STATES={e['entity_id']:dict(state='closed' if e['kind']=='cover' else 'off',attributes={},last_updated='initial') for e in DEFAULT_CATALOG}


def load(path):return json.loads(path.read_text(encoding='utf8'))
def dump(value):print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


def native_client(args, directory):
    from native_ha_adapter import NativeHAAdapter
    if not args.ha_url:raise ValueError('--ha-url is required')
    token=os.environ.get('SENSEHOME_HA_TOKEN')
    if not token:raise ValueError('set SENSEHOME_HA_TOKEN privately')
    return NativeHAAdapter(HAClient(args.ha_url,token,audit_path=directory/'ha.redacted.jsonl'))


def make_planner(args):
    from routing import RoutedPlanner
    from ai6g_runtime.health_monitor import load_calibration
    from deadline import request_bytes
    allow_model_proposals=getattr(args,'allow_model_proposals',False)
    if type(allow_model_proposals) is not bool:raise ValueError('allow_model_proposals must be boolean')
    if args.routes and args.model_endpoint:raise ValueError('choose --routes or --model-endpoint')
    if args.routes and args.calibration_file:raise ValueError('set calibration_file inside --routes instead of --calibration-file')
    if args.routes:
        path=Path(args.routes).resolve();config=load(path)
        if not isinstance(config,dict):raise ValueError('routes config must be an object')
        base=path.parent
        specs=config.get('backends',{})
        if not isinstance(specs,dict) or not specs:raise ValueError('routes backends required')
    elif args.model_endpoint:
        if not args.model_name:raise ValueError('--model-name required')
        base=Path.cwd();config=dict(prefer_rules=False,allow_model_proposals=allow_model_proposals)
        specs={'local-model':dict(endpoint=args.model_endpoint,model=args.model_name,api_key_env='SENSEHOME_MODEL_KEY')}
        if args.calibration_file:config['calibration_file']=args.calibration_file
    else:return None
    if type(config.get('allow_model_proposals',False)) is not bool:
        raise ValueError('routes allow_model_proposals must be boolean')
    if type(config.get('prefer_rules',True)) is not bool:
        raise ValueError('routes prefer_rules must be boolean')
    allow_model_proposals=allow_model_proposals or config.get('allow_model_proposals',False)
    direct_model=config.get('direct_model',bool(args.model_endpoint and allow_model_proposals
                                               and not config.get('calibration_file')))
    if type(direct_model) is not bool:raise ValueError('direct_model must be boolean')
    if direct_model:
        if 'calibration_file' in config:
            raise ValueError('direct_model cannot be combined with calibration_file; use calibrated routing')
        if (not allow_model_proposals or config.get('prefer_rules',True)
                or config.get('privacy','standard')!='standard' or len(specs)!=1):
            raise ValueError('direct_model requires one explicit proposal backend, standard privacy and prefer_rules=false')
        deadline_ms=config.get('deadline_ms',None)
        if deadline_ms is not None and (type(deadline_ms) not in (int,float) or not 0<deadline_ms<=300000):
            raise ValueError('direct_model deadline_ms must be within 0..300000')
    backends={}
    resources={}
    for name,spec in specs.items():
        if not isinstance(spec,dict):raise ValueError('invalid backend configuration')
        key_env=spec.get('api_key_env','SENSEHOME_MODEL_KEY')
        require_key=spec.get('require_api_key',False)
        if type(require_key) is not bool:raise ValueError('require_api_key must be boolean')
        api_key=os.environ.get(key_env)
        if require_key and not api_key:raise ValueError('set '+key_env+' privately before calling this backend')
        timeout=spec.get('timeout_seconds',5)
        if type(timeout) not in (int,float) or not 0<timeout<=300:raise ValueError('invalid model timeout')
        if direct_model and deadline_ms is not None:timeout=min(timeout,deadline_ms/1000)
        if direct_model and spec.get('resources_url'):
            raise ValueError('resources_url requires calibrated routing, not direct_model')
        backends[name]=ModelPlanner(spec['endpoint'],spec['model'],
            api_key=api_key,timeout=timeout,allow_model_proposals=allow_model_proposals,
            seed=spec.get('seed',20261111),json_mode=spec.get('json_mode',True))
        if spec.get('resources_url'):
            url=spec['resources_url'];parsed=urlparse(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('invalid resources URL')
            resources[name]=url
    calibration={}
    if config.get('calibration_file'):
        calibration_path=Path(config['calibration_file'])
        if not calibration_path.is_absolute():calibration_path=base/calibration_path
        entries=load_calibration(calibration_path)
        calibration={name:dict(quality_score=entry.quality_score,source_sha256=entry.source_sha256,
                               quality_by_family=entry.quality_by_family) for name,entry in entries.items() if name in backends}
    def resource_provider(name,remaining):
        if name not in resources:return None
        raw=request_bytes(urllib.request.Request(resources[name]),timeout=min(2,remaining),max_bytes=65536)
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('resource object required')
        return dict(queue_ms=value.get('queue_ms'),free_mem_mb=value.get('free_mem_mb'),
                    inflight=value.get('inflight'),source='configured HTTP resource endpoint',observed_at=time.time())
    if direct_model:
        # An explicitly selected, uncalibrated endpoint is used for proposals
        # without claiming quality or routing superiority.
        return next(iter(backends.values()))
    return RoutedPlanner(backends,prefer_rules=config.get('prefer_rules',True),allow_model_proposals=allow_model_proposals,
        privacy=config.get('privacy','standard'),trusted_backends=set(config.get('trusted_backends',[])),
        deadline_ms=config.get('deadline_ms',3000),quality_floor=config.get('quality_floor',.8),
        calibrations=calibration,resource_provider=resource_provider if resources else None)


def make_app(args):
    directory=Path(args.home);catalog=load(directory/'catalog.json')
    if args.ha_url:
        if args.ha_native and args.ha_map:raise ValueError('choose --ha-native or --ha-map')
        if args.ha_native:
            client=native_client(args,directory);mode='official-ha-native-rest'
        else:
            if not args.ha_map:raise ValueError('choose --ha-native or provide --ha-map with --ha-url')
            token=os.environ.get('SENSEHOME_HA_TOKEN')
            if not token:raise ValueError('set SENSEHOME_HA_TOKEN privately; it will not be printed')
            mapping=load(Path(args.ha_map));client=VirtualHAAdapter(HAClient(args.ha_url,token,audit_path=directory/'ha.redacted.jsonl'),mapping)
            mode='official-ha-virtual-helper'
    else:
        if args.ha_native or args.ha_map:raise ValueError('--ha-url required for platform adapters')
        client=SoftwareClient(load(directory/'software_states.json'),directory/'software_states.json');mode='software-demo'
    planner=make_planner(args)
    return Controller(directory,catalog,client,planner=planner,mode=mode,
                      allow_model_proposals=getattr(args,'allow_model_proposals',False))


def main(argv=None):
    parser=argparse.ArgumentParser(description='目录提案与显式确认执行；默认仅本机软件状态')
    parser.add_argument('--home',default='sensehome_v11_data',help='v11专用持久目录；不复用旧版数据库')
    parser.add_argument('--ha-url');parser.add_argument('--ha-map')
    parser.add_argument('--ha-native',action='store_true')
    parser.add_argument('--model-endpoint');parser.add_argument('--model-name')
    parser.add_argument('--routes',help='双模型路由JSON配置')
    parser.add_argument('--calibration-file',help='显式模型质量校准；缺失时只走规则回退')
    parser.add_argument('--allow-model-proposals',action='store_true',help='允许规则之外的模型即时提案；须核对对象、动作和数值后确认')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init');sub.add_parser('status');sub.add_parser('discover')
    proposal=sub.add_parser('propose');proposal.add_argument('text')
    confirm=sub.add_parser('confirm');confirm.add_argument('--proposal-id',required=True);confirm.add_argument('--digest',required=True)
    confirm.add_argument('--request-id',required=True);confirm.add_argument('--confirm',action='store_true',required=True,help='阅读精确提案后显式确认')
    for name in ('receipt','reconcile'):sub.add_parser(name).add_argument('request_id')
    resolve=sub.add_parser('resolve-unknown',help='可信本地操作员处置未知结果；不取消远端动作、不补发旧请求')
    resolve.add_argument('request_id');resolve.add_argument('--operator',required=True)
    resolve.add_argument('--reason',required=True)
    resolve.add_argument('--disposition',choices=['verified_settled','accept_late_risk'],required=True)
    resolve.add_argument('--platform-settled',action='store_true',help='声明已独立核实平台处理结束')
    resolve.add_argument('--acknowledge-late-risk',action='store_true',help='明确接受原动作迟到影响后续控制的风险')
    args=parser.parse_args(argv);directory=Path(args.home)
    if args.command=='discover':
        if not args.ha_native or args.ha_map:raise ValueError('discover requires --ha-native')
        client=native_client(args,directory)
        dump(dict(catalog=client.discover_catalog(),scope='read-only HA state and capability discovery; review rooms before use'))
        return 0
    if args.command=='init':
        directory.mkdir(parents=True,exist_ok=True)
        for name,value in (('catalog.json',DEFAULT_CATALOG),('software_states.json',DEFAULT_STATES)):
            path=directory/name
            if not path.exists():
                with path.open('x',encoding='utf8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        dump(dict(initialized=True,mode='software-demo',note='目录及软件状态已准备；现有文件保留。模型提案不自动执行。'));return 0
    app=make_app(args)
    if args.command=='status':result=dict(mode=app.mode,catalog=app.catalog,states=app.snapshot())
    elif args.command=='propose':result=app.propose(args.text)
    elif args.command=='confirm':result=app.confirm(dict(proposal_id=args.proposal_id,digest=args.digest,request_id=args.request_id,confirmed=args.confirm))
    elif args.command=='receipt':result=app.receipt(args.request_id)
    elif args.command=='reconcile':result=app.reconcile(args.request_id)
    else:result=app.resolve_unknown(args.request_id,operator=args.operator,reason=args.reason,
                                   disposition=args.disposition,platform_settled=args.platform_settled,
                                   acknowledge_late_risk=args.acknowledge_late_risk)
    dump(result);return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,FileNotFoundError) as error:
        dump(dict(status='rejected',error=str(error)));raise SystemExit(2)
