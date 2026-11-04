"""Background HTTP endpoint monitoring for the PC device simulator.

The device bridge is in this process, so LAN reachability is explicit rather
than inferred from the availability of a separate optional local model.
"""
import copy
import threading
import time


class BackendMonitor:
    def __init__(self, backends, interval_s=2.0):
        self.backends = dict(backends)
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._latest = {'status': 'warming-up', 'backends': {}, 'sampled_at': None,
                        'link': {'wan_ok': False, 'wan_rtt_ms': None, 'lan_ok': True, 'lan_rtt_ms': 0.0},
                        'link_basis': 'HTTP endpoint RTT; devices simulated in gateway process'}

    def refresh_once(self):
        health = {}
        for name, backend in self.backends.items():
            try:
                health[name] = dict(backend.health())
            except Exception as exc:
                health[name] = {'ready': False, 'reachable': False, 'error': type(exc).__name__}
        cloud = health.get('cloud-model', {})
        latest = {'status': 'sampled', 'backends': health, 'sampled_at': time.time(),
                  'link': {'wan_ok': bool(cloud.get('reachable', False)),
                           'wan_rtt_ms': cloud.get('latency_ms'),
                           'lan_ok': True, 'lan_rtt_ms': 0.0},
                  'link_basis': self._latest['link_basis']}
        with self._lock:
            self._latest = latest

    def snapshot(self):
        with self._lock:
            snapshot = copy.deepcopy(self._latest)
        snapshot['age_ms'] = None if snapshot['sampled_at'] is None else round((time.time() - snapshot['sampled_at']) * 1000, 1)
        return snapshot

    def start(self):
        def run():
            while not self._stop.is_set():
                self.refresh_once()
                self._stop.wait(self.interval_s)
        self._thread = threading.Thread(target=run, daemon=True, name='sensehome-backend-monitor')
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
