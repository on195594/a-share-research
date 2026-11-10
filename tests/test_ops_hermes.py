"""Hermes scripts: real plan rows and actual script copies, without host access."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import timedelta

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ops/hermes"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    row = rows[0]
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
def installer_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(SOURCE, repo / "ops/hermes", ignore=shutil.ignore_patterns("__pycache__"))

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
