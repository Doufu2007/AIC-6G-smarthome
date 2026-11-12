"""Run a new, never-overwritten real loopback MQTT/software integration record."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[2]
NET = ROOT / 'SenseHome-6G/net_offload'
sys.path.insert(0, str(NET))
sys.path.insert(0, str(ROOT / 'docs/trial_v2'))
from durable_receipts import SQLiteReceiptExecutor, canonical
from mqtt_receipts import LocalMQTTSession, MQTTReceiptTransport
from run_isolated_gateway import production_bridge, make_isolated_server
from offload_gateway import BackendResult


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


@contextmanager
def environment(changes):
    old = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def request(port, method, path, payload=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        conn.request(method, path, canonical(payload).encode('utf-8') if payload else None,
                     {'Content-Type': 'application/json', 'Host': f'127.0.0.1:{port}'})
        response = conn.getresponse()
        return {'http_status': response.status, 'body': json.loads(response.read())}
    finally:
        conn.close()


def state():
    rooms = ('客厅', '卧室', '厨房', '浴室', '书房', '室外')
    return {'lights': {room: {'is_on': False, 'brightness': 0} for room in rooms},
            'curtains': {room: {'is_open': False, 'position': 0} for room in rooms},
            'temperature': {room: {'temp': 25, 'humidity': 60} for room in rooms}}


class TwoActionPlanner:
    """Fixed contract probe, explicitly not a real model inference."""
    def infer(self, *args):
        return BackendResult(True, True, {'answer': 'fixed integration probe'}, [
            {'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}},
            {'name': 'curtain_control', 'params': {'location': '客厅', 'position': 20}},
        ], source='fixed-integration-probe')


def scenario(directory, *, drop_first_ack=False, partial_after=None):
    rows = []
    directory.mkdir(parents=True, exist_ok=False)
    with LocalMQTTSession(directory / 'mqtt', initial_state=state(), drop_first_ack=drop_first_ack,
                          partial_after=partial_after) as session:
        with environment({**session.environment, 'SENSEHOME_MQTT_TIMEOUT_S': '0.5',
                          'SENSEHOME_EVENT_LOG': str(directory / 'offload_events.jsonl')}):
            bridge = production_bridge(directory)
            server, _ = make_isolated_server(bridge, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            try:
                def call(label, command, command_id='integration-1'):
                    response = request(port, 'POST', '/api/command', {'command': command, 'context': {'command_id': command_id}})
                    rows.append({'case': label, **response})
                    return response['body']

                if partial_after:
                    bridge.gateway.backends['cloud-model'] = TwoActionPlanner()
                    class ContractProbePolicy:
                        def decide(self, *args):
                            return ('cloud-model', 'CONTRACT_PROBE', 'fixed contract probe, not model result')
                        def close(self):
                            original_policy.close()
                    original_policy = bridge.gateway.policy
                    bridge.gateway.policy = ContractProbePolicy()
                    bridge.gateway.link_probe = None
                    first = call('partial_batch', '客厅有点暗并且窗帘太亮')
                    assert first['execution_state'] == 'partial' and not first['semantic_complete']
                    again = call('partial_duplicate', '客厅有点暗并且窗帘太亮')
                    assert again['receipt']['completed_indices'] == [0]
                else:
                    first = call('ack_loss' if drop_first_ack else 'first', '打开客厅灯')
                    if drop_first_ack:
                        assert first['execution_state'] == 'unknown' and not first['semantic_complete']
                    else:
                        assert first['execution_state'] == 'applied' and first['semantic_complete']
                    again = call('query_after_ack_loss' if drop_first_ack else 'duplicate', '打开客厅灯')
                    assert again['execution_state'] == 'applied'
                    conflict = call('conflict', '关闭客厅灯')
                    assert conflict['execution_state'] == 'conflict' and not conflict['semantic_complete']
                    # Restart BOTH the bridge adapter and independent entity;
                    # the saved command is queried, not re-published.
                    bridge.close_gateway()
                    session.restart_entity()
                    bridge._init_gateway(start_monitor=False)
                    after_restart = call('after_restart', '打开客厅灯')
                    assert after_restart['execution_state'] == 'applied'
                    assert after_restart['receipt']['state_version_after'] == 1
                    if not drop_first_ack:
                        newer = call('newer_command_off', '关闭客厅灯', 'integration-2')
                        assert newer['execution_state'] == 'applied'
                        old = call('historical_receipt_after_newer_command', '打开客厅灯')
                        assert old['receipt']['state_version_after'] == 1
                        current = request(port, 'GET', '/api/state')['body']
                        assert current['lights']['客厅']['brightness'] == 0
                        assert current['_mqtt_observation']['state_version'] == 2

                lookup = request(port, 'GET', '/api/receipts/integration-1')
                rows.append({'case': 'http_receipt_lookup', **lookup})
                assert lookup['http_status'] == 200
                rows.append({'case': 'final_dashboard_state', **request(port, 'GET', '/api/state')})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                bridge.close_gateway()
    write(directory / 'http_results.json', rows)
    return rows


def contradictory(directory):
    """Real received MQTT receipt is mutated at the boundary, then rejected."""
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    with LocalMQTTSession(directory / 'mqtt', initial_state=state()) as session:
        transport = MQTTReceiptTransport('127.0.0.1', session.port, session.platform_id,
                                         event_path=directory / 'mqtt_boundary_raw.jsonl')
        class ContradictoryTransport:
            def submit(self, payload):
                receipt = transport.submit(payload)
                write(directory / 'received_original_receipt.json', receipt)
                receipt['platform_id'] = 'contradictory-platform'
                write(directory / 'injected_receipt.json', receipt)
                return receipt
            def lookup(self, command_id):
                receipt = transport.lookup(command_id)
                receipt['platform_id'] = 'contradictory-platform'
                return receipt
        executor = SQLiteReceiptExecutor(directory / 'gateway.sqlite3', ContradictoryTransport(), platform_id=session.platform_id)
        actions = [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}]
        try:
            first = executor(actions, {'command_id': 'contradiction-1', '_receipt_command': '客厅有点暗'})
            rows.append(first)
            assert first['execution_state'] == 'unknown' and first['actions'] == [] and not first['semantic_complete']
            rows.append(executor(actions, {'command_id': 'contradiction-1', '_receipt_command': '客厅有点暗'}))
            assert rows[-1]['execution_state'] == 'unknown'
        finally:
            transport.close()
    write(directory / 'outcomes.json', rows)
    return rows


def main():
    out = Path(__file__).with_name('runs') / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
    def protected_hashes():
        return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
                for p in protected}
    before = protected_hashes()
    try:
        results = {}
        results['normal'] = scenario(out / 'normal')
        results['ack_loss'] = scenario(out / 'ack_loss', drop_first_ack=True)
        results['partial'] = scenario(out / 'partial', partial_after=1)
        results['contradictory'] = contradictory(out / 'contradictory')
    finally:
        after = protected_hashes()
        write(out / 'protected_files.json', {'before': before, 'after': after, 'unchanged': before == after})
    if before != after:
        raise RuntimeError('ORIGINAL_STATE_OR_EVENTS_CHANGED')
    write(out / 'versions.json', {'python': sys.version, 'executable': sys.executable,
          'packages': {name: importlib.metadata.version(name) for name in ('paho-mqtt', 'amqtt')},
          'evidence_kind': 'real-loopback-mqtt-software-entity', 'physical_devices': 0,
          'model_inference_in_this_probe': False})
    sources = [NET / 'durable_receipts.py', NET / 'mqtt_receipts.py', NET / 'offload_gateway.py',
               ROOT / 'openvela_smart_home/dashboard/bridge_server.py',
               ROOT / 'openvela_smart_home/dashboard/device_actions.py', Path(__file__)]
    write(out / 'source_manifest.json', {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    summary = {'schema': 'sensehome.integration-v4.summary.v1', 'run_dir': str(out.relative_to(ROOT)),
               'formal_gateway_wiring': True, 'real_mqtt_broker': True, 'mqtt_software_entity': True,
               'physical_execution_claim': False, 'model_probe': 'fixed contract probe only for partial two-action test',
               'cases': {'duplicate': 'PASS', 'payload_conflict': 'PASS', 'ack_loss_query_only': 'PASS',
                         'gateway_and_entity_restart': 'PASS', 'partial_no_remaining_retry': 'PASS',
                         'contradictory_binding_rejected': 'PASS'}}
    write(out / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
