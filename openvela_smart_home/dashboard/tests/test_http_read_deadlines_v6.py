"""Real loopback regressions for absolute HTTP input budgets."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import inspect
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bounded_http


class NoDeadlineMixin:
    pass


ReadDeadlineMixin = getattr(bounded_http, 'ReadDeadlineHandlerMixin', NoDeadlineMixin)


class Handler(ReadDeadlineMixin, BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/compute':
            self.server.started.set()
            self.server.finish_compute.wait(2)
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.body_started.set()
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.path == '/compute':
            self.server.started.set()
            self.server.finish_compute.wait(2)
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ObservedServer(bounded_http.BoundedHTTPServer):
    def process_request_thread(self, request, address):
        try:
            return super().process_request_thread(request, address)
        finally:
            self.worker_finished.set()


@contextmanager
def running_server(*, budget=0.18):
    options = {'max_workers': 1}
    # The fallback lets the RED run exercise the old implementation's behavior.
    if 'header_read_timeout' in inspect.signature(bounded_http.BoundedHTTPServer).parameters:
        options.update(header_read_timeout=budget, body_read_timeout=budget)
    server = ObservedServer(('127.0.0.1', 0), Handler, **options)
    server.started = threading.Event()
    server.body_started = threading.Event()
    server.finish_compute = threading.Event()
    server.worker_finished = threading.Event()
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    clients = []
    try:
        def connect():
            client = socket.create_connection(server.server_address, timeout=1)
            client.settimeout(0.8)
            clients.append(client)
            return client
        yield server, connect
    finally:
        server.finish_compute.set()
        for client in clients:
            client.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def response(client):
    output = bytearray()
    try:
        while True:
            block = client.recv(4096)
            if not block:
                break
            output.extend(block)
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    return bytes(output)


def drip(client, payload, stop, interval=0.035):
    for byte in payload:
        if stop.wait(interval):
            return
        try:
            client.sendall(bytes([byte]))
        except OSError:
            return


class HTTPReadDeadlineTests(unittest.TestCase):
    def assert_slow_input_closed(self, prefix, payload, *, body=False):
        with running_server() as (server, connect):
            client = connect()
            client.sendall(prefix)
            if body:
                self.assertTrue(server.body_started.wait(0.5))
            stop = threading.Event()
            writer = threading.Thread(target=drip, args=(client, payload, stop))
            writer.start()
            try:
                started = time.monotonic()
                try:
                    received = response(client)
                    closed = True
                except socket.timeout:
                    received = b''
                    closed = False
                self.assertTrue(closed, 'slow bytes renewed the read timeout indefinitely')
                self.assertLess(time.monotonic() - started, 0.65)
                self.assertNotIn(b'200 OK', received)
                # A timed-out input must relinquish the only worker.
                self.assertTrue(server.worker_finished.wait(0.5))
                health = connect()
                health.sendall(b'GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n')
                self.assertIn(b'200 OK', response(health))
            finally:
                stop.set()
                writer.join(1)

    def test_slow_request_line_times_out_and_releases_worker(self):
        self.assert_slow_input_closed(b'G', b'ET /health HTTP/1.0\r\nHost: localhost\r\n\r\n')

    def test_slow_headers_time_out_and_release_worker(self):
        self.assert_slow_input_closed(b'GET /health HTTP/1.0\r\nX-Test: ', b'x' * 50)

    def test_slow_body_has_its_own_absolute_budget(self):
        self.assert_slow_input_closed(
            b'POST /echo HTTP/1.0\r\nContent-Length: 50\r\n\r\n', b'x' * 50, body=True)

    def test_complete_get_input_allows_long_handler_computation(self):
        self.assert_long_computation(b'GET /compute HTTP/1.0\r\nHost: localhost\r\n\r\n')

    def test_complete_post_input_allows_long_handler_computation(self):
        self.assert_long_computation(b'POST /compute HTTP/1.0\r\nContent-Length: 2\r\n\r\nok')

    def assert_long_computation(self, payload):
        with running_server() as (server, connect):
            client = connect()
            client.sendall(payload)
            self.assertTrue(server.started.wait(0.5))
            # Hold computation past both read budgets, then explicitly release it.
            self.assertFalse(server.finish_compute.wait(0.32))
            server.finish_compute.set()
            self.assertIn(b'200 OK', response(client))


if __name__ == '__main__':
    unittest.main()
