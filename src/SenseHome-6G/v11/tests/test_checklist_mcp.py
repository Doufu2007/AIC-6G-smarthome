"""Comprehensive tests for dynamic checklists, layered guardrails, MCP server, and care copilot."""
import tempfile
import unittest

from checklist import evaluate_cloud_candidate
from web_demo import DemoApp


class ChecklistAndMCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = DemoApp(self.directory.name, 'operator-secret', 'tool-secret')

    def test_leave_home_dynamic_checklist_and_skipped_state_drift_rejection(self):
        self.app.apply_demo_preset({'preset': 'leave_home_ready'})
        query = '我要出门了，帮我把书房和阳台还亮着的灯关了，客厅别动。顺便看看入户门关了没有。'
        card = self.app.operator_proposal(query, 'alice')
        self.assertEqual(card['status'], 'awaiting_confirmation')
        self.assertEqual([a['entity_id'] for a in card['actions']], ['light.reading'])
        self.assertEqual([s['entity_id'] for s in card['checklist']['skipped']], ['light.patio'])
        self.assertIn('light.living', [p['entity_id'] for p in card['checklist']['preserved']])
        obs = {o['entity_id']: o for o in card['checklist']['observations']}
        self.assertEqual(obs['binary_sensor.front_door']['state'], 'on')
        self.assertIn('RULE-001', card['rule_refs'])
        self.assertIn('RULE-002', card['rule_refs'])
        self.assertIn('RULE-003', card['rule_refs'])

        # Flip the SKIPPED light (light.patio: off -> on) before confirmation.
        drift = self.app.apply_demo_preset({'preset': 'inject_state_drift', 'entity_id': 'light.patio'})
        self.assertEqual(drift['new_state'], 'on')
        with self.assertRaises(ValueError) as ctx:
            self.app.confirm({
                'proposal_id': card['proposal_id'],
                'digest': card['digest'],
                'request_id': 'req-drift-1',
                'confirmed': True,
            }, owner='alice')
        self.assertIn('dependency changed', str(ctx.exception))

    def test_same_utterance_all_off_produces_read_only_report(self):
        self.app.apply_demo_preset({'preset': 'leave_home_all_off'})
        query = '我先出门，室友还在客厅，按平时的离家约定检查一下'
        reply = self.app.operator_proposal(query, 'alice')
        self.assertEqual(reply['status'], 'read_only_report')
        self.assertEqual(reply['actions'], [])
        self.assertIn('light.living', [p['entity_id'] for p in reply['checklist']['preserved']])
        self.assertEqual(reply['checklist']['observations'][0]['state'], 'off')

    def test_edge_guardrail_blocks_hallucinated_cloud_exclusions_and_readonly_targets(self):
        self.app.apply_demo_preset({'preset': 'leave_home_ready'})
        # Cloud LLM tries to turn off living room light despite "客厅别动"
        bad_exclusion = {
            'result_kind': 'proposal',
            'agent_role': 'leave-home-agent',
            'actions': [
                {'entity_id': 'light.reading', 'service': 'light.turn_off', 'data': {}},
                {'entity_id': 'light.living', 'service': 'light.turn_off', 'data': {}},
            ],
            'reason': '关闭书房和客厅灯',
        }
        blocked = evaluate_cloud_candidate(
            '我要出门了，把书房灯关了，客厅别动', bad_exclusion, self.app.controller, self.app.inventory
        )
        self.assertEqual(blocked['status'], 'clarification')
        self.assertEqual(blocked['code'], 'guardrail_exclusion_blocked')
        self.assertEqual(blocked['actions'], [])

        # Cloud LLM tries to control read-only door sensor
        bad_readonly = {
            'result_kind': 'proposal',
            'agent_role': 'leave-home-agent',
            'actions': [{'entity_id': 'binary_sensor.front_door', 'service': 'switch.turn_off', 'data': {}}],
            'reason': '关闭门磁',
        }
        blocked_ro = evaluate_cloud_candidate(
            '帮我把入户门磁关掉', bad_readonly, self.app.controller, self.app.inventory
        )
        self.assertEqual(blocked_ro['status'], 'clarification')
        self.assertEqual(blocked_ro['code'], 'unsupported_action')

    def test_agentarts_direct_structured_candidate_flow_and_privacy_bypass(self):
        self.app.apply_demo_preset({'preset': 'leave_home_ready'})
        called = {'count': 0}

        def fake_workflow(text, context):
            called['count'] += 1
            return {
                'result_kind': 'proposal',
                'intent_type': 'leave_home',
                'agent_role': 'leave-home-agent',
                'actions': [{'entity_id': 'light.reading', 'service': 'light.turn_off', 'data': {}}],
                'reason': '依据 RULE-001/002/003 生成离家检查单',
                'rule_refs': ['RULE-001', 'RULE-002', 'RULE-003'],
                'checklist': {
                    'skipped': [{'entity_id': 'light.patio', 'reason': '已关闭'}],
                    'preserved': [{'entity_id': 'light.living', 'reason': '室友在客厅', 'rule_id': 'RULE-002'}],
                    'observations': [{'entity_id': 'binary_sensor.front_door'}],
                },
            }

        self.app.workflow = fake_workflow
        # Privacy request MUST NOT call cloud workflow
        priv = self.app.operator_proposal('不要上传云端，打开书房阅读灯', 'alice')
        self.assertEqual(called['count'], 0)
        self.assertEqual(priv['status'], 'awaiting_confirmation')
        self.assertIn('RULE-006', priv['rule_refs'])

        # Standard leave-home request goes through AgentArts direct return & edge guardrail
        cloud_card = self.app.operator_proposal('我先出门，室友还在客厅，按平时的离家约定检查一下', 'alice')
        self.assertEqual(called['count'], 1)
        self.assertEqual(cloud_card['status'], 'awaiting_confirmation')
        self.assertEqual(cloud_card['source'], 'agentarts')
        receipt = self.app.confirm({
            'proposal_id': cloud_card['proposal_id'],
            'digest': cloud_card['digest'],
            'request_id': 'req-cloud-1',
            'confirmed': True,
        }, owner='alice')
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual(self.app.inventory.get_device_state('light.reading')['state'], 'off')

    def test_mcp_server_protocol_and_zero_actuator_invariant(self):
        # Tool credential required on /mcp
        with self.assertRaises(PermissionError):
            self.app.dispatch('POST', '/mcp', {}, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, 'operator', 'alice')

        init = self.app.dispatch('POST', '/mcp', {}, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}, 'tool', 'tool')
        self.assertEqual(init['result']['serverInfo']['name'], 'sensehome_mcp')

        listed = self.app.dispatch('POST', '/mcp', {}, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}, 'tool', 'tool')
        tools = listed['result']['tools']
        names = {t['name'] for t in tools}
        self.assertEqual(len(tools), 6)
        self.assertIn('sensehome_list_devices', names)
        self.assertIn('sensehome_get_device_state', names)
        self.assertIn('sensehome_get_household_rules', names)
        self.assertIn('sensehome_get_care_incidents', names)
        self.assertIn('sensehome_get_sensing_status', names)
        self.assertIn('sensehome_validate_proposal', names)
        # Physical World Firewall invariant: NO confirm / execute / ack tool
        self.assertFalse(any('confirm' in n or 'execute' in n or 'ack' in n for n in names))
        self.assertTrue(all(t['annotations']['destructiveHint'] is False for t in tools))

        # Call tools
        dev_res = self.app.dispatch('POST', '/mcp', {}, {
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'sensehome_list_devices', 'arguments': {'room': '书房', 'response_format': 'markdown'}},
        }, 'tool', 'tool')
        self.assertFalse(dev_res['result']['isError'])
        self.assertGreaterEqual(dev_res['result']['structuredContent']['count'], 2)

        state_res = self.app.dispatch('POST', '/mcp', {}, {
            'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'sensehome_get_device_state', 'arguments': {'entity_ids': ['light.reading', 'binary_sensor.front_door']}},
        }, 'tool', 'tool')
        self.assertEqual(len(state_res['result']['structuredContent']['states']), 2)

        rules_res = self.app.dispatch('POST', '/mcp', {}, {
            'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
            'params': {'name': 'sensehome_get_household_rules', 'arguments': {'query': '客厅'}},
        }, 'tool', 'tool')
        self.assertGreaterEqual(rules_res['result']['structuredContent']['total'], 1)

        # Resources
        res_read = self.app.dispatch('POST', '/mcp', {}, {
            'jsonrpc': '2.0', 'id': 6, 'method': 'resources/read',
            'params': {'uri': 'sensehome://rules/care-sop'},
        }, 'tool', 'tool')
        self.assertIn('SOP-001', res_read['result']['contents'][0]['text'])

    def test_care_copilot_advisory_and_demo_preset_boundary(self):
        incident = self.app.care.create_event('sos_test_01', area_id='卧室')
        advisory = self.app.advise_care_incident(incident['id'])
        self.assertEqual(advisory['severity'], 'high')
        self.assertIn('SOP-001', advisory['sop_refs'])
        self.assertTrue(advisory['recommended_steps'])

        # Demo preset must be blocked if mode is home-assistant
        self.app.controller.mode = 'home-assistant'
        with self.assertRaises(PermissionError):
            self.app.apply_demo_preset({'preset': 'leave_home_ready'})

    def test_audit_hardening_readonly_intent_leave_direction_privacy_provenance_and_mcp_strictness(self):
        # 1. Read-only query cannot be hijacked by cloud candidate with actions
        hijack_read = {
            'result_kind': 'proposal',
            'agent_role': 'inspection-agent',
            'actions': [{'entity_id': 'light.reading', 'service': 'light.turn_on', 'data': {}}],
            'reason': '越权顺便开灯',
        }
        res_ro = evaluate_cloud_candidate('查看入户门磁的状态', hijack_read, self.app.controller, self.app.inventory)
        self.assertEqual(res_ro['status'], 'clarification')
        self.assertEqual(res_ro['code'], 'guardrail_readonly_intent_violation')

        # 2. Pure leave-home check cannot turn ON a light
        hijack_leave = {
            'result_kind': 'proposal',
            'agent_role': 'leave-home-agent',
            'actions': [{'entity_id': 'light.reading', 'service': 'light.turn_on', 'data': {}}],
            'reason': '离家反而开灯',
        }
        res_leave = evaluate_cloud_candidate('我先出门，按平时的离家约定检查一下', hijack_leave, self.app.controller, self.app.inventory)
        self.assertEqual(res_leave['status'], 'clarification')
        self.assertEqual(res_leave['code'], 'guardrail_leave_home_direction_violation')

        # 3. read_only_report with actions is rejected
        bad_report = {
            'result_kind': 'read_only_report',
            'actions': [{'entity_id': 'light.reading', 'service': 'light.turn_off', 'data': {}}],
            'reason': '矛盾类型',
        }
        self.assertEqual(
            evaluate_cloud_candidate('打开书房阅读灯', bad_report, self.app.controller, self.app.inventory)['code'],
            'invalid_candidate',
        )

        # 4. no_upload preserves exact original request_text and privacy_policy='no_upload' in SQLite
        import json
        import sqlite3
        original = '不要上传云端，打开书房阅读灯'
        card = self.app.operator_proposal(original, 'alice')
        self.assertEqual(card['status'], 'awaiting_confirmation')
        with sqlite3.connect(self.app.controller.proposals.path) as db:
            stored = json.loads(db.execute('SELECT card FROM proposals WHERE id=?', (card['proposal_id'],)).fetchone()[0])
        self.assertEqual(stored['request_text'], original)
        self.assertEqual(stored['metadata']['privacy_policy'], 'no_upload')

        # 5. MCP notifications return None, boolean limit rejected, unknown additionalProperties rejected
        notif = self.app.dispatch('POST', '/mcp', {}, {'jsonrpc': '2.0', 'method': 'notifications/cancelled'}, 'tool', 'tool')
        self.assertIsNone(notif)

        bool_limit = self.app.dispatch('POST', '/mcp', {}, {
            'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
            'params': {'name': 'sensehome_list_devices', 'arguments': {'limit': True}},
        }, 'tool', 'tool')
        self.assertTrue(bool_limit['result']['isError'])

        extra_prop = self.app.dispatch('POST', '/mcp', {}, {
            'jsonrpc': '2.0', 'id': 11, 'method': 'tools/call',
            'params': {'name': 'sensehome_list_devices', 'arguments': {'unexpected_field': 1}},
        }, 'tool', 'tool')
        self.assertTrue(extra_prop['result']['isError'])

        # 6. Devices include_states=1 & invalid drift entity ValueError
        devs_with_states = self.app.dispatch('GET', '/api/v1/devices', {'include_states': ['1']}, {}, 'operator', 'alice')
        self.assertIn('light.reading', devs_with_states['states'])
        with self.assertRaises(ValueError):
            self.app.apply_demo_preset({'preset': 'inject_state_drift', 'entity_id': 'camera.front_door'})


if __name__ == '__main__':
    unittest.main()
