"""Zero-dependency Model Context Protocol (MCP) server for SenseHome.

Exposes strictly least-privilege READ and PROPOSE tools over both:
- Streamable HTTP (mounted at ``POST /mcp`` in ``web_demo.py`` with Bearer tool auth)
- stdio (``python -X utf8 -B mcp_server.py --home demo/web``)

Security invariant (Physical World Firewall):
This server intentionally omits ``confirm``, ``execute``, and ``ack`` tools.
Cloud agents connecting via MCP can inspect inventory, live sensor states,
household rules, SOS timelines, and sensing summaries, or validate a candidate
checklist, but can NEVER actuate physical devices without explicit human
confirmation on the local/operator console.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from typing import Any

from checklist import build_care_advisory, get_rules_catalog
from sensing import observe_csi, run_demo


LOG = logging.getLogger('sensehome_mcp')
PROTOCOL_VERSION = '2025-03-26'
SERVER_INFO = {
    'name': 'sensehome_mcp',
    'version': '1.1.0',
}
SERVER_INSTRUCTIONS = (
    'SenseHome Physical World Firewall MCP Server. '
    'All exposed tools are strictly read-only or non-executable proposal validators. '
    'Device execution requires explicit human confirmation on the operator console; '
    'no confirm or actuator tool is exposed to cloud agents.'
)


def _markdown_devices(payload: dict[str, Any]) -> str:
    lines = [f"### SenseHome Devices ({payload['count']} / {payload['total_count']})"]
    for dev in payload['items']:
        status = 'online' if dev.get('available') else 'offline'
        aliases = ', '.join(dev.get('aliases') or [])
        alias_text = f' | aliases: {aliases}' if aliases else ''
        lines.append(
            f"- **{dev['name']}** (`{dev['entity_id']}`) — room: {dev['room']}, "
            f"kind: {dev['kind']}, access: `{dev['access']}`, status: `{status}`{alias_text}"
        )
    if payload.get('has_more'):
        lines.append(f"\n_More devices available: use offset={payload['next_offset']}._")
    return '\n'.join(lines)


def _markdown_states(payload: dict[str, Any]) -> str:
    lines = ['### Live Device States']
    for item in payload['states']:
        attrs = item.get('attributes') or {}
        attr_str = ', '.join(f'{k}={v}' for k, v in attrs.items()) if attrs else 'none'
        lines.append(
            f"- `{item['entity_id']}`: **{item.get('state', 'unknown')}** "
            f"(updated: `{item.get('last_updated')}`, source: `{item.get('source')}`, attrs: {attr_str})"
        )
    if payload.get('errors'):
        lines.append('\n#### Errors')
        for err in payload['errors']:
            lines.append(f"- `{err['entity_id']}`: {err['error']}")
    return '\n'.join(lines)


def _markdown_rules(payload: dict[str, Any]) -> str:
    lines = [f"### SenseHome Rules & Care SOPs ({payload['total']})"]
    for rule in payload['rules']:
        lines.append(f"- **{rule['rule_id']} · {rule['title']}** (`{rule['category']}`): {rule['summary']}")
    return '\n'.join(lines)


def _markdown_care(payload: dict[str, Any]) -> str:
    incidents = payload.get('incidents') or []
    if not incidents:
        return '### Care Incidents\nNo SOS incidents currently recorded.'
    lines = [f'### Care Incidents ({len(incidents)})']
    for inc in incidents:
        lines.append(
            f"- `{inc['id']}` ({inc['area_id']}): status=`{inc['status']}`, "
            f"source=`{inc['source']}`, events={len(inc.get('timeline') or [])}"
        )
    if payload.get('advisory'):
        adv = payload['advisory']
        lines.append(f"\n**SOP Advisory ({', '.join(adv['sop_refs'])}):** {adv['headline']}")
    return '\n'.join(lines)


def _markdown_sensing(payload: dict[str, Any]) -> str:
    lines = [f"### Non-Intrusive Sensing Summary (`{payload.get('mode', 'offline')}`)"]
    for key, csi in (payload.get('csi') or {}).items():
        lines.append(f"- CSI `{key}`: status=`{csi.get('status')}`, source=`{csi.get('source')}`")
    rsi = payload.get('rsi') or {}
    lines.append(f"- Active RSI version: `{rsi.get('active_version', 'unknown')}`")
    return '\n'.join(lines)


TOOLS: list[dict[str, Any]] = [
    {
        'name': 'sensehome_list_devices',
        'description': (
            'List authorized SenseHome household devices with room, aliases, access boundary '
            '(control / read_only / event_only), supported services, and availability. '
            'Read-only; supports room/kind filtering and pagination.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'room': {'type': 'string', 'description': 'Optional room filter, e.g. "书房" or "客厅".'},
                'kind': {'type': 'string', 'description': 'Optional device kind filter: light, cover, switch, climate, binary_sensor, sensor, camera.'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50, 'description': 'Max devices to return.'},
                'offset': {'type': 'integer', 'minimum': 0, 'default': 0, 'description': 'Pagination offset.'},
                'response_format': {'type': 'string', 'enum': ['json', 'markdown'], 'default': 'json'},
            },
            'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': True,
            'destructiveHint': False,
            'idempotentHint': True,
            'openWorldHint': False,
        },
    },
    {
        'name': 'sensehome_get_device_state',
        'description': (
            'Read fresh real-time state, attributes, and sampling timestamp for one entity_id '
            'or a list of entity_ids. Read-only; never modifies device state.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'entity_id': {'type': 'string', 'description': 'Single entity ID such as "light.reading" or "binary_sensor.front_door".'},
                'entity_ids': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'minItems': 1,
                    'maxItems': 20,
                    'description': 'Batch list of entity IDs to inspect.',
                },
                'response_format': {'type': 'string', 'enum': ['json', 'markdown'], 'default': 'json'},
            },
            'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': True,
            'destructiveHint': False,
            'idempotentHint': True,
            'openWorldHint': False,
        },
    },
    {
        'name': 'sensehome_get_household_rules',
        'description': (
            'Retrieve versioned SenseHome household conventions (RULE-001..RULE-006) and '
            'home-care emergency SOPs (SOP-001..SOP-003) for grounded, citation-backed reasoning.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Optional keyword such as "客厅", "离家", "门磁", or "SOS".'},
                'category': {'type': 'string', 'enum': ['leave_home', 'inspection', 'scene', 'safety', 'privacy', 'care']},
                'response_format': {'type': 'string', 'enum': ['json', 'markdown'], 'default': 'json'},
            },
            'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': True,
            'destructiveHint': False,
            'idempotentHint': True,
            'openWorldHint': False,
        },
    },
    {
        'name': 'sensehome_get_care_incidents',
        'description': (
            'Read simulated home-care SOS incidents, timelines, notification delivery records, '
            'and optional SOP-cited family advisory context. Read-only; cannot trigger or acknowledge SOS.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'incident_id': {'type': 'string', 'description': 'Optional specific SOS incident ID.'},
                'include_advisory': {'type': 'boolean', 'default': True, 'description': 'Include SOP-cited read-only context summary.'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 10},
                'response_format': {'type': 'string', 'enum': ['json', 'markdown'], 'default': 'json'},
            },
            'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': True,
            'destructiveHint': False,
            'idempotentHint': True,
            'openWorldHint': False,
        },
    },
    {
        'name': 'sensehome_get_sensing_status',
        'description': (
            'Read non-intrusive Wi-Fi CSI window classification, World Model curtain predictions, '
            'and RSI model version history. Raw CSI IQ stays strictly local.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'response_format': {'type': 'string', 'enum': ['json', 'markdown'], 'default': 'json'},
            },
            'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': True,
            'destructiveHint': False,
            'idempotentHint': True,
            'openWorldHint': False,
        },
    },
    {
        'name': 'sensehome_validate_proposal',
        'description': (
            'Submit a cloud-reasoned candidate action list or dynamic checklist to the SenseHome edge '
            'guardrail for structural, capability, exclusion, and freshness validation. Produces only a '
            'non-executable confirmation card (or clarification/read_only_report); never executes devices.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'minLength': 1, 'maxLength': 2000, 'description': 'Original unmodified user utterance.'},
                'context': {'type': 'string', 'description': 'Optional ephemeral workflow context token.'},
                'candidate': {'type': 'object', 'description': 'Optional structured candidate payload with result_kind, actions, reason, and checklist.'},
                'response_format': {'type': 'string', 'enum': ['json', 'markdown'], 'default': 'json'},
            },
            'required': ['text'],
            'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': False,
            'destructiveHint': False,
            'idempotentHint': False,
            'openWorldHint': False,
        },
    },
]

RESOURCES: list[dict[str, Any]] = [
    {
        'uri': 'sensehome://rules/household',
        'name': 'SenseHome Household Rules',
        'description': 'Versioned household conventions and edge safety boundaries (RULE-001..RULE-006).',
        'mimeType': 'application/json',
    },
    {
        'uri': 'sensehome://rules/care-sop',
        'name': 'SenseHome Home Care SOS SOP',
        'description': 'Emergency SOS escalation and family response SOP (SOP-001..SOP-003).',
        'mimeType': 'application/json',
    },
]


class SenseHomeMCPServer:
    """Stateless JSON-RPC 2.0 MCP handler bound to a live ``DemoApp`` instance."""

    def __init__(self, app: Any):
        self.app = app

    def manifest(self) -> dict[str, Any]:
        """Return an inspection manifest for the Web Console governance view."""
        return {
            'server': SERVER_INFO,
            'protocol_version': PROTOCOL_VERSION,
            'instructions': SERVER_INSTRUCTIONS,
            'security_posture': {
                'read_only_tools': sum(1 for t in TOOLS if t['annotations']['readOnlyHint']),
                'proposal_only_tools': sum(1 for t in TOOLS if not t['annotations']['readOnlyHint']),
                'actuator_execution_tools': 0,
                'confirm_exposed': False,
                'transport': ['streamable-http (/mcp)', 'stdio (mcp_server.py)'],
            },
            'tools': TOOLS,
            'resources': RESOURCES,
        }

    def handle_jsonrpc(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            return self._error(None, -32600, 'Invalid Request: jsonrpc 2.0 object required')
        is_notification = 'id' not in message
        req_id = message.get('id')
        method = message.get('method')
        if not isinstance(method, str) or not method:
            return None if is_notification else self._error(req_id, -32600, 'Invalid Request: method string required')
        params = message.get('params')
        if params is not None and not isinstance(params, dict):
            return None if is_notification else self._error(req_id, -32602, 'Invalid params: object expected')
        params = params or {}

        if is_notification or method.startswith('notifications/'):
            return None
        if method == 'ping':
            return self._result(req_id, {})
        if method == 'initialize':
            return self._result(req_id, {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {'listChanged': False}, 'resources': {'listChanged': False}},
                'serverInfo': SERVER_INFO,
                'instructions': SERVER_INSTRUCTIONS,
            })
        if method == 'tools/list':
            return self._result(req_id, {'tools': TOOLS})
        if method == 'tools/call':
            return self._handle_tool_call(req_id, params)
        if method == 'resources/list':
            return self._result(req_id, {'resources': RESOURCES})
        if method == 'resources/read':
            return self._handle_resource_read(req_id, params)
        return self._error(req_id, -32601, f'Method not found: {method}')

    @staticmethod
    def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}

    @staticmethod
    def _tool_ok(payload: dict[str, Any], *, response_format: str = 'json', markdown: str | None = None) -> dict[str, Any]:
        text = markdown if (response_format == 'markdown' and markdown is not None) else json.dumps(
            payload, ensure_ascii=False, indent=2, allow_nan=False
        )
        return {
            'isError': False,
            'content': [{'type': 'text', 'text': text}],
            'structuredContent': payload,
        }

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': message}],
        }

    def _handle_resource_read(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get('uri')
        if uri == 'sensehome://rules/household':
            data = get_rules_catalog()
            data['rules'] = [r for r in data['rules'] if r['rule_id'].startswith('RULE-')]
            data['total'] = len(data['rules'])
        elif uri == 'sensehome://rules/care-sop':
            data = get_rules_catalog(category='care')
        else:
            return self._error(req_id, -32002, f'Unknown resource URI: {uri}')
        return self._result(req_id, {
            'contents': [{
                'uri': uri,
                'mimeType': 'application/json',
                'text': json.dumps(data, ensure_ascii=False, indent=2),
            }],
        })

    @staticmethod
    def _strict_int(value: Any, default: int) -> int:
        if value is None:
            return default
        if type(value) is not int:
            raise ValueError('integer parameter required')
        return value

    def _handle_tool_call(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get('name')
        raw_args = params.get('arguments')
        if raw_args is not None and not isinstance(raw_args, dict):
            return self._result(req_id, self._tool_error('Tool arguments must be a JSON object.'))
        args = raw_args or {}
        tool_spec = next((t for t in TOOLS if t['name'] == name), None)
        if tool_spec is None:
            return self._error(req_id, -32602, f'Unknown tool: {name}. Note: confirm/execute tools are intentionally excluded.')
        allowed_keys = set(tool_spec['inputSchema'].get('properties', {}))
        unknown_keys = set(args) - allowed_keys
        if unknown_keys:
            return self._result(req_id, self._tool_error(f'Unsupported arguments: {", ".join(sorted(unknown_keys))}.'))
        fmt = args.get('response_format', 'json')
        if fmt not in ('json', 'markdown'):
            return self._result(req_id, self._tool_error('Invalid response_format: choose "json" or "markdown".'))

        try:
            if name == 'sensehome_list_devices':
                room = args.get('room')
                kind = args.get('kind')
                limit = self._strict_int(args.get('limit'), 50)
                offset = self._strict_int(args.get('offset'), 0)
                if not 1 <= limit <= 100 or offset < 0:
                    return self._result(req_id, self._tool_error('limit must be 1..100 and offset >= 0.'))
                devices = self.app.inventory.list_devices(room=room)
                if kind:
                    devices = [d for d in devices if d.get('kind') == kind]
                total = len(devices)
                page = devices[offset:offset + limit]
                payload = {
                    'mode': self.app.controller.mode,
                    'total_count': total,
                    'count': len(page),
                    'offset': offset,
                    'has_more': offset + len(page) < total,
                    'next_offset': offset + len(page) if offset + len(page) < total else None,
                    'items': page,
                }
                return self._result(req_id, self._tool_ok(payload, response_format=fmt, markdown=_markdown_devices(payload)))

            if name == 'sensehome_get_device_state':
                ids = list(args.get('entity_ids') or []) if isinstance(args.get('entity_ids'), list) else (
                    [] if args.get('entity_ids') is None else None
                )
                if ids is None:
                    return self._result(req_id, self._tool_error('entity_ids must be an array of strings.'))
                if args.get('entity_id') is not None:
                    if not isinstance(args['entity_id'], str):
                        return self._result(req_id, self._tool_error('entity_id must be a string.'))
                    ids.insert(0, args['entity_id'])
                ids = list(dict.fromkeys(str(i) for i in ids if isinstance(i, str) and i.strip()))
                if not ids:
                    return self._result(req_id, self._tool_error(
                        'Provide entity_id or entity_ids. Call sensehome_list_devices first to discover valid IDs.'
                    ))
                if len(ids) > 20:
                    return self._result(req_id, self._tool_error('At most 20 entity_ids per batch call.'))
                states, errors = [], []
                for eid in ids:
                    try:
                        states.append(self.app.inventory.get_device_state(eid))
                    except KeyError:
                        errors.append({'entity_id': eid, 'error': 'Unknown entity_id; verify via sensehome_list_devices.'})
                payload = {'states': states, 'errors': errors}
                return self._result(req_id, self._tool_ok(payload, response_format=fmt, markdown=_markdown_states(payload)))

            if name == 'sensehome_get_household_rules':
                payload = get_rules_catalog(query=args.get('query'), category=args.get('category'))
                return self._result(req_id, self._tool_ok(payload, response_format=fmt, markdown=_markdown_rules(payload)))

            if name == 'sensehome_get_care_incidents':
                inc_id = args.get('incident_id')
                include_adv = args.get('include_advisory', True)
                if type(include_adv) is not bool:
                    return self._result(req_id, self._tool_error('include_advisory must be boolean.'))
                limit = self._strict_int(args.get('limit'), 10)
                if not 1 <= limit <= 50:
                    return self._result(req_id, self._tool_error('limit must be 1..50.'))
                if inc_id:
                    inc = self.app.care.get_incident(inc_id)
                    if inc is None:
                        return self._result(req_id, self._tool_error(f'Care incident "{inc_id}" not found.'))
                    incidents = [inc]
                else:
                    incidents = self.app.care.list_incidents()[:limit]
                advisory = None
                if include_adv and incidents:
                    advisory = build_care_advisory(incidents[0], self.app.inventory, run_demo(), source='mcp-care-read')
                payload = {'incidents': incidents, 'count': len(incidents), 'advisory': advisory}
                return self._result(req_id, self._tool_ok(payload, response_format=fmt, markdown=_markdown_care(payload)))

            if name == 'sensehome_get_sensing_status':
                report = run_demo()
                if self.app.csi_csv is not None:
                    with self.app.csi_csv.open(encoding='utf-8-sig') as capture:
                        report['csi']['replay'] = observe_csi(capture)
                return self._result(req_id, self._tool_ok(report, response_format=fmt, markdown=_markdown_sensing(report)))

            if name == 'sensehome_validate_proposal':
                if not isinstance(args.get('text'), str) or not args['text'].strip():
                    return self._result(req_id, self._tool_error('Nonempty string "text" is required.'))
                body = {'text': args['text']}
                if args.get('context'):
                    body['context'] = args['context']
                if args.get('candidate') is not None:
                    body['candidate'] = args['candidate']
                card = self.app.tool_proposal(body)
                return self._result(req_id, self._tool_ok(card, response_format=fmt))

            return self._error(req_id, -32602, f'Unknown tool: {name}.')
        except (ValueError, PermissionError, KeyError, TypeError) as exc:
            return self._result(req_id, self._tool_error(f'Error: {exc}'))


def run_stdio(app: Any) -> None:
    """Serve MCP JSON-RPC 2.0 line-delimited messages over stdin/stdout (logs on stderr)."""
    server = SenseHomeMCPServer(app)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': f'Parse error: {exc}'}}
        else:
            reply = server.handle_jsonrpc(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False, allow_nan=False) + '\n')
            sys.stdout.flush()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    parser = argparse.ArgumentParser(description='SenseHome Read-Only & Proposal MCP Server (stdio)')
    parser.add_argument('--home', default='demo/web', help='SenseHome data directory')
    args = parser.parse_args()

    from web_demo import DemoApp

    op_key = os.environ.get('SENSEHOME_OPERATOR_KEY') or secrets.token_urlsafe(18)
    tool_key = os.environ.get('SENSEHOME_TOOL_KEY') or secrets.token_urlsafe(24)
    if op_key == tool_key:
        tool_key = tool_key + '-mcp'
    app = DemoApp(args.home, op_key, tool_key)
    LOG.info('SenseHome MCP server listening on stdio (mode=%s, tools=%d)', app.controller.mode, len(TOOLS))
    run_stdio(app)


if __name__ == '__main__':
    main()
