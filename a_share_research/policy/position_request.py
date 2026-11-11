"""Strict, JSON-only proposal and saved-monitor reference boundary."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    code: str = Field(pattern=r'^[0-9]{6}$')
    price: float = Field(gt=0)
    framework: Literal['A', 'B', 'C', 'D', 'E', 'F']
    cash: float | None = Field(default=None, ge=0)


class Reference(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    id: int = Field(gt=0)
    input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    result_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


def validate_proposal(value: dict) -> dict:
    return Proposal.model_validate(value).model_dump(mode='json')
