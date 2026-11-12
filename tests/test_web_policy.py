"""Authenticated read-only policy page, using isolated HTTP and captured research fixtures."""
from a_share_research import api_keys,db
from a_share_research.policy import registry
from test_policy_capture import scoped_parameters


def activate(conn,**changes):
    owner=api_keys.authenticate(conn,api_keys.create(conn,'web-policy-owner',role='owner'))
    manifest=registry.register(conn,owner,scoped_parameters(**changes))
    registry.activate(conn,owner,manifest['identity'],expected_id=0,request_id='web-policy',reason='page contract fixture')
    conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',('mirror','0'*64,db.now(),'{}'))
    return manifest


def test_policy_page_requires_owner_session_not_an_api_key(server):
    conn,client,url,*_=server
    response=client.get(url+'/policy',allow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/login'
    assert '风险参数' not in response.text
    owner_token=api_keys.create(conn,'web-owner-not-a-session',role='owner')
    response=client.get(url+'/policy',headers={'Authorization':'Bearer '+owner_token},allow_redirects=False)
    assert response.status_code==303
    client.post(url+'/auth/dev',headers={'Origin':url})
    page=client.get(url+'/policy')
    assert page.status_code==200 and '尚未启用政策' in page.text and '账户来源尚未登记' in page.text
    assert page.headers['cache-control']=='no-store, no-transform'


def test_owner_sees_registered_policy_and_reads_do_not_change_any_database_rows(server):
    conn,client,url,*_=server
    manifest=activate(conn,policy_id='structural <script>alert(1)</script>',confirmation_ref='<img src=x onerror=alert(1)>')
    client.post(url+'/auth/dev',headers={'Origin':url})
    before=list(conn.iterdump())
    page=client.get(url+'/policy')
    assert page.status_code==200 and '政策已启用' in page.text
    assert '单一持仓参考风险预算' in page.text and '2.0%' in page.text
    assert '旧账户镜像' in page.text and '切换前' in page.text
    assert 'aria-current="page"' in page.text
    assert manifest['identity'] in page.text and manifest['guide_version'] in page.text
    assert '<script>alert(1)</script>' not in page.text and '&lt;script&gt;' in page.text
    assert '<img src=x' not in page.text and '&lt;img' in page.text
    assert '"files"' not in page.text and '"guides"' not in page.text
    assert client.post(url+'/policy',json={'identity':'another'},headers={'Origin':url}).status_code==405
    assert list(conn.iterdump())==before


def test_unregistered_or_invalid_policy_never_shows_unverified_parameters(server):
    conn,client,url,*_=server
    conn.execute('INSERT INTO policy_activations(policy,activated_at,activated_by,identity) VALUES(?,?,?,?)',
                 ('v0',db.now(),'historical','old-profile'))
    client.post(url+'/auth/dev',headers={'Origin':url})
    assert '尚未登记完整政策' in client.get(url+'/policy').text
    conn.execute('INSERT INTO policy_versions VALUES(?,?,?,?,?)',('old-profile','v0',db.now(),1,'{"private":"DO_NOT_ECHO"}'))
    response=client.get(url+'/policy')
    assert response.status_code==200 and '政策记录需要核对' in response.text
    assert 'DO_NOT_ECHO' not in response.text and '风险参数' not in response.text


def test_policy_storage_failure_has_owner_read_error_without_record_details(server):
    conn,client,url,*_=server
    client.post(url+'/auth/dev',headers={'Origin':url})
    conn.execute('ALTER TABLE account_source RENAME TO temporarily_missing_source')
    page=client.get(url+'/policy')
    assert page.status_code==503 and '暂时无法读取政策' in page.text
    assert 'temporarily_missing_source' not in page.text


def test_policy_explains_risk_without_claiming_account_assessment(server):
    conn,client,url,*_=server
    activate(conn)
    client.post(url+'/auth/dev',headers={'Origin':url})
    before=list(conn.iterdump())
    text=client.get(url+'/policy').text
    for phrase in ('账户风险：本页未评估', '不是仓位上限', '不自动卖出或调整止损',
                   '来源登记时间不是持仓', '回撤观察目标', '未设置', '来源与版本'):
        assert phrase in text
    assert text.index('我的规则摘要') < text.index('完整政策身份')
    assert list(conn.iterdump())==before


def test_optional_observation_target_and_cash_keep_their_distinct_meaning(server):
    conn,client,url,*_=server
    # Explicit policy-parameter variants; not market or account evidence.
    activate(conn,drawdown_observation_target_pct=4.,off_account_strategy_cash=0.,untradable_boards=['STAR'])
    client.post(url+'/auth/dev',headers={'Origin':url})
    text=client.get(url+'/policy').text
    assert '回撤观察目标</dt><dd>4.0%' in text
    assert '账户外策略现金</dt><dd>0.0 CNY' in text
    assert '科创板（STAR）' in text and '避免重复计算' in text
