"""Operator-only pause/activate through a USER transaction (never a raw edit)."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import supervisor_ctl as ctl


def set_status(root: Path, status: str, reason: str):
    if status not in {'ACTIVE', 'PAUSED'} or not reason.strip():
        raise ValueError('status must be ACTIVE/PAUSED and reason must be nonempty')
    root = root.resolve()
    runtime = ctl.read_json(root / 'control/runtime.json')
    if runtime['project_status'] not in {'ACTIVE', 'PAUSED'}:
        raise ValueError('cannot reopen a terminal project with this helper')
    run = ctl.acquire_lease(root, 'USER', None, None)
    if not run.get('ok'):
        return run
    path = Path(run['request_path'])
    request = {'expected': run['expected'], 'reason': reason,
               'patches': {'runtime': {'project_status': status}},
               'finish': {'outcome': 'USER_PROJECT_STATUS', 'summary': reason}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ctl.persisted_json_bytes(request))
    return ctl.commit_request(root, run['lease_token'], path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--status', choices=['ACTIVE', 'PAUSED'], required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    result = set_status(args.root, args.status, args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
