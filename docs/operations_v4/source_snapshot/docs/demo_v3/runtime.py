"""Own local model children, pin files, and save actual launch argv before use."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = Path(__file__).with_name('local_models.json')


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def checked_url(value):
    p = urlsplit(value)
    try:
        valid = ipaddress.ip_address(p.hostname).is_loopback
    except (ValueError, TypeError):
        valid = False
    if not valid or p.scheme != 'http' or p.username or p.password or p.query or p.fragment:
        raise ValueError('ONLY_LITERAL_LOOPBACK_HTTP')
    return p


def checked_file(path, expected):
    path = Path(path).resolve(strict=True)
    actual = digest(path)
    if actual != expected:
        raise ValueError('HASH_MISMATCH:' + path.name)
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': actual}


def get_json(url, timeout=2):
    p = checked_url(url)
    connection = http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
    result = {'requested_url': url, 'http_status': 0, 'response': None, 'error': None}
    started = time.perf_counter()
    try:
        connection.request('GET', p.path or '/')
        response = connection.getresponse()
        result['http_status'] = response.status
        raw = response.read(262145)
        if response.status != 200:
            result['error'] = 'HTTP_' + str(response.status)
        elif len(raw) > 262144:
            result['error'] = 'RESPONSE_TOO_LARGE'
        else:
            result['response'] = json.loads(raw)
    except Exception as exc:
        result['error'] = type(exc).__name__
    finally:
        connection.close()
    result['wall_ms'] = round((time.perf_counter() - started) * 1000, 3)
    return result


def doctor(config_path=DEFAULT):
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text(encoding='utf-8'))
    executable = checked_file(config_path.parent / cfg['runtime']['path'], cfg['runtime']['sha256'])
    runtime_dir = Path(executable['path']).parent
    pins = cfg['runtime'].get('dependencies', {})
    actual_names = {p.name for p in runtime_dir.glob('*.dll')}
    if actual_names != set(pins):
        raise ValueError('RUNTIME_DEPENDENCY_SET_MISMATCH')
    dependencies = {name: checked_file(runtime_dir / name, expected)
                    for name, expected in sorted(pins.items())}
    models = {}
    ports = []
    for tier in ('local', 'cloud'):
        spec = cfg['models'][tier]
        info = checked_file(config_path.parent / spec['path'], spec['sha256'])
        port = spec['port']
        if type(port) is not int or not 1024 <= port <= 65535 or port in ports:
            raise ValueError('INVALID_OR_DUPLICATE_PORT')
        ports.append(port)
        checked_url(f'http://127.0.0.1:{port}/v1')
        models[tier] = {**info, 'model_id': spec['model_id'], 'port': port}
    return {'schema': 'sensehome.runtime-doctor.v3', 'checked_at_utc': now(),
            'python': os.sys.version, 'runtime': executable,
            'runtime_dependencies': dependencies, 'models': models,
            'scope': 'existing file integrity and launch plan; not live model, deployment or physical-device certification',
            'network': 'literal loopback only; both candidates share this PC/GPU',
            'config_sha256': digest(config_path)}


class LocalRuntime:
    def __init__(self, run_dir, config_path=DEFAULT, startup_timeout=90):
        self.run_dir = Path(run_dir)
        self.inventory = doctor(config_path)
        self.children, self.logs = [], []
        self.manifest = {**self.inventory, 'schema': 'sensehome.live-model-runtime.v3',
                         'started_at_utc': now(), 'children': [], 'status': 'starting'}
        self.startup_timeout = startup_timeout

    def save(self):
        write(self.run_dir / 'runtime_manifest.json', self.manifest)

    def __enter__(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            for tier, model in self.inventory['models'].items():
                with socket.socket() as probe:
                    # A refused connection does not prove a port can be bound:
                    # Windows may reserve an otherwise unused TCP port range.
                    probe.bind(('127.0.0.1', model['port']))
                argv = [self.inventory['runtime']['path'], '-m', model['path'], '--alias', model['model_id'],
                        '--host', '127.0.0.1', '--port', str(model['port']), '-c', '2048', '-np', '1',
                        '-ngl', '99', '--flash-attn', 'on', '--no-webui', '--metrics']
                if tier == 'cloud':
                    argv += ['--reasoning-budget', '0']
                log = (self.run_dir / f'{tier}_server.log').open('wb')
                self.logs.append(log)
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.children.append(process)
                entry = {'tier': tier, 'pid': process.pid, 'argv': argv,
                         'launched_at_utc': now(), 'model_sha256': model['sha256'],
                         'runtime_sha256': self.inventory['runtime']['sha256'], 'health': None}
                self.manifest['children'].append(entry)
                self.save()
                end = time.monotonic() + self.startup_timeout
                while time.monotonic() < end:
                    if process.poll() is not None:
                        raise RuntimeError('MODEL_EXITED:' + tier)
                    result = get_json(f"http://127.0.0.1:{model['port']}/health", 1)
                    if result['http_status'] == 200 and result['error'] is None:
                        inventory = get_json(f"http://127.0.0.1:{model['port']}/v1/models")
                        ids = [x.get('id') for x in (inventory.get('response') or {}).get('data', [])]
                        if model['model_id'] not in ids:
                            raise RuntimeError('MODEL_ID_NOT_IN_INVENTORY')
                        entry.update(health=result, inventory=inventory, ready_at_utc=now())
                        self.save()
                        break
                    time.sleep(.25)
                else:
                    raise TimeoutError('MODEL_STARTUP_TIMEOUT:' + tier)
            self.manifest['status'] = 'ready'
            self.save()
            return self
        except BaseException:
            self.close()
            raise

    def endpoints(self):
        return {tier: {'url': f"http://127.0.0.1:{m['port']}/v1", 'model': m['model_id']}
                for tier, m in self.inventory['models'].items()}

    def close(self):
        for process, entry in zip(self.children, self.manifest['children']):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            entry['exit_code'] = process.returncode
        for log in self.logs:
            log.close()
        self.manifest.update(status='stopped', finished_at_utc=now())
        self.save()

    def __exit__(self, *args):
        self.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = doctor(args.config)
    if args.output:
        write(args.output, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
