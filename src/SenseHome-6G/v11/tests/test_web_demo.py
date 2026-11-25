import tempfile
import unittest
from unittest.mock import patch

from agentarts import AgentArtsWorkflow

from web_demo import DemoApp


class WebDemoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = DemoApp(self.directory.name, 'operator-secret', 'tool-secret')

    def test_agent_tool_cannot_confirm_and_duplicate_is_replayed(self):
        card = self.app.propose({'text': '打开书房阅读灯'}, owner='alice')
        self.assertEqual(card['status'], 'awaiting_confirmation')
        body = {key: card[key] for key in ('proposal_id', 'digest')}
        body.update(request_id='test-one', confirmed=True)
        with self.assertRaises(PermissionError):
            self.app.confirm(body, owner='tool')
        self.assertEqual(self.app.controller.snapshot(['light.reading'])['light.reading']['state'], 'off')
        self.assertEqual(self.app.confirm(body, owner='alice')['status'], 'succeeded')
        self.assertTrue(self.app.confirm(body, owner='alice')['duplicate'])
        self.assertEqual(self.app.controller.snapshot(['light.reading'])['light.reading']['state'], 'on')

    def test_candidate_cannot_strip_negation(self):
        candidate = {'result_kind': 'proposal', 'reason': 'light', 'actions': [
            {'entity_id': 'light.reading', 'service': 'light.turn_on', 'data': {}}]}
        result = self.app.propose({'text': '不要打开阅读灯', 'candidate': candidate}, owner='alice')
        self.assertEqual(result['status'], 'clarification')

    def test_workflow_context_binds_original_text_and_owner(self):
        with self.app.workflow_context('alice', '打开书房阅读灯') as context:
            with self.assertRaises(PermissionError):
                self.app.tool_proposal({'context': context, 'text': '打开客厅顶灯'})
            card = self.app.tool_proposal({'context': context, 'text': '打开书房阅读灯'})
            self.assertEqual(card['status'], 'awaiting_confirmation')
            self.assertEqual(self.app.owned_proposal(card['proposal_id'], 'alice'), card)
        with self.assertRaises(PermissionError):
            self.app.tool_proposal({'context': context, 'text': '打开书房阅读灯'})

    def test_workflow_can_only_return_card_created_for_this_invocation(self):
        self.app.workflow = lambda text, context: self.app.tool_proposal({'context': context, 'text': text})
        card = self.app.operator_proposal('打开书房阅读灯', 'alice')
        self.assertEqual(card['status'], 'awaiting_confirmation')
        self.assertEqual(card['source'], 'agentarts')
        self.app.workflow = lambda text, context: card
        self.assertEqual(self.app.operator_proposal('打开客厅顶灯', 'alice')['code'], 'model_unavailable')

    def test_workflow_runtime_payload_and_response(self):
        import json

        adapter = AgentArtsWorkflow('https://example.com/run', 'runtime-key', result_path='outputs.proposal')
        with patch('agentarts.request_bytes', return_value=b'{"outputs":{"proposal":"{\\"status\\":\\"clarification\\"}"}}') as request:
            self.assertEqual(adapter('打开灯', 'one-context'), {'status': 'clarification'})
        outgoing, timeout = request.call_args.args
        self.assertEqual(timeout, 20)
        self.assertEqual(outgoing.get_header('Authorization'), 'Bearer runtime-key')
        self.assertTrue(outgoing.get_header('X-hw-agentarts-session-id'))
        self.assertEqual(json.loads(outgoing.data), {'inputs': {'query': '打开灯', 'context': 'one-context'}})


if __name__ == '__main__':
    unittest.main()
