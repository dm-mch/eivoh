import json

import pytest

from eivoh.models import catalog, resolve


def test_catalog_reads_visible_models_at_call_time(tmp_path):
    cache = tmp_path / 'models_cache.json'
    assert catalog()['codex'] == []
    cache.write_text(json.dumps({'models': [
        {'slug': 'gpt-6-astra', 'description': 'Astra', 'visibility': 'list'},
        {'slug': 'hidden', 'visibility': 'hide'},
        {'slug': 'gpt-6.1-sol', 'visibility': 'list'},
        {'slug': 'gpt-6.1-sol', 'visibility': 'list'},
        {'slug': None, 'visibility': 'list'}, None,
    ]}))
    assert catalog()['codex'] == [('gpt-6-astra', 'Astra'), ('gpt-6.1-sol', '')]
    cache.write_text('{bad json')
    assert catalog()['codex'] == []
    cache.write_bytes(b'\xff')
    assert catalog()['codex'] == []
    cache.write_text('{"models": null}')
    assert catalog()['codex'] == []


def test_claude_override(monkeypatch):
    assert ('claude-fable-5-2', 'Fable 5.2') in catalog()['claude']
    monkeypatch.setenv('EIVOH_CLAUDE_MODELS', ' special-v1, special-v2, ,special-v1 ')
    assert [model for model, _ in catalog()['claude']] == ['special-v1', 'special-v2']
    assert resolve('SPECIAL-V1') == ('claude', 'special-v1')
    with pytest.raises(ValueError, match='Unknown'):
        resolve('opus')


def test_resolve_exact_substring_number_and_errors(tmp_path):
    (tmp_path / 'models_cache.json').write_text(json.dumps({'models': [
        {'slug': slug, 'visibility': 'list'}
        for slug in ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-sol']]}))
    assert resolve('OPUS') == ('claude', 'opus')
    assert resolve('CLAUDE-OPUS-5-5') == ('claude', 'claude-opus-5-5')
    assert resolve(' Astra ') == ('codex', 'gpt-6-astra')
    assert resolve('1') == ('claude', 'fable')
    assert resolve('9') == ('codex', 'gpt-6-astra')
    with pytest.raises(ValueError, match='Ambiguous.*gpt-6.1-sol.*gpt-6-sol'):
        resolve('sol')
    for query in ['unknown', '', '0', '100', '-1']:
        with pytest.raises(ValueError, match='Unknown.*Candidates:.*opus'):
            resolve(query)
