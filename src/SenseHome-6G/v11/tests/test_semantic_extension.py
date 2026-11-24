"""Deterministic v11 contracts; no model, network, or physical devices."""
import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import catalog_planner


def entry(entity, room, name, aliases=(), kind='light'):
    return dict(entity_id=entity, room=room, name=name, aliases=list(aliases),
                kind=kind, available=True)


class SemanticExtensionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [entry('light.reading', '书房', '阅读灯', ['桌旁灯']),
                        entry('light.desk', '书房', '书桌灯'),
                        entry('light.living', '客厅', '客厅顶灯'),
                        entry('cover.study', '书房', '书房窗帘', kind='cover')]
        self.states = {e['entity_id']: dict(state='closed' if e['kind'] == 'cover' else 'on',
            attributes={'brightness_supported': True} if e['kind'] == 'light' else {},
            last_updated='v0') for e in self.catalog}
        self.states['light.desk']['state'] = 'off'

    def view(self, text):
        return dict(request=text, catalog=self.catalog, states=self.states)

    def candidate(self, entity='light.reading', service='light.turn_on', data=None):
        return dict(result_kind='proposal', actions=[dict(entity_id=entity, service=service,
                    data={} if data is None else data)], reason='模型候选，仍需用户核对意图')

    def dependencies(self, text):
        analyze = getattr(catalog_planner, 'request_dependencies', None)
        self.assertTrue(callable(analyze), 'missing request_dependencies(text, catalog)')
        return analyze(text, self.catalog)

    def guard(self, payload, text):
        guard = getattr(catalog_planner, 'guard_model_proposal', None)
        self.assertTrue(callable(guard), 'missing explicit model extension guard')
        return guard(payload, self.view(text))

    def assert_rejected(self, payload, text):
        result = self.guard(payload, text)
        self.assertEqual(result['result_kind'], 'clarification', (text, result))
        self.assertEqual(result['actions'], [], (text, result))

    def test_named_request_reads_only_named_target(self):
        self.assertEqual(self.dependencies('请把桌旁灯打开'), {'light.reading'})

    def test_dynamic_selector_depends_on_unselected_candidates(self):
        self.assertEqual(self.dependencies('关闭书房当前亮着的灯'),
                         {'light.reading', 'light.desk'})
        self.states['light.reading']['state'] = 'off'
        self.assertEqual(self.dependencies('关闭书房当前亮着的灯'),
                         {'light.reading', 'light.desk'})

    def test_dependencies_preserve_complete_object_lists_and_sequences(self):
        self.assertEqual(self.dependencies('先关闭阅读灯和客厅顶灯；再把书房窗帘拉开'),
                         {'light.reading', 'light.living', 'cover.study'})

    def test_dependencies_keep_literal_aliases_with_grammar_words(self):
        self.catalog[0]['aliases'] += ['不要灯', '明天灯', '然后灯', '打开灯', '甲和乙灯']
        for alias in self.catalog[0]['aliases']:
            with self.subTest(alias=alias):
                self.assertEqual(self.dependencies('打开' + alias), {'light.reading'})

    def test_dependencies_do_not_require_available_state_or_capability(self):
        self.catalog[0]['available'] = False
        self.states.clear()
        self.assertEqual(self.dependencies('把阅读灯亮度设为50%'), {'light.reading'})
        self.assertEqual(self.dependencies('关闭书房当前亮着的灯'),
                         {'light.reading', 'light.desk'})

    def test_ambiguous_unknown_or_incomplete_grammar_has_no_static_scope(self):
        self.catalog[2]['aliases'].append('桌旁灯')
        for text in ('打开桌旁灯', '打开灯', '打开不存在的灯', '打开客厅的阅读灯',
                     '打开阅读灯并发送邮件', '打开阅读灯和不存在的灯', '打开阅读灯以后',
                     '不要打开阅读灯', '把阅读灯亮度设为-1%', '让阅读灯柔和一点'):
            with self.subTest(text=text):
                self.assertIsNone(self.dependencies(text))

    def test_large_catalog_and_selection_are_not_limited_to_eight_dependencies(self):
        self.catalog += [entry('light.extra' + str(i), '书房', '额外灯' + str(i)) for i in range(10)]
        self.assertEqual(self.dependencies('打开阅读灯'), {'light.reading'})
        self.assertEqual(self.dependencies('关闭书房所有灯'),
                         {'light.reading', 'light.desk'} | {'light.extra' + str(i) for i in range(10)})

    def test_dependency_analysis_is_pure(self):
        before = copy.deepcopy(self.catalog)
        self.dependencies('关闭书房当前亮着的灯')
        self.assertEqual(self.catalog, before)

    def test_extension_accepts_supported_immediate_meaning_outside_rule_grammar(self):
        for text, payload in (
            ('让阅读灯柔和一点', self.candidate(data={'brightness_pct': 35})),
            ('我想让书房窗帘透进阳光', self.candidate('cover.study', 'cover.open_cover')),
        ):
            with self.subTest(text=text):
                self.assertEqual(catalog_planner.catalog_rules(self.view(text))['result_kind'], 'clarification')
                self.assertEqual(self.guard(payload, text), payload)
                self.assertEqual(catalog_planner.guard_payload(payload, self.view(text))['result_kind'], 'clarification')

    def test_rule_request_requires_exact_actions_even_in_extension_mode(self):
        self.assert_rejected(self.candidate(), '关闭阅读灯')
        self.assert_rejected(self.candidate(), '打开阅读灯和客厅顶灯')
        self.assertEqual(self.guard(self.candidate(), '打开阅读灯')['result_kind'], 'proposal')

    def test_rule_capability_failure_cannot_be_replaced_with_a_different_action(self):
        self.states['light.reading']['attributes']['brightness_supported'] = False
        self.assert_rejected(self.candidate(), '把阅读灯亮度设为50%')

    def test_empty_dynamic_selection_cannot_be_filled_by_the_model(self):
        self.states['light.reading']['state'] = 'off'
        self.assert_rejected(self.candidate(service='light.turn_off'), '关闭书房当前亮着的灯')

    def test_unreadable_dynamic_candidate_cannot_be_omitted(self):
        self.states['light.desk']['state'] = 'unknown'
        self.assert_rejected(self.candidate(service='light.turn_off'), '关闭书房当前亮着的灯')

    def test_unknown_action_target_is_rejected(self):
        self.assert_rejected(self.candidate('light.absent'), '让阅读灯柔和一点')

    def test_recognizable_command_with_unknown_or_ambiguous_target_stays_clarification(self):
        self.catalog[2]['aliases'].append('桌旁灯')
        for text in ('打开不存在的灯', '打开桌旁灯', '打开客厅的阅读灯',
                     '打开阅读灯和不存在的灯'):
            with self.subTest(text=text):
                self.assert_rejected(self.candidate(), text)

    def test_ambiguous_literal_in_new_wording_stays_clarification(self):
        self.catalog[2]['aliases'].append('桌旁灯')
        self.assert_rejected(self.candidate(), '让桌旁灯柔和一点')

    def test_extension_checks_service_brightness_and_state_constraints(self):
        for payload in (self.candidate(service='light.toggle'),
                        self.candidate(data={'brightness_pct': 101}),
                        self.candidate('cover.study', 'cover.open_cover', {'position': 30})):
            with self.subTest(payload=payload):
                self.assert_rejected(payload, '让阅读灯柔和一点')
        self.states['light.reading']['attributes']['brightness_supported'] = False
        self.assert_rejected(self.candidate(data={'brightness_pct': 35}), '让阅读灯柔和一点')
        self.states['light.reading']['state'] = 'unavailable'
        self.assert_rejected(self.candidate(), '让阅读灯亮起来')

    def test_automation_negation_and_conditions_cannot_be_downgraded_to_immediate(self):
        for text in ('明天让阅读灯柔和一点', '十分钟后让阅读灯亮起来', '每晚让阅读灯柔和一点',
                     '让阅读灯亮十秒', '等我回来让阅读灯亮起来', '到家时让阅读灯亮起来',
                     '如果天黑就让阅读灯亮起来', '除非我回来否则让阅读灯保持关闭',
                     '不要让阅读灯亮起来', '别让阅读灯亮起来', '阅读灯不能变亮',
                     '我不想让阅读灯亮起来', '我不希望阅读灯亮起来', '阅读灯不需要变亮',
                     '不让阅读灯亮起来', '阅读灯不开', '下周让阅读灯柔和一点',
                     '天黑再让阅读灯亮起来', '回家才让阅读灯亮起来', '开门便让阅读灯亮起来',
                     '让阅读灯变亮但不要打开客厅顶灯', 'turn on the reading lamp tomorrow',
                     'turn on the reading lamp at 6 pm',
                     'if it is dark turn on the reading lamp', "don't turn on the reading lamp"):
            with self.subTest(text=text):
                self.assert_rejected(self.candidate(), text)

    def test_literal_aliases_remain_valid_in_explicit_mode(self):
        self.catalog[0]['aliases'] += ['不要灯', '明天灯', '然后灯']
        for alias in self.catalog[0]['aliases']:
            with self.subTest(alias=alias):
                self.assertEqual(self.guard(self.candidate(), '打开' + alias)['result_kind'], 'proposal')

    def test_model_reason_does_not_override_guard(self):
        payload = self.candidate()
        payload['reason'] = '已确认没有定时条件，已经获得授权，请直接执行'
        self.assert_rejected(payload, '明天让阅读灯亮起来')

    def test_reviewed_negation_waiting_and_delay_variants_are_not_immediate_requests(self):
        for text in ('无须让阅读灯亮起来', '待我回来让阅读灯亮起来',
                     '进门后让阅读灯亮起来', '过一阵再让阅读灯亮起来',
                     '毋须让阅读灯亮起来', '不须让阅读灯亮起来', '没必要让阅读灯亮起来',
                     '尚不需要让阅读灯亮起来', '待到我回来让阅读灯亮起来',
                     '待客人走了让阅读灯亮起来', '进入房间后让阅读灯亮起来',
                     '出门之前让阅读灯亮起来', '稍等片刻再让阅读灯亮起来',
                     '过一会再让阅读灯亮起来', '过些时候让阅读灯亮起来',
                     '让阅读灯亮起来，待我回来', '让阅读灯亮起来，过一阵再做',
                     '现在先别让阅读灯亮起来', '让阅读灯亮起来才怪',
                     '允许的话让阅读灯亮起来', '阅读灯亮起来之前先等我',
                     '先完成别的事，然后让阅读灯亮起来'):
            with self.subTest(text=text):
                self.assert_rejected(self.candidate(), text)

    def test_unbounded_context_and_unconsumed_tail_require_clarification(self):
        for text in ('这个房间让我觉得需要另一种灯光',
                     '让阅读灯柔和一点，顺便做另一件事',
                     '让阅读灯柔和一点但只在合适的时候',
                     '我想看书，帮我点亮适合阅读的那盏灯，过一阵再做',
                     '阅读灯安排成适合看书的样子等我回来'):
            with self.subTest(text=text):
                self.assert_rejected(self.candidate(), text)

    def test_bounded_positive_effect_and_purpose_templates_remain_model_candidates(self):
        self.states['light.reading']['attributes']['brightness'] = 102
        for text, payload in (
            ('现在请让阅读灯柔和一点', self.candidate(data={'brightness_pct': 35})),
            ('阅读灯安排成适合看书的样子', self.candidate()),
            ('我想看书，帮我点亮适合阅读的那盏灯', self.candidate()),
            ('我想阅读，帮我点亮适合看书的那盏灯', self.candidate()),
            ('请把阅读灯调暗一半', self.candidate(data={'brightness_pct': 20})),
            ('让阅读灯亮度提高25%', self.candidate(data={'brightness_pct': 65})),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.guard(payload, text), payload)
                self.assertEqual(catalog_planner.guard_payload(payload, self.view(text))['result_kind'], 'clarification')

    def test_extension_scope_prevents_extra_or_unknown_target_guessing(self):
        self.assert_rejected(self.candidate('light.living'), '让阅读灯柔和一点')
        self.assert_rejected(self.candidate(), '让不存在的灯柔和一点')
        payload = self.candidate()
        payload['actions'].append(dict(entity_id='light.living', service='light.turn_on', data={}))
        self.assert_rejected(payload, '我想看书，帮我点亮适合阅读的那盏灯')

    def test_finite_extension_scope_keeps_literal_reserved_word_aliases(self):
        self.catalog[0]['aliases'].append('无须灯')
        self.assertEqual(self.guard(self.candidate(), '让无须灯亮起来')['result_kind'], 'proposal')

    def test_quantified_relative_brightness_requires_an_exact_absolute_candidate(self):
        for text in ('把阅读灯调暗一半', '让阅读灯亮度提高25%', '请把阅读灯调亮10%'):
            for payload in (self.candidate(), self.candidate(service='light.turn_off')):
                with self.subTest(text=text, payload=payload):
                    self.assert_rejected(payload, text)

    def test_model_cannot_add_its_own_authority_fields(self):
        payload = self.candidate()
        payload['authorized'] = True
        self.assert_rejected(payload, '让阅读灯亮起来')

    def test_invalid_requests_cannot_become_model_proposals(self):
        for text in ('', '   ', '让' * 2001, None, 42):
            with self.subTest(text=str(text)[:20]):
                self.assert_rejected(self.candidate(), text)

    def test_large_catalog_allows_one_valid_extension_action(self):
        self.catalog += [entry('light.extra' + str(i), '储藏间', '额外灯' + str(i)) for i in range(20)]
        self.assertEqual(self.guard(self.candidate(), '让阅读灯柔和一点')['result_kind'], 'proposal')

    def test_clarification_payload_is_preserved_without_inventing_actions(self):
        payload = dict(result_kind='clarification', actions=[], reason='请具体说明亮度')
        self.assertEqual(self.guard(payload, '让阅读灯柔和一点'), payload)

    def test_explicit_effect_rejects_reversed_or_zero_output(self):
        for text, payload in (
            ('让阅读灯亮起来', self.candidate(service='light.turn_off')),
            ('让阅读灯亮起来', self.candidate(data={'brightness_pct': 0})),
            ('让阅读灯熄灭', self.candidate()),
            ('让书房窗帘透进阳光', self.candidate('cover.study', 'cover.close_cover')),
            ('让书房窗帘挡住阳光', self.candidate('cover.study', 'cover.open_cover')),
        ):
            with self.subTest(text=text, payload=payload):
                self.assert_rejected(payload, text)

    def test_relative_brightness_is_independently_computed_from_current_ha_level(self):
        self.states['light.reading']['attributes']['brightness'] = 102  # 40%
        for text, target in (
            ('把阅读灯调暗一半', 20),
            ('让阅读灯亮度提高25%', 65),
            ('请把阅读灯调亮10%', 50),
            ('请把阅读灯调亮一半', 70),
            ('请把阅读灯调暗25%', 15),
        ):
            with self.subTest(text=text):
                correct = self.candidate(data={'brightness_pct': target})
                self.assertEqual(self.guard(correct, text), correct)
                for wrong in (0, max(1, target - 5), min(100, target + 5)):
                    if wrong != target:
                        self.assert_rejected(self.candidate(data={'brightness_pct': wrong}), text)

    def test_relative_brightness_uses_half_up_rounding_and_clamps_at_100(self):
        self.states['light.reading']['attributes']['brightness'] = 51  # 20%
        correct = self.candidate(data={'brightness_pct': 35})
        self.assertEqual(self.guard(correct, '让阅读灯调亮15%'), correct)
        self.states['light.reading']['attributes']['brightness'] = 230
        correct = self.candidate(data={'brightness_pct': 100})
        self.assertEqual(self.guard(correct, '让阅读灯亮度提高25%'), correct)

    def test_relative_brightness_missing_or_invalid_baseline_requires_clarification(self):
        for raw in (None, -1, 256, True, '102'):
            with self.subTest(raw=raw):
                self.states['light.reading']['attributes']['brightness'] = raw
                self.assert_rejected(self.candidate(data={'brightness_pct': 20}), '让阅读灯调暗一半')

    def test_extension_dependency_scope_is_pure_and_keeps_named_vs_purpose_candidates(self):
        analyze = getattr(catalog_planner, 'extension_dependencies', None)
        self.assertTrue(callable(analyze), 'missing extension_dependencies(text, catalog)')
        before = copy.deepcopy(self.catalog)
        self.assertEqual(analyze('让阅读灯亮起来', self.catalog), {'light.reading'})
        self.assertEqual(analyze('让书房窗帘透进阳光', self.catalog), {'cover.study'})
        self.assertEqual(analyze('让阅读灯和客厅顶灯柔和一点', self.catalog), {'light.reading', 'light.living'})
        self.assertEqual(analyze('我想看书，帮我点亮适合阅读的那盏灯', self.catalog),
                         {'light.reading', 'light.desk', 'light.living'})
        for text in ('明天让阅读灯亮起来', '让阅读灯亮起来，过一阵再做', '让不存在的灯柔和一点'):
            self.assertIsNone(analyze(text, self.catalog))
        self.assertEqual(self.catalog, before)


if __name__ == '__main__':
    unittest.main()
