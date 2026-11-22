"""Render the model-facing templates and parse the model's JSON response."""

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from a_share_research import forecasts, valuation

if TYPE_CHECKING:
    from a_share_research.material import Material


_PROMPTS = Path(__file__).with_name("prompts")
_ENV = Environment(loader=FileSystemLoader(_PROMPTS), undefined=StrictUndefined,
                   autoescape=False, keep_trailing_newline=True)


def render_prompt(template: str, **facts) -> str:
    return _ENV.get_template(template).render(**facts)


def prompts_sha256() -> str:
    """Hash the sorted relative paths and raw contents of the prompt templates."""
    digest = hashlib.sha256()
    for path in sorted(_PROMPTS.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(_PROMPTS).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def schema_facts(material: "Material") -> dict:
    facts = {key: bool(material.extra.get(key)) for key in ("question", "feedback", "prior", "events")}
    if not facts["feedback"]:
        base = forecasts.base_period(material.table)
        facts.update(years=valuation.years(material.table), next_period=forecasts.next_period(base) if base else None)
    return facts


def output_schema(material: "Material") -> str:
    return render_prompt("output_schema.j2", **schema_facts(material))


def parse_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
