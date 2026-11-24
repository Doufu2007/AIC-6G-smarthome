"""Loopback-only, model-free interactive contract rehearsal.

This entry point deliberately exposes software state, not physical devices or
unverified model services. Its tasks pass through the production TaskRuntime.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai6g_runtime.command_contract_v7 import compile_contract
from ai6g_runtime.deploy.contract_profile_v7 import build_runtime
from ai6g_runtime.serve import RuntimeHandler, _json_safe_value


class RehearsalHandler(RuntimeHandler):
    """Serve same-origin local UI; observations cannot be externally published."""

    def _send(self, status, payload):
        body = json.dumps(_json_safe_value(payload), ensure_ascii=False, indent=2,
                          allow_nan=False).encode('utf8')
        self._bytes(status, body, 'application/json; charset=utf-8')

    def _bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self):
        allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in allowed_hosts or (origin and origin != 'http://' + host):
            self._send(403, {'ok': False, 'error_code': 'same_origin_required'})
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        if self.path == '/':
            page = Path(__file__).with_name('rehearsal_v7.html').read_bytes()
            self._bytes(200, page, 'text/html; charset=utf-8')
        elif self.path == '/v1/info':
            self._send(200, {'version': 7, 'mode': 'explicit_contract_rehearsal',
                'model_real': False, 'physical_device': False,
                'scope': '即时、明确灯光与窗帘命令；软件状态；未知、条件或定时请求需要澄清',
                'persistent_state': True, 'external_trial_count': 0})
        else:
            super().do_GET()

    def do_POST(self):
        if not self._local_request():
            return
        if self.path == '/v1/health':
            self.close_connection = True
            self._send(403, {'ok': False, 'error_code': 'health_read_only'})
        elif self.path == '/v1/contract':
            text = self._body().get('text')
            if not isinstance(text, str) or not text.strip():
                self._send(400, {'ok': False, 'error_code': 'invalid_task'})
                return
            contract = compile_contract(text)
            self._send(200, {'supported': contract is not None,
                'contract': asdict(contract) if contract is not None else None,
                'preview_only': True, 'model_real': False})
        else:
            super().do_POST()


def build_server(work_dir, port=0):
    destination = Path(work_dir)
    destination.mkdir(parents=True, exist_ok=True)
    runtime = build_runtime(destination/'runtime.jsonl', destination/'state.json')
    handler = type('BoundRehearsalHandler', (RehearsalHandler,), {'runtime': runtime})
    return ThreadingHTTPServer(('127.0.0.1', port), handler)


def main():
    parser = argparse.ArgumentParser(description='v7本机合同审验；软件状态；无模型调用')
    parser.add_argument('--port', type=int, default=8097)
    parser.add_argument('--work-dir', default='rehearsal_v7_data')
    args = parser.parse_args()
    server = build_server(args.work_dir, args.port)
    print(f'本机合同审验：http://127.0.0.1:{server.server_port} （Ctrl+C停止）', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
