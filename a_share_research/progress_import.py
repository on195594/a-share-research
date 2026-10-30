"""Import authorized, full research-v1 API exports into a NEW isolated database.

No network, auth copying, production writes, model calls or reconstructed material snapshots.
Original run identity is preserved. The export hash and evidence are additional capture provenance,
not a reconstructed production material snapshot.
"""
import argparse
import hashlib
import json
import logging
from pathlib import Path

from a_share_research import db, progress


def import_exports(database: Path, files: list[Path]):
    exports = []
    seen = set()
    for path in files:
        raw = path.read_bytes()
        value = json.loads(raw)
        if (value.get('version') != 'research-v1' or value.get('view') == 'compact'
                or value.get('status') != 'done' or value.get('passed') is not True
                or not isinstance(value.get('report'), dict) or not isinstance(value.get('evidence'), dict)):
            raise ValueError(f'{path.name}: 需要通过且完整的 research-v1 报告导出')
        if value['id'] in seen or type(value['id']) is not int or type(value['run_id']) is not int:
            raise ValueError('报告 ID 重复或无效')
        if value['report'].get('code') != value['code']:
            raise ValueError('报告公司与信封不一致')
        if value['evidence'].get('material_sha256') != value['report'].get('material_sha256'):
            raise ValueError('导出证据与报告材料哈希不一致')
        if value.get('identity') is not None and not isinstance(value['identity'], dict):
            raise ValueError('导出运行身份格式无效')
        seen.add(value['id'])
        exports.append((value, hashlib.sha256(raw).hexdigest()))
    # An existing database, including production, can never be an import target.
    database.parent.mkdir(parents=True, exist_ok=True)
    database.touch(exist_ok=False)
    conn = db.connect(database)
    try:
        with db.transaction(conn):
            for value, sha in exports:
                body = value['report']
                body_text = json.dumps(body, ensure_ascii=False)
                parent = body.get('parent_report_id') or (body.get('prior') or {}).get('report_id')
                identity = dict(value.get('identity') or {})
                identity['progress_capture'] = {'source': f"/api/reports/{value['id']}", 'export_sha256': sha,
                                                'evidence': value['evidence'], 'basis': 'authorized_api_export_not_original_run_snapshot'}
                conn.execute('INSERT INTO runs(id,kind,code,name,status,created_at,request,identity) VALUES (?,\'research\',?,?,\'done\',?,?,?)',
                             (value['run_id'], value['code'], value['name'], value['created_at'],
                              progress.encode({'question': value.get('question') or body.get('question'), 'parent_report_id': parent}),
                              progress.encode(identity)))
                conn.execute('INSERT INTO reports(id,run_id,code,name,period,passed,verdict,body,created_at,body_sha256) VALUES (?,?,?,?,?,1,?,?,?,?)',
                             (value['id'], value['run_id'], value['code'], value['name'], body['period'], body.get('verdict'),
                              body_text, value['created_at'], hashlib.sha256(body_text.encode()).hexdigest()))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--database', type=Path, required=True, help='必须为尚不存在的独立数据库路径')
    parser.add_argument('exports', type=Path, nargs='+')
    args = parser.parse_args()
    import_exports(args.database, args.exports)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info('Imported %s authorized exports into %s; no model calls', len(args.exports), args.database)


if __name__ == '__main__':
    main()
