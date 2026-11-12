"""Query-only request recovery after receipts precede a usable observation."""
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_state_freshness import bridge, Platform


class CrashAfterSubmission(BaseException):
    """Simulate process loss, bypassing ordinary transport-error recovery."""


def durable_crash_platform(bridge):
    from durable_receipts import SQLiteReceiptExecutor
    from mqtt_receipts import SoftwareEntityStore
    folder = Path(bridge.STATE_FILE).parent
    entity = SoftwareEntityStore(folder / 'crash-entity.sqlite3', 'crash-home', bridge.load_state())
    class Transport:
        def submit(self, payload):
            entity.submit(payload)
            raise CrashAfterSubmission()
        def lookup(self, command_id):
            return entity.lookup(command_id)
        def snapshot(self):
            return entity.snapshot()
    bridge.gateway_receipt_executor = SQLiteReceiptExecutor(folder / 'crash-receipts.sqlite3', Transport(),
                                                            platform_id='crash-home')
    from offload_gateway import OffloadGateway, RuleBackend
    class Policy:
        def decide(self, *args):
            return 'local-rules', 'TEST', ''
    bridge.gateway = OffloadGateway(Policy(), {'local-rules': RuleBackend(bridge._gateway_local_handler)},
                                   receipt_executor=bridge._gateway_action_executor)
    bridge.backend_monitor = None
    return entity


def test_clarified_goal_is_persisted_before_submission_and_recovers_after_crash(bridge):
    entity = durable_crash_platform(bridge)
    bridge.process_http_command('有点暗', {'session_id': 'crash-session', 'command_id': 'clarify-1'})
    bridge.process_http_command('卧室', {'session_id': 'crash-session', 'command_id': 'clarify-2'})
    with pytest.raises(CrashAfterSubmission):
        bridge.process_http_command('调到60%', {'session_id': 'crash-session', 'command_id': 'clarify-3'})
    assert entity.lookup('clarify-3')['status'] == 'applied'
    # Reopen the journal and discard all ephemeral clarification state.
    bridge.request_journal = None
    from clarification_sessions import ClarificationSessions
    bridge.clarification_sessions = ClarificationSessions()
    bridge.gateway = None
    recovered = bridge.lookup_http_request('clarify-3')
    assert recovered['task_complete'] is True
    assert recovered['request_state'] == 'finished'
    assert recovered['resolved_command'] == '把卧室灯光设为60%'


def test_leave_scene_recovery_preserves_unsupported_scope(bridge):
    entity = durable_crash_platform(bridge)
    with pytest.raises(CrashAfterSubmission):
        bridge.process_http_command('离家模式', {'command_id': 'leave-crash'})
    assert len(entity.lookup('leave-crash')['requested_actions']) == 10
    bridge.request_journal = None
    recovered = bridge.lookup_http_request('leave-crash')
    assert recovered['execution_complete'] is True
    assert recovered['supported_scope_complete'] is True
    assert recovered['task_complete'] is False
    assert recovered['semantic_complete'] is False
    assert recovered['unsupported_capabilities'] == ['security_arming']
    assert recovered['request_state'] == 'finished'


def test_patrol_read_goals_recover_after_submission_crash(bridge):
    entity = durable_crash_platform(bridge)
    with pytest.raises(CrashAfterSubmission):
        bridge.process_http_command('服务器主动巡检', {'command_id': 'patrol-crash'}, patrol=True)
    assert entity.lookup('patrol-crash')['status'] == 'applied'
    bridge.request_journal = None
    recovered = bridge.lookup_http_request('patrol-crash')
    assert recovered['execution_complete'] is True
    assert recovered['task_complete'] is True
    assert recovered['request_state'] == 'finished'


def test_resolution_binding_is_immutable_across_reopen(bridge):
    from command_requests import RequestJournal
    journal = bridge._get_request_journal()
    goal = [{'name': 'light_control', 'params': {'location': '卧室', 'brightness': 60}}]
    journal.execute('调到60%', {'command_id': 'immutable-resolution'}, lambda: {})
    journal.bind_resolution('immutable-resolution', resolved_command='把卧室灯光设为60%')
    journal.bind_resolution('immutable-resolution', goals=goal)
    reopened = RequestJournal(journal.path)
    reopened.bind_resolution('immutable-resolution', goals=goal)
    with pytest.raises(ValueError, match='REQUEST_RESOLUTION_CONFLICT'):
        reopened.bind_resolution('immutable-resolution', resolved_command='把客厅灯光设为60%')
    with pytest.raises(ValueError, match='REQUEST_GOAL_CONTRACT_CONFLICT'):
        reopened.bind_resolution('immutable-resolution', goals=[{'name': 'light_control',
            'params': {'location': '卧室', 'brightness': 20}}])
    with pytest.raises(ValueError, match='REQUEST_GOAL_CONTRACT_CONFLICT'):
        reopened.bind_resolution('immutable-resolution', goals=goal, unsupported_capabilities=['security_arming'])
    assert reopened.resolution('immutable-resolution')['goals'] == goal


