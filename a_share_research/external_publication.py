"""Publish an independently reviewed external revision without rewriting its draft."""
from copy import deepcopy

from a_share_research import db, gate, model, report as report_body, research
from a_share_research.reviewing import digest


def publish(conn, run_id, material, draft, ledger, problems, *, submission_id,
            owner_key_id, account_aware, calls) -> int:
    """Write inside the caller's transaction; missing/mismatched review bindings fail closed."""
    if not conn.in_transaction:
        raise ValueError('External publication requires an enclosing transaction')
    run = conn.execute('SELECT kind FROM runs WHERE id=?', (run_id,)).fetchone()
    if run is None or run['kind'] != 'external':
        raise ValueError('External publication requires an external run')
    snapshot = material.snapshot()
    audit = ledger.snapshot()
    rounds = audit['rounds']
    reviewed = bool(rounds and rounds[-1].get('draft_sha256') == digest(draft)
                    and rounds[-1].get('material_sha256') == snapshot['sha256'])
    if not reviewed:
        raise ValueError('Independent review does not bind this draft and material')
    problems = deepcopy(problems)
    pending = ledger.pending()
    # Never let a caller omit an unresolved ledger issue from the publication decision.
    known = {p.get('id') for p in problems}
    problems += [p for p in pending if p.get('id') not in known]
    problems += gate.check_draft(draft, snapshot)
    period = max([material.doc.period] + [p for r in material.table if r['id'].startswith('T.') for p in r['values']])
    judgment = model.judgment_identity()
    judgment['author'] = {'provider': 'external', 'model': None, 'reasoning': 'not_recorded'}
    body = dict(schema_version=report_body.SCHEMA_VERSION, code=material.code, name=material.name,
                period=period, document=material.doc.info(), pages=sorted(material.pages), table=material.table,
                market_date=material.market_date, calls=calls, review_audit=audit,
                review_binding=dict(draft_sha256=digest(draft), material_sha256=snapshot['sha256'],
                                    completed=True, pending_issue_ids=[p['id'] for p in pending]),
                material_sha256=snapshot['sha256'], judgment=judgment,
                producer=dict(kind='external', key_id=owner_key_id, submission_id=submission_id,
                              account_aware=account_aware),
                question=material.extra.get('question', ''), parent_report_id=None, replay_of=None,
                gaps=material.extra.get('gaps', []) + draft.get('gaps', []),
                tool_calls=len(material.extra.get('tools', [])),
                announcement_searches=material.extra.get('announcement_searches', []),
                methods_used=material.extra.get('methods_used', []),
                stop_reasons=material.extra.get('stop_reasons', []),
                sources={k: {'document': v['document'], 'pages': sorted(map(int, v['pages']))}
                         for k, v in material.extra.get('sources', {}).items()})
    if prior := material.extra.get('prior'):
        body['prior'] = {'report_id': prior['report_id'], 'period': prior['period'],
                         'question_added': material.extra.get('question') == research.prior_question(prior)}
    if 'series' in material.extra:
        body['series'] = material.extra['series']
    allowed = research.open_questions(problems, reviewed)
    if allowed is not None:
        body['open_questions'] = [{k: p[k] for k in ('id', 'where', 'problem')} for p in allowed]
        body['gaps'] += [f"复核认为尚未完成：{p['problem']}" for p in allowed]
        problems = []
    passed = not problems
    if passed:
        body.update(research.published(draft))
        body['answer'] = draft.get('answer', [])
        if 'methods' in draft:
            body['methods'] = draft['methods']
        if 'valuation' in draft:
            body['valuation_calc'] = research.calculate_valuation(draft['valuation'], material, replay=True)
        if 'stance' in draft:
            body['stance_calc'] = research.stance_calc(draft['stance'], body.get('valuation_calc'), material.market_date)
    else:
        body.update(failure={'stage': 'review', 'category': 'substantive_review'},
                    facts=research._verified_facts(draft, problems, reviewed=True),
                    facts_reviewed=True, problems=problems)
    report_body.validate(body)
    db.save_report(conn, run_id, material.code, material.name, period, passed,
                   draft['verdict'] if passed else None, body)
    db.update_run(conn, run_id, status='done' if passed else 'failed', step=None,
                  error=None if passed else '外部研究核查未通过，不展示结论', finished_at=db.now())
    return conn.execute('SELECT id FROM reports WHERE run_id=?', (run_id,)).fetchone()[0]
