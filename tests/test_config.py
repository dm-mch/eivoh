import pytest

from eivoh.config import Config


def test_config(monkeypatch, tmp_path):
    for key in ('EIVOH_STATE_DIR', 'EIVOH_WORKDIR', 'EIVOH_CLAUDE_MODEL', 'EIVOH_CLAUDE_PERMISSION_MODE', 'EIVOH_CLAUDE_BIN', 'EIVOH_CLAUDE_ARGS'):
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
    assert config.claude_args == ()
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


@pytest.mark.parametrize(('value', 'expected'), [
    ('', ()),
    ('   ', ()),
    ('''--settings '{"enabledPlugins":{"example-plugin@example-marketplace":false}}' --model "a model"''',
     ('--settings', '{"enabledPlugins":{"example-plugin@example-marketplace":false}}', '--model', 'a model')),
])
def test_claude_args_parsing(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_CLAUDE_ARGS', value)
    assert Config.from_env().claude_args == expected


@pytest.mark.parametrize('value', [
    '--resume session', '--resume=session', '-p', '-p=prompt', '-pprompt',
    '--print', '--print=prompt', '--output-format json', '--output-format=json',
    '--input-format json', '--input-format=json',
    '--permission-mode plan', '--permission-mode=plan',
])
def test_claude_args_reject_owned_flags(monkeypatch, tmp_path, value):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_CLAUDE_ARGS', value)
    with pytest.raises(ValueError, match='EIVOH_CLAUDE_ARGS.*adapter-owned'):
        Config.from_env()


def test_claude_args_invalid_quoting(monkeypatch, tmp_path):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_CLAUDE_ARGS', "--settings 'unclosed")
    with pytest.raises(ValueError, match='Invalid quoting in EIVOH_CLAUDE_ARGS'):
        Config.from_env()
