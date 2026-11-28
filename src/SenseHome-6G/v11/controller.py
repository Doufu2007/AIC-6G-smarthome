"""Trusted local controller: clients submit IDs and confirmation, never plans."""
from __future__ import annotations
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import urllib.request
from urllib.parse import urlparse
from concurrent.futures import wait, FIRST_COMPLETED
import math

from capabilities import DEVICE_SERVICES, temperature_control_available
from proposal import ProposalStore, validate_payload, canonical
from platform_execution import ExecutionStore, UnknownEntity, NotDispatched
from catalog_planner import (catalog_rules, PROMPT, guard_payload, clarification,
                             request_dependencies, guard_model_proposal, SEMANTIC_PROMPT,
                             request_privacy_mode, extension_dependencies)
from deadline import BoundedCallPool, call_with_timeout, request_bytes


_READ_POOL = BoundedCallPool(max_workers=8)
_PLANNER_POOL = BoundedCallPool(max_workers=4)


class SoftwareClient:
    """Clearly labeled software state demo, not an official HA server."""
    def __init__(self, states, path=None, *, seed_missing=False):
        self.path=Path(path) if path else None
        from_file=json.loads(self.path.read_text(encoding='utf8')) if self.path and self.path.exists() else {}
        self.states=copy.deepcopy({**states, **from_file} if seed_missing else (from_file if self.path and self.path.exists() else states))
        self.calls=0;self.lock=threading.RLock()
        self.state_db=Path(str(self.path)+'.sqlite3') if self.path else None
        if self.state_db:
            self.state_db.parent.mkdir(parents=True,exist_ok=True)
            with self._db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS software_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)')
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO software_state VALUES (1,?)',(canonical(self.states),))
                persisted=json.loads(db.execute('SELECT value FROM software_state WHERE singleton=1').fetchone()[0])
                if seed_missing and self.states.keys() - persisted.keys():
                    persisted={**self.states,**persisted}
                    db.execute('UPDATE software_state SET value=? WHERE singleton=1',(canonical(persisted),))
                self.states=persisted

    @contextmanager
    def _db(self):
        db=sqlite3.connect(self.state_db,timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:yield db
        finally:db.close()

    def get_state(self,entity_id):
        with self.lock:
            if self.state_db:
                with self._db() as db:
                    self.states=json.loads(db.execute('SELECT value FROM software_state WHERE singleton=1').fetchone()[0])
            return self._normalized_state(entity_id)

    def _normalized_state(self,entity_id):
        if entity_id not in self.states:raise UnknownEntity('unknown_entity')
        value=dict(entity_id=entity_id,**copy.deepcopy(self.states[entity_id]))
        attrs=value.setdefault('attributes',{})
        if entity_id.startswith('light.'):
            attrs.setdefault('brightness_supported',True)
            attrs.setdefault('supported_services',['light.turn_on','light.turn_off'])
        elif entity_id.startswith('cover.'):
            attrs.setdefault('supported_services',['cover.open_cover','cover.close_cover'])
        elif entity_id.startswith('switch.'):
            attrs.setdefault('supported_services',list(DEVICE_SERVICES['switch']))
        elif entity_id.startswith('climate.'):
            services = list(DEVICE_SERVICES['climate']) if temperature_control_available(value) else []
            attrs.setdefault('supported_services',services)
        return value

    def call_service(self,entity_id,service,data):
        return self._call_service(entity_id,service,data)

    def call_service_checked(self,entity_id,service,data,*,state,authorization_expires_at,now):
        return self._call_service(entity_id,service,data,expected=state,
                                  authorization_expires_at=authorization_expires_at,now=now)

    def _call_service(self,entity_id,service,data,*,expected=None,authorization_expires_at=None,now=time.time):
        def check_write():
            if authorization_expires_at is not None and now()>=authorization_expires_at:
                raise NotDispatched('software_authorization_expired_before_write')
            if expected is not None:
                try:current=self._normalized_state(entity_id)
                except (UnknownEntity,ValueError,TypeError,KeyError,AttributeError) as exc:
                    raise NotDispatched('software_state_unreadable_before_write') from exc
                if any(current.get(key)!=expected.get(key) for key in ('state','attributes','last_updated')):
                    raise NotDispatched('software_precondition_changed_before_write')
                from capabilities import validate_actions
                entry=dict(entity_id=entity_id,kind=entity_id.split('.')[0],available=True)
                try:validate_actions([dict(entity_id=entity_id,service=service,data=data)],[entry],{entity_id:current})
                except (ValueError,TypeError,KeyError) as exc:
                    raise NotDispatched('software_capability_changed_before_write') from exc
        with self.lock:
            if self.state_db:
                with self._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self.states=json.loads(db.execute('SELECT value FROM software_state WHERE singleton=1').fetchone()[0])
                    check_write()
                    self._apply(entity_id,service,data)
                    db.execute('UPDATE software_state SET value=? WHERE singleton=1',(canonical(self.states),))
            else:
                check_write()
                self._apply(entity_id,service,data)
            return []

    def _apply(self,entity_id,service,data):
        if entity_id not in self.states:raise UnknownEntity('unknown_entity')
        if service.split('.')[0]!=entity_id.split('.')[0]:raise ValueError('software entity/service domain mismatch')
        state=self.states[entity_id]
        if service=='light.turn_on':
            state['state']='off' if data.get('brightness_pct')==0 else 'on'
            if 'brightness_pct' in data:state.setdefault('attributes',{})['brightness']=round(data['brightness_pct']*255/100)
        elif service=='light.turn_off':state['state']='off'
        elif service=='cover.open_cover':state['state']='open';state.setdefault('attributes',{})['current_position']=100
        elif service=='cover.close_cover':state['state']='closed';state.setdefault('attributes',{})['current_position']=0
        elif service in ('switch.turn_on', 'switch.turn_off'):state['state']=service.rsplit('_',1)[1]
        elif service=='climate.set_temperature':state.setdefault('attributes',{})['temperature']=data['temperature']
        else:raise ValueError('unsupported software service')
        self.calls+=1;state['last_updated']=str(time.time_ns())


class ModelPlanner:
    def __init__(self,endpoint,model,*,api_key=None,timeout=5,allow_model_proposals=False,
                 seed=20261111,json_mode=True):
        if type(allow_model_proposals) is not bool:raise ValueError('allow_model_proposals must be boolean')
        if seed is not None and (type(seed) is not int):raise ValueError('seed must be an integer or null')
        if type(json_mode) is not bool:raise ValueError('json_mode must be boolean')
        parsed=urlparse(endpoint)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid model endpoint')
        if not isinstance(model,str) or not model.strip():raise ValueError('model name required')
        if type(timeout) not in (int,float) or not 0 < timeout <= 300:raise ValueError('invalid model timeout')
        self.endpoint=endpoint;self.model=model;self.api_key=api_key;self.timeout=timeout
        self.allow_model_proposals=allow_model_proposals
        self.seed=seed;self.json_mode=json_mode
        self._local=threading.local()

    @property
    def last_metrics(self):return copy.deepcopy(getattr(self._local,'metrics',{}))

    def __call__(self,view):
        return self.generate(view)

    def generate(self,view,*,timeout=None):
        if request_privacy_mode(view.get('request')) != 'standard':
            return clarification('当前请求禁止联网或上传数据，已停止模型调用；请移除模型扩展需求或明确更改隐私要求')
        body=dict(model=self.model,messages=[dict(role='system',content=SEMANTIC_PROMPT if self.allow_model_proposals else PROMPT),dict(role='user',content=json.dumps(view,ensure_ascii=False))],
                  temperature=0,max_tokens=512)
        if self.seed is not None:body['seed']=self.seed
        if self.json_mode:body['response_format']=dict(type='json_object')
        headers={'Content-Type':'application/json'}
        if self.api_key:headers['Authorization']='Bearer '+self.api_key
        data=json.dumps(body,ensure_ascii=False).encode('utf8')
        request=urllib.request.Request(self.endpoint,data=data,headers=headers)
        started=time.perf_counter();metrics=dict(request_bytes=len(data),model=self.model)
        self._local.metrics=metrics
        try:
            limit=self.timeout if timeout is None else min(self.timeout,timeout)
            if limit<=0:raise TimeoutError('planning deadline elapsed')
            response_data=request_bytes(request,timeout=limit,max_bytes=2*1024*1024)
            metrics['response_bytes']=len(response_data)
            raw=json.loads(response_data)
            usage=raw.get('usage',{})
            for key in ('prompt_tokens','completion_tokens','total_tokens'):
                if type(usage.get(key)) is int and usage[key]>=0:metrics[key]=usage[key]
            content=raw['choices'][0]['message']['content']
            value=json.loads(content)
            if not isinstance(value,dict):raise ValueError('model JSON object required')
            return value
        except Exception as exc:
            metrics['error_type']=type(exc).__name__
            raise
        finally:metrics['elapsed_ms']=(time.perf_counter()-started)*1000


class Controller:
    def __init__(self,directory,catalog,client,*,planner=None,mode='software-demo',execution_options=None,
                 allow_model_proposals=False,snapshot_timeout_seconds=2,planning_timeout_seconds=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.catalog=copy.deepcopy(catalog);self.client=client;self.planner=planner or catalog_rules;self.mode=mode
        planner_opt_in=getattr(self.planner,'allow_model_proposals',False)
        if type(allow_model_proposals) is not bool or type(planner_opt_in) is not bool:
            raise ValueError('allow_model_proposals must be boolean')
        self.allow_model_proposals=allow_model_proposals or planner_opt_in
        if (type(snapshot_timeout_seconds) not in (int,float) or not math.isfinite(snapshot_timeout_seconds)
                or not 0<snapshot_timeout_seconds<=30):raise ValueError('snapshot timeout must be within 0..30 seconds')
        self.snapshot_timeout_seconds=float(snapshot_timeout_seconds)
        if planning_timeout_seconds is None:
            planning_timeout_seconds=getattr(self.planner,'deadline_ms',getattr(self.planner,'timeout',5)*1000)/1000
        if (type(planning_timeout_seconds) not in (int,float) or not math.isfinite(planning_timeout_seconds)
                or not 0<planning_timeout_seconds<=300):raise ValueError('planning timeout must be within 0..300 seconds')
        self.planning_timeout_seconds=float(planning_timeout_seconds)
        self.proposals=ProposalStore(self.directory/'proposals.sqlite')
        conn=sqlite3.connect(self.proposals.path)
        try:
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS controller_bindings (request_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, proposal_digest TEXT NOT NULL, execution_digest TEXT NOT NULL)')
        finally:conn.close()
        self.execution=ExecutionStore(self.directory/'execution.sqlite',client,**(execution_options or {}))
        self.audit_path=self.directory/'controller.jsonl'
        self.lock=threading.RLock()
        self.audit_lock=threading.RLock()

    def _audit(self,kind,value):
        try:
            with self.audit_lock,self.audit_path.open('a',encoding='utf8') as f:
                f.write(json.dumps(dict(at=time.time(),mode=self.mode,kind=kind,value=value),ensure_ascii=False,allow_nan=False)+'\n')
            return dict(status='written')
        except (OSError,ValueError,TypeError) as exc:
            return dict(status='failed',error_type=type(exc).__name__,
                        scope='supplemental JSONL; durable proposals and execution receipts remain queryable')

    def _audited_result(self,kind,result):
        platform_status=self._platform_audit_status()
        if platform_status is not None and platform_status.get('status')!='disabled':
            result={**result,'platform_audit_status':platform_status}
        return {**result,'audit_status':self._audit(kind,result)}

    def _platform_audit_status(self):
        client=self.client
        for _ in range(4):
            value=getattr(client,'last_audit_status',None)
            if isinstance(value,dict):return copy.deepcopy(value)
            client=getattr(client,'client',None)
            if client is None:break
        return None

    def snapshot(self,entity_ids=None):
        """Read only dependencies with bounded concurrency and a shared deadline."""
        wanted=None if entity_ids is None else set(entity_ids)
        entries=[entry for entry in self.catalog if wanted is None or entry['entity_id'] in wanted]
        states={};pending={};queue=iter(entries);deadline=time.monotonic()+self.snapshot_timeout_seconds
        def unavailable(entity,error):
            states[entity]=dict(entity_id=entity,state='unavailable',attributes={},
                                last_updated='unavailable',observed_at=self.execution.now(),read_error_type=error)
        def submit_next():
            try:entry=next(queue)
            except StopIteration:return False
            entity=entry['entity_id']
            if entry.get('available') is not True:unavailable(entity,'catalog_unavailable');return True
            def read():
                value=self.execution.snapshot([entity])[entity]
                status=self._platform_audit_status()
                if status is not None:value['platform_audit_status']=status
                return value
            try:pending[_READ_POOL.submit(read)]=entity
            except TimeoutError as exc:unavailable(entity,type(exc).__name__)
            return True
        while len(pending)<8 and time.monotonic()<deadline:
            if not submit_next():break
        while pending:
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            finished,_=wait(pending,timeout=remaining,return_when=FIRST_COMPLETED)
            if not finished:break
            for future in finished:
                entity=pending.pop(future)
                try:states[entity]=future.result()
                except Exception as exc:unavailable(entity,type(exc).__name__)
            while len(pending)<8 and time.monotonic()<deadline:
                if not submit_next():break
        for future,entity in pending.items():
            future.cancel()
            if hasattr(future,'discard'):future.discard()
            unavailable(entity,'snapshot_deadline_exceeded')
        for entry in queue:unavailable(entry['entity_id'],'snapshot_deadline_exceeded')
        return states

    def propose(self,text):
        return self._propose(text, self.planner, self.allow_model_proposals)

    def propose_candidate(self, text, candidate):
        """Build a card from an external candidate using the original request."""
        return self._propose(text, lambda view: candidate, True, source='agentarts')

    def propose_local_no_upload(self, original_text, command_text):
        """Execute strict local-rules planning on ``command_text`` while binding ``original_text`` and ``no_upload``."""
        if not isinstance(original_text, str) or not original_text.strip() or len(original_text) > 2000:
            raise ValueError('request length 1..2000 required')
        if not isinstance(command_text, str) or not command_text.strip():
            return self.propose(original_text)
        entered = time.perf_counter()
        deps = request_dependencies(command_text, self.catalog)
        read_ids = set() if deps is None else deps
        snapshot_started = time.perf_counter()
        states = self.snapshot(read_ids)
        snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
        view = dict(request=command_text, catalog=copy.deepcopy(self.catalog), states=states)
        payload = guard_payload(catalog_rules(copy.deepcopy(view)), view)
        metadata = dict(
            route=dict(backend='local-rules', reason='NO_UPLOAD_LOCAL_RULES'),
            privacy_policy='no_upload',
            planning_ms=(time.perf_counter() - entered) * 1000,
            semantic_scope='complete supported grammar; local no-upload bypass',
            semantic_mode='independent_rule_validation',
            intent_review_required=False,
            dependency_scope='no_model_or_state_dependencies' if deps is None else 'parsed_request_dependencies',
            timing=dict(snapshot_ms=snapshot_ms, refresh_snapshot_ms=0,
                        snapshot_budget_ms=self.snapshot_timeout_seconds * 1000),
        )
        if payload['result_kind'] == 'clarification':
            result = dict(status='clarification', authorized=False, payload=payload, mode=self.mode, metadata=metadata)
        else:
            involved = {a['entity_id'] for a in payload['actions']}
            refresh_started = time.perf_counter()
            fresh = self.snapshot(read_ids | involved)
            metadata['timing']['refresh_snapshot_ms'] = (time.perf_counter() - refresh_started) * 1000
            payload = guard_payload(payload, dict(request=command_text, catalog=copy.deepcopy(self.catalog), states=fresh))
            unreadable = any(
                s.get('state') in ('unavailable', 'unknown') or s.get('read_error_type')
                for s in fresh.values()
            )
            if payload['result_kind'] == 'clarification' or unreadable:
                if unreadable:
                    payload = clarification('请求依赖状态不可读，请恢复连接后重新提案')
                result = dict(status='clarification', authorized=False, payload=payload, mode=self.mode, metadata=metadata)
            else:
                result = self.proposals.create(
                    original_text, self.catalog, {e: fresh[e] for e in involved},
                    payload, metadata=metadata, dependency_states=fresh,
                )
            result['mode'] = self.mode
        result = self._audited_result('proposal', result)
        result['handler_total_ms'] = (time.perf_counter() - entered) * 1000
        return result

    def propose_checklist(self, text, candidate, *, extra_dependency_ids=None, checklist=None,
                          source='agentarts', trace=None):
        """Create a proposal card for a structured checklist with full dependency tracking.

        Every controllable entity in ``extra_dependency_ids`` (such as initially-off
        lights skipped during leave-home checks) is bound into ``dependency_states``
        so pre-confirm state drift invalidates stale cards.
        """
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('request length 1..2000 required')
        entered = time.perf_counter()
        privacy_mode = request_privacy_mode(text)
        if privacy_mode != 'standard':
            result = dict(status='clarification', authorized=False,
                          payload=clarification('此请求要求本地处理或离线；已拒绝云端检查单提案'),
                          mode=self.mode,
                          metadata=dict(route=dict(backend='local-rules', reason='PRIVACY_CHECKLIST_BLOCK'),
                                        privacy_policy=privacy_mode, semantic_mode='not_run'))
            result = self._audited_result('proposal', result)
            result['handler_total_ms'] = (time.perf_counter() - entered) * 1000
            return result
        try:
            validated = validate_payload(candidate, self.catalog)
        except (ValueError, TypeError, KeyError):
            payload = clarification('检查单候选提案结构、设备目录或参数不符合要求')
            result = dict(status='clarification', authorized=False, payload=payload, mode=self.mode,
                          metadata=dict(route=dict(backend=source, reason='CHECKLIST_SCHEMA_REJECTED'),
                                        privacy_policy=privacy_mode, checklist=copy.deepcopy(checklist or {}),
                                        trace=copy.deepcopy(trace or {})))
            result = self._audited_result('proposal', result)
            result['handler_total_ms'] = (time.perf_counter() - entered) * 1000
            return result
        if validated['result_kind'] != 'proposal':
            result = dict(status='clarification', authorized=False, payload=validated, mode=self.mode,
                          metadata=dict(route=dict(backend=source, reason='CHECKLIST_CLARIFICATION'),
                                        privacy_policy=privacy_mode, checklist=copy.deepcopy(checklist or {}),
                                        trace=copy.deepcopy(trace or {})))
            result = self._audited_result('proposal', result)
            result['handler_total_ms'] = (time.perf_counter() - entered) * 1000
            return result
        catalog_ids = {entry['entity_id'] for entry in self.catalog}
        involved = {action['entity_id'] for action in validated['actions']}
        extra = {eid for eid in (extra_dependency_ids or ()) if eid in catalog_ids}
        watched = involved | extra
        snapshot_started = time.perf_counter()
        fresh = self.snapshot(watched)
        snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
        unreadable = any(
            state.get('state') in ('unavailable', 'unknown') or state.get('read_error_type')
            for state in fresh.values()
        )
        metadata = dict(
            route=dict(backend=source, reason='STRUCTURED_CHECKLIST_GUARDRAIL'),
            privacy_policy=privacy_mode,
            semantic_scope='边缘护栏已完成目录、能力、排除约束与全范围状态依赖校验；待用户显式确认',
            semantic_mode='structured_checklist_with_explicit_review',
            intent_review_required=True,
            dependency_scope='checklist_watched_entities',
            checklist=copy.deepcopy(checklist or {}),
            trace=copy.deepcopy(trace or {}),
            timing=dict(snapshot_ms=snapshot_ms, entry_to_card_store_ms=(time.perf_counter() - entered) * 1000),
        )
        if unreadable:
            payload = clarification('检查单依赖设备状态不可读，请恢复连接后重新核查')
            result = dict(status='clarification', authorized=False, payload=payload, mode=self.mode, metadata=metadata)
        else:
            try:
                validated = validate_payload(validated, self.catalog, states={e: fresh[e] for e in involved})
                result = self.proposals.create(
                    text, self.catalog, {entity: fresh[entity] for entity in involved},
                    validated, metadata=metadata, dependency_states=fresh,
                )
            except (ValueError, TypeError, KeyError):
                payload = clarification('设备当前能力或状态不满足检查单执行条件，请刷新后重试')
                result = dict(status='clarification', authorized=False, payload=payload, mode=self.mode, metadata=metadata)
            result['mode'] = self.mode
        result = self._audited_result('proposal', result)
        result['handler_total_ms'] = (time.perf_counter() - entered) * 1000
        return result

    def _propose(self, text, selected_planner, allow_model_proposals, *, source=None):
        if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError('request length 1..2000 required')
        entered=time.perf_counter()
        privacy_mode=request_privacy_mode(text)
        if privacy_mode=='offline':
            result=dict(status='clarification',authorized=False,
                        payload=clarification('此请求要求完全离线；尚未读取平台状态、调用模型或访问网络'),
                        mode=self.mode,metadata=dict(route=dict(backend='local-rules',reason='OFFLINE_PRIVACY_BLOCK'),
                        privacy_policy='offline',semantic_mode='not_run'))
            result=self._audited_result('proposal',result)
            result['handler_total_ms']=(time.perf_counter()-entered)*1000
            return result
        rule_dependencies=request_dependencies(text,self.catalog)
        extension=allow_model_proposals and privacy_mode=='standard'
        dependencies=rule_dependencies
        extension_dependencies_used=False
        if extension and rule_dependencies is None:
            dependencies=extension_dependencies(text,self.catalog)
            extension_dependencies_used=dependencies is not None
        read_ids=({entry['entity_id'] for entry in self.catalog} if extension else set()) if dependencies is None else dependencies
        snapshot_started=time.perf_counter()
        states=self.snapshot(read_ids);view=dict(request=text,catalog=copy.deepcopy(self.catalog),states=states)
        snapshot_ms=(time.perf_counter()-snapshot_started)*1000
        started=time.perf_counter();planner=catalog_rules if privacy_mode=='no_upload' else selected_planner
        route=dict(backend='local-rules' if planner is catalog_rules else source or 'configured-planner',reason='DIRECT_PLANNER')
        if privacy_mode=='no_upload':route['reason']='NO_UPLOAD_LOCAL_RULES'
        def run_planner():
            # Thread-local telemetry must cross the worker boundary with its result.
            try:
                value=planner(copy.deepcopy(view))
                own_route=copy.deepcopy(getattr(planner,'last_route',None) or route)
                if hasattr(planner,'last_metrics'):own_route['metrics']=copy.deepcopy(planner.last_metrics)
                return value,own_route
            except Exception as exc:
                return None,dict(backend='local-rules',reason='PLANNER_ERROR_RULE_FALLBACK',
                                 error_type=type(exc).__name__,metrics=copy.deepcopy(getattr(planner,'last_metrics',{})))
        try:
            candidate,route=call_with_timeout(run_planner,self.planning_timeout_seconds,pool=_PLANNER_POOL)
            if candidate is None:candidate=catalog_rules(copy.deepcopy(view))
        except TimeoutError as exc:
            candidate=clarification('规划等待超过时限或资源繁忙，请重新请求')
            route=dict(backend='local-rules',reason='PLANNING_DEADLINE_OR_CAPACITY_EXCEEDED',error_type=type(exc).__name__)
        guard=guard_model_proposal if extension else guard_payload
        payload=guard(candidate,view)
        model_review=(extension and rule_dependencies is None
                      and payload['result_kind']=='proposal')
        if extension and rule_dependencies is None and dependencies is None:
            dependency_scope='catalog_for_model_review'
        elif extension_dependencies_used:
            dependency_scope='extension_request_dependencies'
        elif privacy_mode=='no_upload' and rule_dependencies is None:
            dependency_scope='no_model_or_state_dependencies'
        else:
            dependency_scope='parsed_request_dependencies'
        metadata=dict(route=route,privacy_policy=privacy_mode,planning_ms=(time.perf_counter()-started)*1000,
                      semantic_scope='结构、目录、能力与状态校验；模型语义须由用户核对' if model_review else 'complete supported grammar; independent candidate comparison',
                      semantic_mode='model_proposal_with_explicit_review' if model_review else 'independent_rule_validation',
                      intent_review_required=model_review,
                      dependency_scope=dependency_scope,
                      timing=dict(snapshot_ms=snapshot_ms,refresh_snapshot_ms=0,
                                  snapshot_budget_ms=self.snapshot_timeout_seconds*1000,
                                  route_budget_scope='planner only, including configured model probes'))
        if payload['result_kind']=='clarification':
            result=dict(status='clarification',authorized=False,payload=payload,mode=self.mode,metadata=metadata)
        else:
            involved={a['entity_id'] for a in payload['actions']}
            refresh_started=time.perf_counter();fresh=self.snapshot(read_ids|involved)
            metadata['timing']['refresh_snapshot_ms']=(time.perf_counter()-refresh_started)*1000
            fresh_view=dict(request=text,catalog=copy.deepcopy(self.catalog),states=fresh)
            payload=guard(payload,fresh_view)
            # An open-language candidate may depend on any supplied state.
            # Preserve all supplied dependencies; never infer new actions here.
            unreadable=any(state.get('state') in ('unavailable','unknown') or state.get('read_error_type') for state in fresh.values())
            if payload['result_kind']=='clarification' or unreadable:
                if unreadable:payload=clarification('请求依赖状态不可读，请恢复连接后重新提案')
                result=dict(status='clarification',authorized=False,payload=payload,mode=self.mode,metadata=metadata)
            else:
                metadata['timing']['entry_to_card_store_ms']=(time.perf_counter()-entered)*1000
                result=self.proposals.create(text,self.catalog,{entity:fresh[entity] for entity in involved},payload,
                                             metadata=metadata,dependency_states=fresh)
            result['mode']=self.mode
        result=self._audited_result('proposal',result)
        result['handler_total_ms']=(time.perf_counter()-entered)*1000
        return result

    def confirm(self,body):
        if not isinstance(body,dict) or set(body)!={'proposal_id','digest','request_id','confirmed'}:
            raise ValueError('confirmation accepts only proposal_id/digest/request_id/confirmed')
        if body['confirmed'] is not True:raise ValueError('explicit confirmation required')
        if not isinstance(body['request_id'],str) or not body['request_id'].strip() or len(body['request_id'])>128:
            raise ValueError('invalid request ID')
        conn=sqlite3.connect(self.proposals.path)
        try:row=conn.execute('SELECT digest,card,request_id FROM proposals WHERE id=?',(body['proposal_id'],)).fetchone()
        finally:conn.close()
        if row is None or row[0]!=body['digest']:raise ValueError('unknown proposal or digest mismatch')
        if row[2] is not None and row[2]!=body['request_id']:raise ValueError('proposal bound to another request')
        card=json.loads(row[1])
        owned_execution_digest=hashlib.sha256(canonical(dict(plan=card['payload']['actions'],expected_states=card['states'])).encode('utf8')).hexdigest()
        conflict=dict(request_id=body['request_id'],status='rejected',reason='request_id_conflict',steps=[],duplicate=True)
        # SQLite request bindings and ExecutionStore's per-request/entity guards
        # serialize conflicting work without holding a controller-wide lock.
        conn=sqlite3.connect(self.proposals.path,timeout=10)
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                binding=conn.execute('SELECT proposal_id,proposal_digest,execution_digest FROM controller_bindings WHERE request_id=?',(body['request_id'],)).fetchone()
                if binding is not None:
                    if binding!=(body['proposal_id'],body['digest'],owned_execution_digest):return conflict
                else:
                    other=conn.execute('SELECT request_id FROM controller_bindings WHERE proposal_id=?',(body['proposal_id'],)).fetchone()
                    if other is not None and other[0]!=body['request_id']:raise ValueError('proposal bound to another request')
                    conn.execute('INSERT INTO controller_bindings VALUES (?,?,?,?)',(body['request_id'],body['proposal_id'],body['digest'],owned_execution_digest))
        finally:conn.close()
        execution_conn=sqlite3.connect(self.execution.db_path)
        try:execution_row=execution_conn.execute('SELECT digest FROM requests WHERE request_id=?',(body['request_id'],)).fetchone()
        finally:execution_conn.close()
        if execution_row is not None and execution_row[0]!=owned_execution_digest:return conflict
        receipt=self._owned_receipt(body['request_id'],owned_execution_digest)
        if receipt is not None:
            return self._audited_result('receipt_replay',receipt)
        dependencies=self.snapshot(card.get('dependency_states',card['states']))
        current={entity:dependencies[entity] for entity in card['states']}
        # Another Controller may have completed while this one read HA.
        # Inspect durable ownership again instead of re-validating an old
        # proposal against the new state caused by that same execution.
        receipt=self._owned_receipt(body['request_id'],owned_execution_digest)
        if receipt is not None:
            return self._audited_result('receipt_replay',receipt)
        try:
            approved=self.proposals.confirm(**body,current_catalog=self.catalog,current_states=current,
                                            current_dependency_states=dependencies)
        except ValueError:
            receipt=self._owned_receipt(body['request_id'],owned_execution_digest)
            if receipt is not None:
                return self._audited_result('receipt_replay',receipt)
            raise
        # No client-supplied plan, authority flag, or expected state crosses
        # this boundary. A fresh owned result is consumed immediately.
        validate_payload(card['payload'],self.catalog,current)
        result=self.execution.execute(approved['request_id'],approved['plan'],approved['expected_states'],authorized=True,
                                      authorization_expires_at=approved['expires_at'])
        result=self._decorate_receipt(result)
        return self._audited_result('execution',result)

    def _owned_receipt(self,request_id,expected_digest):
        conn=sqlite3.connect(self.execution.db_path)
        try:row=conn.execute('SELECT digest FROM requests WHERE request_id=?',(request_id,)).fetchone()
        finally:conn.close()
        if row is None:return None
        if row[0]!=expected_digest:
            return dict(request_id=request_id,status='rejected',reason='request_id_conflict',steps=[],duplicate=True)
        receipt=self.execution.get_receipt(request_id)
        return self._decorate_receipt({**receipt,'duplicate':True}) if receipt is not None else None

    def _decorate_receipt(self,result):
        if result is None:return None
        conn=sqlite3.connect(self.proposals.path)
        try:
            row=conn.execute('SELECT p.card FROM controller_bindings b JOIN proposals p ON p.id=b.proposal_id WHERE b.request_id=?',
                             (result['request_id'],)).fetchone()
        finally:conn.close()
        if row:
            card=json.loads(row[0])
            return {**result,'proposal_id':card['proposal_id'],'proposal_digest':card['digest'],
                    'metadata':copy.deepcopy(card.get('metadata',{}))}
        return result

    def receipt(self,request_id):
        return self._decorate_receipt(self.execution.get_receipt(request_id))

    def reconcile(self,request_id):
        result=self._decorate_receipt(self.execution.reconcile(request_id))
        return self._audited_result('reconcile',result)

    def resolve_unknown(self,request_id,**decision):
        """Trusted local operator only. This cannot cancel remote late actions."""
        result=self._decorate_receipt(self.execution.resolve_unknown(request_id,**decision))
        return self._audited_result('operator_resolution',result)
