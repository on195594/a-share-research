"""Lightweight schema for the caller-owned, legacy-compatible stop notification state."""
from datetime import date as calendar_date
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Code = Annotated[str, Field(min_length=1)]
StopLevel = Literal['near', 'stop1', 'stop2']


class StopState(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')

    date: str | None = None
    levels: dict[Code, StopLevel] = Field(default_factory=dict)

    @field_validator('date')
    @classmethod
    def calendar_day(cls, value):
        if value is None or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
            raise ValueError('stop state date must be a calendar date')
        calendar_date.fromisoformat(value)
        return value

    @field_validator('levels')
    @classmethod
    def nonblank_codes(cls, value):
        if any(not code.strip() for code in value):
            raise ValueError('stop state codes must be nonempty')
        return value


def validate_state(value: dict) -> dict:
    """Validate the stored state without inventing omitted dates or notification levels."""
    return StopState.model_validate(value).model_dump(mode='json', exclude_unset=True)
