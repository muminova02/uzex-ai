"""Write BUILD_INFO.json (run at image build, after `web/dist` exists). No secrets are read or written.

    python scripts/write_build_info.py [--commit <sha>] [--out BUILD_INFO.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "BUILD_INFO.json")
    a = ap.parse_args()
    from uzex_assistant.buildinfo import file_sha, tree_hash
    from uzex_assistant.planner import PROMPT_VERSION
    commit = a.commit
    if not commit:
        try:
            commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                                    timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            commit = None
    th = tree_hash(ROOT)
    info = {"build_id": th[:12], "tree_sha256": th, "commit": commit or None,
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "prompt_version_at_build": PROMPT_VERSION, "requirements_lock_sha256": file_sha(ROOT / "requirements.lock"),
            "frontend_lock_sha256": file_sha(ROOT / "web" / "package-lock.json")}
    a.out.write_text(json.dumps(info, indent=1), encoding="utf-8")
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
