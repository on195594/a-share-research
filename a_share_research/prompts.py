"""Render the model-facing templates and hash their model-visible contents."""

import hashlib
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


def _strip_jinja_comments(text: str, name: str) -> str:
    return "".join(
        value for _, token, value in _ENV.lexer.tokeniter(text, name=name)
        if token not in ("comment_begin", "comment", "comment_end")
    )


def prompts_sha256() -> str:
    """Hash the sorted relative paths and model-visible contents (excluding Jinja comments) of the prompt templates."""
    digest = hashlib.sha256()
    for path in sorted(_PROMPTS.rglob("*")):
        if path.is_file():
            relative = path.relative_to(_PROMPTS).as_posix()
            digest.update(relative.encode() + b"\0")
            stripped = _strip_jinja_comments(path.read_text(encoding="utf-8"), relative)
            digest.update(stripped.encode("utf-8") + b"\0")
    return digest.hexdigest()


def schema_facts(material: "Material") -> dict:
    facts = {key: bool(material.extra.get(key)) for key in ("question", "feedback", "prior", "events")}
    if not facts["feedback"]:
        base = forecasts.base_period(material.table)
        facts.update(years=valuation.years(material.table), next_period=forecasts.next_period(base) if base else None)
    return facts


def output_schema(material: "Material") -> str:
    return render_prompt("output_schema.j2", **schema_facts(material))
