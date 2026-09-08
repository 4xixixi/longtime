"""Offline demonstration using the regression suite's synthetic fixture."""
from pathlib import Path
import json
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
from test_supervisor_ctl import make_root, write_json
from control import supervisor_ctl as ctl


def main():
    root = make_root()
    try:
        healthy = ctl.check(root)
        if not healthy.get("ok"):
            raise RuntimeError(healthy)
        print(json.dumps({"healthy": healthy}, ensure_ascii=False, indent=2))
        path = root / "jobs/job-one/state.json"
        job = ctl.read_json(path)
        job["title"] = "Uncommitted edit"
        write_json(path, job)
        try:
            changed = ctl.check(root)
        except ctl.ControlError as exc:
            changed = {"ok": False, "error": str(exc)}
        if changed.get("ok") or "STATE_DRIFT" not in json.dumps(changed):
            raise RuntimeError("Expected state drift rejection: " + repr(changed))
        print(json.dumps({"tamper_detected": changed}, ensure_ascii=False, indent=2))
    finally:
        if root.resolve().parent != Path(tempfile.gettempdir()).resolve():
            raise RuntimeError("Unexpected temporary fixture location")
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
