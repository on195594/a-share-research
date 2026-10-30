"""Shared autoescaped template environment for offline rendering checks."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from a_share_research import reading
from a_share_research.evidence import report_source_view, citation_link

TEMPLATES = Path(__file__).resolve().parents[2] / "a_share_research/templates"
ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
ENV.globals["asset_version"] = "test"
ENV.globals.update(report_source_view=report_source_view, citation_link=citation_link)


def render(name: str, **context) -> str:
    if name == 'report.html':
        context['evidence'] = reading.evidence_context(context['body'], context['report'].get('period'))
    return ENV.get_template(name).render(csrf="", steps={}, request_id="r", **context)

