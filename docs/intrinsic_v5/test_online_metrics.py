from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'SenseHome-6G/net_offload'))


def test_online_task_estimates_change_and_expire_without_inventing_gpu_queue():
    from online_metrics import OnlineMetrics, task_key
    now = [0.]
    metrics = OnlineMetrics(clock=lambda: now[0], prior_ms=100, max_age_s=30)
    assert metrics.snapshot()['upstream_queue_ms'] is None
    command = '关闭书房灯并打开书房窗帘'
    for duration in (.2, .4):
        token = metrics.waiting(command); metrics.admitted(token)
        now[0] += duration; metrics.finished(token, success=True)
    sample = metrics.snapshot()['cost_by_task'][task_key(command)]
    assert sample['samples'] == 2 and 200 < sample['wall_ms'] < 400
    token = metrics.waiting(command); metrics.admitted(token)
    live = metrics.snapshot()
    assert live['inflight'] == 1 and live['queue_ms'] > 0
    assert live['queue_scope'] == 'adapter_serial_admission'
    metrics.finished(token, success=False)
    now[0] += 31
    assert metrics.snapshot()['cost_by_task'][task_key(command)]['usable'] is False


def test_resource_policy_uses_fresh_task_costs_instead_of_static_constants():
    from resource_policy import ResourcePolicyClient
    from experiment_inputs import load_json, load_profiles
    from copy import deepcopy
    cfg = load_json(ROOT / 'SenseHome-6G/net_offload/experiments/policy_config.json')
    cfg['switch_margin_ms'] = 0
    cfg['recovery_samples'] = 0
    profile = load_profiles(ROOT / 'SenseHome-6G/net_offload/experiments/service_profiles.json')['profiles']['interactive_inference']
    sample = {'observation_id': 1, 'observed_age_ms': 0, 'endpoint_age_ms': {'local': 0, 'cloud': 0},
        'compute': {key: {'model_ready': True, 'queue_ms': 0, 'inflight': 0, 'free_mem_mb': None} for key in ('local', 'cloud')},
        'network': {key: {'reachable': True, 'rtt_ms': 1, 'jitter_ms': None, 'loss_pct': None, 'bandwidth_kbps': None} for key in ('lan', 'wan')},
        'profile': profile, 'cost_estimates': {
            'local': {'wall_ms': 900, 'samples': 3, 'age_ms': 0, 'usable': True},
            'cloud': {'wall_ms': 50, 'samples': 3, 'age_ms': 0, 'usable': True}}}
    policy = ResourcePolicyClient(str(ROOT / 'SenseHome-6G/net_offload/policy_cli.exe'), cfg)
    try:
        first = policy.decide_observation('complex', sample)
        assert first[0] == 'cloud-model'
        second = deepcopy(sample); second['observation_id'] = 2
        second['cost_estimates']['local']['wall_ms'] = 20
        second['cost_estimates']['cloud']['wall_ms'] = 1500
        assert policy.decide_observation('complex', second)[0] == 'local-model'
    finally:
        policy.close()
