"""Audit raw model POST usage without new inference or billing assumptions."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import statistics
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / 'docs/experiments/20261111_fused_real'
RUNS = ('reviewed_20_sealed_repeat3', 'challenge_12_sealed_development_repeat3',
        'controlled_faults_sealed', 'holdout_12_programmatic_repeat3')


def loopback(url):
    host = urlsplit(url).hostname
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return host == 'localhost'


def extract_calls(report, source_path, source_sha256, matrix):
    records = []
    for row_index, row in enumerate(report['rows']):
        count = 0
        for attempt_index, attempt in enumerate(row.get('backend_attempts', [])):
            for exchange_index, exchange in enumerate(attempt.get('upstream_exchanges', [])):
                if exchange.get('method') != 'POST':
                    continue
                count += 1
                response = exchange.get('response') or {}
                request = exchange.get('request') or {}
                model = response.get('model') or request.get('model') or 'unknown'
                matching = [endpoint for endpoint in report['endpoints'].values()
                            if endpoint.get('configured_model') == request.get('model')]
                selected = row.get('gateway_event', {}).get('selected_backend', '')
                slot = 'local' if selected == 'local-model' else 'cloud' if selected == 'cloud-model' else None
                endpoint = matching[0] if len(matching) == 1 else report['endpoints'].get(slot, {})
                usage = response.get('usage')
                issues, tokens = [], {}
                for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    value = usage.get(key) if isinstance(usage, dict) else None
                    if type(value) is not int or value < 0:
                        issues.append(key + ': missing or not a nonnegative integer')
                        value = None
                    tokens[key] = value
                if all(value is not None for value in tokens.values()) and tokens['total_tokens'] != tokens['prompt_tokens'] + tokens['completion_tokens']:
                    issues.append('total_tokens differs from prompt_tokens + completion_tokens')
                cached = usage.get('prompt_tokens_details', {}).get('cached_tokens') if isinstance(usage, dict) else None
                if type(cached) is not int or cached < 0:
                    cached = None
                elapsed = exchange.get('wall_ms')
                if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
                    elapsed = None
                records.append({'matrix': matrix, 'case_id': row.get('id'), 'pair_id': row.get('pair_id'),
                    'arm': row.get('arm'), 'model': model, 'endpoint_slot': slot,
                    'endpoint_identity': endpoint.get('endpoint', 'unknown'),
                    'loopback_endpoint': loopback(endpoint.get('endpoint', '')),
                    'http_status': exchange.get('http_status'), 'upstream_http_wall_ms': elapsed,
                    **tokens, 'cached_prompt_tokens': cached, 'usage_issues': issues,
                    'usage_source': 'raw upstream_exchanges.response.usage; server reported, not billing invoice',
                    'task_completed': bool(row.get('task_completed')),
                    'source': {'path': source_path, 'sha256': source_sha256, 'row_index': row_index,
                               'attempt_index': attempt_index, 'exchange_index': exchange_index}})
        if 'model_calls' in row and count != row['model_calls']:
            raise ValueError('Recorded POST count disagrees with model_calls: ' + str(row.get('pair_id')))
    return records


def summarize(records):
    groups = defaultdict(list)
    for row in records:
        groups[row['matrix'], row['model']].append(row)
    result = []
    for (matrix, model), rows in sorted(groups.items()):
        times = sorted(row['upstream_http_wall_ms'] for row in rows if row['upstream_http_wall_ms'] is not None)
        entry = {'matrix': matrix, 'model': model, 'actual_model_post_calls': len(rows),
                 'http_200': sum(row['http_status'] == 200 for row in rows),
                 'calls_missing_or_invalid_usage': sum(bool(row['usage_issues']) for row in rows),
                 'observed_nonloopback_posts': sum(not row['loopback_endpoint'] for row in rows),
                 'upstream_http_wall_median_ms': statistics.median(times) if times else None,
                 'upstream_http_wall_p95_ms': times[math.ceil(len(times) * .95) - 1] if times else None,
                 'latency_scope': 'upstream HTTP wall clock; no token-generation timing or independent user latency inference'}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_prompt_tokens'):
            known = [row[key] for row in rows if row[key] is not None]
            entry[key + '_known_subtotal'] = sum(known)
            entry[key + '_total'] = sum(known) if len(known) == len(rows) else None
        result.append(entry)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path(__file__).with_name('results'))
    args = parser.parse_args()
    index = json.loads((BASE / 'sealed_evidence_index.json').read_text(encoding='utf-8'))
    records, sources = [], []
    for matrix in RUNS:
        path = BASE / matrix / 'report.json'
        raw = path.read_bytes();digest = hashlib.sha256(raw).hexdigest()
        if digest != index['runs'][matrix]['sha256']['report.json']:
            raise ValueError('Sealed report hash mismatch: ' + matrix)
        relative = path.relative_to(ROOT).as_posix()
        records.extend(extract_calls(json.loads(raw), relative, digest, matrix))
        sources.append({'matrix':matrix,'path':relative,'sha256':digest})
    groups = summarize(records)
    result = {'schema':'sensehome.actual-model-usage.v2', 'sources':sources, 'groups':groups,
              'n_actual_post_calls':len(records), 'n_observed_nonloopback_posts':sum(not row['loopback_endpoint'] for row in records),
              'scope':'Execution volume only; do not pool outcome quality or latency across independent matrices.',
              'billing_scope':'All recorded POST destinations are local loopback model services. No direct paid provider inference API calls occur in this bounded record. This is not a full computer cost or account billing audit.',
              'actual_total_cost':None, 'unknown_actual_costs':['hardware allocation','whole-computer energy and tariff','maintenance','incremental network cost'],
              'all_tokens_are_server_reported':True}
    args.out.mkdir(parents=True,exist_ok=True)
    (args.out/'usage_calls.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in records),encoding='utf-8')
    (args.out/'usage_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['# 原始模型用量核验','', '仅从封存报告中的原始 POST 响应读取服务端 usage；不重复计算 adapter 副本、GET 或规则行。没有进行新推理。', '',
           '| 独立矩阵 | 模型 | POST | 输入 token | 输出 token | HTTP中位/p95 ms |', '|---|---|---:|---:|---:|---:|']
    for group in groups:
        lines.append(f"| {group['matrix']} | {group['model']} | {group['actual_model_post_calls']} | {group['prompt_tokens_total']} | {group['completion_tokens_total']} | {group['upstream_http_wall_median_ms']} / {group['upstream_http_wall_p95_ms']} |")
    lines += ['',f'四矩阵记录共{len(records)}次实际POST；非loopback目标{result["n_observed_nonloopback_posts"]}次。校准、历史AutoDL和其他会话不在该口径。',
              '每次调用的文件SHA256、行索引、attempt/exchange索引、case和pair ID见usage_calls.jsonl；token是服务端报告，不是收费提供商账单。',
              '时延为上游HTTP墙钟，不是整条网关时延；不同矩阵和模型分开列出，不用这些计数宣布质量、节能或节费优势。',
              '采购分摊、整机电量、电价、维护及网络费用未知，实际总成本仍为null。GPU板卡功率未代入整机电费。','']
    (args.out/'usage_report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'n_actual_post_calls':len(records),'groups':len(groups),'n_observed_nonloopback_posts':result['n_observed_nonloopback_posts']}))


if __name__ == '__main__':
    main()
