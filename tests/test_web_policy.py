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
    assert '单一持仓风险上限' in page.text and '2.0%' in page.text
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
