"""Send an explicitly selected work order and verify the row returned by Feishu Bitable."""
from dataclasses import dataclass
import hashlib
import os
import re
from urllib.parse import quote
from uuid import UUID

import httpx

FIELDS = ('本地ID', '类型', '事项', '产品', '负责人', '状态', '板块')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,180}')
BASE = 'https://open.feishu.cn/open-apis'


class RemoteError(Exception):
    """Only display messages safe for operators: no request headers, URLs, or upstream bodies."""


@dataclass(frozen=True)
class Destination:
    app_id: str
    secret: str
    base: str
    table: str

    @property
    def identity(self):
        return f'{self.base}/{self.table}'

    @property
    def path(self):
        return f'/bitable/v1/apps/{self.base}/tables/{self.table}'

    @property
    def url(self):
        return f'https://www.feishu.cn/base/{self.base}?table={self.table}'


def configured_target():
    values = [os.environ.get('HOMA_FEISHU_'+key, '').strip() for key in
              ('APP_ID', 'APP_SECRET', 'BASE_TOKEN', 'TABLE_ID')]
    if values[1] and all(IDENTIFIER.fullmatch(values[i]) for i in (0, 2, 3)):
        return Destination(*values)
    return None


def request(method, path, body=None, token=None):
    headers = {'Authorization': 'Bearer '+token} if token else {}
    try:
        response = httpx.request(method, BASE+path, json=body, headers=headers, follow_redirects=False, timeout=8.0)
        if response.status_code != 200:
            raise RemoteError(f'飞书返回 HTTP {response.status_code}，请核对网络与应用权限后重试。')
        payload = response.json()
    except (httpx.RequestError, ValueError):
        raise RemoteError('未收到飞书的有效响应，请重试以核对原记录。') from None
    if not isinstance(payload, dict) or payload.get('code') != 0:
        code = payload.get('code') if isinstance(payload, dict) else None
        raise RemoteError(f'飞书错误 {code if isinstance(code,int) else "未知"}。请核对应用权限、表格授权和字段后重试。')
    return payload


def check_fields(target, token):
    required = set(FIELDS)
    suffix = ''
    visited = set()
    while True:
        data = request('GET', target.path+'/fields?page_size=100'+suffix, token=token)['data']
        for field in data['items']:
            if field['type'] == 1:
                required.discard(field['field_name'])
        if not required:
            return
        if not data.get('has_more'):
            raise RemoteError('请在总表中补齐文本字段：'+'、'.join(sorted(required))+'。')
        cursor = data.get('page_token')
        if not isinstance(cursor,str) or not cursor or cursor in visited or len(visited)>=50:
            raise RemoteError('飞书字段列表分页未完成，请核对目标表后重试。')
        visited.add(cursor)
        suffix = '&page_token='+quote(cursor,safe='')


def text_field(value):
    if isinstance(value, list):
        if not all(isinstance(part,dict) and isinstance(part.get('text'),str) for part in value):
            raise RemoteError('飞书回读的文本格式异常，请核对目标字段类型。')
        return ''.join(part['text'] for part in value)
    return value or ''


def identifier(record):
    value=record.get('record_id')
    if not isinstance(value,str) or not IDENTIFIER.fullmatch(value):
        raise RemoteError('飞书未返回可核对的记录编号，请重试。')
    return value


def synchronize(target, fields, known_record, verify_association):
    """Serialize callers per order in PostgreSQL. The stable create token survives rollback."""
    try:
        token = request('POST','/auth/v3/tenant_access_token/internal',{
            'app_id':target.app_id, 'app_secret':target.secret})['tenant_access_token']
        if not isinstance(token,str) or not token:
            raise RemoteError('未取得飞书应用授权，请管理员核对配置。')
        check_fields(target, token)
        rows = target.path+'/records'
        matched = request('POST', rows+'/search?page_size=2', {
            'field_names':['本地ID'],
            'filter':{'conjunction':'and','conditions':[{'field_name':'本地ID','operator':'is','value':[fields['本地ID']]}]}
        },token)['data']
        hits = matched.get('items', [] if matched.get('total') == 0 else None)
        if not isinstance(hits,list):
            raise RemoteError('飞书检索结果不完整，请重试。')
        if matched.get('has_more') or len(hits)>1:
            raise RemoteError('总表中同一本地ID有多条记录，请人工合并后重试。')
        if hits:
            hit=hits[0]
            if text_field(hit['fields'].get('本地ID')) != fields['本地ID']:
                raise RemoteError('检索返回的本地ID不同，已停止同步。')
            remote_id = identifier(hit)
            if known_record and known_record != remote_id:
                raise RemoteError('记录编号与上次回执不同，请在总表核对记录后重试。')
        elif known_record:
            # A filtered search can lag. Read the acknowledged record and verify ownership below.
            remote_id = known_record
        else:
            seed = (target.identity+'\n'+fields['本地ID']).encode()
            client_token = UUID(bytes=hashlib.sha256(seed).digest()[:16],version=4)
            created=request('POST',rows+'?client_token='+str(client_token),{'fields':fields},token)
            remote_id=identifier(created['data']['record'])
        verify_association(remote_id)
        path=rows+'/'+remote_id
        def read_fields():
            record = request('GET',path,token=token)['data']['record']
            if identifier(record) != remote_id or text_field(record['fields'].get('本地ID')) != fields['本地ID']:
                raise RemoteError('原记录的本地ID已变化，请先在飞书恢复对应关系。')
            return {name:text_field(record['fields'].get(name)) for name in FIELDS}
        if read_fields() != fields:
            request('PUT',path,{'fields':fields},token)
            if read_fields() != fields:
                raise RemoteError('已提交写入，飞书回读仍有差异。请稍后重试核对。')
        return remote_id
    except (TypeError,KeyError,IndexError,AttributeError):
        raise RemoteError('飞书返回的记录不完整，请检查表格权限后重试。') from None
