"""Load Hermes scripts from the current checkout."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ops/hermes"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
