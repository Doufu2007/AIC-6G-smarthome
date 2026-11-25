"""Call a deployed task workflow from the SenseHome backend."""

import json
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
        self.url, self.key, self.timeout = url, key, timeout
        self.result_path = result_path.split('.')

    def __call__(self, text, context):
        request = Request(self.url, method='POST', headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + self.key,
            'x-hw-agentarts-session-id': uuid4().hex,
        }, data=json.dumps({'inputs': {'query': text, 'context': context}}, ensure_ascii=False).encode('utf-8'))
        result = json.loads(request_bytes(request, self.timeout))
        for field in self.result_path:
            if isinstance(result, str):
                result = json.loads(result)
            result = result[field]
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ValueError('unexpected AgentArts result')
        return result
