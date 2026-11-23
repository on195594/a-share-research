"""Hermes scripts: real plan rows and actual script copies, without host access."""

import json
import shutil
import subprocess
import sys
from datetime import timedelta

import pytest
from support.hermes import ROOT, SOURCE, load


@pytest.fixture
def milestones():
    return load("a-share-milestones")


@pytest.fixture
def plan():
    return (ROOT / "docs/implementation-plan.md").read_text()


def test_parse_all_real_rows(milestones, plan):
    table = plan.split("| 步骤 | 到期 | 状态 | 证据 | 备注 |", 1)[1].split("\n\n", 1)[0]
    expected = [line for line in table.splitlines() if line.startswith("| ")]
    rows = milestones.parse_milestones(plan)
    assert len(rows) == len(expected) > 0
    assert [row["name"] for row in rows] == [line.split("|")[1].strip() for line in expected]
    assert all(row["state"] in milestones.STATES for row in rows)


def test_invalid_state_rejected(milestones, plan):
    row = milestones.parse_milestones(plan)[0]
    # Minimal invalid-state mutation of the real plan row.
    invalid = plan.replace(f"| {row['name']} | {row['due']} | {row['state']} |",
                           f"| {row['name']} | {row['due']} | 非法状态 |", 1)
    with pytest.raises(ValueError, match="里程碑行格式不对"):
        milestones.parse_milestones(invalid)


@pytest.mark.parametrize("branch", ["overdue", "soon", "unreleased"])
def test_render_real_rows(milestones, plan, branch):
    rows = milestones.parse_milestones(plan)
    # Delivered stages legitimately leave the overdue/upcoming lists as the plan advances.
    row = rows[0] if branch == "unreleased" else next(item for item in rows if item["state"] != "已交付")
    today = row["due"]
    if branch == "overdue":
        today += timedelta(days=1)
        expected = f"到期 {row['due']}，已逾期 1 天"
    elif branch == "soon":
        today -= timedelta(days=14)
        expected = "14 天内到期"
    else:
        # Real row with only delivery state changed; missing evidence remains visible.
        rows = [dict(item, state="已交付") if item is row else item for item in rows]
        expected = "标已交付但未发布或缺提交号"
    output = milestones.render(rows, today, production=None, released=lambda _: False,
                               counts={"failed": 0, "research": 0})
    assert expected in output
    assert row["name"] in output
    assert output.startswith("里程碑进度 ")
    assert "研究失败 0 个，研究总数 0 个" in output
    assert "生产版本 无法确认" in output
    assert "K7" not in output and "诊断任务" not in output


@pytest.mark.parametrize("name", ["a-share-advisor", "a-share-monthly", "a-share-scoring"])
def test_existing_scripts_import(name, monkeypatch):
    monkeypatch.setattr(sys, "path", sys.path.copy())
    assert callable(load(name).main)


@pytest.fixture
def installer_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("A_SHARE_ADVISOR_STATE", str(tmp_path / "advisor"))
    for job in ("stop-alert", "daily-brief", "benchmark"):
        monkeypatch.setenv("ASHARE_" + job.upper().replace("-", "_") + "_STATE", str(tmp_path / job / "state.json"))
    repo = tmp_path / "repo"
    shutil.copytree(SOURCE, repo / "ops/hermes", ignore=shutil.ignore_patterns("__pycache__"))

    shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("add", "ops/hermes")
    # Commit only the isolated fixture copy, never the working repository.
    git("-c", "user.name=Hermes Test", "-c", "user.email=hermes-test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture scripts")
    return repo, git


def run_install(repo, target, *args):
    return subprocess.run([sys.executable, str(repo / "ops/hermes/install.py"),
                           "--target", str(target), *args], capture_output=True, text=True)


def test_install_copies_dependencies_before_entries(tmp_path, monkeypatch):
    installer = load("install")
    replace = installer.os.replace
    modules = {"advisor_budget.py", "ashare_tools.py", "ashare_schedule.py"}

    def install_file(temporary, dest):
        if dest.name == "ashare_schedule.py":
            assert (dest.parent / "ashare_tools.py").is_file()
        elif dest.name not in modules:
            assert modules <= {item.name for item in dest.parent.iterdir()}
        replace(temporary, dest)

    monkeypatch.setattr(installer.os, "replace", install_file)
    installer.copy_scripts(tmp_path / "installed")


def test_rollback_runbook_lists_all_unconfirmed_runs(policy_setup):
    _, conn, principals, *_ = policy_setup
    for day, status in enumerate(("running", "ready", "failed", "acked", "expired"), 1):
        slot = f"2026-11-{day:02}"
        conn.execute("INSERT INTO scheduled_runs(principal_key_id,job,slot,status,created_at) "
                     "VALUES (?, 'advisor_scan', ?, ?, ?)", (principals["assistant"].key_id, slot, status, slot))

    runbook = (SOURCE / "README.md").read_text().split("## 跨激活 pending 转接", 1)[1]
    sql = runbook.split("```sql\n", 1)[1].split("```", 1)[0]
    rows = conn.execute(sql).fetchall()
    assert rows[0].keys() == ["id", "job", "slot", "status", "scan_id"]
    assert {row["status"] for row in rows} == {"running", "ready", "failed"}
    design = (ROOT / "docs/scheduling-state.md").read_text().split("段 2 必须", 1)[1].split("## 6", 1)[0]
    assert "695a36f" in design


