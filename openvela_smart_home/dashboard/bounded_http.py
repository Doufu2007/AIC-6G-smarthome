"""Bounded HTTP workers and absolute request input read budgets.

Handlers must inherit ReadDeadlineHandlerMixin before BaseHTTPRequestHandler to
apply the separate request-line/header and Content-Length body budgets.
"""
from http.server import ThreadingHTTPServer
import io
import math
import threading
import time


class _DeadlineSocketReader(io.RawIOBase):
    """Recompute remaining time before every recv, even inside BufferedReader."""

    def __init__(self, connection):
        self.connection = connection
        self.deadline = None
        self.idle_timeout = connection.gettimeout()
        super().__init__()

    def readable(self):
        return True

    def begin(self, seconds):
        self.deadline = time.monotonic() + seconds

    def end(self):
        self.deadline = None
        self.connection.settimeout(self.idle_timeout)

    def readinto(self, buffer):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTP input read deadline exceeded')
            timeout = remaining
            if self.idle_timeout is not None:
                timeout = min(timeout, self.idle_timeout)
            self.connection.settimeout(timeout)
        return self.connection.recv_into(buffer)

    def fileno(self):
        return self.connection.fileno()


class _DeadlineInput:
    """Stop the body budget as soon as all declared bytes have been read."""

    def __init__(self, raw):
        self.raw = raw
        self.buffered = io.BufferedReader(raw)
        self.body_remaining = None

    def begin_body(self, length, seconds):
        self.body_remaining = length
        self.raw.begin(seconds)

    def _consume(self, data):
        if self.body_remaining is not None:
            self.body_remaining -= len(data)
            if self.body_remaining <= 0:
                self.body_remaining = None
                self.raw.end()
        return data

    def read(self, size=-1):
        return self._consume(self.buffered.read(size))

    def readline(self, size=-1):
        return self._consume(self.buffered.readline(size))

    def read1(self, size=-1):
        return self._consume(self.buffered.read1(size))

    def readinto(self, buffer):
        count = self.buffered.readinto(buffer)
        if count:
            self._consume(memoryview(buffer)[:count])
        return count

    def close(self):
        self.buffered.close()

    @property
    def closed(self):
        return self.buffered.closed

    def fileno(self):
        return self.raw.fileno()


class ReadDeadlineHandlerMixin:
    """Apply input deadlines without timing out later handler computation."""

    def setup(self):
        super().setup()
        # Replace the default reader, whose socket timeout renews on each recv.
        self.rfile.close()
        self._deadline_raw = _DeadlineSocketReader(self.connection)
        self.rfile = _DeadlineInput(self._deadline_raw)

    def handle_one_request(self):
        self.rfile.body_remaining = None
        self._deadline_raw.begin(getattr(self.server, 'header_read_timeout', 5.0))
        try:
            return super().handle_one_request()
        finally:
            self._deadline_raw.end()

    def parse_request(self):
        parsed = super().parse_request()
        self._deadline_raw.end()
        if parsed:
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except (ValueError, TypeError):
                # The application retains responsibility for rejecting framing.
                length = 0
            if length > 0:
                self.rfile.begin_body(length, getattr(self.server, 'body_read_timeout', 5.0))
        return parsed


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32

    def __init__(self, address, handler, *, max_workers=16,
                 header_read_timeout=5, body_read_timeout=5):
        for value in (header_read_timeout, body_read_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('HTTP read budgets must be finite and positive')
        self.header_read_timeout = float(header_read_timeout)
        self.body_read_timeout = float(body_read_timeout)
        self._workers = threading.BoundedSemaphore(max_workers)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request, address):
        if not self._workers.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._workers.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._workers.release()
