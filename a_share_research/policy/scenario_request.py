"""Strict explicit sale quantities and costs for a static portfolio scenario."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Code = Annotated[str, Field(pattern=r'^[0-9]{6}$')]
Quantity = Annotated[int, Field(strict=True, gt=0)]


class Proposal(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    sales: dict[Code, Quantity] = Field(min_length=1)
    fees: float = Field(ge=0)
    cash: float | None = Field(default=None, ge=0)


def validate_proposal(value: dict) -> dict:
    return Proposal.model_validate(value).model_dump(mode='json')
