#!/usr/bin/env python3
"""Checklist presentation metadata and registry container.

ChecklistDefinition and ChecklistItem remain type-only imports so this module
never creates a runtime import cycle with checklist.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from a_share_research.policy.v0.checklist import ChecklistDefinition, ChecklistItem


@dataclass(frozen=True)
class SkippedChecklistItem:
    label: str
    reason: str


@dataclass(frozen=True)
class FrameworkMetadata:
    key: str
    checklist_name: str
    subjective_items: list[str]
    skipped_items: list[SkippedChecklistItem]
    checklist_definitions: list["ChecklistDefinition"]
    custom_builder: Callable[[dict], "ChecklistItem"] | None = None


FrameworkRegistry = dict[str, FrameworkMetadata]

FRAMEWORK_REGISTRY: FrameworkRegistry = {}
