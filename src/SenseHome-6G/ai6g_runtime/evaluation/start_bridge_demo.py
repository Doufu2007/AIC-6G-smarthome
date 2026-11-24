"""Start an isolated, rules-only Runtime + dashboard demonstration.

The launcher owns exactly two child PIDs and terminates only those children.
It never inherits AI6G endpoint variables, so an absent model endpoint is an
explicit runtime failure for complex requests rather than a fake local answer.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser
from urllib.request import build_opener, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[3]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def ready(url, process):
    deadline = time.time()+8
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError('child exited before ready')
        try:
            with build_opener(ProxyHandler({})).open(Request(url), timeout=.3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(.05)
    raise RuntimeError('child readiness timeout')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--runtime-port', type=int, default=0)
    parser.add_argument('--bridge-port', type=int, default=0)
    parser.add_argument('--open-browser', action='store_true',
                        help='open the dashboard after both services are ready')
    parser.add_argument('--duration', type=float, default=0,
                        help='seconds to keep services alive; 0 waits for Ctrl-C')
    args = parser.parse_args(argv)
    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    if args.runtime_port == 0: args.runtime_port = free_port()
    if args.bridge_port == 0: args.bridge_port = free_port()
    session = out/f'demo_session_{time.strftime("%Y%m%d_%H%M%S")}.json'
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('AI6G_', 'SENSEHOME_AI6G_'))}
    env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1',
               AI6G_STATE_PATH=str(out/'runtime_state.json'))
    processes, handles = [], []
    try:
        runtime_out = (out/'runtime.stdout.log').open('wb'); runtime_err = (out/'runtime.stderr.log').open('wb')
        handles.extend((runtime_out, runtime_err))
        runtime = subprocess.Popen([sys.executable, str(ROOT/'SenseHome-6G/ai6g_runtime/serve.py'),
            '--host', '127.0.0.1', '--port', str(args.runtime_port), '--log', str(out/'runtime_requests.jsonl'),
            '--state', str(out/'runtime_state.json')], cwd=ROOT, env=env, stdout=runtime_out, stderr=runtime_err,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes.append(runtime)
        ready(f'http://127.0.0.1:{args.runtime_port}/v1/health', runtime)
        bridge_out = (out/'bridge.stdout.log').open('wb'); bridge_err = (out/'bridge.stderr.log').open('wb')
        handles.extend((bridge_out, bridge_err))
        bridge_env = {**env, 'AI6G_RUNTIME_URL': f'http://127.0.0.1:{args.runtime_port}/v1/tasks'}
        bridge = subprocess.Popen([sys.executable, str(ROOT/'openvela_smart_home/dashboard/bridge_server.py'),
            '--host', '127.0.0.1', '--port', str(args.bridge_port), '--patrol-interval', '0',
            '--state', str(out/'legacy_unused.json')], cwd=ROOT, env=bridge_env, stdout=bridge_out, stderr=bridge_err,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes.append(bridge)
        bridge_url = f'http://127.0.0.1:{args.bridge_port}'
        ready(bridge_url + '/api/meta', bridge)
        if args.open_browser:
            webbrowser.open(bridge_url)
        if args.duration > 0:
            time.sleep(args.duration)
        else:
            print(f'Demo ready: http://127.0.0.1:{args.bridge_port}', flush=True)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        exits = []
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
            exits.append({'pid': child.pid, 'exit_code': child.returncode})
        for handle in handles: handle.close()
        report = {'schema': 'ai6g.bridge_demo_session.v1', 'source_kind': 'rules_only_software_demo',
                  'runtime_port': args.runtime_port, 'bridge_port': args.bridge_port,
                  'processes': list(reversed(exits)), 'state_path': str(out/'runtime_state.json'),
                  'log_path': str(out/'runtime_requests.jsonl'), 'legacy_state_expected': False,
                  'limits': ['no GPU/model endpoint inherited', 'software state only', 'no hardware sensors or 6G radio']}
        session.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
