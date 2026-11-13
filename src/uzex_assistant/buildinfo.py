"""Build identity of the running prototype (no secrets).

At image build, ``scripts/write_build_info.py`` writes BUILD_INFO.json: a SHA-256 of the shipped source,
frontend bundle and lock file, the build time, the git commit when one exists, the prompt version and the
dependency lock hash. Outside a built image the same values are computed on the fly and marked ``dev``.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .config import PROJECT_ROOT, get_settings

TREE = ("src/uzex_assistant", "web/dist", "requirements.lock", "pyproject.toml")


def tree_hash(root: Path = PROJECT_ROOT) -> str:
    h = hashlib.sha256()
    for top in TREE:
        p = root / top
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts) if p.exists() else []
        for f in files:
            h.update(str(f.relative_to(root)).encode())
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


@lru_cache(maxsize=1)
def build_info() -> dict:
    from .planner import PROMPT_VERSION
    from .schema import SCHEMA_VERSION
    s = get_settings()
    p = Path(s.build_info_path or PROJECT_ROOT / "BUILD_INFO.json")
    info = None
    if p.is_file():
        info = json.loads(p.read_text(encoding="utf-8"))
        info["source"] = "BUILD_INFO.json"
        # a manifest is only trusted for the code it describes: a stale file must never make changed code look like
        # the frozen build (that would bypass check_freeze)
        if info.get("tree_sha256") and info["tree_sha256"] != tree_hash():
            info = None
    if info is None:
        info = {"build_id": "dev-" + tree_hash()[:12], "commit": None, "built_at": None, "source": "computed (not a built image)"}
    info.update({"prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
                 "requirements_lock_sha256": info.get("requirements_lock_sha256") or file_sha(PROJECT_ROOT / "requirements.lock")})
    if info.get("prompt_version_at_build") and info["prompt_version_at_build"] != PROMPT_VERSION:
        info["warning"] = "prompt version differs from the build manifest"
    return info


class FrozenBuildMismatch(RuntimeError):
    pass


def freeze_file() -> Path:
    s = get_settings()
    return Path(s.freeze_path or Path(s.db_path).parent / "FREEZE.json")


def check_freeze() -> dict | None:
    """If FREEZE.json exists, the running build and prompt must be the frozen ones (unless overridden)."""
    s = get_settings()
    f = freeze_file()
    if not f.is_file():
        return None
    frozen = json.loads(f.read_text(encoding="utf-8"))
    bi = build_info()
    diff = [k for k in ("build_id", "prompt_version") if frozen.get(k) != bi.get(k)]
    if diff and not s.allow_after_freeze:
        raise FrozenBuildMismatch(
            f"this build differs from the frozen submission in {diff} (frozen {frozen.get('build_id')}, running "
            f"{bi.get('build_id')}). Restart the frozen image instead; changes need organizer approval "
            "(override: UZEX_ALLOW_AFTER_FREEZE=true).")
    return {"frozen": not diff, "frozen_at": frozen.get("frozen_at"), "build_id": frozen.get("build_id")}
