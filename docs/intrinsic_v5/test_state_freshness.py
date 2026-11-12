"""F07/F04 regressions using private files and a controlled MQTT boundary."""
from copy import deepcopy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / 'openvela_smart_home/dashboard'
sys.path.insert(0, str(DASHBOARD))
sys.path.insert(0, str(ROOT / 'SenseHome-6G/net_offload'))


@pytest.fixture
def bridge():
    work = ROOT / '_work/intrinsic_v5'
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='freshness-', dir=work) as folder:
        private = Path(folder)
        with mock.patch.dict(os.environ, {
            'SENSEHOME_POLICY_CLI': str(private / 'missing-cli'),
            'SENSEHOME_EVENT_LOG': str(private / 'events.jsonl'),
            'SENSEHOME_STATE_FILE': str(private / 'state.json'),
            'SENSEHOME_REQUEST_DB': str(private / 'requests.sqlite3'),
            'SENSEHOME_RECEIPT_DB': '',
        }):
            spec = importlib.util.spec_from_file_location('private_freshness_bridge', DASHBOARD / 'bridge_server.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        module.save_state(module.default_state())
        yield module
        stop = getattr(module, 'close_state_refresh', None)
        if stop:
            assert stop(timeout_s=2)
        module.gateway_receipt_executor = None
        module.close_gateway()


class Platform:
    platform_id = 'freshness-home'

    def __init__(self, state):
        self.reported = deepcopy(state)
        self.reported['lights']['客厅'] = {'is_on': True, 'brightness': 60}
        self.version = 3
        self.sampled_at = time.time()
        self.connected = True
        self.reply = True
        self.transport = self

    def connection_state(self):
        return {'connected': self.connected, 'subscribed': self.connected, 'closed': False, 'generation': 1}

    def snapshot(self):
        if not self.reply:
            return None
        return {'schema': 'sensehome.software-state.v1', 'platform_id': self.platform_id,
                'evidence_kind': 'mqtt-software-entity', 'state_version': self.version,
                'software_state': deepcopy(self.reported), 'sampled_at': self.sampled_at}


def test_failed_snapshot_keeps_last_values_but_removes_current_scope(bridge):
    platform = Platform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    assert bridge._sync_mqtt_state() is not None
    platform.reply = False
    assert bridge._sync_mqtt_state() is None
    state = bridge.cached_state_view(refresh=False)
    assert state['lights']['客厅']['brightness'] == 60
    observation = state['_mqtt_observation']
    assert observation['sampled_at'] == platform.sampled_at
    assert observation['stale'] is True and observation['unknown'] is True
    assert observation['scope'] == 'last-known-software-platform-observation'
    assert observation['last_refresh_error'] == 'MQTT_SNAPSHOT_UNAVAILABLE'


def test_age_uses_monotonic_time_and_expires_without_wall_clock_drift(bridge):
    import state_freshness
    platform = Platform(bridge.load_state())
    platform.sampled_at = 99.0
    bridge.gateway_receipt_executor = platform
    with mock.patch.object(state_freshness, '_wall_time', return_value=100.0), \
            mock.patch.object(state_freshness, '_monotonic', return_value=10.0):
        assert bridge._sync_mqtt_state() is not None
    with mock.patch.object(state_freshness, '_wall_time', return_value=10000.0), \
            mock.patch.object(state_freshness, '_monotonic', return_value=12.0):
        observation = bridge.cached_state_view(refresh=False)['_mqtt_observation']
        assert observation['age_ms'] == pytest.approx(3000.0)
        assert observation['stale'] is False
    with mock.patch.object(state_freshness, '_monotonic', return_value=20.0):
        observation = bridge.cached_state_view(refresh=False)['_mqtt_observation']
        assert observation['age_ms'] == pytest.approx(11000.0)
        assert observation['stale'] is True and observation['unknown'] is True


def test_disconnected_transport_invalidates_even_recent_observation(bridge):
    platform = Platform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    assert bridge._sync_mqtt_state() is not None
    platform.connected = False
    observation = bridge.cached_state_view(refresh=False)['_mqtt_observation']
    assert observation['connection']['status'] == 'disconnected'
    assert observation['stale'] is True and observation['unknown'] is True


def test_fast_refresh_does_not_attach_new_metadata_to_old_cached_values(bridge):
    platform = Platform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    # Force the race where the refresh commits after GET loaded old values,
    # but before GET builds observation metadata.
    with mock.patch.object(bridge._mqtt_state_refresh, 'start',
                           side_effect=lambda callback: callback(threading.Event())):
        state = bridge.cached_state_view()
    assert state['lights']['客厅']['brightness'] == 0
    assert state['_mqtt_observation']['unknown'] is True
    assert state['_mqtt_observation']['sampled_at'] is None


def test_gateway_reopen_requires_live_reconciliation_of_persisted_cache(bridge):
    platform = Platform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    assert bridge._sync_mqtt_state() is not None
    assert bridge.close_state_refresh() is True
    bridge.reset_state_refresh()
    state = bridge.cached_state_view(refresh=False)
    assert state['lights']['客厅']['brightness'] == 60
    assert state['_mqtt_observation']['sampled_at'] == platform.sampled_at
    assert state['_mqtt_observation']['unknown'] is True
    assert bridge._sync_mqtt_state() is not None
    assert bridge.cached_state_view(refresh=False)['_mqtt_observation']['stale'] is False


def test_disabled_adapter_does_not_leave_persisted_snapshot_current(bridge):
    platform = Platform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    assert bridge._sync_mqtt_state() is not None
    bridge.gateway_receipt_executor = None
    observation = bridge.cached_state_view()['_mqtt_observation']
    assert observation['unknown'] is True and observation['stale'] is True
    assert observation['connection']['status'] == 'disabled'
    assert observation['scope'] == 'last-known-software-platform-observation'


@pytest.mark.parametrize('malformation', ['missing_timestamp', 'wrong_platform', 'invalid_structure', 'older_version'])
def test_invalid_or_older_snapshot_never_refreshes_trust(bridge, malformation):
    platform = Platform(bridge.load_state())
    bridge.gateway_receipt_executor = platform
    assert bridge._sync_mqtt_state() is not None
    snapshot = platform.snapshot()
    if malformation == 'missing_timestamp':
        snapshot.pop('sampled_at')
    elif malformation == 'wrong_platform':
        snapshot['platform_id'] = 'another-home'
    elif malformation == 'invalid_structure':
        snapshot['software_state']['lights']['客厅']['brightness'] = 101
    else:
        snapshot['state_version'] = 2
    platform.snapshot = lambda: snapshot
    assert bridge._sync_mqtt_state() is None
    state = bridge.cached_state_view(refresh=False)
    assert state['lights']['客厅']['brightness'] == 60
    assert state['_mqtt_observation']['state_version'] == 3
    assert state['_mqtt_observation']['stale'] is True


def test_state_http_returns_without_waiting_for_snapshot_and_has_one_refresh(bridge):
    entered, release = threading.Event(), threading.Event()
    platform = Platform(bridge.load_state())
    calls = []
    def delayed_snapshot():
        calls.append(1)
        entered.set()
        release.wait(1.0)
        return None
    platform.snapshot = delayed_snapshot
    bridge.gateway_receipt_executor = platform
    server = ThreadingHTTPServer(('127.0.0.1', 0), bridge.BridgeHandler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    def request():
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        try:
            started = time.monotonic()
            connection.request('GET', '/api/state')
            response = connection.getresponse()
            return json.loads(response.read()), time.monotonic() - started
        finally:
            connection.close()
    try:
        state, elapsed = request()
        assert elapsed < .3, 'GET /api/state waited for MQTT snapshot'
        assert entered.wait(.5)
        assert state['_mqtt_observation']['unknown'] is True
        assert state['_mqtt_observation']['sampled_at'] is None
        for _ in range(3):
            assert request()[1] < .3
        assert len(calls) == 1, 'concurrent polling created extra snapshot work'
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(2)


def test_close_cancels_refresh_result_and_joins_owned_worker(bridge):
    entered, release = threading.Event(), threading.Event()
    platform = Platform(bridge.load_state())
    snapshot = platform.snapshot()
    def delayed_snapshot():
        entered.set()
        release.wait(2)
        return snapshot
    platform.snapshot = delayed_snapshot
    bridge.gateway_receipt_executor = platform
    bridge.cached_state_view()
    assert entered.wait(.5)
    try:
        assert bridge.close_state_refresh(timeout_s=.01) is False
    finally:
        release.set()
    assert bridge.close_state_refresh(timeout_s=1) is True
    assert bridge.load_state()['lights']['客厅']['brightness'] == 0
    assert bridge.cached_state_view()['_mqtt_observation']['unknown'] is True
