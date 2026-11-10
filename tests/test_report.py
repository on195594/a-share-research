"""Stored-body compatibility using captured production reports and disclosed source excerpts."""
import gzip
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from a_share_research import report
from tests.support.reports import report as source_report

DATA = Path(__file__).parent / 'data'


def captured_bodies():
    """Find complete bodies and explicitly reduced body fixtures without filling missing facts."""
    def walk(value, location):
        if isinstance(value, str) and location.rsplit('/', 1)[-1] == 'body':
            yield from walk(json.loads(value), location)
        elif isinstance(value, dict):
            is_body = ('body' == location.rsplit('/', 1)[-1]
                       or ('risks' in value and ('summary' in value or location.endswith('/report')))
                       or ('failure' in value and 'problems' in value and 'code' in value))
            if is_body:
                yield location, value
                return
            for key, item in value.items():
                yield from walk(item, f'{location}/{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, f'{location}/{index}')

    for path in sorted(DATA.glob('*')):
        if path.suffix not in ('.json', '.gz'):
            continue
        text = gzip.open(path, 'rt').read() if path.suffix == '.gz' else path.read_text()
        yield from walk(json.loads(text), path.name)
    # This builder contains real 贵州茅台 disclosure excerpts; it is a model-output
    # fixture, hence identity and runtime metadata are intentionally absent.
    yield 'tests/support/reports.py', source_report()


BODIES = list(captured_bodies())


@pytest.mark.parametrize('location,body', BODIES, ids=[item[0] for item in BODIES])
def test_captured_report_bodies(location, body):
    loaded = report.load(json.dumps(body, ensure_ascii=False))
    assert loaded == body | {'schema_version': body.get('schema_version', 1)}
    assert 'judgment' not in loaded or loaded['judgment'] == body['judgment']
    report.validate(loaded)


def test_top_level_and_required_core_are_checked():
    body = report.load(json.dumps(BODIES[0][1]))
    with pytest.raises(ValidationError, match='undeclared'):
        report.validate(body | {'undeclared': True})
    del body['schema_version']
    with pytest.raises(ValidationError, match='schema_version'):
        report.validate(body)


def test_current_report_requires_identity_and_complete_judgment():
    body = report.load(json.dumps(BODIES[0][1])) | {'schema_version': 2}
    with pytest.raises(ValidationError, match='judgment'):
        report.validate(body)
    schema = report.json_schema()
    assert set(schema['required']) == {'schema_version', 'code', 'name', 'period', 'judgment'}
    assert schema['additionalProperties'] is False
    judgment = schema['$defs']['Judgment']
    assert set(judgment['required']) == {'contract', 'surface_sha256', 'author', 'reviewer'}


def test_projections_are_declared_and_compact_exposes_judgment():
    for fields in (report.PUBLISHED_REQUIRED_FIELDS, report.PUBLISHED_FIELDS, report.COMPACT_FIELDS,
                   report.FAILED_FIELDS, report.CARD_FIELDS):
        assert set(fields) <= report.Report.__annotations__.keys()
    assert 'judgment' in report.COMPACT_FIELDS
    assert 'relative' in report.Report.__annotations__


def test_loading_does_not_validate_or_change_existing_fields():
    # Deliberately malformed historical data is read unchanged; validation belongs
    # at the write boundary and must not break existing API reads.
    body = BODIES[0][1] | {'unknown_historical_field': 'preserved'}
    assert report.load(json.dumps(body)) == body | {'schema_version': 1}
