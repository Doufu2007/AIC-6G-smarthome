from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from controller import Controller
from native_ha_adapter import NativeHAAdapter
from platform_execution import HAClient
import test_http_pipeline_v10 as fixtures


class PlatformAuditOutcome(unittest.TestCase):
    def test_unwritable_platform_audit_preserves_verified_result_and_reports_error(self):
        fixture=fixtures.HTTPPipelineTests();fixture.setUp()
        try:
            blocked=Path(fixture.temp.name)/'blocked';blocked.mkdir()
            platform=HAClient(fixture.url,'fixture-private-token',audit_path=blocked)
            entries=[dict(entity_id='light.reading',kind='light',room='书房',name='阅读灯',aliases=[],available=True)]
            app=Controller(Path(fixture.temp.name)/'app',entries,NativeHAAdapter(platform))
            card=app.propose('打开阅读灯')
            self.assertEqual(card['status'],'awaiting_confirmation')
            result=app.confirm(dict(proposal_id=card['proposal_id'],digest=card['digest'],request_id='audit',confirmed=True))
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual(result['platform_audit_status']['status'],'failed')
            self.assertEqual(result['platform_audit_status']['error_type'],'PermissionError')
            self.assertEqual(len([r for r in fixture.requests if r[0]=='POST']),1)
            self.assertEqual(app.receipt('audit')['status'],'succeeded')
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
