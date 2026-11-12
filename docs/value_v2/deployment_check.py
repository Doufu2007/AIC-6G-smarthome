"""Read-only endpoint/config checks. Never starts services or submits inference."""
import argparse
import http.client
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit


def inspect_config(config):
    entries=config.get('endpoints',{})
    if set(entries)!={'local','cloud'}:raise ValueError('two logical endpoint slots required')
    hosts={};loopbacks={}
    for slot,url in entries.items():
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid endpoint; no credentials/query/fragment allowed')
        hosts[slot]=parsed.hostname
        try:loopbacks[slot]=ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:loopbacks[slot]=parsed.hostname=='localhost'
    return {'schema':'sensehome.deployment-check.v2','declared_status':config.get('status'),
            'hosts':hosts,'both_slots_loopback':all(loopbacks.values()),
            'syntactically_distinct_hosts':len(set(hosts.values()))==2 and not all(loopbacks.values()),
            'two_host_deployment_verified':False,
            'scope':'URL shape and optional read-only model inventory only. Host identity, independent hardware, WAN path, queue telemetry, actuator delivery and task success require separate deployment evidence.'}


def validate_probe_target(url):
    """Require a literal loopback address; do not resolve a supplied hostname."""
    parsed = urlsplit(url)
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError('live probes require literal loopback IP addresses') from exc
    if not address.is_loopback:
        raise ValueError('live probes restricted to literal loopback IP addresses')
    # Accessing .port validates malformed and out-of-range port values.
    parsed.port
    return parsed


def probe_inventory(url):
    parsed = validate_probe_target(url)
    requested_url = url.rstrip('/') + '/models'
    result = {'method': 'GET', 'requested_url': requested_url, 'inference_requests': 0,
              'transport': 'direct-http.client-no-proxy-no-redirect'}
    connection_type = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.request('GET', parsed.path.rstrip('/') + '/models')
        if connection.sock is not None:
            peer = connection.sock.getpeername()
            result['connected_peer'] = {'address': peer[0], 'port': peer[1]}
        response = connection.getresponse()
        result['http_status'] = response.status
        if 300 <= response.status < 400:
            result['error'] = 'HTTP_REDIRECT_REFUSED'
            return result
        if response.status != 200:
            result['error'] = 'HTTP_STATUS'
            return result
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('model inventory exceeds 1 MiB')
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise ValueError('invalid model inventory')
        models = [item.get('id') for item in body['data'] if isinstance(item, dict)]
        if len(models) != len(body['data']) or any(not isinstance(item, str) for item in models):
            raise ValueError('invalid model inventory ids')
        result['models'] = models
    except Exception as exc:
        result['error'] = type(exc).__name__
    finally:
        connection.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--probe-loopback',action='store_true',help='GET /models only; refuse all nonloopback destinations')
    parser.add_argument('--out',type=Path)
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8-sig'));result=inspect_config(config)
    if args.probe_loopback:
        if not result['both_slots_loopback']:raise ValueError('live probes restricted to loopback; use deployment guide for remote host checks')
        for url in config['endpoints'].values():validate_probe_target(url)
        probes={slot:probe_inventory(url) for slot,url in config['endpoints'].items()}
        result['read_only_loopback_probes']=probes
    raw=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.out:args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(raw,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
