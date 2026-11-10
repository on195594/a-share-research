#!/usr/bin/env python3
"""Weekly Hermes no-agent scoring; stdout is delivered by Hermes, silence means no change."""
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONTAINER = 'a-share-research-worker'
CACHE = '/tmp/a-share-scoring'
STATE = Path.home() / '.local/state/a-share-scoring/state.json'
HORIZONS = ('20d', '60d', '120d')


def signature(name, data):
    """Only mature counts belong here: pending samples and live prices cannot notify."""
    if name == 'V0':
        counts = {}
        for row in data['rows']:
            for horizon in HORIZONS:
                if horizon in row['returns']:
                    source = counts.setdefault(row['source'], dict.fromkeys(HORIZONS, 0))
                    source[horizon] += 1
        return counts
    if name == 'V1':
        return {key: data['summary'][key] for key in ('scored', 'compared')}
    if name == 'H1':
        counts = dict.fromkeys(('3m', '6m', '12m'), 0)
        for group in data['summary']['h1']:
            if group['horizon'] in counts:
                counts[group['horizon']] = group['matured_batches']
        return counts
    if name == 'H3':
        return {g['direction']: {k: g[k] for k in ('matured', 'eligible')}
                for g in data['summary']['h3']}
    if name == 'A3':
        counts = {}
        for group in data['summary']:
            if group['horizon'] in HORIZONS and group['n']:
                rule = counts.setdefault(group['rule'], dict.fromkeys(HORIZONS, 0))
                rule[group['horizon']] += group['n']
        return counts
    raise ValueError('unknown scorer')


def changed(previous, current):
    return [name for name in current if previous is None or previous.get(name) != current[name]]


def pct(value, signed=False):
    return '—' if value is None else (f'{value:+.1%}' if signed else f'{value:.0%}')


def digest(data, previous, day):
    current = {name: signature(name, value) for name, value in data.items()}
    names = changed(previous, current)
    if not names:
        return ''
    lines = [f'【判断记分周报】{day}']
    if 'V0' in names:
        counts = current['V0']
        for source in sorted(counts):
            horizon = next(h for h in reversed(HORIZONS) if counts[source][h])
            groups = [g for g in data['V0']['summary']['verdict']
                      if g['source'] == source and g['horizon'] == horizon]
            parts = [f"{g['verdict']} n={g['n']} 均值{pct(g['mean'], True)} "
                     f"跑赢{pct(g['beat'])} 左尾≤−30% {pct(g['left_tail'])}" for g in groups]
            lines.append(f'V0 {source} {horizon}：' + '；'.join(parts))
        if not counts:
            lines.append('V0：暂无到期样本')
    if 'V1' in names:
        s = data['V1']['summary']
        hits = sum(g['n'] * g['hit'] for g in s['by_confidence'].values())
        hit = hits / s['scored'] if s['scored'] else None
        lines.append(f"V1：兑现 n={s['scored']} 命中{pct(hit)}；胜过朴素{pct(s['beat_baseline'])}（n={s['compared']}）")
    if 'H1' in names:
        g = next((g for g in data['H1']['summary']['h1'] if g['horizon'] == '6m'), None)
        k = current['H1']['6m']
        b = g['mean'] if g else None
        label = '描述读数'
        if k == 12:
            label = '正式检验（第12批，仅一次）'
        elif k > 12:
            label = '扩样描述；正式检验仅第12批一次'
        value = '—' if b is None else f'{b * 100:+.2f}pp'
        lines.append(f'H1 6m：成熟批次 {k}/12，B={value}（逐批 BOTTOM−TOP 差的均值）；{label}；'
                     '冻结阈值 B < −6.09pp 通过，B ≥ 0 否定，其间证据不足')
        auxiliary = current['H1']
        lines.append(f"H1 辅助：3m {auxiliary['3m']} 批，12m {auxiliary['12m']} 批")
    if 'H3' in names:
        for g in data['H3']['summary']['h3']:
            lines.append(f"H3 {g['direction']}：一致率{pct(g['consistency'])}（有效 n={g['eligible']}，到期 {g['matured']}）")
    if 'A3' in names:
        parts = [rule + ' ' + '/'.join(f'{h}={counts[h]}' for h in HORIZONS)
                 for rule, counts in sorted(current['A3'].items())]
        lines.append('A3：可记分观测 ' + ('；'.join(parts) if parts else '暂无') + '（按规则分别计数，非独立样本）')
    lines.append('仅描述样本；正式判据见 judgment-validation.md')
    return '\n'.join(lines)


def command(args):
    return subprocess.run(['docker', 'exec', *args], check=True, capture_output=True,
                          text=True, timeout=20 * 60).stdout


def score(name, module, flags):
    output = f'{CACHE}/{name}.json'
    # Separate cache dirs: the scorers run concurrently and must not share cache writes.
    command(['-e', f'DATA_DIR={CACHE}/{name}', CONTAINER, 'python', '-m',
             f'a_share_research.{module}', '--database', '/data/app.db', *flags, '--json', output])
    return json.loads(command([CONTAINER, 'cat', output]))


def save_state(current):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates 0600 even when replacing an older, less restrictive file.
    fd, path = tempfile.mkstemp(prefix='.state-', dir=STATE.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(current, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, STATE)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def main():
    stage = 'state'
    try:
        previous = json.loads(STATE.read_text()) if STATE.exists() else None
        if previous is not None and not isinstance(previous, dict):
            raise ValueError('state must be an object')
        stage = '初始化'
        stages = (('V0', 'validation', []), ('V1', 'validation', ['--forecasts']),
                  ('H1/H3', 'validation', ['--hypotheses']), ('A3', 'zones', []))
        command([CONTAINER, 'mkdir', '-p', *(f"{CACHE}/{s.replace('/', '-')}" for s, _, _ in stages)])
        # Hermes kills cron scripts after cron.script_timeout_seconds (600 s since 11-02); parallel keeps it near 3 min.
        with ThreadPoolExecutor(len(stages)) as pool:
            futures = [pool.submit(score, s.replace('/', '-'), module, flags) for s, module, flags in stages]
        data = {}
        for (stage, _, _), future in zip(stages, futures):
            result = future.result()
            for name in (('H1', 'H3') if stage == 'H1/H3' else (stage,)):
                data[name] = result
                signature(name, result)
            # Validate each output before saving any state; errors retain the scorer name.
            selected = ('H1', 'H3') if stage == 'H1/H3' else (stage,)
            digest({name: data[name] for name in selected}, None, '校验')
        stage = 'digest'
        text = digest(data, previous, datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
        current = {name: signature(name, value) for name, value in data.items()}
        stage = 'state'
        if text:
            save_state(current)
            print(text)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = '命令超时（20分钟）'
        elif isinstance(exc, subprocess.CalledProcessError):
            reason = f'命令退出码 {exc.returncode}'
        elif isinstance(exc, FileNotFoundError):
            reason = '找不到命令或文件'
        elif isinstance(exc, OSError):
            reason = f'系统读写失败（errno={exc.errno}）'
        else:
            reason = 'JSON 格式或字段无效'
        print(f'【判断记分】任务未完成：{stage} {reason}')


if __name__ == '__main__':
    main()
