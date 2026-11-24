import json
import pathlib
import sys
import tempfile
import unittest
from argparse import Namespace

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from controller import Controller,ModelPlanner,SoftwareClient
import cli


class StrictOptIn(unittest.TestCase):
    def test_controller_rejects_nonboolean_opt_in(self):
        entries=[dict(entity_id='light.a',kind='light',room='书房',name='台灯',aliases=[],available=True)]
        for flag in ('false','true',0,1,None):
            with self.subTest(flag=flag),tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ValueError):
                    Controller(temporary,entries,SoftwareClient({'light.a':dict(state='off',attributes={},last_updated='v0')}),
                               allow_model_proposals=flag)

    def test_model_planner_rejects_nonboolean_opt_in(self):
        for flag in ('false','true',0,1,None):
            with self.subTest(flag=flag),self.assertRaises(ValueError):
                ModelPlanner('http://127.0.0.1:1/v1/chat/completions','fixture',allow_model_proposals=flag)

    def test_route_config_string_false_cannot_enable_model_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=pathlib.Path(temporary)/'routes.json'
            path.write_text(json.dumps(dict(allow_model_proposals='false',backends={'local-model':dict(
                endpoint='http://127.0.0.1:1/v1/chat/completions',model='fixture')})),encoding='utf8')
            args=Namespace(routes=str(path),model_endpoint=None,model_name=None,calibration_file=None,allow_model_proposals=False)
            with self.assertRaises(ValueError):cli.make_planner(args)


if __name__=='__main__':unittest.main()