def test_public_context_cannot_grant_a_recovery_goal(bridge):
    durable_crash_platform(bridge)
    result = bridge.process_http_command('打开灯', {'command_id': 'untrusted-resolution',
        'resolved_command': '把客厅灯光设为60%',
        '_server_goals': [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}]})
    assert result['task_complete'] is False
    binding = bridge._get_request_journal().resolution('untrusted-resolution')
    assert binding['resolved_command'] == '打开灯'
    assert binding.get('goals') is None


def test_concurrent_server_bindings_do_not_cross_request_ids(bridge):
    from concurrent.futures import ThreadPoolExecutor
    from command_requests import bind_active_resolution
    import threading
    journal = bridge._get_request_journal()
    barrier = threading.Barrier(2)
    def request(room):
        command_id = 'parallel-' + ('living' if room == '客厅' else 'bedroom')
        def operation():
            barrier.wait(timeout=2)
            bind_active_resolution(command_id=command_id, resolved_command=f'把{room}灯光设为60%')
            bind_active_resolution(command_id=command_id,
                goals=[{'name': 'light_control', 'params': {'location': room, 'brightness': 60}}])
            return {}
        journal.execute('调到60%', {'command_id': command_id}, operation)
        return journal.resolution(command_id)
    with ThreadPoolExecutor(2) as pool:
        resolutions = list(pool.map(request, ['客厅', '卧室']))
    assert [r['goals'][0]['params']['location'] for r in resolutions] == ['客厅', '卧室']
    assert bind_active_resolution(resolved_command='不能串到前一个请求') is None


def test_applied_receipt_waits_for_snapshot_then_confirms_without_resubmission(bridge):
    class AppliedPlatform(Platform):
        def lookup(self, command_id):
            return {'command_id': command_id, 'execution_state': 'applied', 'error': None,
                    'receipt': {'state_version_after': 3},
                    'actions': [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}]}
        def __call__(self, *args, **kwargs):
            raise AssertionError('request lookup must never execute an action')
        def submit(self, *args, **kwargs):
            raise AssertionError('request lookup must never submit an action')
    platform = AppliedPlatform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    journal = bridge._get_request_journal()
    initial = journal.execute('把客厅灯光设为60%', {'command_id': 'recover-observation'},
        lambda: {'execution_state': 'unknown', 'task_complete': False, 'semantic_complete': False})
    assert initial['request_state'] == 'unknown'
    platform.reply = False
    pending = bridge.lookup_http_request('recover-observation')
    assert pending['execution_complete'] is True
    assert pending['task_complete'] is False and pending['semantic_complete'] is False
    assert pending['request_state'] == 'observation_pending'
    platform.reply = True
    completed = bridge.lookup_http_request('recover-observation')
    assert completed['task_complete'] is True and completed['semantic_complete'] is True
    assert completed['request_state'] == 'finished'
    assert bridge.load_state()['lights']['客厅']['brightness'] == 60


def test_initial_applied_result_without_observation_remains_queryable(bridge):
    journal = bridge._get_request_journal()
    result = journal.execute('把客厅灯光设为60%', {'command_id': 'initial-observation-pending'},
        lambda: {'execution_state': 'applied', 'execution_complete': True,
                 'task_complete': False, 'semantic_complete': False})
    assert result['request_state'] == 'observation_pending'
    assert journal.lookup('initial-observation-pending')['task_complete'] is False


@pytest.mark.parametrize('receipt_version', [10, None])
def test_reconciliation_requires_observation_covering_receipt_version(bridge, receipt_version):
    class AppliedPlatform(Platform):
        def lookup(self, command_id):
            return {'command_id': command_id, 'execution_state': 'applied',
                    'receipt': {'state_version_after': receipt_version}, 'actions': []}

        def __call__(self, *args, **kwargs):
            raise AssertionError('reconciliation must not execute actions')

        def submit(self, *args, **kwargs):
            raise AssertionError('reconciliation must not submit actions')

    platform = AppliedPlatform(bridge.load_state())
    platform.version = 1
    bridge.gateway_receipt_executor = platform
    journal = bridge._get_request_journal()
    journal.execute('把客厅灯光设为60%', {'command_id': 'version-recovery'},
        lambda: {'execution_state': 'unknown', 'task_complete': False})
    pending = bridge.lookup_http_request('version-recovery')
    assert pending['task_complete'] is False
    assert pending['request_state'] == 'observation_pending'
    assert pending['goal_acceptance']['observation_covers_receipt_version'] is False
    if receipt_version is not None:
        platform.version = receipt_version
        finished = bridge.lookup_http_request('version-recovery')
        assert finished['task_complete'] is True
        assert finished['request_state'] == 'finished'
