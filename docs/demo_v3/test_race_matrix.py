from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))


def test_registered_schedule_is_balanced_and_rotates():
    import race_matrix
    rows = race_matrix.schedule()
    assert len(rows) == 18
    assert {r['case_id'] for r in rows} == {'C01', 'C04'}
    assert {r['condition'] for r in rows} == {'healthy', 'local_post503', 'both_post503'}
    assert all(sorted(r['arms']) == ['baseline', 'recovery'] for r in rows)
    assert sum(r['arms'][0] == 'baseline' for r in rows) == 9


def test_attempt_count_does_not_count_get_as_inference():
    import race_matrix
    values = [
        {'method': 'GET', 'upstream_forwarded': True, 'upstream_status': 200},
        {'method': 'POST', 'upstream_forwarded': False, 'http_status': 503},
        {'method': 'POST', 'upstream_forwarded': True, 'upstream_status': 200},
    ]
    assert race_matrix.count_calls(values) == {'proxy_post': 2, 'model_post': 1, 'proxy_503': 1}
