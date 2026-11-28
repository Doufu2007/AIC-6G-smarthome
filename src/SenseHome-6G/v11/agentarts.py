"""Call a deployed task workflow or multi-agent app from the SenseHome backend."""

import copy
import json
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request
from uuid import uuid4

from deadline import request_bytes


class AgentArtsWorkflow:
    def __init__(self, url, key, *, result_path='proposal', timeout=20):
        endpoint = urlsplit(url)
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password:
            raise ValueError('AgentArts workflow URL must be HTTPS')
        if not key:
            raise ValueError('AgentArts runtime key is required')
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError('AgentArts timeout must be within 0..300 seconds')
        self.url, self.key, self.timeout = url, key, timeout
        self.endpoint_host = endpoint.hostname
        self.result_path = result_path.split('.') if result_path else []
        self._local = threading.local()

    @property
    def last_trace(self):
        return copy.deepcopy(getattr(self._local, 'trace', {}))

    def __call__(self, text, context):
        session_id = uuid4().hex
        started = time.perf_counter()
        trace = {
            'session_id': session_id,
            'endpoint_host': self.endpoint_host,
            'result_path': '.'.join(self.result_path),
        }
        self._local.trace = trace
        request = Request(self.url, method='POST', headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + self.key,
            'x-hw-agentarts-session-id': session_id,
        }, data=json.dumps({'inputs': {'query': text, 'context': context}}, ensure_ascii=False).encode('utf-8'))
        try:
            result = json.loads(request_bytes(request, self.timeout))
            for field in self.result_path:
                if isinstance(result, str):
                    result = json.loads(result)
                result = result[field]
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                raise ValueError('unexpected AgentArts result')
            trace['status'] = 'ok'
            return result
        except Exception as exc:
            trace['status'] = 'error'
            trace['error_type'] = type(exc).__name__
            raise
        finally:
            trace['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)

