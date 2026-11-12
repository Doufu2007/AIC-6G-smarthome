"""Capture host resource facts without attributing shared GPU power to a request."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import time

import psutil


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def gpu_sample() -> dict:
    command = ['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu,power.draw',
               '--format=csv,noheader,nounits']
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode:
        return {'available': False, 'error': 'NVIDIA_SMI_EXIT_' + str(result.returncode)}
    rows = []
    for row in csv.reader(io.StringIO(result.stdout)):
        values = [value.strip() for value in row]
        if len(values) != 5:
            continue
        record = {'name': values[0]}
        for key, value in zip(('memory_total_mib', 'memory_used_mib', 'utilization_pct', 'board_power_w'), values[1:]):
            try:
                record[key] = float(value)
            except ValueError:
                record[key] = None
        rows.append(record)
    return {'available': bool(rows), 'devices': rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--interval-s', type=float, default=1.0)
    parser.add_argument('--model-file', action='append', default=[], metavar='LABEL=PATH')
    parser.add_argument('--model-pid', action='append', default=[], metavar='LABEL=PID')
    args = parser.parse_args()
    if args.samples < 1 or args.interval_s < 0:
        parser.error('samples must be positive and interval nonnegative')
    if args.output.exists():
        parser.error('refusing to overwrite a captured run')
    files = []
    for entry in args.model_file:
        label, path = entry.split('=', 1)
        model = Path(path)
        files.append({'label': label, 'file_name': model.name, 'bytes': model.stat().st_size,
                      'sha256': digest(model)})
    processes = {label: psutil.Process(int(pid)) for label, pid in
                 (entry.split('=', 1) for entry in args.model_pid)}
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    psutil.cpu_percent(interval=None)
    for index in range(args.samples):
        began = time.monotonic()
        memory = psutil.virtual_memory()
        process_rows = {}
        for label, process in processes.items():
            try:
                info = process.memory_info()
                process_rows[label] = {'rss_bytes': info.rss, 'cpu_time_s': sum(process.cpu_times()[:2]),
                                       'threads': process.num_threads()}
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                process_rows[label] = {'error': type(exc).__name__}
        rows.append({'index': index, 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                     'host_cpu_pct': psutil.cpu_percent(interval=None),
                     'ram_total_bytes': memory.total, 'ram_available_bytes': memory.available,
                     'processes': process_rows, 'gpu': gpu_sample()})
        if index + 1 < args.samples:
            time.sleep(max(0.0, args.interval_s - (time.monotonic() - began)))
    output = {'schema': 'sensehome.host-resources.v1', 'started_utc': started,
              'finished_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'platform': platform.system() + ' ' + platform.release(),
              'cpu': platform.processor(), 'logical_cpus': os.cpu_count(),
              'psutil_version': psutil.__version__, 'models': files, 'samples': rows,
              'scope': 'Existing shared local model services; no devices or 6G air interface.',
              'power_scope': 'nvidia-smi board power is shared GPU telemetry, not wall power or attributable per-request energy.',
              'cost_scope': 'No cloud billed in this run; laptop purchase, electricity tariff, and wall energy are unknown.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'samples': len(rows), 'models': files}, ensure_ascii=False))


if __name__ == '__main__':
    main()
