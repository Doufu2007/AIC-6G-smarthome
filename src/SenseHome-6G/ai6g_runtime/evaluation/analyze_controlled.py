"""Analyze COMPLETE controlled experiments without rerunning or changing evidence.

Example (the contract report remains explicitly non-performance evidence)::

  python analyze_controlled.py --report report.json --calibration-rows calibration.rows.jsonl \
      --tasks tasksets_v4/test.jsonl --calibration-tasks tasksets_v4/calibration.jsonl \
      --trace tasksets_v4/trace.jsonl --out analysis/

Gold/trace are used only for offline slices. No model endpoint is contacted.
The two predeclared primary contrasts are adaptive-legal_cloud and adaptive-fixed_ttl.
Their cluster-bootstrap intervals are descriptive: temporal policy-state dependence
and shared synthetic templates are not modeled by this bootstrap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai6g_runtime.evaluation.controlled import paired_bootstrap
    from ai6g_runtime.protocol import classify_task
else:
    from .controlled import paired_bootstrap
    from ..protocol import classify_task

PRIMARY_PAIRS = (('adaptive', 'legal_cloud'), ('adaptive', 'fixed_ttl'))
COST_FIELDS = ('model_requests', 'model_attempts', 'probe_requests', 'probe_attempts',
               'total_tokens', 'provider_tokens', 'probe_tokens', 'unknown_usage_requests',
               'probe_elapsed_ms', 'model_elapsed_ms', 'network_bytes')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jsonl(path):
    if not path:
        return []
    return [json.loads(s) for s in Path(path).read_text(encoding='utf-8-sig').splitlines() if s.strip()]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    position = (len(values)-1)*q
    low = int(position)
    return values[low]+(values[min(low+1, len(values)-1)]-values[low])*(position-low)


def _validate_report(report, tasks):
    if report.get('schema') != 'ai6g.controlled.v1' or report.get('complete') is not True:
        raise ValueError('仅接受 complete=true 的完整 controlled.v1 结果')
    if report.get('source_kind') not in ('real_endpoints', 'contract_simulator'):
        raise ValueError('unknown source_kind')
    config = report.get('manifest', {}).get('config', {})
    classifier_sha = report.get('manifest', {}).get('sources', {}).get('protocol.py')
    if classifier_sha and classifier_sha != _sha(Path(__file__).resolve().parents[1]/'protocol.py'):
        raise ValueError('classifier source mismatch; use the experiment-matched protocol.py')
    arms = list(config.get('strategies', report.get('strategies', {})))
    repeats, task_count = report.get('repeats'), report.get('task_count')
    rows = report.get('rows', [])
    if not arms or not isinstance(repeats, int) or repeats < 1 or not isinstance(task_count, int) or task_count < 1:
        raise ValueError('invalid experiment coverage declaration')
    if len(set(arms)) != len(arms) or len(rows) != task_count*repeats*len(arms):
        raise ValueError('experiment row coverage mismatch')
    keys = [(r['strategy'], str(r['task_id']), r['repeat']) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate strategy/task/repeat row')
    identities = {key[1] for key in keys}
    expected = {(arm, identity, repeat) for arm in arms for identity in identities for repeat in range(repeats)}
    if len(identities) != task_count or set(keys) != expected:
        raise ValueError('incomplete paired task/repeat coverage')
    if tasks and ({str(t['id']) for t in tasks} != identities or len(tasks) != task_count):
        raise ValueError('task metadata coverage mismatch')
    for row in rows:
        for key in ('engineering_ok', 'semantic_success', 'deadline_correct', 'wrong_side_effect'):
            if not isinstance(row.get(key), bool):
                raise ValueError(f'missing/invalid metric: {key}')
        if not _number(row.get('total_elapsed_ms')) or row['total_elapsed_ms'] < 0:
            raise ValueError('invalid task latency')
    return arms, config


def _expected_kind(task):
    gold = task.get('gold', {})
    if gold.get('expected_result_kind'):
        return gold['expected_result_kind']
    if gold.get('must_reject'):
        return 'refusal'
    if not gold.get('expected_action') and gold.get('allow_clarification'):
        return 'clarification'
    return 'action' if gold.get('expected_action') else 'unknown'


def _task_family(task, row):
    categories = task.get('gold', {}).get('categories', [])
    if categories:
        return str(categories[0])
    template = row.get('template_group')
    return str(template).split('-utterance-')[0] if template else 'unknown'


def _phase_intervals(events):
    """Name offline trace segments; unknown traces are never silently invented."""
    if not events:
        return []
    by_step = defaultdict(list)
    for event in events:
        if not isinstance(event.get('step'), int) or event['step'] < 0:
            raise ValueError('invalid trace event step')
        by_step[event['step']].append(event)
    states, phases, outage_seen, initial_cloud_delay = {}, [], False, None
    for index, (step, changes) in enumerate(sorted(by_step.items())):
        explicit = None
        for event in changes:
            explicit = event.get('phase', explicit)
            for name in ('local-model', 'cloud-model') if event.get('backend') == '*' else (event.get('backend'),):
                if name not in ('local-model', 'cloud-model'):
                    raise ValueError('trace requires flat backend events')
                state = states.setdefault(name, {'reachable': True, 'rtt_ms': 0, 'queue_ms': 0})
                state.update({k: v for k, v in event.items() if k not in ('step', 'backend', 'phase')})
        cloud = states.get('cloud-model', {})
        delay = cloud.get('rtt_ms', 0)+cloud.get('queue_ms', 0)
        if initial_cloud_delay is None:
            initial_cloud_delay = delay
        if not cloud.get('reachable', True):
            label, outage_seen = '云断连', True
        elif outage_seen:
            label = '恢复'
        elif delay > initial_cloud_delay:
            label = '云延迟/排队'
        else:
            label = '平稳'
        phases.append({'id': f'phase_{index}', 'label': str(explicit or label), 'start_step': step,
                       'states': json.loads(json.dumps(states))})
    for index, phase in enumerate(phases):
        phase['end_step_exclusive'] = phases[index+1]['start_step'] if index+1 < len(phases) else None
    return phases


def _derive(row, task, trusted):
    calls = row.get('calls', [])
    task_calls = [c for c in calls if c.get('phase') != 'probe' and c.get('backend') != 'local-rules']
    probes = [c for c in calls if c.get('phase') == 'probe']
    text = row.get('task_text', row.get('task', task.get('text', '')))
    privacy = classify_task(text).privacy if text else 'unknown'
    strict = privacy in ('strict', 'local', 'local_only')
    actions = (row.get('plan') or {}).get('actions', [])
    no_actions = not actions and not row.get('execution', {}).get('applied_actions', 0)
    no_change = row.get('initial_state') == row.get('state')
    kind, good = row.get('result_kind', 'failure'), row['engineering_ok']
    return {**row, 'expected_kind': _expected_kind(task), 'task_family': _task_family(task, row),
        'online_family': str(row.get('family', 'unknown')), 'privacy': privacy, 'strict': strict,
        'refusal_protocol_ok': good and kind == 'refusal',
        'clarification_protocol_ok': good and kind == 'clarification',
        'action_protocol_ok': good and kind == 'action',
        'no_action_fault': not good and no_actions and no_change,
        'privacy_blocked': row.get('error_code') == 'privacy_violation',
        'strict_safe_failure': strict and not good and no_actions and no_change,
        'strict_untrusted_task_dispatches': sum(strict and c.get('dispatched', False) and c.get('backend') not in trusted for c in task_calls),
        'strict_untrusted_probe_dispatches': sum(strict and c.get('dispatched', False) and c.get('backend') not in trusted for c in probes),
        'task_real_rows': any(c.get('model_real', False) for c in task_calls),
        'probe_real_rows': any(c.get('model_real', False) for c in probes),
        'task_real_calls': sum(bool(c.get('model_real')) for c in task_calls),
        'probe_real_calls': sum(bool(c.get('model_real')) for c in probes),
        'task_model_requests': sum(bool(c.get('dispatched')) for c in task_calls),
        'task_model_attempts': len(task_calls)}


def _summarize(rows):
    n = len(rows)
    flags = ('engineering_ok', 'semantic_success', 'deadline_correct', 'wrong_side_effect',
             'refusal_protocol_ok', 'clarification_protocol_ok', 'action_protocol_ok',
             'no_action_fault', 'privacy_blocked', 'strict_safe_failure', 'task_real_rows', 'probe_real_rows')
    result = {'requests': n, 'tasks': len({r['task_id'] for r in rows}),
        'clusters': len({r.get('cluster_id', r['task_id']) for r in rows}),
        **{key: sum(bool(r[key]) for r in rows) for key in flags},
        **{key: sum(r[key] for r in rows) for key in ('task_real_calls', 'probe_real_calls',
            'task_model_requests', 'task_model_attempts', 'strict_untrusted_task_dispatches', 'strict_untrusted_probe_dispatches')},
        'semantic_rate': sum(r['semantic_success'] for r in rows)/n if n else None,
        'deadline_correct_rate': sum(r['deadline_correct'] for r in rows)/n if n else None,
        'strict_rows': sum(r['strict'] for r in rows),
        'expected_kind_counts': dict(Counter(r['expected_kind'] for r in rows)),
        'returned_kind_counts': dict(Counter(r.get('result_kind', 'failure') for r in rows)),
        'expected_refusal_correct': sum(r['expected_kind'] == 'refusal' and r['semantic_success'] for r in rows),
        'expected_clarification_correct': sum(r['expected_kind'] == 'clarification' and r['semantic_success'] for r in rows),
        'p50_total_ms': _percentile([r['total_elapsed_ms'] for r in rows], .5),
        'p95_total_ms': _percentile([r['total_elapsed_ms'] for r in rows], .95),
        'total_elapsed_ms': sum(r['total_elapsed_ms'] for r in rows),
        'errors': dict(Counter(r.get('error_code') for r in rows if r.get('error_code'))),
        'routing_reasons': dict(Counter(r.get('route', {}).get('reason', 'unknown') for r in rows))}
    costs = [r.get('cost', {}) for r in rows]
    money = [c.get('usd') for c in costs]
    result['cost'] = {key: sum(c.get(key, 0) or 0 for c in costs) for key in COST_FIELDS}
    result['cost'].update(usd=sum(money) if all(_number(v) for v in money) else None,
        known_usd_subtotal=sum(v for v in money if _number(v)), unknown_money_rows=sum(not _number(v) for v in money),
        missing_cost_fields=sorted({key for c in costs for key in COST_FIELDS if key not in c}))
    return result


def _rate(rows):
    n = len(rows)
    return {'n': n, 'successes': sum(bool(r.get('semantic_success')) for r in rows),
            'rate': sum(bool(r.get('semantic_success')) for r in rows)/n if n else None,
            'real_rows': sum(bool(r.get('model_real')) for r in rows)}


def _calibration_summary(rows, tasks, config):
    if not rows:
        return {}
    keys = [(r['backend'], r['task_id']) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate calibration backend/task rows')
    task_map = {t['id']: t for t in tasks}
    if tasks:
        expected = {(b, identity) for b in {r['backend'] for r in rows} for identity in task_map}
        if set(keys) != expected:
            raise ValueError('incomplete calibration task coverage')
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['backend'], row.get('family', 'unknown'))].append(row)
    result = defaultdict(dict)
    for (backend, family), group in sorted(grouped.items()):
        eligible = [r for r in group if 'skipped' not in r]
        buckets = {'shared_fastpath': [], 'delegated': [], 'policy_unknown': []}
        for row in eligible:
            meta = task_map.get(row['task_id'])
            if meta is None:
                buckets['policy_unknown'].append(row)
                continue
            task = classify_task(meta['text'])
            fast = (config.get('shared_safety_fastpath', True) and (task.policy_result or task.task_class == 'safety')) or (
                    config.get('shared_rule_fastpath', True) and task.rule_eligible)
            buckets['shared_fastpath' if fast else 'delegated'].append(row)
        result[backend][family] = {'all': _rate(eligible), 'skipped': len(group)-len(eligible),
                                   **{name: _rate(items) for name, items in buckets.items()}}
    return dict(result)


def analyze_report(report: Mapping[str, Any], calibration_rows: Sequence[Mapping[str, Any]] = (), *,
                   tasks=(), calibration_tasks=(), trace_events=(), provenance=None):
    """Return derived summaries. Incomplete source reports are rejected, not scored."""
    arms, config = _validate_report(report, tasks)
    task_map = {t['id']: t for t in tasks}
    trusted = set(config.get('trusted_backends', [])) | {'local-rules'}
    rows = [_derive(row, task_map.get(row['task_id'], {}), trusted) for row in report['rows']]
    phases = _phase_intervals(trace_events)
    phase_groups = defaultdict(list)
    for row in rows:
        matching = [p for p in phases if p['start_step'] <= row['trace_step']]
        phase_groups[matching[-1]['id'] if matching else 'unknown'].append(row)
    def by_arm(group):
        return {arm: _summarize([r for r in group if r['strategy'] == arm]) for arm in arms}
    def slices(field):
        return {value: by_arm([r for r in rows if r[field] == value]) for value in sorted({r[field] for r in rows})}
    primary, missing = [], []
    for arm, reference in PRIMARY_PAIRS:
        if arm not in arms or reference not in arms:
            missing.append({'arm': arm, 'reference': reference})
            continue
        item = paired_bootstrap(rows, arm, reference, metric='deadline_correct',
            samples=config.get('bootstrap_samples', 2000), seed=config.get('seed', 20261104))
        item.update(comparison_role='primary', inference='descriptive_state_dependence_not_modeled')
        primary.append(item)
    return {'schema': 'ai6g.controlled.analysis.v1', 'source_kind': report['source_kind'],
        'simulator_only': report['source_kind'] == 'contract_simulator', 'source_complete': True,
        'source_fingerprint': report.get('fingerprint'), 'provenance': provenance or {},
        'task_count': report['task_count'], 'repeats': report['repeats'], 'strategies': by_arm(rows),
        'phases': phases, 'by_phase': {key: by_arm(group) for key, group in phase_groups.items()},
        'by_task_family': slices('task_family'), 'by_online_family': slices('online_family'),
        'calibration': _calibration_summary(calibration_rows, calibration_tasks, config),
        'primary_comparisons': primary, 'primary_missing': missing,
        'boundaries': {'gold_used_offline_only': True, 'latency_includes_probes_and_cascade': True,
            'no_action_fault_is_not_refusal_or_clarification_success': True,
            'task_real_excludes_probe': True, 'calibration_scores_not_replaced': True,
            'bootstrap_descriptive_only': True, 'temporal_policy_state_dependence_modeled': False,
            'money_scope': 'provider tokens × explicit rates; GPU rental/taxes excluded',
            'network_bytes_scope': 'UTF-8 task text + serialized response JSON; HTTP headers/system prompt excluded',
            'phase_scope': 'global trace_step; no automatic replay at each repeat',
            'calibration_policy_scope': 'current classify_task and saved fastpath config; diagnostic only'},
        'metadata_coverage': {'tasks': len(tasks), 'calibration_tasks': len(calibration_tasks),
                              'trace_events': len(trace_events)}}


def _fmt(value, percent=False):
    if value is None:
        return '未知'
    return f'{value*100:.1f}%' if percent else f'{value:,.3f}'


def _escape(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def _metric_table(groups):
    lines = ['| 策略 | 请求 | 语义正确 | 按期正确 | 错误副作用 | P50 ms | P95 ms |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for arm, s in groups.items():
        lines.append(f'| {_escape(arm)} | {s["requests"]} | {s["semantic_success"]} ({_fmt(s["semantic_rate"], True)}) | '
                     f'{s["deadline_correct"]} ({_fmt(s["deadline_correct_rate"], True)}) | {s["wrong_side_effect"]} | '
                     f'{_fmt(s["p50_total_ms"])} | {_fmt(s["p95_total_ms"])} |')
    return lines


def render_markdown(a):
    lines = ['# 受控实验离线分析', '']
    if a['simulator_only']:
        lines += ['> **仅为合约模拟器验证：本报告所有任务指标、时延、token及比较区间均不可当作真实模型或真实网络性能。**', '']
    else:
        lines += ['本报告来自完整的真实端点实验；应用层故障注入不代表真实6G无线链路。', '']
    lines += [f'来源指纹：`{a["source_fingerprint"]}`。任务 {a["task_count"]} 条，重复 {a["repeats"]} 次。原始结果和校准分数均未改写。', '',
        '## 总体结果', '', *_metric_table(a['strategies']), '',
        '所有成功指标均保留源报告语义判定；安全地无动作失败仍计失败。时延包含同步探针、cascade及注入延迟；模拟注入采用虚拟时间，真实端点实验采用实际等待。', '',
        '## 两组预设主比较', '',
        '| 比较（adaptive − 对照） | 按期正确率差/百分点 | 描述性区间/百分点 | 配对簇 | 配对行 |',
        '|---|---:|---:|---:|---:|']
    for item in a['primary_comparisons']:
        lines.append(f'| {_escape(item["reference"])} | {item["estimate"]*100:.2f} | '
                     f'[{item["ci95"][0]*100:.2f}, {item["ci95"][1]*100:.2f}] | {item["clusters"]} | {item["paired_rows"]} |')
    if a['primary_missing']:
        lines.append('缺失的预设比较：'+', '.join(f'{x["arm"]} vs {x["reference"]}' for x in a['primary_missing'])+'；未补造比较结果。')
    lines += ['', '按task_id平均重复，再按cluster_id平均表述并成对bootstrap。策略状态跨任务持续，共享模板与轨迹也造成依赖；该bootstrap未建模这些时序依赖。区间仅为固定合成序列的描述，不作显著性、因果或外部泛化结论。', '',
        '## 结果类型、故障与隐私', '',
        '| 策略 | 正常动作协议成功 | 拒绝协议成功 | 澄清协议成功 | 无动作故障 | 隐私拦截 | strict请求 | strict域外任务/探针调用 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm, s in a['strategies'].items():
        lines.append(f'| {arm} | {s["action_protocol_ok"]} | {s["refusal_protocol_ok"]} | {s["clarification_protocol_ok"]} | '
                     f'{s["no_action_fault"]} | {s["privacy_blocked"]} | {s["strict_rows"]} | '
                     f'{s["strict_untrusted_task_dispatches"]}/{s["strict_untrusted_probe_dispatches"]} |')
    lines += ['', '协议成功表示运行时完成合法结果；其语义正确与否仍看gold。拒绝、澄清要求明确的result_kind，不能用route_unavailable、超时或privacy_violation冒充。无动作故障要求工程失败、无动作且状态未变。各类计数可能交叉；隐私拦截属于故障子集。', '',
        '| 策略 | 应拒绝/正确 | 应澄清/正确 | strict安全失败 | 错误码分布 |', '|---|---:|---:|---:|---|']
    for arm, s in a['strategies'].items():
        lines.append(f'| {arm} | {s["expected_kind_counts"].get("refusal", 0)}/{s["expected_refusal_correct"]} | '
                     f'{s["expected_kind_counts"].get("clarification", 0)}/{s["expected_clarification_correct"]} | '
                     f'{s["strict_safe_failure"]} | {_escape(json.dumps(s["errors"], ensure_ascii=False))} |')
    lines += ['', '## 真实调用与全成本', '',
        '| 策略 | 任务真实行/调用 | 探针真实行/调用 | 模型请求/尝试 | 探针请求/尝试 | 全token（探针） | provider token | 探针ms | 模型尝试总ms | 记录字节 | 美元 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for arm, s in a['strategies'].items():
        c = s['cost']
        lines.append(f'| {arm} | {s["task_real_rows"]}/{s["task_real_calls"]} | {s["probe_real_rows"]}/{s["probe_real_calls"]} | '
            f'{c["model_requests"]}/{c["model_attempts"]} | {c["probe_requests"]}/{c["probe_attempts"]} | '
            f'{c["total_tokens"]} ({c["probe_tokens"]}) | {c["provider_tokens"]} | {_fmt(c["probe_elapsed_ms"])} | '
            f'{_fmt(c["model_elapsed_ms"])} | {c["network_bytes"]} | {_fmt(c["usd"])} |')
    lines += ['', '真实任务行只统计phase≠probe的模型响应；探针真实行单列。两种行可能重叠。全token已经包含探针，不能再相加。网络字节仅任务UTF-8文本与响应JSON，不含系统提示词/HTTP头。美元仅显式费率与provider用量；未知不能当作零，且不含GPU租用和税费。', '',
        '| 策略 | 任务模型请求/尝试 | 未知usage请求 | 未知金额行 | 已知金额小计（美元） | 总任务ms | 缺失成本字段 |', '|---|---:|---:|---:|---:|---:|---|']
    for arm, s in a['strategies'].items():
        c = s['cost']
        lines.append(f'| {arm} | {s["task_model_requests"]}/{s["task_model_attempts"]} | {c["unknown_usage_requests"]} | '
                     f'{c["unknown_money_rows"]} | {_fmt(c["known_usd_subtotal"])} | {_fmt(s["total_elapsed_ms"])} | {_escape(c["missing_cost_fields"])} |')
    lines += ['', '## 按服务阶段', '']
    phase_meta = {p['id']: p for p in a['phases']}
    for key, groups in a['by_phase'].items():
        phase = phase_meta.get(key)
        title = f'{phase["label"]}：step≥{phase["start_step"]}' if phase else '未提供可匹配轨迹的阶段'
        if phase and phase['end_step_exclusive'] is not None:
            title += f'，step<{phase["end_step_exclusive"]}'
        lines += [f'### {title}', '', *_metric_table(groups), '']
    lines += ['阶段按全局trace_step划分，重复不会自动重放四阶段；阶段子集仅描述实际出现的任务，不虚增独立簇。', '', '## 按任务族与在线路由族', '']
    for name, slices in (('离线任务族', a['by_task_family']), ('在线分类族', a['by_online_family'])):
        for family, groups in slices.items():
            lines += [f'### {name}：{_escape(family)}', '', *_metric_table(groups), '']
    lines += ['## 校准分母与生产策略条件', '',
        '校准直接调用模型并执行同一动作协议校验；生产评测可先走共享规则/安全快路。因此全族校准率不等于生产实际委托任务的条件准确率。下表只诊断这一分母差异，既不替换原校准分数，也不重新选择阈值。快路切片按保存的策略配置与当前classify_task重建；源码哈希见来源记录。', '',
        '| 后端/在线族 | 全族正确/n | 共享快路正确/n | 委托模型正确/n | 条件未知n | 隐私等跳过n |', '|---|---:|---:|---:|---:|---:|']
    for backend, families in a['calibration'].items():
        for family, g in families.items():
            def count(key):
                return f'{g[key]["successes"]}/{g[key]["n"]} ({_fmt(g[key]["rate"], True)})'
            lines.append(f'| {backend}/{family} | {count("all")} | {count("shared_fastpath")} | {count("delegated")} | '
                         f'{g["policy_unknown"]["n"]} | {g["skipped"]} |')
    lines += ['', '## 来源与证据边界', '',
        '任务为AI辅助构造的合成协议验证；同卡双模型服务和应用层注入不能外推到独立家庭硬件、真实6G空口或实际用户收益。完整源报告才可进入分析，本脚本不会从半成品估计最终结果。', '',
        '```json', json.dumps(a['provenance'], ensure_ascii=False, indent=2), '```', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--report', required=True)
    parser.add_argument('--calibration-rows', required=True)
    parser.add_argument('--calibration', help='calibration JSON; binds its raw source hash and experiment manifest')
    parser.add_argument('--tasks', help='frozen test JSONL; needed for expected-result/12-family denominators')
    parser.add_argument('--calibration-tasks', help='frozen calibration JSONL; needed for fastpath condition slices')
    parser.add_argument('--trace', help='frozen flat trace JSONL; needed for phase slices')
    parser.add_argument('--out', required=True, help='output directory; writes analysis.json and analysis.md')
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text(encoding='utf-8-sig'))
        provenance = {name: {'path': str(Path(path).resolve()), 'sha256': _sha(path)}
                      for name, path in [('report', args.report), ('calibration_rows', args.calibration_rows), ('calibration', args.calibration),
                          ('tasks', args.tasks), ('calibration_tasks', args.calibration_tasks), ('trace', args.trace)] if path}
        provenance['analysis_source_sha256'] = _sha(__file__)
        provenance['classifier_source_sha256'] = _sha(Path(__file__).resolve().parents[1]/'protocol.py')
        for arg, key in ((args.tasks, 'tasks_sha256'), (args.trace, 'trace_sha256')):
            if arg and report.get('manifest', {}).get(key) and _sha(arg) != report['manifest'][key]:
                raise ValueError(f'frozen {key} mismatch')
        calibration_rows = _jsonl(args.calibration_rows)
        if args.calibration:
            if report.get('manifest', {}).get('calibration_sha256') != _sha(args.calibration):
                raise ValueError('frozen calibration file mismatch')
            calibration = json.loads(Path(args.calibration).read_text(encoding='utf-8-sig'))
            raw_sha = _sha(args.calibration_rows)
            if calibration.get('source_sha256') != raw_sha:
                raise ValueError('calibration raw rows source hash mismatch')
            provenance['calibration_binding'] = 'report manifest → calibration JSON → calibration rows SHA-256 verified'
        else:
            provenance['calibration_binding'] = 'not checked: --calibration was not provided'
        if args.calibration_tasks and any(r.get('task_source_sha256') != _sha(args.calibration_tasks) for r in calibration_rows):
            raise ValueError('calibration task source mismatch')
        analysis = analyze_report(report, calibration_rows, tasks=_jsonl(args.tasks),
            calibration_tasks=_jsonl(args.calibration_tasks), trace_events=_jsonl(args.trace), provenance=provenance)
        output = Path(args.out)
        output.mkdir(parents=True, exist_ok=True)
        (output/'analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        (output/'analysis.md').write_text(render_markdown(analysis), encoding='utf-8')
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(f'已生成 {output.resolve() / "analysis.md"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
