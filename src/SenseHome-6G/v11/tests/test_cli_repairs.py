import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cli
from controller import ModelPlanner


class CLIRepairs(unittest.TestCase):
    def test_operator_resolution_command_passes_explicit_declarations(self):
        class FakeApp:
            def resolve_unknown(self,request_id,**decision):
                return dict(status='unknown',request_id=request_id,operator_resolution=decision)
        with patch.object(cli,'make_app',return_value=FakeApp()),contextlib.redirect_stdout(io.StringIO()) as output:
            result=cli.main(['resolve-unknown','original','--operator','本机操作员',
                             '--reason','已查看平台记录','--disposition','verified_settled','--platform-settled'])
        self.assertEqual(result,0)
        value=json.loads(output.getvalue())
        self.assertTrue(value['operator_resolution']['platform_settled'])
        self.assertFalse(value['operator_resolution']['acknowledge_late_risk'])

    def test_single_explicit_model_can_offer_semantics_without_invented_calibration(self):
        from argparse import Namespace
        args=Namespace(routes=None,model_endpoint='http://127.0.0.1:1/v1/chat/completions',
                       model_name='fixture',calibration_file=None,allow_model_proposals=True)
        planner=cli.make_planner(args)
        self.assertIsInstance(planner,ModelPlanner)
        self.assertTrue(planner.allow_model_proposals)

    def test_global_semantic_option_reaches_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):cli.main(['--home',temporary,'init'])
            from argparse import Namespace
            args=Namespace(home=temporary,ha_url=None,ha_map=None,ha_native=False,routes=None,
                           model_endpoint=None,model_name=None,calibration_file=None,allow_model_proposals=True)
            self.assertTrue(cli.make_app(args).allow_model_proposals)


if __name__=='__main__':unittest.main()
