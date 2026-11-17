"""Individual accounts for process actions. Only the initial account is an admin."""
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .db import connection

router = APIRouter(prefix='/api/workflow-auth', tags=['Workflow accounts'])
COOKIE = 'homa_workflow_session'


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
    return f'pbkdf2_sha256${salt}${digest}'


def password_matches(password, encoded):
    try:
        algorithm, salt, digest = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
        return algorithm == 'pbkdf2_sha256' and hmac.compare_digest(actual, digest)
    except (ValueError, AttributeError):
        return False


def setup_code():
    configured = os.getenv('WORKFLOW_SETUP_TOKEN')
    if configured:
        return configured
    path = Path(os.getenv('WORKFLOW_SETUP_TOKEN_FILE', '/var/run/amazon-agent/workflow-setup-token'))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    code = secrets.token_urlsafe(32)
    with os.fdopen(fd, 'w') as out:
        out.write(code)
    return code


def mutation_request(request: Request):
    # The custom header forces cross-site fetches to preflight; additionally
    # reject foreign Origins, including a different port on the same hostname.
    if request.headers.get('x-workflow-request') != '1':
        raise HTTPException(403, '请从工作台发起操作')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '不接受其他站点发起的操作')


def optional_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with connection() as conn:
        return conn.execute('''SELECT u.id,u.display_name,u.email,a.is_admin FROM workflow_sessions s
            JOIN workflow_accounts a ON a.user_id=s.user_id JOIN app_users u ON u.id=a.user_id
            WHERE s.token_hash=%s AND s.expires_at>now() AND u.is_active''',
            (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()


def require_user(request: Request):
    if request.method not in {'GET', 'HEAD'}:
        mutation_request(request)
    user = optional_user(request)
    if not user:
        raise HTTPException(401, '请先登录流程协作账户')
    return user


def require_admin(request: Request):
    user = require_user(request)
    if not user['is_admin']:
        raise HTTPException(403, '只有流程管理员可以修改填写项目、人员和审核规则')
    return user


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=12, max_length=200)


class Setup(Credentials):
    display_name: str = Field(min_length=1, max_length=100)
    setup_code: str = Field(min_length=1, max_length=200)


class Member(Credentials):
    display_name: str = Field(min_length=1, max_length=100)
    department: str = Field(min_length=1, max_length=100)


def create_session(conn, user_id, response, request):
    token = secrets.token_urlsafe(32)
    conn.execute('DELETE FROM workflow_sessions WHERE expires_at<now()')
    conn.execute("INSERT INTO workflow_sessions VALUES(%s,%s,now()+interval '12 hours')",
                 (hashlib.sha256(token.encode()).hexdigest(), user_id))
    secure = os.getenv('WORKFLOW_COOKIE_SECURE', 'false').lower() == 'true' or request.url.scheme == 'https'
    response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite='strict', max_age=43200, path='/')


@router.get('/session')
def session(request: Request):
    with connection() as conn:
        initialized = conn.execute('SELECT EXISTS(SELECT 1 FROM workflow_accounts WHERE is_admin) ready').fetchone()['ready']
    return {'user': optional_user(request), 'initialized': initialized}


@router.post('/setup')
def setup(body: Setup, request: Request, response: Response):
    mutation_request(request)
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(7142026)')
        if conn.execute('SELECT 1 FROM workflow_accounts WHERE is_admin').fetchone():
            raise HTTPException(409, '管理员已设置，请直接登录')
        if not hmac.compare_digest(body.setup_code, setup_code()):
            raise HTTPException(403, '管理员设置码不正确，请从部署端获取')
        email = body.email.strip().lower()
        existing = conn.execute('SELECT id FROM app_users WHERE lower(email)=%s', (email,)).fetchone()
        if existing:
            raise HTTPException(409, '该邮箱已有人员档案，请使用新的管理员邮箱')
        if '@' not in email or not body.display_name.strip():
            raise HTTPException(422, '请填写有效邮箱和姓名')
        user = conn.execute("INSERT INTO app_users(email,display_name,role_code) VALUES(%s,%s,'project_owner') RETURNING id,email,display_name",
                            (email, body.display_name.strip())).fetchone()
        conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,true)',
                     (user['id'], password_hash(body.password)))
        create_session(conn, user['id'], response, request)
        conn.commit()
    return {**user, 'is_admin': True}


