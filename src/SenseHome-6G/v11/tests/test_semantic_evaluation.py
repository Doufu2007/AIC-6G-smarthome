import pathlib
import sys
import unittest
import json
import io
from contextlib import redirect_stdout

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))


class SemanticEvaluation(unittest.TestCase):
    def test_main_reads_utf8_bom_cases_and_uses_local_endpoint(self):
        import evaluate_semantics
        import test_http_pipeline_v10 as fixtures
        fixture=fixtures.HTTPPipelineTests();fixture.setUp()
        try:
            case=dict(id='one',request='打开阅读灯',expected_actions=[dict(entity_id='light.reading',service='light.turn_on',data={})])
            cases=pathlib.Path(fixture.temp.name)/'cases.jsonl'
            cases.write_text(json.dumps(case,ensure_ascii=False)+'\n',encoding='utf-8-sig')
            output=pathlib.Path(fixture.temp.name)/'result.json'
            with redirect_stdout(io.StringIO()):
                code=evaluate_semantics.main(['--endpoint',fixture.url+'/v1/chat/completions','--model','HTTP-FIXTURE',
                    '--source-label','localhost_fixture_not_real_model','--cases',str(cases),'--output',str(output)])
            self.assertEqual(code,0)
            result=json.loads(output.read_text(encoding='utf8'))
            self.assertEqual(result['summary']['guarded_correct'],1)
            self.assertEqual(result['service_calls'],0)
            self.assertFalse(any(path.startswith('/api/services') for _,path,_ in fixture.requests))
        finally:fixture.tearDown()

    def test_reports_incremental_coverage_and_blocks_timed_action_without_dispatch(self):
        import evaluate_semantics
        entries=[dict(entity_id='light.reading',kind='light',room='书房',name='阅读灯',aliases=[],available=True)]
        state={'light.reading':dict(state='off',attributes={'supported_services':['light.turn_on','light.turn_off']},last_updated='v0')}
        action=dict(entity_id='light.reading',service='light.turn_on',data={})
        cases=[dict(id='immediate',request='我想看书，帮我点亮适合阅读的那盏灯',expected_actions=[action]),
               dict(id='timed',request='进门后让阅读灯亮起来',expected_actions=[])]
        def fixture(view):return dict(result_kind='proposal',actions=[action],reason='deterministic fixture')
        result=evaluate_semantics.evaluate_cases(fixture,entries,state,cases,source_label='deterministic_test_fixture')
        self.assertEqual(result['summary']['additional_action_cases_correct'],1)
        self.assertEqual(result['summary']['wrong_action_proposals'],0)
        self.assertEqual(result['summary']['guarded_correct'],2)
        self.assertEqual(result['service_calls'],0)
        self.assertEqual(result['source_label'],'deterministic_test_fixture')

    def test_wrong_candidate_is_counted_as_wrong_even_when_confirmable(self):
        import evaluate_semantics
        entries=[dict(entity_id='light.reading',kind='light',room='书房',name='阅读灯',aliases=[],available=True)]
        state={'light.reading':dict(state='off',attributes={},last_updated='v0')}
        expected=dict(entity_id='light.reading',service='light.turn_on',data={})
        wrong=dict(entity_id='light.reading',service='light.turn_off',data={})
        result=evaluate_semantics.evaluate_cases(lambda view:dict(result_kind='proposal',actions=[wrong],reason='wrong fixture'),
            entries,state,[dict(id='a',request='我想看书，帮我点亮适合阅读的那盏灯',expected_actions=[expected])],
            source_label='deterministic_test_fixture')
        self.assertEqual(result['summary']['guarded_correct'],0)
        self.assertEqual(result['summary']['wrong_action_proposals'],1)
        self.assertEqual(result['summary']['additional_action_cases_correct'],0)


if __name__=='__main__':unittest.main()
