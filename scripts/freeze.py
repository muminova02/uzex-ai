"""Record the frozen submission (run ONCE, manually, before 2026-11-05 18:00 Asia/Tashkent).

Writes <db dir>/FREEZE.json: build id and prompt version of the running code, model id, the prepared DB's
identity (source SHA-256 fingerprints, schema version, as-of) and a SHA-256 of the DB file. After this,
web/bot refuse a different build or prompt, and the import CLI refuses changed data (override only with
organizer approval: UZEX_ALLOW_AFTER_FREEZE=true). Contains no secrets.

    python scripts/freeze.py [--image-tag uzex-assistant:frozen-XXXX]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-tag", default=None)
    ap.add_argument("--force", action="store_true", help="overwrite an existing FREEZE.json")
    a = ap.parse_args()
    from uzex_assistant.buildinfo import build_info, freeze_file
    from uzex_assistant.config import get_settings
    from uzex_assistant.importer import read_identity
    s = get_settings()
    f = freeze_file()
    if f.exists() and not a.force:
        print(f"{f} already exists; refusing to overwrite (use --force only with organizer approval)", file=sys.stderr)
        return 2
    ident = read_identity(Path(s.db_path))
    if ident is None:
        print("prepared database missing; run the import first", file=sys.stderr)
        return 2
    h = hashlib.sha256()
    with open(s.db_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    bi = build_info()
    out = {"frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "image_tag": a.image_tag,
           "build_id": bi.get("build_id"), "commit": bi.get("commit"), "prompt_version": bi.get("prompt_version"),
           "schema_version": bi.get("schema_version"), "requirements_lock_sha256": bi.get("requirements_lock_sha256"),
           "llm_provider": s.llm_provider, "llm_model": s.llm_model, "db_identity": ident, "db_file_sha256": h.hexdigest()}
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("frozen_at", "build_id", "prompt_version", "llm_model", "db_file_sha256")}))
    print("wrote", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
