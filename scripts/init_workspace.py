"""Create a new PAUSED control workspace; never overwrite an existing path."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from control import supervisor_ctl as ctl


def initialize(root: Path, workspace: Path, specification: Path, job_id: str = 'first-job'):
    root, workspace, specification = root.resolve(), workspace.resolve(), specification.resolve()
    if not workspace.is_dir():
        raise ValueError('workspace must be an existing directory')
    if not job_id or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in job_id):
        raise ValueError('job_id must use lowercase letters, digits, hyphen or underscore')
    spec = specification.read_bytes()
    if not spec.strip():
        raise ValueError('specification cannot be empty')
    root.mkdir(parents=True, exist_ok=False)
    (root / 'control').mkdir()
    for p in (REPO / 'control').iterdir():
        if p.is_file() and p.suffix in {'.py', '.ps1', '.md'}:
            shutil.copy2(p, root / 'control' / p.name)
    shutil.copy2(REPO / 'AGENTS.md', root / 'AGENTS.md')
    job_dir = root / 'jobs' / job_id
    job_dir.mkdir(parents=True)
    (job_dir / 'specification.md').write_bytes(spec)
    now = ctl.iso_utc()
    project = {'schema_version': 1, 'contract_id': 'initial-project',
               'outcome': 'Complete the supplied specification within its stated boundaries.',
               'required_queue_order': [job_id], 'user_only_gates': sorted(ctl.VALID_USER_GATES)}
    def write(rel, obj):
        (root / rel).write_bytes(ctl.persisted_json_bytes(obj))
    write('control/project-contract.json', project)
    project_hash = ctl.digest_file(root / 'control/project-contract.json')
    spec_hash = ctl.digest_file(job_dir / 'specification.md')
    protocol = json.loads((REPO / 'templates/protocol-contract.json').read_text(encoding='utf8'))
    protocol.update(job_id=job_id, project_contract_sha256=project_hash, specification_sha256=spec_hash)
    write(f'jobs/{job_id}/protocol-contract.json', protocol)
    job = json.loads((REPO / 'templates/job-state.json').read_text(encoding='utf8'))
    job.update(job_id=job_id, title=job_id, status='QUEUED', created_at=now, updated_at=now, workspace=str(workspace))
    # A draft bootstrap is not acceptance evidence or authorization to run.
    job['baseline'] = {'git_commit': None, 'path_manifest': None, 'specification_sha256': spec_hash}
    job['authorized_specification']['current_sha256'] = spec_hash
    job['runtime_tracking']['last_progress_at'] = now
    job['controller']['protocol_contract_sha256'] = ctl.digest_file(job_dir / 'protocol-contract.json')
    write(f'jobs/{job_id}/state.json', job)
    queue = {'schema_version': ctl.QUEUE_SCHEMA, 'queue_revision': 1, 'updated_at': now,
             'planning_generation': 0, 'last_empty_event': None, 'jobs': [{
                 'job_id': job_id, 'priority': 1, 'queued_at': now, 'specification_sha256': spec_hash,
                 'protocol_contract_sha256': job['controller']['protocol_contract_sha256']}]}
    write('control/queue.json', queue)
    runtime = {'schema_version': ctl.RUNTIME_SCHEMA, 'updated_at': now, 'project_status': 'PAUSED',
               'active_job_id': job_id, 'controller': {'workflow_version': ctl.WORKFLOW_VERSION,
               'project_contract_sha256': project_hash, 'user_only_gates': sorted(ctl.VALID_USER_GATES)},
               'defaults': {'max_parallel_jobs': 1, 'check_interval_minutes': 45, 'lease_ttl_minutes': 60,
               'routing_action_deadline_seconds': 75, 'max_routing_heartbeat_wall_seconds': 90,
               'stale_after_minutes': 90, 'max_auto_rework_epochs': 3, 'max_auto_runtime_hours': 6,
               'max_sol_escalations_per_job': 5, 'max_same_root_cause_escalations': 2}}
    write('control/runtime.json', runtime)
    ctl.bootstrap(root)
    return ctl.check(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--job-id', default='first-job')
    args = parser.parse_args()
    result = initialize(args.root, args.workspace, args.spec, args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
