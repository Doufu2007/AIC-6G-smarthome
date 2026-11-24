import sys,time,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ai6g_runtime.runtime import TaskRuntime
from ai6g_runtime.router import TaskRouter,ObservationCache,HealthObservation,RouteDecision
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.models import ModelResult
from ai6g_runtime.protocol import ActionPlan,Action

class Backend:
 def __init__(self):self.calls=0;self.timeout=None
 def infer(self,task,timeout_s):
  self.calls+=1;self.timeout=timeout_s
  return ModelResult(True,{'actions':[{'type':'light_on','location':'客厅'}]},model_real=True)
class Fixed:
 def __init__(self,name='local-model'):self.name=name
 def choose(self,task,now=None):return RouteDecision(self.name,'test',1,1,0,False)

class HardeningTests(unittest.TestCase):
 def test_remote_local_label_cannot_bypass_privacy(self):
  b=Backend();r=TaskRuntime(router=Fixed(),backends={'local-model':b})
  out=r.handle('不要上传，关闭客厅和卧室的灯')
  self.assertFalse(out['ok']);self.assertEqual(out['error_code'],'privacy_violation');self.assertEqual(b.calls,0)
 def test_expired_external_budget_never_calls_model(self):
  b=Backend();r=TaskRuntime(router=Fixed(),backends={'local-model':b})
  out=r.handle('打开客厅的灯',deadline_at=time.perf_counter()-1)
  self.assertFalse(out['ok']);self.assertEqual(out['error_code'],'deadline_exceeded');self.assertEqual(b.calls,0)
 def test_backend_gets_remaining_not_full_budget(self):
  b=Backend();r=TaskRuntime(router=Fixed(),backends={'local-model':b})
  r.handle('打开客厅的灯',deadline_at=time.perf_counter()+0.2)
  self.assertLessEqual(b.timeout,0.2)
 def test_validation_consumed_budget_cannot_commit(self):
  b=Backend();r=TaskRuntime(router=Fixed(),backends={'local-model':b},default_deadline_ms=5)
  from ai6g_runtime import runtime
  original=runtime.validate_plan
  def slow(payload,task):time.sleep(.015);return original(payload,task)
  with patch.object(runtime,'validate_plan',side_effect=slow):out=r.handle('打开客厅的灯')
  self.assertFalse(out['ok']);self.assertFalse(r.state()['lights']['客厅']['is_on'])
 def test_executor_checks_deadline_after_waiting_for_lock(self):
  s=DeviceStore();p=ActionPlan((Action('light_on','客厅'),))
  out=s.apply(p,'expired',deadline_at=time.perf_counter()-1)
  self.assertFalse(out.ok);self.assertFalse(s.snapshot()['lights']['客厅']['is_on'])
 def test_router_distinguishes_low_quality_from_unreachable(self):
  c=ObservationCache();c.put(HealthObservation('local-model',True,5,.5,100))
  out=TaskRouter(c).choose(SimpleNamespace(task_class='complex',privacy='standard',quality_floor=.9,deadline_ms=500),now=101)
  self.assertIn('QUALITY',out.reason)
 def test_family_calibration_is_used_without_changing_global_score(self):
  c=ObservationCache();c.put(HealthObservation('local-model',True,5,.5,100,quality_by_family={'multi_action':.92}))
  t=SimpleNamespace(task_class='complex',family='multi_action',privacy='standard',quality_floor=.9,deadline_ms=500)
  self.assertEqual(TaskRouter(c).choose(t,now=101).backend,'local-model')
 def test_future_or_nonfinite_health_not_ranked(self):
  for value in [float('nan'),float('inf')]:
   with self.assertRaises(ValueError):ObservationCache(ttl_s=value)
  c=ObservationCache();c.put(HealthObservation('local-model',True,5,.99,200))
  self.assertFalse(c.is_fresh('local-model',now=100))

class FamilyHealthTests(unittest.TestCase):
 def test_health_api_preserves_calibrated_family_scores(self):
  from ai6g_runtime.health_monitor import load_calibration
  import json
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'cal.json';p.write_text(json.dumps({'backends':{'local-model':{'quality_score':.5,'source_sha256':'a'*64,'quality_by_family':{'lighting':.9}}}}))
   self.assertEqual(load_calibration(p)['local-model'].quality_by_family['lighting'],.9)

if __name__=='__main__':unittest.main()
