import pytest

from eivoh.config import Config


def test_config(monkeypatch, tmp_path):
    for key in ('EIVOH_STATE_DIR', 'EIVOH_WORKDIR', 'EIVOH_CLAUDE_MODEL', 'EIVOH_CLAUDE_PERMISSION_MODE', 'EIVOH_CLAUDE_BIN'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    with pytest.raises(ValueError, match='EIVOH_WORKDIR must be set explicitly'):
        Config.from_env()
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    config = Config.from_env()
    assert config.owner_id == 123
    assert config.permission_mode == 'bypassPermissions'
    assert config.claude_bin == 'claude'
    assert config.workdir == tmp_path
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EIVOH_CLAUDE_MODEL', 'haiku')
    monkeypatch.setenv('EIVOH_CLAUDE_PERMISSION_MODE', 'dontAsk')
    monkeypatch.setenv('EIVOH_CLAUDE_BIN', '/fake/claude')
    config = Config.from_env()
    assert config.workdir == tmp_path
    assert config.claude_model == 'haiku'
    assert config.claude_bin == '/fake/claude'
    assert config.permission_mode == 'dontAsk'
    monkeypatch.setenv('EIVOH_OWNER_ID', 'name')
    with pytest.raises(ValueError):
        Config.from_env()
