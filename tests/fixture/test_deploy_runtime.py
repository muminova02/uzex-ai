"""Prompt 7: runtime never re-imports; build identity and health endpoint (fixture DB, no network)."""
import json

import pytest

from uzex_assistant import importer
from uzex_assistant.buildinfo import build_info, tree_hash
from uzex_assistant.config import get_settings


def test_runtime_refuses_missing_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UZEX_RUNTIME_IMPORT", raising=False)
    with pytest.raises(importer.StaleDatabase) as e:
        importer.require_database(tmp_path / "src", tmp_path / "none.duckdb")
    assert "one-time import" in str(e.value)
    assert not (tmp_path / "none.duckdb").exists()  # nothing was built


def test_runtime_import_is_off_by_default():
    assert get_settings().runtime_import is False


def test_build_info_has_no_secrets_and_names_prompt():
    bi = build_info()
    assert bi["prompt_version"] and bi["build_id"] and len(tree_hash()) == 64
    blob = json.dumps(bi).lower()
    assert "api_key" not in blob and "token\"" not in blob


def _settings(tmp_path, **kw):
    from uzex_assistant.config import Settings
    return Settings(_env_file=None, db_path=tmp_path / "db" / "uzex.duckdb", **kw)


def test_import_cli_refuses_new_ingestion_after_freeze(tmp_path):
    import datetime as dt
    s = _settings(tmp_path, freeze_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
    assert "freeze" in importer._freeze_block(s, s.db_path, {"x": 1})
    s2 = _settings(tmp_path, freeze_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), allow_after_freeze=True)
    assert importer._freeze_block(s2, s2.db_path, {"x": 1}) is None


def test_frozen_identity_allows_identical_rebuild_only(tmp_path):
    s = _settings(tmp_path)
    s.db_path.parent.mkdir(parents=True)
    (s.db_path.parent / "FREEZE.json").write_text(json.dumps({"db_identity": {"x": 1}}), encoding="utf-8")
    assert importer._freeze_block(s, s.db_path, {"x": 1}) is None
    assert "differ" in importer._freeze_block(s, s.db_path, {"x": 2})


def test_runtime_refuses_different_frozen_build(tmp_path, monkeypatch):
    from uzex_assistant import buildinfo
    f = tmp_path / "FREEZE.json"
    f.write_text(json.dumps({"build_id": "other", "prompt_version": build_info()["prompt_version"]}), encoding="utf-8")
    monkeypatch.setenv("UZEX_FREEZE_PATH", str(f))
    get_settings.cache_clear()
    try:
        with pytest.raises(buildinfo.FrozenBuildMismatch):
            buildinfo.check_freeze()
        monkeypatch.setenv("UZEX_ALLOW_AFTER_FREEZE", "true")
        get_settings.cache_clear()
        assert buildinfo.check_freeze()["frozen"] is False
    finally:
        monkeypatch.delenv("UZEX_FREEZE_PATH")
        monkeypatch.delenv("UZEX_ALLOW_AFTER_FREEZE")
        get_settings.cache_clear()


def test_stale_build_manifest_is_not_trusted(tmp_path, monkeypatch):
    """Release acceptance 2026-11-05: a BUILD_INFO.json left in the tree must not make changed code report the
    frozen build id (it would bypass the freeze guard)."""
    import json as _json

    from uzex_assistant import buildinfo
    from uzex_assistant.config import get_settings
    real = buildinfo.tree_hash()
    for th, expect_manifest in ((real, True), ("0" * 64, False)):
        p = tmp_path / f"BI_{expect_manifest}.json"
        p.write_text(_json.dumps({"build_id": "frozen123456", "tree_sha256": th}), encoding="utf-8")
        monkeypatch.setenv("UZEX_BUILD_INFO_PATH", str(p))
        get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
        buildinfo.build_info.cache_clear()
        bi = buildinfo.build_info()
        assert (bi["build_id"] == "frozen123456") is expect_manifest, bi
        assert expect_manifest or bi["build_id"] == "dev-" + real[:12]
    buildinfo.build_info.cache_clear()
