#!/usr/bin/env python3
"""Prepare a new review bundle; never install clients, credentials, or host configuration."""
import argparse
import hashlib
import json
from pathlib import Path


ROOT=Path(__file__).resolve().parents[2]
HOSTS=('claude','codex','hermes','pi','agy')


def prepare(output: Path, home: Path, role: str='assistant') -> dict:
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
    profiles={host:home/f'.config/ashare/{host}-{role}.json' for host in HOSTS}
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
    def stdio(host):
        args=['--config',str(profiles[host]),'mcp']
        if host=='agy':args.append('--portable-names')
        return dict(command=str(client),args=args)
    write('bin/ashare',(ROOT/'scripts/ashare.py').read_bytes(),executable=True)
    write('skill/a-share/SKILL.md',(ROOT/'skill/a-share/SKILL.md').read_bytes())
    # Claude's per-project disabled state is in ~/.claude.json, not a supported
    # per-server stdio field. This standalone file is deliberately not installed.
    dump('configs/claude.mcp.json',{'mcpServers':{'ashare':dict(type='stdio',**stdio('claude'))}})
    settings=stdio('codex')
    write('configs/codex.config.toml','[mcp_servers.ashare]\n'
          f'command = {json.dumps(settings["command"],ensure_ascii=False)}\n'
          f'args = {json.dumps(settings["args"],ensure_ascii=False)}\n'
          'enabled = false\nstartup_timeout_sec = 10\ntool_timeout_sec = 40\n')
    dump('configs/hermes.config.json',{'mcp_servers':{'ashare':dict(**stdio('hermes'),enabled=False,connect_timeout=10,timeout=40)}})
    dump('configs/pi.mcp.json',{'mcpServers':{'ashare':dict(**stdio('pi'),enabled=False,timeout=40,exposure='direct')}})
    dump('configs/agy.mcp_config.json',{'mcpServers':{'ashare':dict(**stdio('agy'),disabled=True)}})
    dump('credentials.example.json',{'url':'http://127.0.0.1:8621','token':''})
    manifest=dict(schema_version=1,status='prepared-only',role=role,client_destination=str(client),
        credential_destinations={host:str(path) for host,path in profiles.items()},
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
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--home',type=Path,default=Path.home())
    parser.add_argument('--role',choices=('assistant','research'),default='assistant')
    args=parser.parse_args()
    try:
        result=prepare(args.output,args.home,args.role)
    except (OSError,ValueError) as exc:
        parser.exit(1,f'准备失败：{exc}\n')
    print(json.dumps(dict(status=result['status'],output=str(args.output),role=args.role),ensure_ascii=False))


if __name__=='__main__':
    main()
