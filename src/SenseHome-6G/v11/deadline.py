"""Absolute HTTP budgets and bounded isolation for planning / read-only work.

Python cannot safely stop an arbitrary synchronous function. Timed-out work
therefore keeps its worker slot until it actually exits; its late value is
discarded. Never use this isolation helper to dispatch device actions.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from io import BytesIO
import http.client
import math
import queue
import socket
import threading
import time
from urllib.error import HTTPError
import urllib.request


class DeadlineExceeded(TimeoutError):
    """The caller's total monotonic time budget expired."""


class CapacityExceeded(TimeoutError):
    """No worker was admitted; no callable was scheduled."""


def _positive_timeout(timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    return float(timeout)


class _BoundedFuture(Future):
    def __init__(self):
        super().__init__()
        self._publish_lock = threading.Lock()
        self._discarded = False

    def discard(self):
        """Discard an outstanding result without releasing its running slot."""
        with self._publish_lock:
            self._discarded = True

    def _publish(self, value, error):
        with self._publish_lock:
            if self.cancelled():
                return
            if self._discarded:
                self.set_result(None)
            elif error is not None:
                self.set_exception(error)
            else:
                self.set_result(value)


class BoundedCallPool:
    """Fixed daemon workers, no backlog beyond admitted worker capacity.

    ``submit`` fails immediately when full. ``call_with_timeout`` includes
    waiting for admission in its total budget. Daemon workers do not impose an
    unbounded interpreter-exit wait for a broken third-party read/planner.
    """
    def __init__(self, max_workers=8, *, name='deadline-call'):
        if type(max_workers) is not int or not 1 <= max_workers <= 64:
            raise ValueError('max_workers must be an integer from 1 to 64')
        self.max_workers = max_workers
        self.name = name
        self._slots = threading.BoundedSemaphore(max_workers)
        self._queue = queue.Queue(maxsize=max_workers)
        self._lock = threading.Lock()
        self._threads = []
        self._active = 0

    @property
    def active_count(self):
        with self._lock:
            return self._active

    def _submit(self, function, args, kwargs, wait=0):
        if not callable(function):
            raise TypeError('function must be callable')
        if not self._slots.acquire(timeout=max(0, wait)):
            raise CapacityExceeded('bounded callable capacity unavailable')
        future = _BoundedFuture()
        with self._lock:
            self._active += 1
            self._queue.put_nowait((future, function, args, kwargs))
            if len(self._threads) < self.max_workers:
                worker = threading.Thread(target=self._worker, daemon=True,
                                          name=self.name + '-' + str(len(self._threads) + 1))
                self._threads.append(worker)
                worker.start()
        return future

    def submit(self, function, /, *args, **kwargs):
        return self._submit(function, args, kwargs)

    def _worker(self):
        while True:
            future, function, args, kwargs = self._queue.get()
            value = error = None
            running = future.set_running_or_notify_cancel()
            try:
                if running:
                    value = function(*args, **kwargs)
            except BaseException as exc:
                error = exc
            finally:
                # Capacity represents actual work, not the caller's patience.
                with self._lock:
                    self._active -= 1
                self._slots.release()
                self._queue.task_done()
            if running:
                future._publish(value, error)
            # Do not retain a completed payload on an idle worker's stack.
            del future, function, args, kwargs, value, error


_CALL_POOL = BoundedCallPool()
_HTTP_POOL = BoundedCallPool(name='deadline-http')


def call_with_timeout(function, timeout, /, *args, pool=None, **kwargs):
    """Run read/planning work with bounded admission and one total deadline."""
    deadline_at = time.monotonic() + _positive_timeout(timeout)
    pool = _CALL_POOL if pool is None else pool
    future = pool._submit(function, args, kwargs, wait=max(0, deadline_at - time.monotonic()))
    try:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded('call deadline elapsed')
        value = future.result(timeout=remaining)
        if time.monotonic() >= deadline_at:
            raise DeadlineExceeded('call deadline elapsed')
        return value
    except FutureTimeout as exc:
        future.discard()
        # A completed callable's own TimeoutError is also a timeout; preserve
        # its type if it reached us within the caller's remaining budget.
        if future.done() and time.monotonic() < deadline_at and not isinstance(exc, DeadlineExceeded):
            raise
        raise DeadlineExceeded('call deadline elapsed') from exc


class _HTTPBudget:
    def __init__(self, deadline_at):
        self.deadline_at = deadline_at
        self._lock = threading.Lock()
        self._sockets = []
        self._cancelled = False

    def remaining(self):
        remaining = self.deadline_at - time.monotonic()
        if self._cancelled or remaining <= 0:
            raise DeadlineExceeded('HTTP deadline elapsed')
        return remaining

    @staticmethod
    def _close_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def register(self, sock):
        with self._lock:
            cancelled = self._cancelled or time.monotonic() >= self.deadline_at
            if not cancelled:
                self._sockets.append(sock)
        if cancelled:
            self._close_socket(sock)
            raise DeadlineExceeded('HTTP connection completed after deadline')
        sock.settimeout(self.remaining())

    def cancel(self):
        with self._lock:
            self._cancelled = True
            sockets = list(self._sockets)
            self._sockets.clear()
        # Response.close() can wait on a BufferedReader lock held by read().
        # Interrupt the raw socket instead, so cancellation itself stays fast.
        for sock in sockets:
            self._close_socket(sock)


def _request_response(request, budget, max_bytes):
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.timeout = budget.remaining()
            super().connect()
            budget.register(self.sock)

    class SecureConnection(http.client.HTTPSConnection):
        def connect(self):
            self.timeout = budget.remaining()
            http.client.HTTPConnection.connect(self)
            budget.register(self.sock)
            server_hostname = self._tunnel_host or self.host
            self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname,
                                                 do_handshake_on_connect=False)
            budget.register(self.sock)
            self.sock.do_handshake()
            budget.remaining()

    class Handler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(Connection, req)

    class SecureHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(SecureConnection, req, context=self._context)

    class StatusHandler(urllib.request.HTTPErrorProcessor):
        def http_response(self, req, response):
            return response

        https_response = http_response

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Handler(),
                                         SecureHandler(), StatusHandler())
    response = opener.open(request, timeout=budget.remaining())
    status, headers, reason = response.status, response.headers, response.reason
    with response:
        data = bytearray()
        while True:
            remaining = budget.remaining()
            # HTTPResponse.read1 avoids an unbounded buffered read that keeps
            # accepting a fresh per-recv timeout on a slow stream of bytes.
            for sock in list(budget._sockets):
                if sock.fileno() >= 0:
                    sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
            budget.remaining()
            if not chunk:
                return status, headers, reason, bytes(data)
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError('HTTP response too large')


def request_response(request, timeout, max_bytes=2 * 1024 * 1024):
    """Read status, headers and body within one connection's total time budget.

    Redirects share the original deadline. A stuck DNS/system connect remains
    in bounded isolation; after cancellation a late connection is closed
    before any HTTP request is sent. HTTP error bodies receive the same budget.
    """
    timeout = _positive_timeout(timeout)
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('max_bytes must be a positive integer')
    budget = _HTTPBudget(time.monotonic() + timeout)
    try:
        return call_with_timeout(_request_response, budget.remaining(), request, budget,
                                 max_bytes, pool=_HTTP_POOL)
    finally:
        budget.cancel()


def request_bytes(request, timeout, max_bytes=2 * 1024 * 1024):
    """Read a successful HTTP body within one total deadline.

    Preserve urllib's HTTPError contract for existing model/resource callers.
    """
    status, headers, reason, data = request_response(request, timeout, max_bytes)
    if not 200 <= status < 300:
        raise HTTPError(request.full_url, status, reason, headers, BytesIO(data))
    return data
