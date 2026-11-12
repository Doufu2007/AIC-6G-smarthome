"""Monotonic observation age and one owned, cancellable cache refresh."""
from copy import deepcopy
import math
import threading
import time

_monotonic = time.monotonic
_wall_time = time.time


def valid_sample_time(value):
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 < value <= _wall_time() + 1.0)


class StateFreshness:
    def __init__(self, stale_after_ms=5000.0):
        self.stale_after_ms = stale_after_ms
        self._lock = threading.Lock()
        self._key = None
        self._metadata = {}
        self._sample_monotonic = None
        self._initial_age_ms = None
        self._confirmed = False
        self._error = None

    def _select(self, key, persisted=None):
        if key == self._key:
            return
        self._key = key
        self._metadata = deepcopy(persisted or {})
        sampled_at = self._metadata.get('sampled_at')
        self._sample_monotonic = _monotonic()
        self._initial_age_ms = (max(0.0, (_wall_time() - sampled_at) * 1000.0)
                               if type(sampled_at) in (int, float) and math.isfinite(sampled_at) else None)
        # A persisted cache is evidence of a previous observation, not startup
        # reconciliation. Only a validated live query can confirm it again.
        self._confirmed = False
        self._error = 'MQTT_SNAPSHOT_NOT_CONFIRMED'

    def accepted(self, key, metadata):
        with self._lock:
            self._select(key)
            self._metadata = deepcopy(metadata)
            self._sample_monotonic = _monotonic()
            self._initial_age_ms = max(0.0, (_wall_time() - metadata['sampled_at']) * 1000.0)
            self._confirmed = True
            self._error = None

    def failed(self, key, reason, persisted=None):
        with self._lock:
            self._select(key, persisted)
            self._error = reason

    def view(self, key, persisted, connection, *, refreshing=False, closed=False):
        with self._lock:
            self._select(key, persisted)
            # GET may have read the old atomic file just before a refresh
            # commits. Attach metadata only to that file's own values.
            metadata = deepcopy(persisted)
            same_sample = all(metadata.get(field) == self._metadata.get(field)
                              for field in ('platform_id', 'state_version', 'sampled_at'))
            if same_sample:
                age = (self._initial_age_ms + max(0.0, _monotonic() - self._sample_monotonic) * 1000.0
                       if self._initial_age_ms is not None else None)
            else:
                sampled_at = metadata.get('sampled_at')
                age = (max(0.0, (_wall_time() - sampled_at) * 1000.0)
                       if type(sampled_at) in (int, float) and math.isfinite(sampled_at) else None)
            generation_changed = (type(metadata.get('connection_generation')) is int
                                  and type(connection.get('generation')) is int
                                  and metadata['connection_generation'] != connection['generation'])
            stale = (not self._confirmed or not same_sample or bool(self._error) or age is None
                     or age > self.stale_after_ms or connection['status'] in ('disconnected', 'connecting')
                     or generation_changed or closed)
            has_sample = age is not None
            metadata.update(sampled_at=metadata.get('sampled_at'), age_ms=age,
                stale=stale, unknown=stale, connection=deepcopy(connection),
                last_refresh_error='STATE_REFRESH_CLOSED' if closed else self._error,
                refresh_in_progress=refreshing,
                scope=('current-software-platform-observation' if not stale else
                       'last-known-software-platform-observation' if has_sample else
                       'unknown-software-platform-observation'),
                physical_execution_confirmed=False)
            return metadata


class SingleFlightRefresh:
    """Own at most one worker; stop prevents admitting work or applying results."""
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None

    def start(self, callback):
        with self._lock:
            if self._cancel.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            self._thread = threading.Thread(target=callback, args=(self._cancel,),
                                            name='sensehome-state-refresh', daemon=True)
            self._thread.start()
            return True

    def status(self):
        with self._lock:
            return bool(self._thread and self._thread.is_alive()), self._cancel.is_set()

    def close(self, timeout_s=3.0):
        with self._lock:
            self._cancel.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout_s))
        return thread is None or not thread.is_alive()

    def reset(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('STATE_REFRESH_STILL_RUNNING')
            self._cancel = threading.Event()
            self._thread = None
