"""The resume identity follows the real implementation across module boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from a_share_research import backtest_experiment


def test_factor_module_change_invalidates_experiment_identity(tmp_path):
    package = Path(backtest_experiment.__file__).parent
    copied = tmp_path / package.name
    shutil.copytree(package, copied, ignore=shutil.ignore_patterns("__pycache__"))
    command = [sys.executable, "-c", "import json; from a_share_research.backtest_experiment import implementation_hashes; print(json.dumps(implementation_hashes()))"]
    def identity():
        return json.loads(subprocess.check_output(command, cwd=tmp_path, text=True,
                                                  env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(tmp_path)}))
    before = identity()
    assert "screening_factors.py" in before
    factors = copied / "screening_factors.py"
    factors.write_text(factors.read_text() + "\n# Exercise implementation drift in a disposable source copy.\n")
    after = identity()
    assert before["screening_factors.py"] != after["screening_factors.py"]
    assert all(before[name] == after[name] for name in before if name != "screening_factors.py")
