"""Generic execution grounding checks; no dataset IDs or gold available online."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ai6g_runtime.models import ModelResult
from ai6g_runtime.protocol import classify_task
from ai6g_runtime.grounding_v6 import GroundedBackend, explicit_brightness_targets

class Backend:
    name='local-model'
    def __init__(self,payload):self.payload=payload
    def infer(self,task,timeout_s):return ModelResult(True,self.payload,model_real=True,backend=self.name,raw_response={'usage':{'total_tokens':30}})

def plan(kind='set_brightness',room='客厅',value=50):
    action={'type':kind,'location':room}
    if value is not None:action['brightness']=value
    return {'result_kind':'action','actions':[action],'explanation':'model action'}

class GroundingTests(unittest.TestCase):
    def test_unrequested_device_type_cannot_be_substituted_by_supported_actuator(self):
        for text,payload in [('帮我调整客厅音响音量到45',plan(value=45)),
                             ('关闭卧室的灯',plan('curtain_close','卧室',None))]:
            self.assertEqual(self.call(text,payload).payload['result_kind'],'clarification')
    def test_light_on_omitted_brightness_uses_protocol_default_when_target_is_100(self):
        payload=plan('light_on',value=None)
        self.assertEqual(self.call('请让客厅灯打开，亮度设为100',payload).payload,payload)
    def test_light_on_cannot_bypass_missing_requested_brightness(self):
        self.assertEqual(self.call('客厅照明亮度请设置得舒适些',plan('light_on',value=50)).payload['result_kind'],'clarification')
    def test_clear_brightness_verb_variant_not_overfiltered(self):
        self.assertEqual(self.call('卧室灯请帮忙调亮到35',plan('set_brightness','卧室',35)).payload['result_kind'],'action')
    def test_targets_bound_to_room_cannot_be_swapped_between_rooms(self):
        payload={'result_kind':'action','actions':[{'type':'set_brightness','location':'客厅','brightness':75},
            {'type':'set_brightness','location':'卧室','brightness':35}],'explanation':'swapped'}
        self.assertEqual(self.call('客厅灯调到35，卧室灯调到75',payload).payload['result_kind'],'clarification')
    def test_fractional_or_out_of_range_target_not_truncated_to_valid_integer(self):
        for text in ('客厅灯调到35.5%','客厅灯调到105','客厅灯调到3e1'):
            self.assertEqual(explicit_brightness_targets(text),[])
    def call(self,text,payload):return GroundedBackend(Backend(payload)).infer(classify_task(text),timeout_s=1)
    def test_missing_brightness_becomes_explicit_clarification(self):
        result=self.call('客厅照明亮度请设置得舒适些',plan())
        self.assertTrue(result.model_real);self.assertEqual(result.payload['result_kind'],'clarification')
        self.assertEqual(result.payload['actions'],[])
        self.assertEqual(result.raw_response['grounding_v6']['original_payload'],plan())
    def test_explicit_target_cannot_be_silently_changed(self):
        result=self.call('请将客厅的灯调到35%',plan(value=50))
        self.assertEqual(result.payload['result_kind'],'clarification')
    def test_unmentioned_room_cannot_be_executed(self):
        result=self.call('请替我把客厅的窗帘合拢',plan('curtain_close','卧室',None))
        self.assertEqual(result.payload['result_kind'],'clarification')
    def test_digit_and_chinese_targets_are_preserved_at_boundaries(self):
        for text,value in [('客厅灯调到零',0),('客厅灯调到一百',100),('客厅灯亮度设置为三十五',35),('客厅灯调到75%',75)]:
            with self.subTest(text=text):self.assertEqual(self.call(text,plan(value=value)).payload['result_kind'],'action')
    def test_light_on_default_and_valid_multiroom_are_not_overfiltered(self):
        self.assertEqual(self.call('打开客厅灯',plan('light_on',value=100)).payload['result_kind'],'action')
        payload={'result_kind':'action','actions':[{'type':'light_off','location':'客厅'},{'type':'curtain_open','location':'卧室'}],'explanation':'both'}
        self.assertEqual(self.call('关闭客厅灯然后拉开卧室窗帘',payload).payload,payload)
    def test_policy_clarification_and_transport_failure_are_not_rewritten_as_success(self):
        payload={'result_kind':'clarification','actions':[],'explanation':'请明确目标'}
        self.assertEqual(self.call('灯光调好些',payload).payload,payload)
        class Failed(Backend):
            def infer(self,*args,**kwargs):return ModelResult(False,error='offline')
        self.assertFalse(GroundedBackend(Failed(None)).infer(classify_task('打开客厅灯'),1).ok)

if __name__=='__main__':unittest.main()
