"""Captured filing pages and citation mutations shared by both number-check paths."""
import json
from pathlib import Path

from a_share_research.material import Material
from support.reports import MATERIAL

DATA = json.loads((Path(__file__).parents[1] / 'data/split-number-pages.json').read_text())
RUN = DATA['run154']
CITE = next(c for c in RUN['cites'] if c.get('source') == 'D2' and c.get('page') == 171)


def material():
    result = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    result.extra.setdefault('sources', {})['D2'] = {
        'pages': {171: RUN['layout']}, 'raw_pages': {171: RUN['raw']}}
    result.extra['sources']['D3'] = {
        'pages': DATA['pages']['layout'], 'raw_pages': DATA['pages']['raw']}
    return result


def mutations():
    # Cross-column mutations use real quoted halves that are not adjacent on the page.
    for value in ('2682208991.66', '2682208990.00', '2740768581.65'):
        yield CITE, value
    yield CITE | {'quote': CITE['quote'].splitlines()[0]}, '2682208991.65'
    yield CITE | {'quote': CITE['quote'].splitlines()[-1]}, '2682208991.65'
    yield CITE | {'page': 7, 'source': 'D3'}, '2682208991.65'
    # The tail 9 occurs within the head's longer numeric token, not as a quoted token.
    sample = DATA['samples'][2]
    assert sample['head'] == '1,331,014,793.7' and sample['tail'] == '9'
    yield {'source': 'D3', 'page': sample['page'], 'quote': sample['head']}, '1331014793.79'
