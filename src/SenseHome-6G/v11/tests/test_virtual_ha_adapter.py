"""Explicit logical-light to official helper mapping, using real HTTP fixture."""
from pathlib import Path
import importlib.util
import tempfile
import unittest
from test_platform_execution import Fixture, load

SOURCE=Path(__file__).resolve().parents[1]/'virtual_ha_adapter.py'
def adapter_module():
    assert SOURCE.exists(), 'missing explicit virtual HA mapping'
    spec=importlib.util.spec_from_file_location('virtual_ha_adapter_under_test',SOURCE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class VirtualAdapterTests(unittest.TestCase):
    def setUp(self):
        self.module=adapter_module();self.execution=load()
        self.fixture=Fixture();self.temp=tempfile.TemporaryDirectory()
        self.client=self.execution.HAClient(self.fixture.url,'fixture-token')
        self.adapter=self.module.VirtualHAAdapter(self.client,{'light.living':'input_boolean.living','light.bedroom':'input_boolean.bedroom'})
    def tearDown(self):
        if hasattr(self,'fixture'):self.fixture.close()
        if hasattr(self,'temp'):self.temp.cleanup()
    def test_store_changes_physical_helper_and_reads_logical_identity(self):
        store=self.execution.ExecutionStore(Path(self.temp.name)/'journal.sqlite3',self.adapter)
        states=store.snapshot(['light.living'])
        self.assertEqual(states['light.living']['entity_id'],'light.living')
        self.assertEqual(states['light.living']['attributes']['physical_entity_id'],'input_boolean.living')
        self.assertEqual(states['light.living']['attributes']['virtual_mapping'],'input_boolean helper representing a light')
        result=store.execute('mapped',[{'entity_id':'light.living','service':'light.turn_on','data':{}}],states,authorized=True)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.fixture.states['input_boolean.living']['state'],'on')
        self.assertEqual(self.fixture.states['input_boolean.bedroom']['state'],'off')
        self.assertEqual(self.fixture.calls[-1][:2],('GET','/api/states/input_boolean.living'))
    def test_brightness_cover_toggle_and_unmapped_entities_do_not_post(self):
        invalid=[('light.living','light.turn_on',{'brightness_pct':50}),('light.living','cover.open_cover',{}),('light.living','light.toggle',{}),('light.unknown','light.turn_on',{})]
        for entity,service,data in invalid:
            with self.assertRaises(ValueError):self.adapter.call_service(entity,service,data)
        self.assertFalse(any(method=='POST' for method,_,_ in self.fixture.calls))
    def test_mapping_requires_distinct_input_boolean_targets(self):
        for mapping in ({'light.living':'sensor.example'},{'cover.window':'input_boolean.living'},{'light.living':'input_boolean.living','light.bedroom':'input_boolean.living'}):
            with self.assertRaises(ValueError):self.module.VirtualHAAdapter(self.client,mapping)

if __name__=='__main__':unittest.main(verbosity=2)
