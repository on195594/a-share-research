"""Prepared client bundles must not install, connect, expose credentials, or change existing configuration."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tomllib

import pytest

spec=importlib.util.spec_from_file_location('client_prepare',Path(__file__).parents[1]/'ops/clients/prepare.py')
prepare=importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

MCP_URL = 'https://research.example:8443/mcp'


@pytest.mark.parametrize('role',['assistant','research'])
def test_bundle_is_parseable_disabled_and_keeps_host_state_untouched(tmp_path,role):
    home=tmp_path/'home with spaces'
    home.mkdir()
    existing=home/'.codex'
    existing.mkdir()
    config=existing/'config.toml'
    config.write_text('model = "existing"\n')
    output=tmp_path/'prepared'
    manifest=prepare.prepare(output,home,MCP_URL,role)
    assert config.read_text()=='model = "existing"\n'
    assert not (home/'.local').exists() and not (home/'.config').exists()
    assert manifest['status']=='prepared-only' and manifest['role']==role
    assert (output/'bin/ashare').stat().st_mode&0o777==0o700
    for relative,digest in manifest['files'].items():
        assert hashlib.sha256((output/relative).read_bytes()).hexdigest()==digest
    assert manifest['cli_config_destination'] == str(home/f'.config/ashare/hermes-{role}.json')
    assert json.loads((output/'credentials.example.json').read_text()) == {'url': MCP_URL, 'token': ''}
    codex=tomllib.loads((output/'configs/codex.config.toml').read_text())['mcp_servers']['ashare']
    records={'codex':codex}
    for host,file,root in [('claude','claude.mcp.json','mcpServers'),('hermes','hermes.config.json','mcp_servers'),
                           ('pi','pi.mcp.json','mcpServers'),('agy','agy/ashare/mcp_config.json','mcpServers')]:
        records[host]=json.loads((output/'configs'/file).read_text())[root]['ashare']
    for host, value in records.items():
        assert value['url'] == MCP_URL
        assert not {'command', 'args', 'env'} & value.keys()
        token_var = f'ASHARE_{host.upper()}_{role.upper()}_TOKEN'
        if host == 'codex':
            assert value['bearer_token_env_var'] == token_var
        else:
            assert value['headers'] == {'Authorization': 'Bearer ${' + token_var + '}'}
        if host == 'agy':
            assert [item['name'] for item in value['variables']] == [token_var]
    assert json.loads((output/'configs/agy/ashare/plugin.json').read_text())['name'] == 'ashare'
    assert all(records[host]['enabled'] is False for host in ('codex','hermes','pi'))
    assert records['claude']['type']=='http' and 'enabled' not in records['claude']
    assert records['hermes']['timeout']==40 and 'tool_timeout' not in records['hermes']
    assert records['pi']['exposure']=='direct'


def test_existing_review_bundle_is_never_overwritten(tmp_path):
    output=tmp_path/'existing'
    output.mkdir()
    marker=output/'keep'
    marker.write_text('owner file')
    with pytest.raises(FileExistsError):prepare.prepare(output,tmp_path/'home',MCP_URL)
    assert marker.read_text()=='owner file' and list(output.iterdir())==[marker]


@pytest.mark.parametrize('directory',['.agents/skills/prepared','.codex/new','.hermes/new','.local/bin/staging','.config/ashare/new'])
def test_prepare_refuses_auto_loaded_or_install_locations(tmp_path,directory):
    home=tmp_path/'home'
    home.mkdir()
    with pytest.raises(ValueError):prepare.prepare(home/directory,home,MCP_URL)
    assert list(home.iterdir())==[]


def test_owner_distribution_and_relative_paths_refused(tmp_path):
    for output,home,role in [(tmp_path/'out',tmp_path/'home','owner'),(Path('relative'),tmp_path,'assistant'),
                             (tmp_path/'out',Path('relative'),'assistant')]:
        with pytest.raises(ValueError):prepare.prepare(output,home,MCP_URL,role)


@pytest.mark.parametrize('url', ['http://research.example/mcp', 'https://research.example/tools',
                                 'https://research.example/mcp?key=secret'])
def test_invalid_endpoint_is_rejected_before_writing_bundle(tmp_path, url):
    with pytest.raises(ValueError, match='mcp-url'):
        prepare.prepare(tmp_path/'out', tmp_path/'home', url)
    assert not (tmp_path/'out').exists()
