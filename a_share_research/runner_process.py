"""Worker-owned lifetime, pipes and failure mapping for the pi agent process."""
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import anyio

from a_share_research.model_profile import PROFILE, PROFILE_PATH
from a_share_research.research_mcp import ERROR_KINDS, ResearchSession, serve_stdio

MAIN = Path(__file__).resolve().parents[1] / "agent-runner" / "main.ts"


class RunnerError(RuntimeError):
    def __init__(self, category: str, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.category, self.kind = category, kind


def _command(*args: str) -> list[str]:
    return ["node", str(MAIN), *args, "--profile", str(PROFILE_PATH)]


def _env() -> dict[str, str]:
    # The worker also has market and service credentials. Pass only pi's environment.
    names = {"PATH", "HOME", "PI_AGENT_DIR", "PI_RUNTIME", "PI_EXTENSION",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}
    for role in PROFILE.roles.values():
        names.add(role.provider.upper().replace("-", "_") + "_API_KEY")
    return {key: os.environ[key] for key in names if key in os.environ} | {"PI_OFFLINE": "1"}


@contextmanager
def time_left(deadline: float, *, terminate: Callable[[], None] | None = None):
    """Interrupt synchronous provider I/O as well as pipe waits in the single-threaded Linux worker."""
    # Provider adapters catch Exception to report correctable fetch failures. A run deadline
    # must unwind those frames, including any MCP task group, before classifying the run.
    class Expired(BaseException):
        pass

    expired = False

    def expire(*_):
        nonlocal expired
        expired = True
        if terminate is not None:
            terminate()
        raise Expired()

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            expire()
        signal.setitimer(signal.ITIMER_REAL, remaining)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if expired:
            raise RunnerError("run_limit", "研究超过整体时限，已保存取得的材料") from None


def _check_exit(code: int, model_error: str | None = None):
    if code == 0:
        return
    if code in (2, 3):
        kind = model_error or "pi_runtime"
        raise RunnerError("model_error", f"研究阶段未能完成（{kind}）；请核对模型配置与调用记录。", kind=kind)
    if code == 4:
        raise RunnerError("run_limit", "模型调用次数或研究时限已达上限")
    raise RunnerError("pipeline_error", f"runner 通信中断（退出码 {code}）；请核对阶段记录。")


def run_research(session: ResearchSession, *, replay_of: int | None = None) -> None:
    args = ["run", "--run-id", str(session.run_id)]
    if replay_of is not None:
        args.extend(["--replay", str(replay_of)])
    with subprocess.Popen(_command(*args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=_env()) as proc:
        async def serve():
            await serve_stdio(session, proc.stdout, proc.stdin, terminate=proc.kill)

        try:
            with time_left(session.deadline, terminate=proc.kill):
                anyio.run(serve)
                _check_exit(proc.wait(), session.last_error_kind)
        finally:
            # Popen.__exit__ closes both pipes and waits, including after protocol failure.
            if proc.poll() is None:
                proc.kill()


@contextmanager
def single_batch(batch_id: int) -> Iterator[Callable[[str, int, str], dict]]:
    with subprocess.Popen(_command("single", "--batch-id", str(batch_id)), env=_env(),
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1) as proc:
        broken = False

        def request(task: str, attempt: int, prompt: str) -> dict:
            nonlocal broken
            # Allow for startup and the pipe transfer in addition to the runner's model timer.
            try:
                with time_left(time.monotonic() + PROFILE.limits.model_call_seconds + 30, terminate=proc.kill):
                    proc.stdin.write(json.dumps(dict(task=task, attempt=attempt, prompt=prompt), ensure_ascii=False) + "\n")
                    line = proc.stdout.readline()
                    if not line:
                        _check_exit(proc.wait())
                        raise RunnerError("pipeline_error", "批次 runner 未返回比较结果")
                    reply = json.loads(line)
                    valid_output = (reply["error_kind"] in ERROR_KINDS if "error_kind" in reply
                                    else isinstance(reply["text"], str))
                    if (reply["task"] != task or reply["attempt"] != attempt or not valid_output
                            or not isinstance(reply["identity"], dict) or not isinstance(reply["usage"], dict | None)
                            or not isinstance(reply["seconds"], (int, float))):
                        raise ValueError("invalid completion")
                    return reply
            except RunnerError:
                broken = True
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                broken = True
                raise RunnerError("pipeline_error", "批次 runner 通信失败或回执与请求不符") from exc

        try:
            yield request
        finally:
            # EOF lets pi finish credential writes, including on a rejected comparison.
            # A broken transport has already been reported to the individual task.
            if broken:
                proc.kill()
            else:
                try:
                    proc.stdin.close()
                    _check_exit(proc.wait(timeout=30))
                finally:
                    if proc.poll() is None:
                        proc.kill()


def preflight() -> dict:
    """Check both configured roles offline before opening the research queue."""
    try:
        proc = subprocess.run(_command("--check"), env=_env(), stdout=subprocess.PIPE, timeout=60)
        result = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError("Pi 启动预检未完成，请检查 Node、pi 安装与模型配置档。") from exc
    if not isinstance(result, dict) or proc.returncode or result.get("ready") is not True:
        reason = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError(f"Pi 启动预检失败：{reason or '运行时未就绪'}")
    return result
