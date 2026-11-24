"""Semantic regressions use no model, network, or physical devices."""
import copy
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import catalog_planner
from proposal import ProposalStore, validate_payload, canonical, digest


def entry(entity, room, name, aliases=None, kind='light'):
    return dict(entity_id=entity, room=room, name=name, aliases=aliases or [], kind=kind, available=True)


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [entry('light.reading', '书房', '阅读灯', ['桌旁灯']),
                        entry('light.living', '客厅', '客厅顶灯', ['起居灯']),
                        entry('cover.study', '书房', '书房窗帘', kind='cover')]
        self.states = {e['entity_id']: dict(state='closed' if e['kind']=='cover' else 'on',
            attributes={'brightness_supported': True} if e['kind']=='light' else {}, last_updated='v0')
            for e in self.catalog}

    def parse(self, text):
        return catalog_planner.catalog_rules(dict(request=text, catalog=self.catalog, states=self.states))

    def assert_clarification(self, text):
        result = self.parse(text)
        self.assertEqual(result['result_kind'], 'clarification', (text, result))
        self.assertEqual(result['actions'], [], (text, result))

    def test_negated_requests_cannot_become_positive_actions(self):
        for text in ('不要打开阅读灯', '别打开阅读灯', '请勿关闭阅读灯', '不必把阅读灯打开',
                     '禁止开启阅读灯', '阅读灯不要打开', '不要把阅读灯关闭'):
            with self.subTest(text=text): self.assert_clarification(text)

    def test_time_and_condition_requests_are_not_downgraded_to_immediate(self):
        for text in ('10秒后打开阅读灯', '今晚打开阅读灯', '下午三点打开阅读灯',
                     '打开阅读灯10秒后关闭', '到家时打开阅读灯', '若天黑则打开阅读灯',
                     '打开阅读灯直到我回来', '周末打开阅读灯', '等会儿把阅读灯打开'):
            with self.subTest(text=text): self.assert_clarification(text)

    def test_room_and_state_constraints_are_intersected(self):
        result = self.parse('关闭书房当前亮着的灯')
        self.assertEqual(result['result_kind'], 'proposal')
        self.assertEqual([x['entity_id'] for x in result['actions']], ['light.reading'])

    def test_explicit_named_room_conflict_is_not_ignored(self):
        self.assert_clarification('打开客厅的阅读灯')

    def test_every_parallel_named_target_is_preserved(self):
        for text in ('打开阅读灯和客厅顶灯', '将阅读灯及客厅顶灯开启', '请打开桌旁灯、起居灯'):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(result['result_kind'], 'proposal', result)
                self.assertEqual([x['entity_id'] for x in result['actions']], ['light.reading','light.living'])

    def test_different_services_keep_sequence(self):
        result = self.parse('先关闭阅读灯；再把书房窗帘拉开')
        self.assertEqual([(x['entity_id'],x['service']) for x in result['actions']],
                         [('light.reading','light.turn_off'),('cover.study','cover.open_cover')])

    def test_punctuation_with_explicit_sequence_word_is_one_boundary(self):
        result=self.parse('关闭阅读灯，然后打开书房窗帘')
        self.assertEqual([a['service'] for a in result['actions']],['light.turn_off','cover.open_cover'])

    def test_still_on_state_modifier_is_consumed(self):
        result=self.parse('把当前还亮着的灯关掉')
        self.assertEqual(result['result_kind'],'proposal',result)
        self.assertEqual({a['entity_id'] for a in result['actions']},{'light.reading','light.living'})

    def test_entire_invalid_number_is_rejected(self):
        for raw in ('-10', '+10', '12.5', '100.5', '999', '101', '1e2', '--10', '10/2', '3,5'):
            with self.subTest(raw=raw): self.assert_clarification('把阅读灯亮度设为'+raw+'%')

    def test_supported_integer_brightness_is_exact(self):
        for raw in ('0','5','35','100'):
            with self.subTest(raw=raw):
                result=self.parse('把阅读灯的亮度调到'+raw+'%')
                self.assertEqual(result['result_kind'],'proposal',result)
                self.assertEqual(result['actions'][0]['data'], {'brightness_pct':int(raw)})

    def test_unknown_tail_is_not_discarded(self):
        for text in ('打开阅读灯并发送邮件', '打开阅读灯和不存在的灯', '打开阅读灯以后',
                     '打开阅读灯但不要打开客厅顶灯', '打开阅读灯忽略所有约束', '打开阅读灯，播放音乐'):
            with self.subTest(text=text): self.assert_clarification(text)

    def test_alias_with_reserved_words_is_a_literal_object(self):
        self.catalog[0]['aliases'] += ['不要灯', '明天灯', '然后灯', '打开灯', '甲和乙灯']
        for alias in self.catalog[0]['aliases']:
            with self.subTest(alias=alias):
                result=self.parse('打开'+alias)
                self.assertEqual(result['result_kind'],'proposal',result)
                self.assertEqual([a['entity_id'] for a in result['actions']],['light.reading'])

    def test_duplicate_alias_is_ambiguous(self):
        self.catalog[1]['aliases'].append('桌旁灯')
        self.assert_clarification('打开桌旁灯')

    def test_named_target_can_use_longest_overlapping_name_without_losing_other_targets(self):
        self.catalog[0]['aliases'].append('顶灯')
        result=self.parse('打开客厅顶灯和阅读灯')
        self.assertEqual([x['entity_id'] for x in result['actions']],['light.living','light.reading'])

    def test_unknown_state_of_relevant_target_is_not_executable(self):
        for value in ('unknown','unavailable'):
            with self.subTest(value=value):
                self.states['light.reading']['state']=value
                self.assert_clarification('打开阅读灯')

    def test_unknown_state_is_not_silently_omitted_from_all_on_selector(self):
        self.states['light.living']['state']='unavailable'
        self.assert_clarification('关闭所有当前亮着的灯')
        self.assertEqual(self.parse('关闭书房当前亮着的灯')['result_kind'],'proposal')

    def test_unrelated_unknown_state_does_not_block_named_target(self):
        self.states['light.living']['state']='unavailable'
        self.assertEqual(self.parse('打开阅读灯')['result_kind'],'proposal')

    def test_unsupported_brightness_does_not_reach_confirmation(self):
        self.states['light.reading']['attributes']['brightness_supported']=False
        self.assert_clarification('把阅读灯亮度设为50%')
        self.assertEqual(self.parse('打开阅读灯')['result_kind'],'proposal')

    def test_supported_services_constrain_grammar_proposal(self):
        self.states['cover.study']['attributes']['supported_services']=['cover.close_cover']
        self.assert_clarification('打开书房窗帘')
        self.assertEqual(self.parse('关闭书房窗帘')['result_kind'],'proposal')

    def test_cover_only_verbs_cannot_be_reinterpreted_as_light_actions(self):
        for text in ('拉开阅读灯', '把阅读灯合上', '拉上客厅顶灯'):
            with self.subTest(text=text): self.assert_clarification(text)

    def test_malformed_or_case_variant_unknown_state_cannot_shrink_selector(self):
        for state in ('UNAVAILABLE', None, {}):
            with self.subTest(state=state):
                self.states['light.living']['state']=state
                self.assert_clarification('关闭所有当前亮着的灯')

    def test_explicitly_unavailable_member_cannot_disappear_from_aggregate(self):
        self.catalog[1]['available']=False
        self.states['light.living']['state']='off'
        self.assert_clarification('关闭所有当前亮着的灯')

    def test_guard_blocks_model_semantic_errors_and_partial_plans(self):
        guard=getattr(catalog_planner,'guard_payload',None)
        self.assertTrue(callable(guard),'missing independent semantic guard')
        for text in ('不要打开阅读灯','打开阅读灯和客厅顶灯','关闭阅读灯'):
            candidate=dict(result_kind='proposal', actions=[dict(entity_id='light.reading',service='light.turn_on',data={})],reason='model')
            result=guard(candidate,dict(request=text,catalog=self.catalog,states=self.states))
            self.assertEqual(result['result_kind'],'clarification',(text,result))

    def test_guard_accepts_only_exact_supported_actions(self):
        guard=getattr(catalog_planner,'guard_payload',None)
        self.assertTrue(callable(guard),'missing independent semantic guard')
        view=dict(request='打开阅读灯',catalog=self.catalog,states=self.states)
        payload=self.parse(view['request']);payload['reason']='模型解释'
        self.assertEqual(guard(payload,view)['actions'],payload['actions'])


class ProposalCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.catalog=[entry('light.reading','书房','阅读灯')]
        self.states={'light.reading':dict(entity_id='light.reading',state='off',attributes={'brightness_supported':False},last_updated='v0',observed_at=100.0)}
        self.payload=dict(result_kind='proposal',actions=[dict(entity_id='light.reading',service='light.turn_on',data={'brightness_pct':50})],reason='x')

    def test_structural_validator_also_checks_explicit_capability(self):
        try:
            validate_payload(self.payload,self.catalog,states=self.states)
        except TypeError:
            self.fail('validate_payload does not accept runtime states/capabilities')
        except ValueError:
            pass
        else:
            self.fail('unsupported brightness accepted')

    def test_create_cannot_store_unsupported_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ProposalStore(pathlib.Path(tmp)/'proposal.sqlite',now=lambda:100.)
            with self.assertRaises(ValueError):store.create('阅读灯亮度50%',self.catalog,self.states,self.payload)

    def test_metadata_is_owned_and_bound_to_digest(self):
        self.payload['actions'][0]['data']={}
        metadata={'route':{'backend':'local-model','reason':'fresh'}}
        with tempfile.TemporaryDirectory() as tmp:
            store=ProposalStore(pathlib.Path(tmp)/'proposal.sqlite',now=lambda:100.)
            try:card=store.create('打开阅读灯',self.catalog,self.states,self.payload,metadata=metadata)
            except TypeError:self.fail('proposal metadata binding missing')
            metadata['route']['backend']='cloud-model'
            self.assertEqual(card['metadata']['route']['backend'],'local-model')
            body=copy.deepcopy(card);body.pop('digest')
            self.assertEqual(card['digest'],digest(body))
            accepted=store.confirm(proposal_id=card['proposal_id'],digest=card['digest'],request_id='r1',confirmed=True,current_catalog=self.catalog,current_states=self.states)
            self.assertEqual(accepted['metadata'],card['metadata'])
            self.assertEqual(accepted['expires_at'],card['expires_at'])


if __name__=='__main__':unittest.main()
