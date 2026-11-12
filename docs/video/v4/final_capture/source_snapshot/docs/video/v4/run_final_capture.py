"""Own final model+MQTT capture without UI interaction; keep source and all errors."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import threading

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/demo_v4'))
from live_v4 import IntegratedSession
from capture_runtime import main as capture


def main():
    out = HERE / 'final_capture'
    out.mkdir(exist_ok=False)
    source_paths = [ROOT / p for p in (
        'openvela_smart_home/dashboard/bridge_server.py', 'openvela_smart_home/dashboard/device_actions.py',
        'SenseHome-6G/net_offload/offload_gateway.py', 'SenseHome-6G/net_offload/durable_receipts.py',
        'SenseHome-6G/net_offload/mqtt_receipts.py', 'docs/demo_v4/live_v4.py', 'docs/demo_v3/run_live.py',
        'docs/demo_v3/planning_recovery.py', 'docs/trial_v4/run_trial.py', 'docs/trial_v2/collector.py',
        'docs/video/v4/capture_runtime.py', 'docs/video/v4/run_final_capture.py')]
    hashes = {}
    for source in source_paths:
        rel = source.relative_to(ROOT)
        dest = out / 'source_snapshot' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        hashes[rel.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    session = IntegratedSession(port=0, models=True, mqtt=True)
    worker = threading.Thread(target=session.server.serve_forever, daemon=True)
    worker.start()
    try:
        capture(session.server.server_port, HERE / 'final_runtime_records.json',
                ROOT / 'docs/trial_v4/full_model_smoke_final.json')
    finally:
        session.close()
        worker.join(timeout=5)
        for name in ('run_manifest.json', 'runtime_manifest.json', 'offload_events.jsonl',
                     'model_backend_records.json', 'planning_recovery_records.json'):
            p = session.session.run_dir / name
            if p.exists():
                shutil.copy2(p, out / name)
        for name in ('cleanup.json', 'processes.json', 'gateway_mqtt.jsonl', 'entity_events.jsonl',
                     'gateway.sqlite3', 'entity.sqlite3'):
            p = session.run_dir / 'mqtt' / name
            if p.exists():
                shutil.copy2(p, out / ('mqtt_' + name))
        manifest = {**session.manifest, 'record_kind': 'automation_test',
            'method': 'current real model+gateway+MQTT HTTP API capture; later edited replay, not UI screen recording',
            'sources_unchanged': all(hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest
                                     for rel, digest in hashes.items()), 'final_source_sha256': hashes}
        (HERE / 'final_runtime_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'finished': True, 'sources_unchanged': manifest['sources_unchanged'],
            'original_state_and_events_unchanged': manifest['original_state_and_events_unchanged'],
            'strategy': manifest['strategy'], 'owned_runtime_status': manifest['status']}), flush=True)


if __name__ == '__main__':
    main()