def test_install_lifecycle(installer_repo, tmp_path):
    repo, _ = installer_repo
    target = tmp_path / "target"
    result = run_install(repo, target)
    assert result.returncode == 1 and "缺失" in result.stdout
    assert not target.exists()
    target.mkdir()
    unrelated = target / "unrelated.sh"
    unrelated.write_text("unrelated host script\n")
    unrelated.chmod(0o640)
    before = unrelated.stat()
    installer = load("install")
    for name in installer.OBSOLETE:
        (target / name).write_text("retired script\n")
    result = run_install(repo, target, "--check")
    assert result.returncode == 1 and "待移除" in result.stdout
    assert all((target / name).exists() for name in installer.OBSOLETE)
    result = run_install(repo, target, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("一致 ") == len(installer.scripts())
    assert not any((target / name).exists() for name in installer.OBSOLETE)
    assert unrelated.read_text() == "unrelated host script\n"
    assert (unrelated.stat().st_mode, unrelated.stat().st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    for source in installer.scripts():
        assert installer.fingerprint(source) == installer.fingerprint(target / source.name)
    installed = target / "a-share-milestones.sh"
    installed.write_text(installed.read_text() + "# changed\n")
    result = run_install(repo, target, "--check")
    assert result.returncode == 1 and "不同 a-share-milestones.sh" in result.stdout
    assert run_install(repo, target, "--apply").returncode == 0
    installed.chmod(installed.stat().st_mode ^ 0o111)
    result = run_install(repo, target, "--check")
    assert result.returncode == 1 and "不同 a-share-milestones.sh" in result.stdout


@pytest.mark.parametrize(("version", "message"), [(1, "旧 research key"), (2, "扫描仍有待查请求")])
def test_install_preserves_scripts_while_scan_pending(installer_repo, tmp_path, version, message):
    from datetime import datetime, timezone

    repo, _ = installer_repo
    state = tmp_path / "advisor" / "scan.json"
    state.parent.mkdir()
    pending = dict(request_id="hermes-scan-" + "a" * 32, binding="b" * 64,
                   created_at=datetime.now(timezone.utc).isoformat(), scan_id=7)
    state.write_text(json.dumps(dict(schema_version=version, pending=pending)))
    state.chmod(0o600)
    target = tmp_path / "target"
    result = run_install(repo, target, "--apply")
    assert result.returncode == 1 and message in result.stdout
    assert not target.exists() and json.loads(state.read_text())["pending"] == pending


@pytest.mark.parametrize(("relative", "fields", "args"), [
    ("stop-alert/state.json", {"state": {}}, {"previous_state": {}}),
    ("daily-brief/state.json", {}, {}),
    ("benchmark/state.json", {}, {"end": "2026-11-20"}),
    ("advisor/opportunities.json", {"seen": []}, {}),
    ("advisor/weekly-opportunities.json", {"seen": []}, {}),
])
def test_module_install_preserves_pending_policy(installer_repo, tmp_path, relative, fields, args):
    repo, _ = installer_repo
    state = tmp_path / relative
    state.parent.mkdir()
    pending = {"request_id": "hermes-test", "expected_activation_id": 1, **args}
    original = json.dumps({"schema_version": 1, "pending": pending, **fields})
    state.write_text(original)
    target = tmp_path / "target"
    result = subprocess.run([sys.executable, "-m", "ops.hermes.install", "--apply", "--target", str(target)],
                            cwd=repo, capture_output=True, text=True)
    assert result.returncode == 1 and "原 assistant 身份与旧脚本" in result.stdout
    assert state.read_text() == original and not target.exists()


@pytest.mark.parametrize("change", ["modified", "staged", "untracked"])
def test_apply_refuses_dirty_source(installer_repo, tmp_path, change):
    repo, git = installer_repo
    source = repo / "ops/hermes" / ("extra.sh" if change == "untracked" else "a-share-milestones.sh")
    source.write_text("# uncommitted fixture mutation\n")
    if change == "staged":
        git("add", "ops/hermes")
    target = tmp_path / "not-created"
    result = run_install(repo, target, "--apply")
    assert result.returncode == 1
    assert "未提交改动" in result.stdout
    assert not target.exists()


def test_apply_refuses_ignored_script(installer_repo, tmp_path):
    repo, git = installer_repo
    (repo / ".gitignore").write_text("ops/hermes/*.local.sh\n")
    git("add", ".gitignore")
    git("-c", "user.name=Hermes Test", "-c", "user.email=hermes-test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Add gitignore")
    (repo / "ops/hermes/secret.local.sh").write_text("#!/usr/bin/env bash\n")
    target = tmp_path / "not-created"
    result = run_install(repo, target, "--apply")
    assert result.returncode == 1
    assert "被忽略的脚本文件" in result.stdout
    assert not target.exists()


@pytest.mark.parametrize(
    ("inspect_rc", "inspect_stdout", "expected"),
    [
        (0, "sha256:aaa111\n", "9fd1f6b"),
        (0, "sha256:ccc333\n", None),  # latest tag points elsewhere; running container matches neither
        (0, "\n", None),               # stopped container
        (1, "", None),                 # missing container
    ],
)
def test_production_commit_reads_running_container(milestones, monkeypatch, inspect_rc, inspect_stdout, expected):
    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(cmd, inspect_rc, stdout=inspect_stdout, stderr="")
        if cmd[:3] == ["docker", "image", "ls"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="latest sha256:bbb222\n9fd1f6b sha256:aaa111\n5d97d85 sha256:bbb222\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(milestones.subprocess, "run", fake_run)
    assert milestones.production_commit() == expected


def test_install_waits_for_the_weekly_delivery_lock(installer_repo, tmp_path):
    import fcntl
    repo, _ = installer_repo
    path = tmp_path / 'advisor/weekly.lock'
    path.parent.mkdir()
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = tmp_path / 'target'
        result = run_install(repo, target, '--apply')
    assert result.returncode == 1 and '任务正在运行' in result.stdout and not target.exists()
