#!/usr/bin/env python3
"""Weekly Hermes no-agent scoring; stdout is delivered by Hermes, silence means no change."""
import json
import fcntl
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONTAINER = 'a-share-research-worker'
CACHE = '/tmp/a-share-scoring'
STATE = Path.home() / '.local/state/a-share-scoring/state.json'
HORIZONS = ('20d', '60d', '120d')
RUN_SECONDS = 540
CLEANUP_MARGIN = 10
OUTPUT_ARGUMENT = '__ASHARE_SCORING_OUTPUT__'

# Runs inside the container, so killing the host docker CLI cannot orphan an
# unbounded scorer. Stdlib only: also usable with prior compatible worker images.
SUPERVISOR = r'''
import ctypes, json, math, os, signal, subprocess, sys, tempfile, time
from pathlib import Path

deadline = float(sys.argv[1])
if not math.isfinite(deadline) or deadline <= time.time():
    sys.exit(124)
# Adopt forked descendants so cleanup can reap them, even when the scorer exits first.
if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
    sys.exit(125)

cancelled = False

def cancel(signum, frame):
    global cancelled
    cancelled = True

def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass

def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False

def stop(process):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal_group(process, signal.SIGTERM)
    grace = time.monotonic() + 3
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # Do not reap Popen's own child behind its back.
    if process.poll() is not None:
        while time.monotonic() < grace:
            if reap():
                return
            time.sleep(.01)
    signal_group(process, signal.SIGKILL)
    process.wait(timeout=2)
    end = time.monotonic() + 2
    while time.monotonic() < end:
        if reap():
            return
        time.sleep(.01)
    raise RuntimeError('descendant cleanup incomplete')

signal.signal(signal.SIGTERM, cancel)
signal.signal(signal.SIGINT, cancel)
status = 125
try:
    with tempfile.TemporaryDirectory(prefix='ashare-score-') as folder:
        directory = Path(folder)
        output = directory / 'result.json'
        if os.environ.get('DATA_DIR'):
            Path(os.environ['DATA_DIR']).mkdir(mode=0o700, parents=True, exist_ok=True)
        command = [str(output) if arg == '__ASHARE_SCORING_OUTPUT__' else arg for arg in sys.argv[2:]]
        remaining = deadline - time.time()
        if remaining <= 0 or cancelled:
            sys.exit(124)
        end = time.monotonic() + remaining
        process = subprocess.Popen(command,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            while True:
                remaining = end - time.monotonic()
                if cancelled or remaining <= 0:
                    status = 124
                    break
                try:
                    status = process.wait(timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            stop(process)
        if status == 0:
            # No separate cat request, and no previous invocation's output can be reused.
            data = json.loads(output.read_text())
            print(json.dumps(data, ensure_ascii=False, allow_nan=False))
except Exception:
    status = 125
sys.exit(status if status >= 0 else 128 - status)
'''


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


def score(name, module, flags, wall_deadline, monotonic_deadline):
    remaining = monotonic_deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(name, RUN_SECONDS)
    result = subprocess.run(['docker', 'exec', '-e', f'DATA_DIR={CACHE}/{name}',
        CONTAINER, 'python', '-c', SUPERVISOR, str(wall_deadline),
        'python', '-m', f'a_share_research.{module}', '--database', '/data/app.db', *flags,
        '--json', OUTPUT_ARGUMENT], capture_output=True, text=True, timeout=remaining + CLEANUP_MARGIN)
    if result.returncode == 124:
        raise subprocess.TimeoutExpired(name, RUN_SECONDS)
    result.check_returncode()
    return json.loads(result.stdout)


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


def run():
    stage = 'state'
    wall_deadline, monotonic_deadline = time.time() + RUN_SECONDS, time.monotonic() + RUN_SECONDS
    try:
        previous = json.loads(STATE.read_text()) if STATE.exists() else None
        if previous is not None and not isinstance(previous, dict):
            raise ValueError('state must be an object')
        stages = (('V0', 'validation', []), ('V1', 'validation', ['--forecasts']),
                  ('H1/H3', 'validation', ['--hypotheses']), ('A3', 'zones', []))
        # Every cohort shares one deadline; queued work cannot restart its budget.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(score, s.replace('/', '-'), module, flags, wall_deadline, monotonic_deadline)
                       for s, module, flags in stages]
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
            reason = '整次记分超过 540 秒预算；原通知状态未推进'
        elif isinstance(exc, subprocess.CalledProcessError):
            reason = f'命令退出码 {exc.returncode}'
        elif isinstance(exc, FileNotFoundError):
            reason = '找不到命令或文件'
        elif isinstance(exc, OSError):
            reason = f'系统读写失败（errno={exc.errno}）'
        else:
            reason = 'JSON 格式或字段无效'
        print(f'【判断记分】任务未完成：{stage} {reason}')


def main():
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(STATE.with_suffix('.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # Another invocation owns the caches and notification state.
            run()
    except OSError as exc:
        print(f'【判断记分】任务未完成：状态锁系统读写失败（errno={exc.errno}）')


if __name__ == '__main__':
    main()
