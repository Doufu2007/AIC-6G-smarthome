import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cli


class CLITests(unittest.TestCase):
    def test_clean_user_flow_and_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            def run(*args):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):self.assertEqual(cli.main(['--home',temporary,*args]),0)
                return json.loads(output.getvalue())
            self.assertEqual(run('init')['mode'],'software-demo')
            card=run('propose','打开阅读灯')
            self.assertFalse(card['authorized'])
            request=['confirm','--proposal-id',card['proposal_id'],'--digest',card['digest'],'--request-id','cli-one','--confirm']
            self.assertEqual(run(*request)['status'],'succeeded')
            self.assertTrue(run(*request)['duplicate'])
            self.assertEqual(run('receipt','cli-one')['status'],'succeeded')
            state=run('status');self.assertEqual(state['states']['light.reading']['state'],'on')

    def test_init_never_overwrites_existing_user_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):cli.main(['--home',temporary,'init'])
            target=Path(temporary)/'software_states.json';before=target.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):cli.main(['--home',temporary,'init'])
            self.assertEqual(before,target.read_bytes())


if __name__=='__main__':unittest.main()
