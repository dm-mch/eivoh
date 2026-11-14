import pytest

from eivoh.config import Config


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    # Never depend on the real ~/eivoh-home.
    path = tmp_path / 'home'
    path.mkdir()
    monkeypatch.setenv('EIVOH_HOME', str(path))
    return path


def test_home_and_legacy_workdir(monkeypatch, tmp_path, home):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    for value in (None, '', '  '):
        if value is None:
            monkeypatch.delenv('EIVOH_WORKDIR', raising=False)
        else:
            monkeypatch.setenv('EIVOH_WORKDIR', value)
        config = Config.from_env()
        assert config.home == home and config.workdir is None
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    assert (Config.from_env().home, Config.from_env().workdir) == (home, tmp_path)
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path / 'missing'))
    with pytest.raises(ValueError, match='EIVOH_WORKDIR must be an existing directory'):
        Config.from_env()
    monkeypatch.delenv('EIVOH_WORKDIR')
    monkeypatch.setenv('EIVOH_HOME', str(tmp_path / 'missing'))
    with pytest.raises(ValueError, match='does not exist.*deploy/release.sh'):
        Config.from_env()
    # Default: ~/eivoh-home.
    monkeypatch.delenv('EIVOH_HOME')
    monkeypatch.setenv('HOME', str(tmp_path))
    with pytest.raises(ValueError, match=f'EIVOH_HOME {tmp_path}/eivoh-home does not exist'):
        Config.from_env()
    (tmp_path / 'eivoh-home').mkdir()
    assert Config.from_env().home == tmp_path / 'eivoh-home'


def test_config(monkeypatch, tmp_path):
    for key in ('EIVOH_STT', 'SONIOX_API_KEY', 'EIVOH_STATE_DIR', 'EIVOH_WORKDIR', 'EIVOH_CLAUDE_MODEL', 'EIVOH_CLAUDE_PERMISSION_MODE', 'EIVOH_CLAUDE_BIN', 'EIVOH_CLAUDE_ARGS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
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
    ('''--settings '{"enabledPlugins":{"example-plugin@example-marketplace":false}}' ''',
     ('--settings', '{"enabledPlugins":{"example-plugin@example-marketplace":false}}')),
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
    '--model opus', '--model=opus',
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


def test_speech_provider_selection():
    from eivoh.speech import make_transcriber, provider_from_env
    assert provider_from_env({}) is None
    assert provider_from_env({'SONIOX_API_KEY': 'k'}) == 'soniox'
    assert provider_from_env({'SONIOX_API_KEY': 'k', 'EIVOH_STT': 'none'}) is None
    with pytest.raises(ValueError, match='requires SONIOX_API_KEY'):
        provider_from_env({'EIVOH_STT': 'soniox'})
    with pytest.raises(ValueError, match='Unknown EIVOH_STT'):
        provider_from_env({'EIVOH_STT': 'whisper'})
    assert make_transcriber(None, {}) is None
    assert type(make_transcriber('soniox', {'SONIOX_API_KEY': 'k'})).__name__ == 'SonioxTranscriber'


def test_codex_config(monkeypatch, tmp_path):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    for key in ('EIVOH_DEFAULT_EXECUTOR', 'EIVOH_CODEX_BIN', 'EIVOH_CODEX_MODEL', 'EIVOH_CODEX_ARGS', 'EIVOH_STT', 'SONIOX_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    config = Config.from_env()
    assert (config.default_executor, config.codex_bin, config.codex_model, config.codex_args) == ('claude', 'codex', None, ())
    monkeypatch.setenv('EIVOH_DEFAULT_EXECUTOR', 'Codex')
    monkeypatch.setenv('EIVOH_CODEX_ARGS', '-c model_reasoning_effort="high"')
    config = Config.from_env()
    assert config.default_executor == 'codex' and config.codex_args == ('-c', 'model_reasoning_effort=high')
    for bad in ('-C /tmp', 'resume', '--cd=/tmp', '--model model', '--model=model', '-m model', '-mmodel'):
        monkeypatch.setenv('EIVOH_CODEX_ARGS', bad)
        with pytest.raises(ValueError, match='adapter-owned'):
            Config.from_env()
    monkeypatch.setenv('EIVOH_CODEX_ARGS', '')
    monkeypatch.setenv('EIVOH_DEFAULT_EXECUTOR', 'gemini')
    with pytest.raises(ValueError, match='EIVOH_DEFAULT_EXECUTOR'):
        Config.from_env()


@pytest.mark.parametrize('value,expected', [(None, 3), ('1', 1), ('8', 8), ('0', None), ('-1', None), ('1.5', None), ('many', None), ('', None)])
def test_parallel_config(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    if value is None:
        monkeypatch.delenv('EIVOH_MAX_PARALLEL', raising=False)
    else:
        monkeypatch.setenv('EIVOH_MAX_PARALLEL', value)
    if expected is None:
        with pytest.raises(ValueError, match='EIVOH_MAX_PARALLEL'):
            Config.from_env()
    else:
        assert Config.from_env().max_parallel == expected


@pytest.mark.parametrize('value', ['--add-dir /tmp', '-p profile', '-s read-only', '--search', '--skip-git-repo-check',
                                   '--ignore-user-config', '-i image.png', '--help', '--startup-sign-out'])
def test_codex_exec_options_fail_at_config_loading(monkeypatch, tmp_path, value):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_CODEX_ARGS', value)
    with pytest.raises(ValueError, match='now configures app-server.*not supported'):
        Config.from_env()


@pytest.mark.parametrize('value', ['-c x=true --enable feature --disable other --strict-config',
                                   '--config=x=true --enable=feature --disable=other', '--strict-config'])
def test_codex_supported_app_server_options(monkeypatch, tmp_path, value):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_CODEX_ARGS', value)
    assert Config.from_env().codex_args


@pytest.mark.parametrize('value', ['-c', '--config=missing-equals', '--enable', '--disable=', '--enable --strict-config'])
def test_codex_app_server_option_missing_values(monkeypatch, tmp_path, value):
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(tmp_path))
    monkeypatch.setenv('EIVOH_CODEX_ARGS', value)
    with pytest.raises(ValueError, match='requires'):
        Config.from_env()


def test_startup_configuration_errors_exit_78(monkeypatch):
    # RestartPreventExitStatus=78: systemd must not restart-loop on these.
    import eivoh.__main__ as entry
    from eivoh.storage import LegacyWorkdirRequired

    monkeypatch.delenv('EIVOH_TELEGRAM_TOKEN', raising=False)
    with pytest.raises(SystemExit) as exit_info:
        entry.main()
    assert exit_info.value.code == 78
    monkeypatch.setattr(entry.Config, 'from_env', classmethod(lambda cls: object()))
    async def serve(config):
        raise LegacyWorkdirRequired('EIVOH_WORKDIR must be set')
    monkeypatch.setattr(entry, 'serve', serve)
    with pytest.raises(SystemExit) as exit_info:
        entry.main()
    assert exit_info.value.code == 78
