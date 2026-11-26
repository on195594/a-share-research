#!/usr/bin/env python3
"""Prepare a new review bundle; never install clients, credentials, or host configuration."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


ROOT=Path(__file__).resolve().parents[2]
HOSTS=('claude','codex','hermes','pi','agy')


def prepare(output: Path, home: Path, mcp_url: str, role: str='assistant') -> dict:
    if (not re.fullmatch(r'https://(?:[A-Za-z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]+)?/mcp', mcp_url)
            or urlsplit(mcp_url).port == 0):
        raise ValueError('--mcp-url 须为完整的 HTTPS /mcp 地址')
    if role not in ('research','assistant'):
        raise ValueError('宿主客户端只准备 research 或 assistant，不分发 owner 凭据')
    if not home.is_absolute() or not output.is_absolute():
        raise ValueError('home 与 output 须为绝对路径')
    output=output.resolve()
    home=home.resolve()
    if any(output.is_relative_to(home/path) for path in
           ('.agents','.claude','.codex','.pi','.hermes','.gemini','.local/bin','.config/ashare')):
        raise ValueError('准备目录不能位于宿主自动加载或客户端安装目录')
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    client=home/'.local/bin/ashare'
    hashes={}
    def write(relative, content, executable=False):
        path=output/relative
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        data=content if isinstance(content,bytes) else content.encode()
        with path.open('xb') as stream:stream.write(data)
        path.chmod(0o700 if executable else 0o600)
        hashes[relative]=hashlib.sha256(data).hexdigest()
    def dump(relative,value):
        write(relative,json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    token_vars = {host: f'ASHARE_{host.upper()}_{role.upper()}_TOKEN' for host in HOSTS}

    def http(host, **options):
        return dict(url=mcp_url, headers={'Authorization': 'Bearer ${' + token_vars[host] + '}'}, **options)

    write('bin/ashare',(ROOT/'scripts/ashare.py').read_bytes(),executable=True)
    write('skill/a-share/SKILL.md',(ROOT/'skill/a-share/SKILL.md').read_bytes())
    dump('configs/claude.mcp.json', {'mcpServers': {'ashare': http('claude', type='http')}})
    write('configs/codex.config.toml','[mcp_servers.ashare]\n'
          f'url = "{mcp_url}"\n'
          f'bearer_token_env_var = "{token_vars["codex"]}"\n'
          'enabled = false\nstartup_timeout_sec = 10\ntool_timeout_sec = 40\n')
    dump('configs/hermes.config.json', {'mcp_servers': {'ashare': http('hermes', enabled=False, connect_timeout=10, timeout=40)}})
    # Pi declares only the role's entry tools to the model; the rest stay reachable through codemode.
    direct = (('research_card', 'research_company_overview', 'account_portfolio_overview', 'policy_current')
              if role == 'assistant' else ('research_card', 'research_search'))
    dump('configs/pi.mcp.json', {'mcpServers': {'ashare': http(
        'pi', enabled=False, timeout=40, exposure='codemode', toolExposure=dict.fromkeys(direct, 'direct'))}})
    # AGY substitutes plugin setup variables in headers, not process environment variables.
    dump('configs/agy/ashare/plugin.json', {
        'name': 'ashare', 'displayName': 'A 股研究', 'version': '1.0.0', 'description': '连接 A 股研究 MCP 服务。',
    })
    dump('configs/agy/ashare/mcp_config.json', {'mcpServers': {'ashare': http('agy', variables=[{
        'name': token_vars['agy'], 'title': 'A 股研究 PAT', 'description': '为 AGY 单独签发的访问令牌。',
    }])}})
    dump('credentials.example.json', {'url': mcp_url, 'token': ''})
    manifest=dict(schema_version=1,status='prepared-only',role=role,client_destination=str(client),
        cli_config_destination=str(home/f'.config/ashare/hermes-{role}.json'),
        skill_destinations={
            'claude':str(home/'.claude/skills/a-share'),
            'codex':str(home/'.agents/skills/a-share'),
            'hermes':str(home/'.hermes/skills/a-share'),
            'pi':str(home/'.agents/skills/a-share'),
            'agy':str(home/'.gemini/config/skills/a-share')},
        files=hashes.copy())
    dump('manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mcp-url',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--home',type=Path,default=Path.home())
    parser.add_argument('--role',choices=('assistant','research'),default='assistant')
    args=parser.parse_args()
    try:
        result=prepare(args.output,args.home,args.mcp_url,args.role)
    except (OSError,ValueError) as exc:
        parser.exit(1,f'准备失败：{exc}\n')
    print(json.dumps(dict(status=result['status'],output=str(args.output),role=args.role),ensure_ascii=False))


if __name__=='__main__':
    main()
