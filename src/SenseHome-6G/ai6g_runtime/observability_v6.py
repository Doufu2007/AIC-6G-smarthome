"""Separate calibrated capability, HTTP liveness, passive latency and budgeted canaries.

HTTP 200 never establishes semantic capability or inference speed. A canary is
non-actuating and only accepts an explicit clarification with zero actions.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from queue import SimpleQueue, Empty
import threading
import time
import urllib.request

from .health_monitor import CalibrationEntry, ProbeTask
from .local_resources import _local_url


@dataclass(frozen=True)
class LatencyEvidence:
    latency_ms: float
    source_sha256: str
    measured_at: float
    kind: str = 'offline_calibration_p95_inference_wall_ms'

    def __post_init__(self):
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0
               for v in (self.latency_ms,self.measured_at)):
            raise ValueError('measured inference latency and timestamp must be finite nonnegative numbers')
        if not re.fullmatch('[0-9a-fA-F]{64}',self.source_sha256):
            raise ValueError('latency source must be a SHA256')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class BudgetedHealthMonitor:
    def __init__(self,*,backends,health_urls,calibration,latencies,publish,
                 resource_provider=None,clock=time.time,monotonic=time.perf_counter,
                 canary_ttl_s=60.,min_canary_interval_s=30.,max_backoff_s=300.,
                 canary_timeout_s=5.,health_timeout_s=2.,passive_latency_ttl_s=60.,evidence_path=None):
        limits=(canary_ttl_s,min_canary_interval_s,max_backoff_s,canary_timeout_s,health_timeout_s,passive_latency_ttl_s)
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in limits):
            raise ValueError('finite positive monitoring limits required')
        if min_canary_interval_s>canary_ttl_s or max_backoff_s<min_canary_interval_s:
            raise ValueError('canary interval must fit validity and backoff')
        if not backends or set(backends)!=set(health_urls):raise ValueError('explicit health URL per model required')
        self.backends=dict(backends);self.health_urls={name:_local_url(url) for name,url in health_urls.items()}
        self.calibration=dict(calibration);self.latencies=dict(latencies)
        if any(not isinstance(v,CalibrationEntry) for v in self.calibration.values()):raise ValueError('explicit CalibrationEntry required')
        if any(not isinstance(v,LatencyEvidence) for v in self.latencies.values()):raise ValueError('explicit LatencyEvidence required')
        if not callable(publish):raise ValueError('health publisher required')
        self.publish=publish;self.resource_provider=resource_provider;self.clock=clock;self.monotonic=monotonic
        self.ttl=float(canary_ttl_s);self.minimum=float(min_canary_interval_s);self.max_backoff=float(max_backoff_s)
        self.passive_ttl=float(passive_latency_ttl_s)
        self.canary_timeout=float(canary_timeout_s);self.health_timeout=float(health_timeout_s)
        self.path=Path(evidence_path) if evidence_path else None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self.lock=threading.RLock()
        self.business_events=SimpleQueue()
        self.verified_latencies=set()
        self.state={name:{'attempt_at':None,'success_at':None,'failures':0,'invalidated':True,
                          'passive':deque(maxlen=16)} for name in self.backends}
        self.totals={'ticks':0,'health_http_requests':0,'generated_canary_requests':0,
                     'canary_known_tokens':0,'canary_unknown_usage_requests':0,'resource_collections':0}

    def record_business(self,name,*,ok,latency_ms):
        if name not in self.state:raise KeyError(name)
        if not isinstance(ok,bool) or isinstance(latency_ms,bool) or not math.isfinite(float(latency_ms)) or latency_ms<0:
            raise ValueError('finite measured business latency and explicit outcome required')
        # Completing a business request must never wait for health network I/O.
        self.business_events.put((name,ok,float(latency_ms),self.monotonic()))

    def _health(self,name):
        started=self.monotonic();raw=None;error=None;healthy=False
        self.totals['health_http_requests']+=1
        try:
            with self.opener.open(self.health_urls[name],timeout=self.health_timeout) as response:
                data=response.read(65537)
                if len(data)>65536:raise ValueError('oversized health response')
                raw=json.loads(data)
                healthy=200<=response.status<300 and isinstance(raw,dict) and raw.get('status')=='ok'
                if not healthy:error='health_not_ok'
        except Exception as exc:error=type(exc).__name__
        return {'healthy':healthy,'http_rtt_ms':max(0.,(self.monotonic()-started)*1000),
                'response':raw,'error':error}

    @staticmethod
    def _percentile(values):
        values=sorted(values)
        return values[max(0,math.ceil(len(values)*.95)-1)]

    def _canary(self,name,now):
        state=self.state[name];state['attempt_at']=self.monotonic()
        self.totals['generated_canary_requests']+=1
        started=self.monotonic();result=None;error=None;passed=False
        try:
            result=self.backends[name].infer(ProbeTask(),timeout_s=self.canary_timeout)
            payload=result.payload
            passed=bool(result.ok and result.model_real and isinstance(payload,dict)
                and payload.get('result_kind')=='clarification' and payload.get('actions')==[]
                and isinstance(payload.get('explanation'),str) and payload['explanation'].strip())
            if not passed:error=getattr(result,'error',None) or 'semantic_canary_contract_failed'
        except Exception as exc:error=type(exc).__name__
        usage=None if result is None or not isinstance(result.raw_response,dict) else result.raw_response.get('usage')
        tokens=usage.get('total_tokens') if isinstance(usage,dict) else None
        if isinstance(tokens,int) and not isinstance(tokens,bool) and tokens>=0:self.totals['canary_known_tokens']+=tokens
        else:self.totals['canary_unknown_usage_requests']+=1
        if passed:state.update(success_at=self.monotonic(),failures=0,invalidated=False)
        else:state.update(failures=min(10,state['failures']+1),invalidated=True)
        return {'passed':passed,'elapsed_ms':max(0.,(self.monotonic()-started)*1000),'error':error,
                'model_real':bool(result and result.model_real),'payload':None if result is None else result.payload,
                'raw_response':None if result is None else result.raw_response}

    def run_once(self):
        with self.lock:
            tick_start=self.monotonic();self.totals['ticks']+=1
            while True:
                try:name,ok,value,stamp=self.business_events.get_nowait()
                except Empty:break
                self.state[name]['passive'].append((stamp,value))
                if not ok:self.state[name]['invalidated']=True
            resources={};resource_error=None;resource_elapsed=0.
            if self.resource_provider is not None:
                started=self.monotonic();self.totals['resource_collections']+=1
                try:resources=self.resource_provider.collect(list(self.backends))['resources']
                except Exception as exc:resource_error=type(exc).__name__
                resource_elapsed=max(0.,(self.monotonic()-started)*1000)
            rows=[]
            for name in self.backends:
                now=self.clock();entry=self.calibration.get(name);latency=self.latencies.get(name)
                row={'backend':name,'checked_at':now,'published':False,'reachable':False,
                     'canary':None,'resources':resources.get(name),'resource_error':resource_error,
                     'resource_collection_ms':resource_elapsed,'monitoring_kind':'separated_budgeted_v6'}
                if entry is None or latency is None or (latency not in self.verified_latencies and latency.measured_at>now):
                    row['error']='missing_or_future_calibration_or_latency_evidence';rows.append(row);continue
                # Verify the immutable evidence once. A later wall-clock rollback
                # must not invalidate already verified history or its monotonic budget.
                self.verified_latencies.add(latency)
                live=self._health(name);row['liveness']=live;state=self.state[name]
                if not live['healthy']:state['invalidated']=True
                steady=self.monotonic();success=state['success_at'];age=None if success is None else steady-success
                valid=age is not None and 0<=age<=self.ttl and not state['invalidated']
                last=state['attempt_at'];backoff=min(self.max_backoff,self.minimum*2**max(0,state['failures']-1))
                budget_available=last is None or steady-last>=backoff
                if live['healthy'] and not valid and budget_available:
                    row['canary']=self._canary(name,now)
                    success=state['success_at'];age=None if success is None else self.monotonic()-success
                    valid=age is not None and 0<=age<=self.ttl and not state['invalidated']
                passive=[value for stamp,value in state['passive'] if 0<=self.monotonic()-stamp<=self.passive_ttl]
                estimate=max(latency.latency_ms,self._percentile(passive) if passive else latency.latency_ms)
                reachable=live['healthy'] and valid
                detail=json.dumps({'monitor':'separated_budgeted_v6','liveness_http_rtt_ms':live['http_rtt_ms'],
                    'latency_kind':'max_offline_p95_recent_business_p95','latency_source_sha256':latency.source_sha256,
                    'latency_measured_at':latency.measured_at,'canary_age_s':age,'canary_valid':valid,
                    'passive_latency_ttl_s':self.passive_ttl,
                    'calibration_source_sha256':entry.source_sha256,'quality_updated_by_liveness':False},sort_keys=True)
                payload={'reachable':reachable,'latency_ms':estimate,'quality_score':entry.quality_score,
                         'quality_by_family':entry.quality_by_family,'checked_at':self.clock(),
                         'error_rate':0. if reachable else 1.,'detail':detail}
                if resources.get(name) is not None:payload['resources']=resources[name]
                try:
                    acknowledged=self.publish(name,**payload)
                    row['published']=acknowledged is not False
                except Exception as exc:row['publish_error']=type(exc).__name__
                row.update(reachable=reachable,inference_latency_estimate_ms=estimate,canary_age_s=age,
                           canary_budget_available=budget_available,canary_backoff_s=backoff,
                           quality_score=entry.quality_score,calibration_source_sha256=entry.source_sha256)
                rows.append(row)
            report={'schema':'ai6g.separated_observability.v6','checked_at':self.clock(),
                'elapsed_ms':max(0.,(self.monotonic()-tick_start)*1000),'backends':rows,'totals':dict(self.totals),
                'physical_6g':False,'quality_not_reestimated_online':True}
            if self.path:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                with self.path.open('a',encoding='utf8') as handle:handle.write(json.dumps(report,ensure_ascii=False,allow_nan=False)+'\n')
            return rows
