"""Restore losslessly compressed public raw evidence without overwriting files."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'SenseHome-6G/evidence/network_compute/20261111_fused/public_raw_manifest.json'


def contained(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('MANIFEST_PATH_OUTSIDE_ROOT')
    return path


def restore(output_root=ROOT):
    rows = json.loads(MANIFEST.read_text(encoding='utf-8'))['files']
    results = []
    for row in rows:
        source = contained(ROOT, row['compressed_path'])
        if hashlib.file_digest(source.open('rb'), 'sha256').hexdigest() != row['compressed_sha256']:
            raise ValueError('COMPRESSED_CHECKSUM_MISMATCH: ' + row['compressed_path'])
        target = contained(output_root, row['original_path'])
        if target.exists():
            if hashlib.file_digest(target.open('rb'), 'sha256').hexdigest() != row['original_sha256']:
                raise ValueError('EXISTING_FILE_DIFFERS: ' + row['original_path'])
            results.append({'path': row['original_path'], 'status': 'already_verified'})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.restore-', dir=target.parent)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, 'wb') as output, gzip.open(source, 'rb') as input_file:
                while block := input_file.read(1024 * 1024):
                    size += len(block)
                    if size > row['original_bytes']:
                        raise ValueError('EXPANDED_SIZE_MISMATCH')
                    digest.update(block)
                    output.write(block)
            if size != row['original_bytes'] or digest.hexdigest() != row['original_sha256']:
                raise ValueError('RESTORED_CHECKSUM_MISMATCH')
            # A concurrent creator must never be overwritten.
            os.link(temporary, target)
            results.append({'path': row['original_path'], 'status': 'restored_and_verified'})
        finally:
            Path(temporary).unlink(missing_ok=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(restore(args.output_root), ensure_ascii=False, indent=2))
