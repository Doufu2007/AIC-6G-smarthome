from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent))


def test_literal_loopback_only():
    import runtime
    assert runtime.checked_url('http://127.0.0.1:64552/v1')
    for value in ['http://localhost:8/v1', 'http://example.com/v1', 'http://127.0.0.1:8/v1?q=1',
                  'http://a:b@127.0.0.1:8/v1', 'file:///tmp/model']:
        with pytest.raises(ValueError):
            runtime.checked_url(value)


def test_model_paths_must_exist_and_match_pin(tmp_path):
    import runtime
    path = tmp_path / 'model.gguf'
    path.write_bytes(b'test model')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        runtime.checked_file(path, '0' * 64)
    info = runtime.checked_file(path, runtime.digest(path))
    assert info['bytes'] == 10 and info['sha256'] == runtime.digest(path)


def test_get_fixture_rejects_redirect_and_proxy(monkeypatch):
    import runtime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/destination')
            self.send_header('Content-Length', '0')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    try:
        response = runtime.get_json(f'http://127.0.0.1:{server.server_port}/models')
        assert response['http_status'] == 302 and response['error'] == 'HTTP_302'
        assert seen == ['/models']
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_runtime_dependency_set_is_pinned(tmp_path):
    import runtime
    import json
    exe = tmp_path / 'llama-server.exe'
    exe.write_bytes(b'launcher')
    dll = tmp_path / 'llama-server-impl.dll'
    dll.write_bytes(b'implementation')
    model = tmp_path / 'm.gguf'
    model.write_bytes(b'model')
    cfg = {'runtime': {'path': exe.name, 'sha256': runtime.digest(exe),
                       'dependencies': {dll.name: runtime.digest(dll)}},
           'models': {tier: {'path': model.name, 'sha256': runtime.digest(model),
                            'model_id': tier, 'port': port}
                      for tier, port in [('local', 64552), ('cloud', 64554)]}}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    report = runtime.doctor(path)
    assert report['runtime_dependencies'][dll.name]['sha256'] == runtime.digest(dll)
    dll.write_bytes(b'changed implementation')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        runtime.doctor(path)
    dll.write_bytes(b'implementation')
    (tmp_path / 'unlisted.dll').write_bytes(b'new dependency')
    with pytest.raises(ValueError, match='RUNTIME_DEPENDENCY_SET_MISMATCH'):
        runtime.doctor(path)


def test_runtime_closes_started_child_on_second_startup_failure(tmp_path, monkeypatch):
    import runtime
    class Child:
        pid=12345
        returncode=None
        terminated=False
        def poll(self): return self.returncode
        def terminate(self): self.terminated=True; self.returncode=-15
        def wait(self, timeout): return self.returncode
    child=Child()
    inventory={'runtime': {'path': 'pinned.exe', 'sha256': 'x'},
               'models': {tier: {'path': tier+'.gguf', 'sha256': 'y',
                                'model_id': tier, 'port': port}
                          for tier,port in [('local',64552),('cloud',64554)]}}
    monkeypatch.setattr(runtime, 'doctor', lambda path: inventory)
    class Socket:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def bind(self,address): pass
    monkeypatch.setattr(runtime.socket, 'socket', Socket)
    started=[]
    def popen(argv, **kwargs):
        started.append(argv)
        if len(started)==2: raise OSError('controlled second launch failure')
        return child
    monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
    def get_json(url, timeout=2):
        return {'http_status':200,'error':None,'response':
                {'data':[{'id':'local'}]} if url.endswith('/models') else {'status':'ok'}}
    monkeypatch.setattr(runtime,'get_json',get_json)
    with pytest.raises(OSError,match='second launch failure'):
        with runtime.LocalRuntime(tmp_path): pass
    assert child.terminated
    import json
    result=json.loads((tmp_path/'runtime_manifest.json').read_text(encoding='utf-8'))
    assert result['status']=='stopped' and len(result['children'])==1
    assert result['children'][0]['exit_code']==-15
    assert result['children'][0]['argv']==started[0]


def test_reserved_port_failure_launches_no_model(tmp_path, monkeypatch):
    import runtime
    inventory={'runtime': {'path':'pinned.exe','sha256':'x'},
               'models': {'local': {'path':'m.gguf','sha256':'y','model_id':'m','port':64552}}}
    monkeypatch.setattr(runtime,'doctor',lambda _: inventory)
    class ReservedSocket:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def bind(self,address): raise PermissionError(13,'reserved TCP port')
    monkeypatch.setattr(runtime.socket,'socket',ReservedSocket)
    def forbidden_launch(*args,**kwargs): raise AssertionError('must not launch model')
    monkeypatch.setattr(runtime.subprocess,'Popen',forbidden_launch)
    with pytest.raises(PermissionError,match='reserved TCP port'):
        with runtime.LocalRuntime(tmp_path): pass
    import json
    report=json.loads((tmp_path/'runtime_manifest.json').read_text(encoding='utf-8'))
    assert report['status']=='stopped' and report['children']==[]
