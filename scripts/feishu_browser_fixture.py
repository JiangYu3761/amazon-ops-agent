"""Local replacement for the external HTTP transport in --feishu-ui only."""
from app.feishu_transport import FIELDS, RemoteError

record = None


def simulated_request(method, path, body=None, token=None):
    global record
    if path == '/auth/v3/tenant_access_token/internal':
        return {'code':0,'tenant_access_token':'local-fixture-token'}
    assert token == 'local-fixture-token'
    if '/fields?' in path:
        return {'data':{'items':[{'field_name':key,'type':1} for key in FIELDS]}}
    if '/records/search?' in path:
        wanted=body['filter']['conditions'][0]['value'][0]
        return {'data':{'items':[record] if record and record['fields']['本地ID']==wanted else []}}
    if method=='POST' and '/records?client_token=' in path:
        assert record is None, 'retry attempted a duplicate external create'
        assert set(body['fields']) == set(FIELDS)
        record={'record_id':'recBrowserFixture','fields':body['fields'].copy()}
        raise RemoteError('模拟飞书已保存，但首次回执丢失；请重试。')
    if path.endswith('/records/recBrowserFixture') and record:
        if method=='PUT':
            assert set(body['fields']) == set(FIELDS)
            record['fields']=body['fields'].copy()
        else: assert method=='GET'
        return {'data':{'record':record}}
    raise AssertionError('unexpected fake Feishu request')
