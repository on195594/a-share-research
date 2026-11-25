"""The checked-in pi model configuration and the identity frozen at research start."""

import hashlib
import json
import os
import platform
from functools import cache
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from a_share_research import core
from a_share_research.contract import JUDGMENT_CONTRACT

PROFILE_PATH = Path(__file__).with_name("model_profile.json")
Role = Literal["author", "reviewer"]
Stage = Literal["explore", "compose", "repair", "review", "q20_select", "q20_rank", "s10_select"]
Text = Annotated[str, Field(min_length=1)]


class ProfileFields(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Model(ProfileFields):
    provider: Text
    model: Text
    reasoning: Text
    max_tokens: PositiveInt


class Limits(ProfileFields):
    model_calls: PositiveInt
    monthly_model_calls: PositiveInt
    run_seconds: PositiveInt
    model_call_seconds: PositiveInt
    acquisition_seconds: PositiveInt
    acquisition_rounds: PositiveInt
    fetch_calls: PositiveInt
    explore_tool_calls: PositiveInt
    repair_extra_tool_calls: PositiveInt
    draft_call_reserve: PositiveInt
    no_progress_rounds: PositiveInt
    review_rounds: PositiveInt
    review_format_corrections: int = Field(ge=0)
    full_rewrites_per_invalid_patch: int = Field(ge=0)
    comparison_attempts_per_task: PositiveInt
    monthly_batch_model_calls: PositiveInt


class ModelProfile(ProfileFields):
    roles: dict[Role, Model]
    role_for: dict[Stage, Role]
    limits: Limits
    rationale: dict[str, Text]

    @model_validator(mode="after")
    def check_keys(self):
        for name, actual, required in (
            ("roles", self.roles, get_args(Role)), ("role_for", self.role_for, get_args(Stage)),
            ("rationale", self.rationale, Limits.model_fields),
        ):
            if difference := set(required) ^ actual.keys():
                raise ValueError(f"{name}: missing or unexpected keys: {sorted(difference)}")
        return self


def load_profile(path: Path = PROFILE_PATH) -> tuple[ModelProfile, str]:
    raw = path.read_bytes()
    return ModelProfile.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


PROFILE, PROFILE_SHA256 = load_profile()


def model_for(stage: Stage) -> Model:
    return PROFILE.roles[PROFILE.role_for[stage]]


def judgment_identity(frozen: dict | None = None) -> dict:
    """Project the original run/batch identity when publishing or resuming a batch."""
    if frozen is not None:
        return {key: frozen[key] for key in ("contract", "profile_sha256", "author", "reviewer")}
    return {"contract": JUDGMENT_CONTRACT, "profile_sha256": PROFILE_SHA256,
            "author": model_for("explore").model_dump(), "reviewer": model_for("review").model_dump()}


@cache
def _application_sha256() -> str:
    package = PROFILE_PATH.parent
    runner = package.parent / "agent-runner"
    files = {PROFILE_PATH.name: PROFILE_SHA256}
    for directory, extensions in ((package, {".py", ".mjs", ".html", ".css", ".j2"}),
                                  (runner, {".ts", ".js", ".mjs", ".json"})):
        for path in directory.rglob("*"):
            if path.suffix in extensions and path.is_file():
                files[str(path.relative_to(package.parent))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def application_identity() -> dict:
    return {"application_sha256": _application_sha256(), "a_stock_lib_version": core.__version__,
            "python_version": platform.python_version(), "code_commit": os.environ.get("GIT_COMMIT") or None,
            **judgment_identity()}


def run_identity(effective_model_call_limit: int) -> dict:
    return application_identity() | {"effective_model_call_limit": effective_model_call_limit}
