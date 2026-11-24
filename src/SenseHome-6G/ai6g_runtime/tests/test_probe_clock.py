import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ai6g_runtime.evaluation import run_dual
from ai6g_runtime.router import ObservationCache,HealthObservation
class ProbeClockTests(unittest.TestCase):
 def test_second_backend_is_rechecked_after_first_probe_ages_it(self):
  now=[100.0];cache=ObservationCache(ttl_s=5);cache.put(HealthObservation('cloud-model',True,10,.9,96))
  called=[]
  def probe(**kw):
   called.append(kw['name']);now[0]+=2
   return {'backend':kw['name'],'reachable':True,'model_real':True,'latency_ms':1,'checked_at':now[0],'error':None}
  with tempfile.TemporaryDirectory() as td, patch.object(run_dual.time,'time',side_effect=lambda:now[0]),patch.object(run_dual,'_probe',side_effect=probe):
   run_dual._ensure_observations(cache=cache,backends={'local-model':object(),'cloud-model':object()},calibration={n:{'quality_score':.9,'source_sha256':'a'*64} for n in ['local-model','cloud-model']},probe_records=[],probe_log=Path(td)/'p.jsonl',ttl_s=5)
  self.assertEqual(called,['local-model','cloud-model'])
class MonitorClockTests(unittest.TestCase):
 def test_monitor_checks_clock_again_after_first_probe(self):
  from ai6g_runtime.health_monitor import HealthMonitor,CalibrationEntry,ProbeReport
  clock=[100.0];m=HealthMonitor(health_url='http://127.0.0.1',backends={'a':object(),'b':object()},ttl_s=5,clock=lambda:clock[0])
  m._last_checked={'b':96.0};seen=[]
  def probe(name,backend):
   seen.append(name);clock[0]+=2
   return ProbeReport(name,False,False,0,clock[0],None,None,False,'test')
  with patch.object(m,'_probe',side_effect=probe):m.run_due()
  self.assertEqual(seen,['a','b'])

if __name__=='__main__':unittest.main()