@router.post('/login')
def login(body: Credentials, request: Request, response: Response):
    mutation_request(request)
    invalid = False
    with connection() as conn:
        user = conn.execute('''SELECT u.id,u.display_name,u.email,a.*,
            COALESCE(a.locked_until>now(),false) locked FROM app_users u
            JOIN workflow_accounts a ON a.user_id=u.id WHERE lower(u.email)=%s AND u.is_active FOR UPDATE OF a''',
            (body.email.strip().lower(),)).fetchone()
        if user and user['locked']:
            raise HTTPException(429, '尝试次数过多，请十五分钟后重试')
        encoded = user['password_hash'] if user else 'pbkdf2_sha256$missing$' + '0'*64
        if not password_matches(body.password, encoded) or not user:
            if user:
                conn.execute("""UPDATE workflow_accounts SET failed_attempts=failed_attempts+1,
                    locked_until=CASE WHEN failed_attempts>=4 THEN now()+interval '15 minutes' ELSE NULL END WHERE user_id=%s""", (user['id'],))
            invalid = True
        else:
            conn.execute('UPDATE workflow_accounts SET failed_attempts=0,locked_until=NULL WHERE user_id=%s', (user['id'],))
            create_session(conn, user['id'], response, request)
        conn.commit()
    if invalid:
        raise HTTPException(401, '邮箱或密码不正确')
    return {key:user[key] for key in ('id','display_name','email','is_admin')}


@router.post('/logout')
def logout(request: Request, response: Response):
    mutation_request(request)
    token = request.cookies.get(COOKIE, '')
    with connection() as conn:
        conn.execute('DELETE FROM workflow_sessions WHERE token_hash=%s', (hashlib.sha256(token.encode()).hexdigest(),))
        conn.commit()
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


@router.get('/members')
def members(user=Depends(require_user)):
    with connection() as conn:
        return conn.execute('''SELECT u.id,u.email,u.display_name,a.is_admin,COALESCE(d.name,'未分组') department
            FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id
            LEFT JOIN departments d ON d.id=u.department_id WHERE u.is_active ORDER BY a.is_admin DESC,u.created_at''').fetchall()


@router.post('/members', status_code=201)
def add_member(body: Member, actor=Depends(require_admin)):
    email = body.email.strip().lower()
    if '@' not in email or not body.display_name.strip() or not body.department.strip():
        raise HTTPException(422, '请填写有效的人员资料')
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(7142026)')
        existing = conn.execute('SELECT id FROM app_users WHERE lower(email)=%s FOR UPDATE', (email,)).fetchone()
        if existing and conn.execute('SELECT 1 FROM workflow_accounts WHERE user_id=%s', (existing['id'],)).fetchone():
            raise HTTPException(409, '该人员已有登录账户，不能覆盖其密码或权限')
        dept = conn.execute('INSERT INTO departments(name) VALUES(%s) ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name RETURNING id', (body.department.strip(),)).fetchone()
        if existing:
            row = conn.execute('UPDATE app_users SET display_name=%s,department_id=%s,is_active=true WHERE id=%s RETURNING id,email,display_name',
                               (body.display_name.strip(), dept['id'], existing['id'])).fetchone()
        else:
            row = conn.execute('INSERT INTO app_users(email,display_name,department_id) VALUES(%s,%s,%s) RETURNING id,email,display_name',
                               (email, body.display_name.strip(), dept['id'])).fetchone()
        conn.execute('INSERT INTO workflow_accounts(user_id,password_hash) VALUES(%s,%s)', (row['id'], password_hash(body.password)))
        conn.execute("INSERT INTO audit_logs(actor_id,entity_type,entity_id,action) VALUES(%s,'workflow_account',%s,'member_created')", (actor['id'], str(row['id'])))
        conn.commit()
    return {**row, 'department': body.department.strip(), 'is_admin': False}


if __name__ == '__main__':
    # Run from a trusted deployment terminal, never return the code via HTTP.
    print(setup_code())
