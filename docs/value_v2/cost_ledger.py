"""Validate cost provenance; missing or quoted inputs never become actual totals."""
import argparse
import hashlib
import json
import math
from pathlib import Path

CATEGORIES = {'hardware_amortization','electricity','cloud_inference','operations','network'}
ROOT = Path(__file__).resolve().parents[2]


def number(value):
    return type(value) in (int,float) and math.isfinite(value) and value >= 0


def validate_ledger(ledger, root):
    if ledger.get('schema') != 'sensehome.actual-ledger.v2':
        raise ValueError('unsupported ledger schema')
    if not all(isinstance(ledger.get(key),str) and ledger[key].strip() for key in ('currency','period','scope')):
        raise ValueError('currency, period and scope are required')
    entries = ledger.get('entries',[])
    if len(entries) != 5 or {entry.get('category') for entry in entries} != CATEGORIES:
        raise ValueError('exactly one entry per cost category is required')
    result, actual, known = [], [], []
    for entry in entries:
        status, amount = entry.get('status'), entry.get('amount')
        if entry.get('currency') != ledger['currency']:
            raise ValueError('currency mismatch; no silent currency conversion')
        if status not in {'actual','quote','assumption','unknown'}:
            raise ValueError('invalid evidence status')
        if not isinstance(entry.get('source'),str) or not entry['source'].strip():
            raise ValueError('source required even for unknown entries')
        fraction = entry.get('allocation_fraction')
        if not number(fraction) or fraction > 1:
            raise ValueError('allocation_fraction must be within [0,1]')
        if (amount is None) != (status == 'unknown') or amount is not None and not number(amount):
            raise ValueError('unknown amounts must be null; known amounts finite and nonnegative')
        receipt_verified = False
        if status == 'actual':
            receipt = entry.get('receipt',{})
            if not isinstance(receipt.get('path'),str) or not isinstance(receipt.get('kind'),str) or not receipt['kind']:
                raise ValueError('actual entries require receipt path, hash and kind')
            path = (Path(root)/receipt['path']).resolve()
            if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
                raise ValueError('receipt must be an existing file inside the evidence root')
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get('sha256'):
                raise ValueError('receipt SHA256 mismatch')
            receipt_verified = True
        allocated = None if amount is None else amount * fraction
        if allocated is not None:known.append(allocated)
        if status == 'actual':actual.append(allocated)
        result.append({**entry,'allocated_amount':allocated,'receipt_file_hash_verified':receipt_verified})
    return {'schema':'sensehome.actual-ledger-result.v2','currency':ledger['currency'],'period':ledger['period'],
            'scope':ledger['scope'],'entries':result,'known_actual_subtotal':sum(actual),
            'known_all_status_subtotal':sum(known),
            'actual_total':sum(actual) if len(actual)==5 else None,
            'scenario_total':sum(known) if len(known)==5 else None,
            'unknown_categories':[e['category'] for e in entries if e['status']=='unknown'],
            'non_actual_categories':[e['category'] for e in entries if e['status']!='actual'],
            'interpretation':'A matching receipt hash proves file integrity, not authenticity, invoice eligibility or allocation fairness. Actual status is a documented source claim requiring human verification. Quotes and assumptions stay separate.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    result=validate_ledger(json.loads(args.ledger.read_text(encoding='utf-8-sig')),ROOT)
    raw=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.out:args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(raw,encoding='utf-8')
    print(json.dumps({'actual_total':result['actual_total'],'scenario_total':result['scenario_total'],
                      'known_actual_subtotal':result['known_actual_subtotal'],'unknown_categories':result['unknown_categories']}))


if __name__=='__main__':main()
