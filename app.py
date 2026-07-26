import base64,json,os,re,hashlib,time,threading,sqlite3,logging,secrets,smtplib,csv,hmac,urllib.parse,urllib.request
import json5
from io import BytesIO, StringIO
from datetime import datetime, timedelta
from email.message import EmailMessage
from flask import Flask,jsonify,request,send_from_directory,send_file,session,Response
import xlsxwriter
from openai import OpenAI
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
app=Flask(__name__,static_folder='.')
# Render·Cloudflare 같은 역방향 프록시 뒤에서도 실제 HTTPS/호스트를 인식해
# 로그인 POST와 보안 세션 쿠키가 정상 동작하도록 합니다.
app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=1,x_port=1)
app.config['MAX_CONTENT_LENGTH']=24*1024*1024
app.config['SECRET_KEY']=os.getenv('SECRET_KEY') or 'CHANGE-ME-RESELL-PICK-BETA'
app.config['PERMANENT_SESSION_LIFETIME']=timedelta(days=30)
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'
app.config['SESSION_COOKIE_SECURE']=os.getenv('COOKIE_SECURE','1')=='1'
# 앱과 API가 같은 주소에서 동작하므로 검증된 Lax 세션 쿠키를 사용합니다.
# Partitioned 쿠키는 일부 삼성 인터넷·PWA에서 저장 실패를 일으켜 사용하지 않습니다.
app.config['SESSION_COOKIE_PARTITIONED']=False
app.config['SESSION_COOKIE_NAME']='resell_pick_session'
app.config['JSON_AS_ASCII']=False

@app.after_request
def _security_headers(response):
    response.headers.setdefault('X-Content-Type-Options','nosniff')
    response.headers.setdefault('X-Frame-Options','DENY')
    response.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy','camera=(self), microphone=(self), geolocation=()')
    response.headers.setdefault('Cross-Origin-Opener-Policy','same-origin-allow-popups')
    if request.is_secure or os.getenv('COOKIE_SECURE','1')=='1':
        response.headers.setdefault('Strict-Transport-Security','max-age=31536000; includeSubDomains')
    if request.path.startswith('/api/'):
        response.headers.setdefault('Cache-Control','no-store')
    return response

@app.before_request
def _same_origin_write_guard():
    if request.method not in {'POST','PUT','PATCH','DELETE'}: return None
    origin=(request.headers.get('Origin') or '').strip().rstrip('/')
    if not origin: return None
    # 프록시·사용자 지정 도메인 환경에서 Host가 달라져 정상 로그인까지 차단되던 문제를 방지합니다.
    allowed=set()
    proto=(request.headers.get('X-Forwarded-Proto') or request.scheme or 'https').split(',')[0].strip()
    hosts=[request.host]
    forwarded_host=(request.headers.get('X-Forwarded-Host') or '').split(',')[0].strip()
    if forwarded_host: hosts.append(forwarded_host)
    for host in hosts:
        if host:
            allowed.add(f'{proto}://{host}'.rstrip('/'))
            allowed.add(f'https://{host}'.rstrip('/'))
    public_url=(os.getenv('PUBLIC_APP_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').strip().rstrip('/')
    if public_url: allowed.add(public_url)
    if origin not in allowed:
        logging.warning('same-origin guard rejected origin=%s allowed=%s',origin,sorted(allowed))
        return jsonify(error='로그인 서버 연결을 확인하지 못했습니다. 앱을 완전히 종료한 뒤 다시 실행해 주세요.',code='origin_mismatch'),403
DATABASE_URL=os.getenv('DATABASE_URL') or ''
DB_PATH=os.getenv('DATABASE_PATH') or os.path.join(os.getenv('DATA_DIR','.'),'resell_pick.db')
ALLOWED={'image/jpeg','image/png','image/webp'}
_AI_SEMAPHORE=threading.BoundedSemaphore(max(1,int(os.getenv('MAX_CONCURRENT_AI','4'))))

from sqlalchemy import create_engine,text
from sqlalchemy.exc import IntegrityError

def _db_url():
    if DATABASE_URL:
        url=DATABASE_URL.strip()
        # Render의 연결 문자열은 postgresql:// 또는 postgres:// 형태일 수 있다.
        # SQLAlchemy가 설치되지 않은 psycopg2를 찾지 않도록 Psycopg 3 드라이버를 명시한다.
        if url.startswith('postgresql://'):
            return url.replace('postgresql://','postgresql+psycopg://',1)
        if url.startswith('postgres://'):
            return url.replace('postgres://','postgresql+psycopg://',1)
        return url
    folder=os.path.dirname(os.path.abspath(DB_PATH));os.makedirs(folder,exist_ok=True)
    return 'sqlite:///'+os.path.abspath(DB_PATH)

DB_URL=_db_url()
ENGINE=create_engine(
    DB_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=max(2,int(os.getenv('DB_POOL_SIZE','5'))) if DB_URL.startswith('postgresql') else 5,
    max_overflow=max(2,int(os.getenv('DB_MAX_OVERFLOW','10'))) if DB_URL.startswith('postgresql') else 10,
    pool_timeout=30,
    future=True,
    connect_args={'timeout':30,'check_same_thread':False} if DB_URL.startswith('sqlite') else {'connect_timeout':10},
)

def _canonical_email(email):
    email=str(email or '').strip().lower()
    if '@' not in email:return email
    local,domain=email.rsplit('@',1)
    if domain in {'gmail.com','googlemail.com'}:
        local=local.split('+',1)[0].replace('.','')
        domain='gmail.com'
    return f'{local}@{domain}'

def _client_ip_hash():
    raw=(request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    salt=app.config['SECRET_KEY']
    return hashlib.sha256((salt+'|'+raw).encode()).hexdigest()

def _admins():
    # Gmail 점(.)/플러스 별칭까지 같은 계정으로 처리해 관리자 권한 누락을 방지한다.
    return {_canonical_email(x) for x in os.getenv('ADMIN_EMAILS','').split(',') if str(x).strip()}

def _requires_email_verification():
    return os.getenv('REQUIRE_EMAIL_VERIFICATION','0')=='1'

def _send_verification_email(email,code):
    host=os.getenv('SMTP_HOST','').strip();user=os.getenv('SMTP_USER','').strip();password=os.getenv('SMTP_PASSWORD','')
    port=int(os.getenv('SMTP_PORT','587'));sender=os.getenv('SMTP_FROM',user).strip()
    if not host or not user or not password or not sender:
        return False
    msg=EmailMessage();msg['Subject']='리셀 PICK 이메일 인증번호';msg['From']=sender;msg['To']=email
    msg.set_content(f'리셀 PICK 이메일 인증번호는 {code} 입니다. 10분 안에 입력해 주세요. 본인이 요청하지 않았다면 이 메일을 무시하세요.')
    with smtplib.SMTP(host,port,timeout=20) as server:
        server.starttls();server.login(user,password);server.send_message(msg)
    return True


def _send_password_reset_email(email,code):
    host=os.getenv('SMTP_HOST','').strip();user=os.getenv('SMTP_USER','').strip();password=os.getenv('SMTP_PASSWORD','')
    port=int(os.getenv('SMTP_PORT','587'));sender=os.getenv('SMTP_FROM',user).strip()
    if not host or not user or not password or not sender:return False
    msg=EmailMessage();msg['Subject']='리셀 PICK 비밀번호 재설정 인증번호';msg['From']=sender;msg['To']=email
    msg.set_content(f'비밀번호 재설정 인증번호는 {code} 입니다. 10분 안에 입력해 주세요. 본인이 요청하지 않았다면 비밀번호를 변경하지 말고 이 메일을 삭제하세요.')
    with smtplib.SMTP(host,port,timeout=20) as server:
        server.starttls();server.login(user,password);server.send_message(msg)
    return True

def _issue_verification(con,uid,email):
    code=f'{secrets.randbelow(1000000):06d}'
    digest=hashlib.sha256((app.config['SECRET_KEY']+'|'+code).encode()).hexdigest()
    expires=(datetime.utcnow()+timedelta(minutes=10)).isoformat(timespec='seconds')+'Z'
    con.execute(text('UPDATE users SET verification_code_hash=:h,verification_expires_at=:x WHERE id=:i'),{'h':digest,'x':expires,'i':uid})
    return code

def _registration_allowed(ip_hash,email_key):
    cutoff=(datetime.utcnow()-timedelta(hours=24)).isoformat(timespec='seconds')+'Z'
    max_per_ip=max(1,int(os.getenv('MAX_REGISTRATIONS_PER_IP_DAY','3')))
    with ENGINE.connect() as con:
        count=int(con.execute(text('SELECT COUNT(*) FROM registration_attempts WHERE ip_hash=:i AND created_at>=:c'),{'i':ip_hash,'c':cutoff}).scalar_one())
    return count<max_per_ip

def _init_db():
    users_sql="""CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
      email VARCHAR(255) NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      display_name VARCHAR(60) NOT NULL DEFAULT '',
      plan VARCHAR(20) NOT NULL DEFAULT 'free',
      created_at VARCHAR(40) NOT NULL,
      last_login_at VARCHAR(40),
      email_key VARCHAR(255),
      email_verified INTEGER NOT NULL DEFAULT 0,
      verification_code_hash VARCHAR(128),
      verification_expires_at VARCHAR(40),
      reset_code_hash VARCHAR(128),
      reset_expires_at VARCHAR(40),
      reset_requested_at VARCHAR(40),
      auth_version INTEGER NOT NULL DEFAULT 1,
      plan_started_at VARCHAR(40),
      plan_expires_at VARCHAR(40),
      registration_ip_hash VARCHAR(128),
      role VARCHAR(20) NOT NULL DEFAULT 'user',
      account_status VARCHAR(20) NOT NULL DEFAULT 'active',
      member_number INTEGER,
      failed_login_count INTEGER NOT NULL DEFAULT 0,
      locked_until VARCHAR(40))"""
    if DB_URL.startswith('sqlite'):
        users_sql=users_sql.replace('INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY','INTEGER PRIMARY KEY AUTOINCREMENT')
    snapshot_sql="""CREATE TABLE IF NOT EXISTS user_snapshots(
      user_id INTEGER PRIMARY KEY,payload TEXT NOT NULL DEFAULT '{}',version INTEGER NOT NULL DEFAULT 1,
      updated_at VARCHAR(40) NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"""
    usage_sql="""CREATE TABLE IF NOT EXISTS monthly_usage(
      user_id INTEGER NOT NULL,month_key VARCHAR(7) NOT NULL,analysis_count INTEGER NOT NULL DEFAULT 0,
      updated_at VARCHAR(40) NOT NULL,PRIMARY KEY(user_id,month_key),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"""
    audit_sql="""CREATE TABLE IF NOT EXISTS admin_audit_logs(
      id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,admin_user_id INTEGER NOT NULL,
      action VARCHAR(80) NOT NULL,target_user_id INTEGER,detail TEXT,created_at VARCHAR(40) NOT NULL)"""
    if DB_URL.startswith('sqlite'):
        audit_sql=audit_sql.replace('INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY','INTEGER PRIMARY KEY AUTOINCREMENT')
    reg_sql="""CREATE TABLE IF NOT EXISTS registration_attempts(
      id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,ip_hash VARCHAR(128) NOT NULL,
      email_key VARCHAR(255) NOT NULL,created_at VARCHAR(40) NOT NULL)"""
    if DB_URL.startswith('sqlite'):
        reg_sql=reg_sql.replace('INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY','INTEGER PRIMARY KEY AUTOINCREMENT')
    notice_sql="""CREATE TABLE IF NOT EXISTS service_notices(
      id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,title VARCHAR(120) NOT NULL,body TEXT NOT NULL,
      is_active INTEGER NOT NULL DEFAULT 1,starts_at VARCHAR(40),ends_at VARCHAR(40),
      created_by INTEGER NOT NULL,created_at VARCHAR(40) NOT NULL,updated_at VARCHAR(40) NOT NULL)"""
    if DB_URL.startswith('sqlite'):
        notice_sql=notice_sql.replace('INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY','INTEGER PRIMARY KEY AUTOINCREMENT')
    consent_sql="""CREATE TABLE IF NOT EXISTS user_consents(
      id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,user_id INTEGER NOT NULL,
      consent_type VARCHAR(30) NOT NULL,document_version VARCHAR(40) NOT NULL,accepted_at VARCHAR(40) NOT NULL,
      ip_hash VARCHAR(128),user_agent VARCHAR(500),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"""
    if DB_URL.startswith('sqlite'):
        consent_sql=consent_sql.replace('INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY','INTEGER PRIMARY KEY AUTOINCREMENT')
    with ENGINE.begin() as con:
        for q in (users_sql,snapshot_sql,usage_sql,reg_sql,audit_sql,notice_sql,consent_sql): con.execute(text(q))
        if DB_URL.startswith('postgresql'):
            for q in (
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS email_key VARCHAR(255)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified INTEGER NOT NULL DEFAULT 0',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_code_hash VARCHAR(128)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_expires_at VARCHAR(40)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_ip_hash VARCHAR(128)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_code_hash VARCHAR(128)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_expires_at VARCHAR(40)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_requested_at VARCHAR(40)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 1',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_started_at VARCHAR(40)',
                'ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at VARCHAR(40)',
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS account_status VARCHAR(20) NOT NULL DEFAULT 'active'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS member_number INTEGER",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until VARCHAR(40)"
            ): con.execute(text(q))
        else:
            cols=[r[1] for r in con.exec_driver_sql('PRAGMA table_info(users)').fetchall()]
            additions={'email_key':'VARCHAR(255)','email_verified':'INTEGER NOT NULL DEFAULT 0','verification_code_hash':'VARCHAR(128)','verification_expires_at':'VARCHAR(40)','registration_ip_hash':'VARCHAR(128)','reset_code_hash':'VARCHAR(128)','reset_expires_at':'VARCHAR(40)','reset_requested_at':'VARCHAR(40)','auth_version':'INTEGER NOT NULL DEFAULT 1','plan_started_at':'VARCHAR(40)','plan_expires_at':'VARCHAR(40)','role':"VARCHAR(20) NOT NULL DEFAULT 'user'",'account_status':"VARCHAR(20) NOT NULL DEFAULT 'active'",'member_number':'INTEGER','failed_login_count':'INTEGER NOT NULL DEFAULT 0','locked_until':'VARCHAR(40)'}
            for name,typ in additions.items():
                if name not in cols: con.exec_driver_sql(f'ALTER TABLE users ADD COLUMN {name} {typ}')
            cols2=[r[1] for r in con.exec_driver_sql('PRAGMA table_info(user_snapshots)').fetchall()]
            if 'version' not in cols2: con.exec_driver_sql('ALTER TABLE user_snapshots ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        # 기존 회원의 이메일 식별키를 채운다.
        rows=con.execute(text('SELECT id,email,email_key FROM users')).fetchall()
        for row in rows:
            if not row[2]:
                con.execute(text('UPDATE users SET email_key=:k WHERE id=:i'),{'k':_canonical_email(row[1]),'i':row[0]})
        
        # 회원번호는 가입 후 변경되지 않는 6자리 숫자입니다. 기존 회원은 내부 id를 기준으로 안전하게 소급 부여합니다.
        con.execute(text('UPDATE users SET member_number=100000+id WHERE member_number IS NULL'))
        admin_emails=_admins()
        # 기존 이메일 식별키 형식이 달라도 실제 이메일을 정규화해 관리자 권한을 복구한다.
        admin_rows=con.execute(text('SELECT id,email,member_number FROM users')).fetchall()
        for ar in admin_rows:
            if _canonical_email(ar[1]) in admin_emails or int(ar[2] or 0)==100000:
                con.execute(text("UPDATE users SET role='admin',account_status='active' WHERE id=:i"),{'i':int(ar[0])})
        # 첫 관리자 계정은 관리 편의를 위해 100000번을 사용합니다. 다른 관리자는 기존 고유번호를 유지합니다.
        first_admin=con.execute(text("SELECT id FROM users WHERE role='admin' ORDER BY id ASC LIMIT 1")).first()
        if first_admin:
            con.execute(text('UPDATE users SET member_number=100000,role=\'admin\',account_status=\'active\',failed_login_count=0,locked_until=NULL WHERE id=:i'),{'i':int(first_admin[0])})
        # 배포 중 권한/잠금 정보가 꼬여 관리자 계정이 막히지 않도록 관리자만 안전하게 복구합니다.
        if admin_emails:
            for ar in admin_rows:
                if _canonical_email(ar[1]) in admin_emails:
                    con.execute(text("UPDATE users SET role='admin',account_status='active',failed_login_count=0,locked_until=NULL WHERE id=:i"),{'i':int(ar[0])})
        con.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_key ON users(email_key)'))
        con.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_member_number ON users(member_number)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS ix_registration_attempts_ip_created ON registration_attempts(ip_hash,created_at)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS ix_user_consents_user_type ON user_consents(user_id,consent_type,accepted_at)'))

def _init_db_with_retry():
    attempts=max(1,int(os.getenv('DB_INIT_RETRIES','8')))
    for attempt in range(1,attempts+1):
        try:
            _init_db();return
        except Exception:
            logging.exception('database initialization failed (%s/%s)',attempt,attempts)
            if attempt>=attempts: raise
            time.sleep(min(2**attempt,15))

_init_db_with_retry()

def _row_dict(row): return dict(row._mapping) if row else None

AUTH_TOKEN_MAX_AGE=30*24*60*60
AUTH_COOKIE_NAME='resell_pick_auth'

def _auth_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'],salt='resell-pick-auth-v1')

def _issue_auth_token(uid,auth_version):
    return _auth_serializer().dumps({'uid':int(uid),'av':int(auth_version or 1)})

def _decode_auth_token(token):
    token=str(token or '').strip()
    if not token:return None,None
    try:
        payload=_auth_serializer().loads(token,max_age=AUTH_TOKEN_MAX_AGE)
        return int(payload.get('uid')),int(payload.get('av') or 1)
    except (BadSignature,SignatureExpired,TypeError,ValueError):
        return None,None

def _request_auth_token():
    # 기존 Flask 세션이 가장 우선이며, 저장소·헤더 제한이 있는 PWA를 위해
    # 전용 헤더 → Bearer 헤더 → JSON 본문 → 보조 HttpOnly 쿠키 순서로 복구합니다.
    token=str(request.headers.get('X-Resell-Pick-Token') or '').strip()
    if not token:
        raw=str(request.headers.get('Authorization') or '').strip()
        if raw.lower().startswith('bearer '):token=raw[7:].strip()
    if not token and request.is_json:
        data=request.get_json(silent=True) or {}
        token=str(data.get('auth_token') or '').strip()
    if not token:token=str(request.cookies.get(AUTH_COOKIE_NAME) or '').strip()
    return _decode_auth_token(token)

def _load_auth_user(uid,expected_auth=None):
    if not uid:return None
    with ENGINE.connect() as con:
        row=_row_dict(con.execute(text('SELECT id,member_number,email,display_name,plan,created_at,last_login_at,email_verified,auth_version,plan_started_at,plan_expires_at,role,account_status FROM users WHERE id=:id'),{'id':int(uid)}).first())
    if not row or row.get('account_status')!='active':return None
    current_auth=int(row.get('auth_version') or 1)
    if expected_auth is not None and int(expected_auth)!=current_auth:return None
    row['is_admin']=row.get('role')=='admin';row['email_verified']=bool(row.get('email_verified'))
    expires=row.get('plan_expires_at')
    if expires:
        try:
            expiry_dt=datetime.fromisoformat(expires.replace('Z','+00:00')).replace(tzinfo=None)
            row['days_remaining']=max(0,(expiry_dt.date()-datetime.utcnow().date()).days)
            row['subscription_expired']=expiry_dt<datetime.utcnow()
        except Exception:
            row['days_remaining']=None;row['subscription_expired']=False
    else:
        row['days_remaining']=None;row['subscription_expired']=False
    row['billing_plan']=row.get('plan')
    if row.get('subscription_expired') and row.get('plan') in {'pro','proplus'}:row['plan']='free'
    return row

def _current_user():
    # 1) 기존에 정상 동작하던 Flask 세션을 우선 사용합니다.
    cookie_uid=session.get('user_id');cookie_auth=session.get('auth_version')
    if cookie_uid:
        row=_load_auth_user(cookie_uid,cookie_auth)
        if row:return row
        # 오래된 세션 쿠키가 남아 있어도 아래의 유효한 보조 토큰을 막지 않도록 제거합니다.
        session.clear()
    # 2) 세션 쿠키 갱신이 지연되는 PWA/인앱 브라우저에서는 서명 토큰으로 복구합니다.
    token_uid,token_auth=_request_auth_token()
    row=_load_auth_user(token_uid,token_auth)
    if not row:return None
    # 다음 요청부터는 다시 기존 Flask 세션 방식으로 동작하도록 자동 복구합니다.
    session.clear();session.permanent=True
    session['user_id']=int(row['id']);session['auth_version']=int(row.get('auth_version') or 1)
    return row

def _auth_json_response(payload,status=200,token=None):
    response=jsonify(**payload);response.status_code=status
    if token:
        same_site=str(app.config.get('SESSION_COOKIE_SAMESITE') or 'Lax')
        response.set_cookie(
            AUTH_COOKIE_NAME,token,max_age=AUTH_TOKEN_MAX_AGE,
            httponly=True,secure=bool(app.config.get('SESSION_COOKIE_SECURE')),
            samesite=same_site,path='/'
        )
    return response

def _clear_auth_response(payload=None,status=200):
    response=jsonify(**(payload or {'ok':True}));response.status_code=status
    response.delete_cookie(AUTH_COOKIE_NAME,path='/')
    return response

def _consent_status(uid):
    with ENGINE.connect() as con:
        rows=con.execute(text('SELECT consent_type,document_version,accepted_at FROM user_consents WHERE user_id=:u ORDER BY id DESC'),{'u':uid}).fetchall()
    latest={}
    for r in rows:
        if r[0] not in latest: latest[r[0]]={'version':r[1],'accepted_at':r[2]}
    return {
      'terms':latest.get('terms'), 'privacy':latest.get('privacy'),
      'terms_current':bool(latest.get('terms') and latest['terms']['version']==TERMS_VERSION),
      'privacy_current':bool(latest.get('privacy') and latest['privacy']['version']==PRIVACY_VERSION),
      'required_versions':{'terms':TERMS_VERSION,'privacy':PRIVACY_VERSION}
    }

def _user_json(row): return row

def _plan_limit(plan): return {'free':20,'pro':1000,'proplus':3000}.get(plan,20)
def _month_key(): return datetime.utcnow().strftime('%Y-%m')
def _usage_for(uid):
    with ENGINE.connect() as con: row=con.execute(text('SELECT analysis_count FROM monthly_usage WHERE user_id=:u AND month_key=:m'),{'u':uid,'m':_month_key()}).first()
    return int(row[0]) if row else 0

def _reserve_analysis(uid,plan):
    month=_month_key();limit=_plan_limit(plan);now=datetime.utcnow().isoformat(timespec='seconds')+'Z'
    with ENGINE.begin() as con:
        q='SELECT analysis_count FROM monthly_usage WHERE user_id=:u AND month_key=:m'
        if DB_URL.startswith('postgresql'): q+=' FOR UPDATE'
        row=con.execute(text(q),{'u':uid,'m':month}).first()
        used=int(row[0]) if row else 0
        if used>=limit:return False,used,limit
        if row:
            con.execute(text('UPDATE monthly_usage SET analysis_count=:c,updated_at=:t WHERE user_id=:u AND month_key=:m'),{'c':used+1,'t':now,'u':uid,'m':month})
        else:
            con.execute(text('INSERT INTO monthly_usage(user_id,month_key,analysis_count,updated_at) VALUES(:u,:m,1,:t)'),{'u':uid,'m':month,'t':now})
    return True,used+1,limit

def _rollback_analysis(uid):
    if not uid:return
    with ENGINE.begin() as con:
        con.execute(text('UPDATE monthly_usage SET analysis_count=CASE WHEN analysis_count>0 THEN analysis_count-1 ELSE 0 END,updated_at=:t WHERE user_id=:u AND month_key=:m'),{'t':datetime.utcnow().isoformat(timespec='seconds')+'Z','u':uid,'m':_month_key()})

def _analysis_access():
    u=_current_user()
    if not u:
        if os.getenv('REQUIRE_LOGIN_FOR_AI','0')=='1':
            return None,(jsonify(error='사진 분석은 로그인 후 이용할 수 있습니다.'),401)
        return None,None
    if _requires_email_verification() and not u.get('email_verified') and not u.get('is_admin'):
        return None,(jsonify(error='이메일 인증을 완료한 뒤 사진 분석을 이용해 주세요.',email_verification_required=True),403)
    ok,used,limit=_reserve_analysis(u['id'],u['plan'])
    if not ok:return None,(jsonify(error=f'이번 달 분석 제공량 {limit:,}회를 모두 사용했습니다.',usage=used,limit=limit),429)
    return u,None

def _require_user():
    u=_current_user(); return (u,None) if u else (None,(jsonify(error='로그인이 필요합니다.'),401))

@app.route('/api/account/me',methods=['GET','POST'])
def account_me():
    u=_current_user(); return jsonify(authenticated=bool(u),user=u,usage=(_usage_for(u['id']) if u else 0),limit=(_plan_limit(u['plan']) if u else 20),consents=(_consent_status(u['id']) if u else None),server_version='6.9.4')

@app.post('/api/account/register')
def account_register():
    d=request.get_json(silent=True) or {};email=str(d.get('email') or '').strip().lower();password=str(d.get('password') or '');name=str(d.get('display_name') or '').strip()[:60]
    terms_agreed=bool(d.get('terms_agreed'));privacy_agreed=bool(d.get('privacy_agreed'))
    if not terms_agreed or not privacy_agreed:return jsonify(error='이용약관과 개인정보 처리방침에 모두 동의해 주세요.'),400
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):return jsonify(error='이메일 형식을 확인해 주세요.'),400
    if len(password)<10 or not re.search(r'[A-Za-z]',password) or not re.search(r'\d',password):return jsonify(error='비밀번호는 영문과 숫자를 포함해 10자 이상으로 입력해 주세요.'),400
    email_key=_canonical_email(email);ip_hash=_client_ip_hash()
    if not _registration_allowed(ip_hash,email_key):return jsonify(error='같은 접속 환경에서 계정을 너무 많이 만들었습니다. 24시간 후 다시 시도하거나 고객지원에 문의해 주세요.'),429
    now=datetime.utcnow().isoformat(timespec='seconds')+'Z';is_configured_admin=_canonical_email(email) in _admins();verified=1 if (is_configured_admin or not _requires_email_verification()) else 0
    try:
        with ENGINE.begin() as con:
            uid=int(con.execute(text('INSERT INTO users(email,email_key,password_hash,display_name,plan,created_at,last_login_at,email_verified,registration_ip_hash) VALUES(:e,:k,:p,:n,:pl,:c,:l,:v,:ip) RETURNING id'),{'e':email,'k':email_key,'p':generate_password_hash(password),'n':name,'pl':'free','c':now,'l':now,'v':verified,'ip':ip_hash}).first()[0])
            member_number=100000 if is_configured_admin and not con.execute(text('SELECT 1 FROM users WHERE member_number=100000 AND id<>:i'),{'i':uid}).first() else 100000+uid
            con.execute(text('UPDATE users SET member_number=:m WHERE id=:i'),{'m':member_number,'i':uid})
            con.execute(text('INSERT INTO registration_attempts(ip_hash,email_key,created_at) VALUES(:i,:e,:c)'),{'i':ip_hash,'e':email_key,'c':now})
            ua=str(request.headers.get('User-Agent') or '')[:500]
            for ctype,version in (('terms',TERMS_VERSION),('privacy',PRIVACY_VERSION)):
                con.execute(text('INSERT INTO user_consents(user_id,consent_type,document_version,accepted_at,ip_hash,user_agent) VALUES(:u,:t,:v,:a,:ip,:ua)'),{'u':uid,'t':ctype,'v':version,'a':now,'ip':ip_hash,'ua':ua})
            code=_issue_verification(con,uid,email) if not verified else None
        if code:
            try: sent=_send_verification_email(email,code)
            except Exception: logging.exception('verification email failed');sent=False
            if not sent:
                with ENGINE.begin() as con: con.execute(text('DELETE FROM users WHERE id=:i'),{'i':uid})
                return jsonify(error='인증메일 발송 설정이 완료되지 않아 가입을 진행할 수 없습니다. 관리자에게 문의해 주세요.'),503
        session.clear();session.permanent=True;session['user_id']=uid;session['auth_version']=1
        user=_current_user();return jsonify(ok=True,user=user,auth_token=_issue_auth_token(uid,1),verification_required=bool(code))
    except IntegrityError:return jsonify(error='이미 가입된 이메일입니다. Gmail의 점(.) 또는 +별칭을 바꾼 주소도 같은 계정으로 처리됩니다.'),409

@app.post('/api/account/consents')
def account_consents():
    u,err=_require_user()
    if err:return err
    d=request.get_json(silent=True) or {}
    if not bool(d.get('terms_agreed')) or not bool(d.get('privacy_agreed')):
        return jsonify(error='이용약관과 개인정보 처리방침에 모두 동의해 주세요.'),400
    now=datetime.utcnow().isoformat(timespec='seconds')+'Z';ip_hash=_client_ip_hash();ua=str(request.headers.get('User-Agent') or '')[:500]
    with ENGINE.begin() as con:
        for ctype,version in (('terms',TERMS_VERSION),('privacy',PRIVACY_VERSION)):
            con.execute(text('INSERT INTO user_consents(user_id,consent_type,document_version,accepted_at,ip_hash,user_agent) VALUES(:u,:t,:v,:a,:ip,:ua)'),{'u':u['id'],'t':ctype,'v':version,'a':now,'ip':ip_hash,'ua':ua})
    return jsonify(ok=True,consents=_consent_status(u['id']))

def _normalize_login_identifier(value):
    raw=str(value or '').strip()
    # 모바일 키보드가 넣는 전각 숫자·공백·하이픈을 정리합니다.
    trans=str.maketrans('０１２３４５６７８９','0123456789')
    raw=raw.translate(trans)
    compact=re.sub(r'[\s\-]+','',raw)
    if re.fullmatch(r'\d{6,10}',compact):
        return compact,True
    return raw.lower(),False

@app.post('/api/account/login')
def account_login():
    d=request.get_json(silent=True) or {}
    identifier,is_member_number=_normalize_login_identifier(d.get('email') or d.get('identifier') or '')
    password=str(d.get('password') or '')
    if not identifier or not password:
        return jsonify(error='이메일 또는 회원번호와 비밀번호를 모두 입력해 주세요.',code='missing_credentials'),400
    now_dt=datetime.utcnow();now=now_dt.isoformat(timespec='seconds')+'Z'
    try:
        with ENGINE.connect() as con:
            if is_member_number:
                row=_row_dict(con.execute(text('SELECT * FROM users WHERE member_number=:m'),{'m':int(identifier)}).first())
            else:
                canonical=_canonical_email(identifier)
                row=_row_dict(con.execute(text('SELECT * FROM users WHERE email_key=:e OR LOWER(email)=:raw ORDER BY id ASC LIMIT 1'),{'e':canonical,'raw':identifier.lower()}).first())
                if not row and '@' in canonical:
                    domain=canonical.rsplit('@',1)[1]
                    candidates=con.execute(text('SELECT * FROM users WHERE LOWER(email) LIKE :d'),{'d':'%@'+domain}).fetchall()
                    row=next((_row_dict(x) for x in candidates if _canonical_email(x._mapping.get('email'))==canonical),None)
    except Exception:
        logging.exception('login database lookup failed')
        return jsonify(error='로그인 서버가 데이터베이스에 연결되지 않았습니다. 잠시 후 다시 시도해 주세요.',code='database_unavailable'),503

    configured_admin=bool(row and (_canonical_email(row.get('email')) in _admins() or int(row.get('member_number') or 0)==100000 or row.get('role')=='admin'))
    if configured_admin:
        try:
            with ENGINE.begin() as con:
                con.execute(text("UPDATE users SET role='admin',account_status='active' WHERE id=:i"),{'i':row['id']})
            row['role']='admin';row['account_status']='active'
        except Exception:
            logging.exception('admin account recovery failed')

    if row and row.get('locked_until'):
        try:
            locked=datetime.fromisoformat(str(row['locked_until']).replace('Z','+00:00')).replace(tzinfo=None)
            if locked>now_dt:
                mins=max(1,int((locked-now_dt).total_seconds()//60)+1)
                return jsonify(error=f'로그인 시도가 여러 번 실패해 계정이 잠시 보호되고 있습니다. 약 {mins}분 후 다시 시도해 주세요.',code='account_locked'),429
        except Exception:pass

    valid_password=False
    if row:
        try:valid_password=check_password_hash(str(row.get('password_hash') or ''),password)
        except Exception:logging.exception('stored password hash check failed user_id=%s',row.get('id'))
    if not row or not valid_password:
        if row:
            fails=int(row.get('failed_login_count') or 0)+1
            lock_until=(now_dt+timedelta(minutes=15)).isoformat(timespec='seconds')+'Z' if fails>=5 else None
            with ENGINE.begin() as con:
                con.execute(text('UPDATE users SET failed_login_count=:f,locked_until=:l WHERE id=:i'),{'f':0 if lock_until else fails,'l':lock_until,'i':row['id']})
            remaining=max(0,5-fails)
            msg='관리자 비밀번호가 맞지 않습니다.' if configured_admin else '이메일·회원번호 또는 비밀번호가 맞지 않습니다.'
            if lock_until:msg+=' 계정 보호를 위해 15분 동안 로그인이 제한됩니다.'
            elif remaining:msg+=f' {remaining}회 더 실패하면 15분 동안 로그인이 제한됩니다.'
        else:msg='가입된 계정을 찾지 못했습니다. 이메일 또는 회원번호를 다시 확인해 주세요.'
        return jsonify(error=msg,code='invalid_credentials'),401
    if row.get('account_status')=='suspended':
        return jsonify(error='이 계정은 이용이 중지되었습니다. 고객지원에 문의해 주세요.',code='account_suspended'),403

    try:
        with ENGINE.begin() as con:
            con.execute(text('UPDATE users SET last_login_at=:n,failed_login_count=0,locked_until=NULL WHERE id=:i'),{'n':now,'i':row['id']})
        session.clear();session.permanent=True
        session['user_id']=int(row['id']);session['auth_version']=int(row.get('auth_version') or 1)
        user=_load_auth_user(row['id'],row.get('auth_version') or 1)
        if not user:raise RuntimeError('authenticated user lookup failed')
        token=_issue_auth_token(row['id'],row.get('auth_version') or 1)
        return _auth_json_response({
            'ok':True,'user':user,'auth_token':token,'server_version':'6.9.4',
            'message':'관리자 계정으로 로그인했습니다.' if user.get('is_admin') else '로그인했습니다.'
        },token=token)
    except Exception:
        logging.exception('login session creation failed user_id=%s',row.get('id'))
        session.clear()
        return jsonify(error='로그인 처리 중 서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.',code='login_server_error'),500

@app.get('/api/account/login-status')
def account_login_status():
    try:
        with ENGINE.connect() as con:con.execute(text('SELECT 1')).scalar_one()
        return jsonify(ok=True,database=True,secure_cookie=bool(app.config.get('SESSION_COOKIE_SECURE')),version='6.9.4')
    except Exception:
        logging.exception('login status database failed')
        return jsonify(ok=False,database=False,error='로그인 데이터베이스 연결 실패',version='6.9.4'),503

@app.post('/api/account/verify-email')
def account_verify_email():
    u,err=_require_user()
    if err:return err
    d=request.get_json(silent=True) or {};code=str(d.get('code') or '').strip()
    if not re.fullmatch(r'\d{6}',code):return jsonify(error='6자리 인증번호를 입력해 주세요.'),400
    with ENGINE.connect() as con: row=_row_dict(con.execute(text('SELECT verification_code_hash,verification_expires_at,email_verified FROM users WHERE id=:i'),{'i':u['id']}).first())
    if row and row.get('email_verified'):return jsonify(ok=True,user=_current_user())
    if not row or not row.get('verification_code_hash'):return jsonify(error='발급된 인증번호가 없습니다.'),400
    if not row.get('verification_expires_at') or row['verification_expires_at']<datetime.utcnow().isoformat(timespec='seconds')+'Z':return jsonify(error='인증번호가 만료되었습니다. 인증메일을 다시 보내 주세요.'),400
    digest=hashlib.sha256((app.config['SECRET_KEY']+'|'+code).encode()).hexdigest()
    if not secrets.compare_digest(digest,row['verification_code_hash']):return jsonify(error='인증번호가 맞지 않습니다.'),400
    with ENGINE.begin() as con: con.execute(text('UPDATE users SET email_verified=1,verification_code_hash=NULL,verification_expires_at=NULL WHERE id=:i'),{'i':u['id']})
    return jsonify(ok=True,user=_current_user())

@app.post('/api/account/resend-verification')
def account_resend_verification():
    u,err=_require_user()
    if err:return err
    if u.get('email_verified'):return jsonify(ok=True)
    with ENGINE.begin() as con: code=_issue_verification(con,u['id'],u['email'])
    try: sent=_send_verification_email(u['email'],code)
    except Exception: logging.exception('verification resend failed');sent=False
    if not sent:return jsonify(error='인증메일 발송 설정을 확인해 주세요.'),503
    return jsonify(ok=True)

@app.post('/api/account/request-password-reset')
def account_request_password_reset():
    d=request.get_json(silent=True) or {};email=str(d.get('email') or '').strip().lower()
    generic='가입된 계정이 확인되면 비밀번호 재설정 인증번호를 이메일로 보냈습니다.'
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):return jsonify(error='이메일 형식을 확인해 주세요.'),400
    email_key=_canonical_email(email);now=datetime.utcnow();now_s=now.isoformat(timespec='seconds')+'Z'
    with ENGINE.connect() as con:row=_row_dict(con.execute(text('SELECT id,email,reset_requested_at FROM users WHERE email_key=:e'),{'e':email_key}).first())
    if not row:return jsonify(ok=True,message=generic)
    if row.get('reset_requested_at'):
        try:
            last=datetime.fromisoformat(row['reset_requested_at'].replace('Z','+00:00')).replace(tzinfo=None)
            if (now-last).total_seconds()<60:return jsonify(ok=True,message=generic)
        except Exception:pass
    code=f'{secrets.randbelow(1000000):06d}';digest=hashlib.sha256((app.config['SECRET_KEY']+'|reset|'+code).encode()).hexdigest();expires=(now+timedelta(minutes=10)).isoformat(timespec='seconds')+'Z'
    with ENGINE.begin() as con:con.execute(text('UPDATE users SET reset_code_hash=:h,reset_expires_at=:x,reset_requested_at=:r WHERE id=:i'),{'h':digest,'x':expires,'r':now_s,'i':row['id']})
    try:sent=_send_password_reset_email(row['email'],code)
    except Exception:logging.exception('password reset email failed');sent=False
    if not sent:return jsonify(error='비밀번호 재설정 메일 발송 설정을 확인해 주세요.'),503
    return jsonify(ok=True,message=generic)

@app.post('/api/account/reset-password')
def account_reset_password():
    d=request.get_json(silent=True) or {};email=str(d.get('email') or '').strip().lower();code=str(d.get('code') or '').strip();new=str(d.get('new_password') or '')
    if not re.fullmatch(r'\d{6}',code):return jsonify(error='이메일로 받은 6자리 인증번호를 입력해 주세요.'),400
    if len(new)<10 or not re.search(r'[A-Za-z]',new) or not re.search(r'\d',new):return jsonify(error='새 비밀번호는 영문과 숫자를 포함해 10자 이상이어야 합니다.'),400
    email_key=_canonical_email(email)
    with ENGINE.connect() as con:row=_row_dict(con.execute(text('SELECT id,reset_code_hash,reset_expires_at,auth_version FROM users WHERE email_key=:e'),{'e':email_key}).first())
    if not row or not row.get('reset_code_hash'):return jsonify(error='재설정 요청을 다시 진행해 주세요.'),400
    now_s=datetime.utcnow().isoformat(timespec='seconds')+'Z'
    if not row.get('reset_expires_at') or row['reset_expires_at']<now_s:return jsonify(error='인증번호가 만료되었습니다. 인증번호를 다시 받아 주세요.'),400
    digest=hashlib.sha256((app.config['SECRET_KEY']+'|reset|'+code).encode()).hexdigest()
    if not secrets.compare_digest(digest,row['reset_code_hash']):return jsonify(error='인증번호가 맞지 않습니다.'),400
    with ENGINE.begin() as con:con.execute(text('UPDATE users SET password_hash=:p,reset_code_hash=NULL,reset_expires_at=NULL,auth_version=auth_version+1 WHERE id=:i'),{'p':generate_password_hash(new),'i':row['id']})
    session.clear();return jsonify(ok=True)

@app.post('/api/account/logout')
def account_logout():
    session.clear();return _clear_auth_response({'ok':True})

@app.post('/api/account/logout-all')
def account_logout_all():
    u,err=_require_user()
    if err:return err
    with ENGINE.begin() as con:
        con.execute(text('UPDATE users SET auth_version=auth_version+1 WHERE id=:i'),{'i':u['id']})
        row=con.execute(text('SELECT auth_version FROM users WHERE id=:i'),{'i':u['id']}).first()
    session.clear();session.permanent=True;session['user_id']=u['id'];session['auth_version']=int(row[0])
    token=_issue_auth_token(u['id'],int(row[0]))
    return _auth_json_response({'ok':True,'auth_token':token,'message':'현재 기기를 제외한 모든 기기에서 로그아웃했습니다.'},token=token)

@app.get('/api/account/export')
def account_export():
    u,err=_require_user()
    if err:return err
    with ENGINE.connect() as con:
        profile=_row_dict(con.execute(text('SELECT member_number,email,display_name,plan,created_at,last_login_at,email_verified,plan_started_at,plan_expires_at FROM users WHERE id=:i'),{'i':u['id']}).first())
        snap=_row_dict(con.execute(text('SELECT payload,version,updated_at FROM user_snapshots WHERE user_id=:i'),{'i':u['id']}).first())
        usages=[dict(r._mapping) for r in con.execute(text('SELECT month_key,analysis_count,updated_at FROM monthly_usage WHERE user_id=:i ORDER BY month_key DESC'),{'i':u['id']}).fetchall()]
    payload={}
    if snap and snap.get('payload'):
        try:payload=json.loads(snap['payload'])
        except Exception:payload={}
    out={'service':'리셀 PICK','exported_at':datetime.utcnow().isoformat(timespec='seconds')+'Z','profile':profile or {},'usage':usages,'cloud':{'version':(snap or {}).get('version',0),'updated_at':(snap or {}).get('updated_at'),'payload':payload}}
    raw=json.dumps(out,ensure_ascii=False,indent=2)
    filename='resell_pick_my_data_'+datetime.utcnow().strftime('%Y%m%d_%H%M')+'.json'
    return Response(raw,mimetype='application/json; charset=utf-8',headers={'Content-Disposition':f'attachment; filename={filename}','Cache-Control':'no-store'})

@app.post('/api/account/change-password')
def account_change_password():
    u,err=_require_user()
    if err:return err
    d=request.get_json(silent=True) or {};old=str(d.get('old_password') or '');new=str(d.get('new_password') or '')
    if len(new)<10 or not re.search(r'[A-Za-z]',new) or not re.search(r'\d',new):return jsonify(error='새 비밀번호는 영문과 숫자를 포함해 10자 이상이어야 합니다.'),400
    with ENGINE.connect() as con:row=_row_dict(con.execute(text('SELECT password_hash FROM users WHERE id=:i'),{'i':u['id']}).first())
    if not row or not check_password_hash(row['password_hash'],old):return jsonify(error='현재 비밀번호가 맞지 않습니다.'),401
    with ENGINE.begin() as con:
        con.execute(text('UPDATE users SET password_hash=:p,auth_version=auth_version+1 WHERE id=:i'),{'p':generate_password_hash(new),'i':u['id']})
    session['auth_version']=int(u.get('auth_version') or 1)+1
    return jsonify(ok=True,auth_token=_issue_auth_token(u['id'],session['auth_version']))

@app.delete('/api/account')
def account_delete():
    u,err=_require_user()
    if err:return err
    d=request.get_json(silent=True) or {};password=str(d.get('password') or '')
    with ENGINE.connect() as con:row=_row_dict(con.execute(text('SELECT password_hash FROM users WHERE id=:i'),{'i':u['id']}).first())
    if not row or not check_password_hash(row['password_hash'],password):return jsonify(error='비밀번호가 맞지 않습니다.'),401
    with ENGINE.begin() as con:con.execute(text('DELETE FROM users WHERE id=:i'),{'i':u['id']})
    session.clear();return jsonify(ok=True)

@app.get('/api/cloud/snapshot')
def cloud_snapshot_get():
    u,err=_require_user()
    if err:return err
    with ENGINE.connect() as con:row=_row_dict(con.execute(text('SELECT payload,version,updated_at FROM user_snapshots WHERE user_id=:u'),{'u':u['id']}).first())
    if not row:return jsonify(payload=None,version=0,updated_at=None)
    try:payload=json.loads(row['payload'])
    except Exception:payload={}
    return jsonify(payload=payload,version=row['version'],updated_at=row['updated_at'])

@app.put('/api/cloud/snapshot')
def cloud_snapshot_put():
    u,err=_require_user()
    if err:return err
    d=request.get_json(silent=True) or {};payload=d.get('payload');client_version=int(d.get('version') or 0)
    if not isinstance(payload,dict):return jsonify(error='저장할 데이터 형식이 올바르지 않습니다.'),400
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    if len(raw.encode())>8*1024*1024:return jsonify(error='클라우드 저장 데이터가 8MB를 초과했습니다.'),413
    now=datetime.utcnow().isoformat(timespec='seconds')+'Z'
    with ENGINE.begin() as con:
        current=_row_dict(con.execute(text('SELECT version FROM user_snapshots WHERE user_id=:u FOR UPDATE'),{'u':u['id']}).first())
        if current and client_version and int(current['version'])!=client_version:return jsonify(error='다른 기기에서 데이터가 변경되었습니다. 먼저 클라우드 데이터를 불러온 뒤 다시 저장해 주세요.',conflict=True,server_version=current['version']),409
        if current:
            newv=int(current['version'])+1;con.execute(text('UPDATE user_snapshots SET payload=:p,version=:v,updated_at=:t WHERE user_id=:u'),{'p':raw,'v':newv,'t':now,'u':u['id']})
        else:
            newv=1;con.execute(text('INSERT INTO user_snapshots(user_id,payload,version,updated_at) VALUES(:u,:p,:v,:t)'),{'u':u['id'],'p':raw,'v':newv,'t':now})
    return jsonify(ok=True,version=newv,updated_at=now)

def _require_admin():
    u,err=_require_user()
    if err:return None,err
    if not u.get('is_admin'):return None,(jsonify(error='운영자 권한이 없습니다.'),403)
    return u,None

def _audit(admin_id,action,target_user_id=None,detail=''):
    with ENGINE.begin() as con:
        con.execute(text('INSERT INTO admin_audit_logs(admin_user_id,action,target_user_id,detail,created_at) VALUES(:a,:x,:t,:d,:c)'),{'a':admin_id,'x':action,'t':target_user_id,'d':str(detail)[:1000],'c':datetime.utcnow().isoformat(timespec='seconds')+'Z'})

@app.get('/api/admin/summary')
def admin_summary():
    u,err=_require_admin()
    if err:return err
    today=datetime.utcnow().strftime('%Y-%m-%d')
    month=_month_key()
    with ENGINE.connect() as con:
        users=int(con.execute(text('SELECT COUNT(*) FROM users')).scalar_one())
        paid=int(con.execute(text("SELECT COUNT(*) FROM users WHERE plan<>'free' AND account_status='active'")).scalar_one())
        verified=int(con.execute(text('SELECT COUNT(*) FROM users WHERE email_verified=1')).scalar_one())
        suspended=int(con.execute(text("SELECT COUNT(*) FROM users WHERE account_status='suspended'")).scalar_one())
        today_new=int(con.execute(text('SELECT COUNT(*) FROM users WHERE created_at LIKE :d'),{'d':today+'%'}).scalar_one())
        usage=int(con.execute(text('SELECT COALESCE(SUM(analysis_count),0) FROM monthly_usage WHERE month_key=:m'),{'m':month}).scalar_one())
    return jsonify(users=users,paid_users=paid,verified_users=verified,suspended_users=suspended,today_new=today_new,month_analysis=usage,database='PostgreSQL' if DATABASE_URL else 'SQLite 테스트',server_time=datetime.utcnow().isoformat(timespec='seconds')+'Z')

@app.get('/api/admin/users')
def admin_users():
    u,err=_require_admin()
    if err:return err
    q=str(request.args.get('q') or '').strip().lower()[:100]
    limit=min(100,max(1,int(request.args.get('limit') or 50)))
    params={'lim':limit}
    where=''
    if q:
        where='WHERE LOWER(email) LIKE :q OR LOWER(display_name) LIKE :q OR CAST(member_number AS TEXT) LIKE :q'
        params['q']='%'+q+'%'
    sql=f'''SELECT id,member_number,email,display_name,plan,role,account_status,email_verified,created_at,last_login_at,plan_started_at,plan_expires_at
            FROM users {where} ORDER BY id DESC LIMIT :lim'''
    with ENGINE.connect() as con: rows=[dict(r._mapping) for r in con.execute(text(sql),params).fetchall()]
    for r in rows:
        r['email_verified']=bool(r.get('email_verified'))
        r['is_admin']=r.get('role')=='admin'
    return jsonify(users=rows)


@app.get('/api/admin/users-export.csv')
def admin_users_export():
    admin,err=_require_admin()
    if err:return err
    with ENGINE.connect() as con:
        rows=[dict(r._mapping) for r in con.execute(text('''SELECT member_number,email,display_name,plan,role,account_status,email_verified,created_at,last_login_at,plan_started_at,plan_expires_at FROM users ORDER BY id ASC''')).fetchall()]
    out=StringIO();out.write('\ufeff')
    writer=csv.writer(out)
    writer.writerow(['회원번호','이메일','이름','이용권','권한','계정상태','이메일인증','가입일','최근로그인','이용권시작','이용권만료'])
    for r in rows:
        writer.writerow([r.get('member_number') or '',r.get('email') or '',r.get('display_name') or '',r.get('plan') or 'free',r.get('role') or 'user',r.get('account_status') or 'active','예' if r.get('email_verified') else '아니오',r.get('created_at') or '',r.get('last_login_at') or '',r.get('plan_started_at') or '',r.get('plan_expires_at') or ''])
    _audit(admin['id'],'export_users',None,f'{len(rows)}명')
    filename='resell_pick_members_'+datetime.utcnow().strftime('%Y%m%d_%H%M')+'.csv'
    return Response(out.getvalue(),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename={filename}','Cache-Control':'no-store'})

@app.post('/api/admin/users/<int:uid>/plan')
def admin_user_plan(uid):
    admin,err=_require_admin()
    if err:return err
    d=request.get_json(silent=True) or {};plan=str(d.get('plan') or '').lower();days=int(d.get('days') or 0)
    if plan not in {'free','pro','proplus'}:return jsonify(error='올바른 이용권을 선택해 주세요.'),400
    if days<0 or days>3660:return jsonify(error='이용기간은 0~3660일 범위로 입력해 주세요.'),400
    now=datetime.utcnow();start=now.isoformat(timespec='seconds')+'Z';expiry=(now+timedelta(days=days)).isoformat(timespec='seconds')+'Z' if plan!='free' and days>0 else None
    with ENGINE.begin() as con:
        row=con.execute(text('SELECT role FROM users WHERE id=:i'),{'i':uid}).first()
        if not row:return jsonify(error='회원을 찾을 수 없습니다.'),404
        con.execute(text('UPDATE users SET plan=:p,plan_started_at=:s,plan_expires_at=:e WHERE id=:i'),{'p':plan,'s':start if plan!='free' else None,'e':expiry,'i':uid})
    _audit(admin['id'],'change_plan',uid,json.dumps({'plan':plan,'days':days},ensure_ascii=False))
    return jsonify(ok=True)

@app.post('/api/admin/users/<int:uid>/status')
def admin_user_status(uid):
    admin,err=_require_admin()
    if err:return err
    d=request.get_json(silent=True) or {};status=str(d.get('status') or '').lower()
    if status not in {'active','suspended'}:return jsonify(error='올바른 계정 상태를 선택해 주세요.'),400
    if uid==admin['id']:return jsonify(error='현재 로그인한 관리자 계정은 중지할 수 없습니다.'),400
    with ENGINE.begin() as con:
        row=con.execute(text('SELECT role FROM users WHERE id=:i'),{'i':uid}).first()
        if not row:return jsonify(error='회원을 찾을 수 없습니다.'),404
        if row[0]=='admin':return jsonify(error='다른 관리자 계정은 이 화면에서 중지할 수 없습니다.'),400
        con.execute(text('UPDATE users SET account_status=:s,auth_version=auth_version+1 WHERE id=:i'),{'s':status,'i':uid})
    _audit(admin['id'],'change_status',uid,status)
    return jsonify(ok=True)

@app.get('/api/admin/audit-logs')
def admin_audit_logs():
    u,err=_require_admin()
    if err:return err
    with ENGINE.connect() as con:
        rows=[dict(r._mapping) for r in con.execute(text('SELECT action,target_user_id,detail,created_at FROM admin_audit_logs ORDER BY id DESC LIMIT 50')).fetchall()]
    return jsonify(logs=rows)

@app.get('/api/notices')
def public_notices():
    now=datetime.utcnow().isoformat(timespec='seconds')+'Z'
    with ENGINE.connect() as con:
        rows=[dict(r._mapping) for r in con.execute(text("""SELECT id,title,body,starts_at,ends_at,updated_at FROM service_notices
          WHERE is_active=1 AND (starts_at IS NULL OR starts_at='' OR starts_at<=:n)
          AND (ends_at IS NULL OR ends_at='' OR ends_at>=:n) ORDER BY id DESC LIMIT 5"""),{'n':now}).fetchall()]
    return jsonify(notices=rows)

@app.get('/api/admin/notices')
def admin_notices():
    admin,err=_require_admin()
    if err:return err
    with ENGINE.connect() as con:
        rows=[dict(r._mapping) for r in con.execute(text('SELECT id,title,body,is_active,starts_at,ends_at,created_at,updated_at FROM service_notices ORDER BY id DESC LIMIT 50')).fetchall()]
    return jsonify(notices=rows)

@app.post('/api/admin/notices')
def admin_notice_create():
    admin,err=_require_admin()
    if err:return err
    d=request.get_json(silent=True) or {};title=str(d.get('title') or '').strip()[:120];body=str(d.get('body') or '').strip()[:5000]
    starts=str(d.get('starts_at') or '').strip() or None;ends=str(d.get('ends_at') or '').strip() or None;active=1 if d.get('is_active',True) else 0
    if not title or not body:return jsonify(error='공지 제목과 내용을 입력해 주세요.'),400
    if starts and ends and ends<starts:return jsonify(error='종료일은 시작일보다 뒤여야 합니다.'),400
    now=datetime.utcnow().isoformat(timespec='seconds')+'Z'
    with ENGINE.begin() as con:
        nid=int(con.execute(text('INSERT INTO service_notices(title,body,is_active,starts_at,ends_at,created_by,created_at,updated_at) VALUES(:t,:b,:a,:s,:e,:u,:c,:c) RETURNING id'),{'t':title,'b':body,'a':active,'s':starts,'e':ends,'u':admin['id'],'c':now}).first()[0])
    _audit(admin['id'],'create_notice',None,f'notice_id={nid}; title={title}')
    return jsonify(ok=True,id=nid)

@app.post('/api/admin/notices/<int:nid>/status')
def admin_notice_status(nid):
    admin,err=_require_admin()
    if err:return err
    d=request.get_json(silent=True) or {};active=1 if d.get('is_active') else 0
    with ENGINE.begin() as con:
        row=con.execute(text('SELECT id FROM service_notices WHERE id=:i'),{'i':nid}).first()
        if not row:return jsonify(error='공지를 찾을 수 없습니다.'),404
        con.execute(text('UPDATE service_notices SET is_active=:a,updated_at=:t WHERE id=:i'),{'a':active,'t':datetime.utcnow().isoformat(timespec='seconds')+'Z','i':nid})
    _audit(admin['id'],'change_notice_status',None,f'notice_id={nid}; active={active}')
    return jsonify(ok=True)

@app.delete('/api/admin/notices/<int:nid>')
def admin_notice_delete(nid):
    admin,err=_require_admin()
    if err:return err
    with ENGINE.begin() as con:
        row=con.execute(text('SELECT title FROM service_notices WHERE id=:i'),{'i':nid}).first()
        if not row:return jsonify(error='공지를 찾을 수 없습니다.'),404
        con.execute(text('DELETE FROM service_notices WHERE id=:i'),{'i':nid})
    _audit(admin['id'],'delete_notice',None,f'notice_id={nid}; title={row[0]}')
    return jsonify(ok=True)

def cli():
    k=os.getenv('OPENAI_API_KEY')
    if not k: raise RuntimeError('OPENAI_API_KEY가 설정되지 않았습니다.')
    return OpenAI(api_key=k, timeout=60.0, max_retries=1)
def parse(t):
    raw=(t or '').strip()
    raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw,flags=re.I|re.S).strip()
    start=raw.find('{')
    if start<0: raise ValueError('JSON 응답을 찾지 못했습니다.')
    raw=raw[start:]
    # 먼저 표준 JSON과 JSON5를 순서대로 시도한다.
    for loader in (json.loads,json5.loads):
        try:return loader(raw)
        except Exception:pass
    # 모델이 줄바꿈 사이 쉼표를 빠뜨리거나 끝부분을 조금 잘랐을 때 자동 복구한다.
    repaired=raw
    repaired=re.sub(r',\s*([}\]])',r'\1',repaired)
    repaired=re.sub(r'([0-9truefalsenull"\]\}])\s*\n\s*(")',r'\1,\n\2',repaired,flags=re.I)
    repaired=re.sub(r'(}\s*)({)',r'\1,\2',repaired)
    # 문자열 내부가 아닌 괄호를 세어 잘린 응답의 닫는 괄호를 보충한다.
    in_str=False;esc=False;stack=[]
    for ch in repaired:
        if in_str:
            if esc:esc=False
            elif ch=='\\':esc=True
            elif ch=='"':in_str=False
        else:
            if ch=='"':in_str=True
            elif ch in '[{':stack.append(ch)
            elif ch in ']}' and stack:stack.pop()
    if in_str:repaired+='"'
    repaired+=''.join('}' if ch=='{' else ']' for ch in reversed(stack))
    for loader in (json.loads,json5.loads):
        try:return loader(repaired)
        except Exception:pass
    raise ValueError('AI 응답 JSON 자동 복구에 실패했습니다.')

def _clean_code(v):
    return re.sub(r'[^A-Z0-9-]', '', str(v or '').upper())

def _model_score(code, brand=''):
    c=_clean_code(code)
    if not c or len(c)<5 or len(c)>14:
        return -999
    # 바코드/일련번호로 보이는 긴 문자열은 강하게 제외
    if len(c)>=13:
        return -80
    if re.fullmatch(r'\d{8,}', c):
        return -100
    score=0
    patterns=[
        r'^(?:U|M|ML|MR|BB|CM|MS|MT|WR|WL|GC|GS)\d{3,4}[A-Z0-9]{1,5}$', # New Balance
        r'^(?:DD|DV|FD|DQ|CZ|DH|DR|FB|FN|HF|HJ|HV)\d{4}-?\d{3}$', # Nike
        r'^(?:IF|IG|ID|IE|IH|JI|JR|JS|GX|GY|HQ|HP|H0)\d{4}$', # Adidas
        r'^12(?:01|03)[A-Z]\d{3}-?\d{3}$', # ASICS
        r'^[A-Z]{1,3}\d{3,5}[A-Z0-9]{1,5}$' # generic sneaker model
    ]
    for i,pat in enumerate(patterns):
        if re.fullmatch(pat,c):
            score=max(score,100-i*8)
    if re.search(r'[A-Z]',c) and re.search(r'\d',c): score+=12
    if 7<=len(c)<=10: score+=15
    if '-' in c: score+=3
    # 라벨 내부관리번호/바코드 계열로 자주 보이는 패턴 감점
    if c.startswith(('NBPDFS','NBP','EAN','UPC','SKU')): score-=120
    if re.search(r'\d{5,}$',c) and len(c)>11: score-=45
    return score


WATERMARK_WORDS = ('올데이픽', 'ALLDAYPICK', 'ALL DAY PICK', 'AI 소싱계산기', 'AI소싱계산기')

def _remove_watermark_text(v):
    text=str(v or '')
    for word in WATERMARK_WORDS:
        text=re.sub(re.escape(word), ' ', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip(' -_·|/')

def _pick_best_model(candidates, brand=''):
    cleaned=[]
    for x in candidates:
        c=_clean_code(x)
        if c and c not in cleaned:
            cleaned.append(c)
    ranked=sorted(((_model_score(x,brand), x) for x in cleaned), reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] >= 70 else ''

def normalize_general_result(d):
    d=dict(d or {})
    text_fields=('brand','product_name','variant','product_code','manufacturer','origin','volume','color','promotion','coupang_query','fallback_query','internal_code')
    for key in text_fields:
        d[key]=_remove_watermark_text(d.get(key))
    for key in ('design_features','visible_text','warnings'):
        vals=d.get(key) or []
        d[key]=[x for x in (_remove_watermark_text(v) for v in vals) if x]

    # 신발 라벨에서는 내부 관리번호가 아니라 실제 브랜드 스타일코드를 상품코드로 선택한다.
    # 예: NBPDFS193I(내부번호) / U9060ECA(상품코드) / NBPDFS193I39240(바코드문자열)
    candidates=[d.get('product_code'), d.get('model_no'), d.get('style_code'), d.get('article_no')]
    for item in d.get('model_candidates') or []:
        if isinstance(item,dict):
            if item.get('role') == 'model': candidates.insert(0,item.get('text') or item.get('code'))
            elif item.get('role') not in ('internal','barcode_text'): candidates.append(item.get('text') or item.get('code'))
        else: candidates.append(item)
    for line in d.get('visible_text') or []:
        candidates.extend(re.findall(r'\b[A-Z]{1,3}[A-Z0-9-]{4,13}\b', str(line).upper()))
    best=_pick_best_model(candidates,d.get('brand',''))
    if best: d['product_code']=best

    # 정상적인 숫자형 EAN/UPC/GTIN만 일반 바코드로 유지한다.
    raw=str(d.get('barcode') or '')
    digits=re.sub(r'\D','',raw)
    d['barcode']=digits if 8 <= len(digits) <= 14 else ''

    # 잘못 인식된 내부번호가 쿠팡 검색어에 들어가지 않게 검색어를 서버에서 다시 조립한다.
    if best:
        parts=[d.get('brand'), d.get('product_name'), d.get('variant'), best]
        if d.get('size_mm'): parts.append(str(d.get('size_mm')))
        elif d.get('volume'): parts.append(d.get('volume'))
        if d.get('count'): parts.append(str(d.get('count'))+'개')
        d['coupang_query']=' '.join(str(x).strip() for x in parts if x and str(x).strip())
    else:
        d['coupang_query']=_remove_watermark_text(d.get('coupang_query'))
    d['fallback_query']=d['barcode'] or _remove_watermark_text(d.get('fallback_query'))
    d.pop('model_candidates',None)
    return d

def normalize_sneaker_result(d):
    d=dict(d or {})
    candidates=[]
    for key in ('model_no','style_code','article_no','product_code'):
        if d.get(key): candidates.append(d.get(key))
    for x in d.get('model_candidates') or []:
        if isinstance(x,dict): candidates.append(x.get('text') or x.get('code'))
        else: candidates.append(x)
    ranked=sorted(((_model_score(x,d.get('brand','')), _clean_code(x)) for x in candidates), reverse=True)
    best=ranked[0][1] if ranked and ranked[0][0]>0 else _clean_code(d.get('model_no'))
    d['model_no']=best
    d['barcode']=_clean_code(d.get('barcode'))
    d['internal_code']=_clean_code(d.get('internal_code'))
    d.pop('model_candidates',None)
    return d

# 짧은 서버 메모리 캐시: 같은 서버 인스턴스에서 같은 사진/키워드가 반복될 때 API 재호출 방지
_CACHE={}
_CACHE_LOCK=threading.Lock()
_CACHE_MAX=1000

def _cache_get(key,ttl):
    now=time.time()
    with _CACHE_LOCK:
        row=_CACHE.get(key)
        if not row:return None
        if now-row[0]>ttl:
            _CACHE.pop(key,None);return None
        return row[1]

def _cache_set(key,value):
    with _CACHE_LOCK:
        if len(_CACHE)>=_CACHE_MAX:
            for k,_ in sorted(_CACHE.items(),key=lambda x:x[1][0])[:100]:_CACHE.pop(k,None)
        _CACHE[key]=(time.time(),value)

def _friendly_openai_error(exc):
    msg=str(exc or '')
    if 'insufficient_quota' in msg or 'exceeded your current quota' in msg:
        return 'AI 분석 사용 한도가 소진되었습니다. OpenAI API 크레딧과 월 지출 한도를 확인해 주세요.',429
    if 'rate_limit' in msg.lower() or '429' in msg:
        return 'AI 요청이 잠시 몰렸습니다. 잠시 후 다시 시도해 주세요.',429
    if '401' in msg or 'api key' in msg.lower():
        return 'AI 서버 인증 설정을 확인해 주세요.',401
    if 'timeout' in msg.lower():
        return 'AI 서버 응답이 늦습니다. 잠시 후 다시 시도해 주세요.',504
    return 'AI 분석 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.',502


# 각 실제 OpenAI 호출의 응답 토큰을 기준으로 예상 비용을 계산한다.
# 이 값은 앱 내 사용량 표시용이며 OpenAI 결제 페이지의 실제 청구액과 소수점 차이가 날 수 있다.
_MODEL_PRICES={
    'gpt-4.1-mini':(0.40,1.60),
    'gpt-4.1':(2.00,8.00),
    'gpt-4o-mini':(0.15,0.60),
    'gpt-4o':(2.50,10.00),
}
def _usage_value(obj,*names):
    for name in names:
        try:
            value=getattr(obj,name,None)
            if value is None and isinstance(obj,dict): value=obj.get(name)
            if value is not None:return int(value or 0)
        except Exception:pass
    return 0

def _api_usage_meta(response,model,kind='analysis'):
    usage=getattr(response,'usage',None)
    input_tokens=_usage_value(usage,'input_tokens','prompt_tokens')
    output_tokens=_usage_value(usage,'output_tokens','completion_tokens')
    total_tokens=_usage_value(usage,'total_tokens') or input_tokens+output_tokens
    model_name=str(model or '')
    input_rate,output_rate=_MODEL_PRICES.get(model_name,(float(os.getenv('OPENAI_INPUT_USD_PER_M','0.40')),float(os.getenv('OPENAI_OUTPUT_USD_PER_M','1.60'))))
    estimated=(input_tokens*input_rate+output_tokens*output_rate)/1_000_000
    return {'month':datetime.utcnow().strftime('%Y-%m'),'model':model_name,'kind':kind,'input_tokens':input_tokens,'output_tokens':output_tokens,'total_tokens':total_tokens,'estimated_usd':round(estimated,8),'cached':False,'occurred_at':datetime.utcnow().isoformat(timespec='seconds')+'Z'}

def _cached_result(value):
    try:out=json.loads(json.dumps(value,ensure_ascii=False))
    except Exception:out=dict(value or {})
    out['_api_usage']={'month':datetime.utcnow().strftime('%Y-%m'),'model':'','kind':'cache','input_tokens':0,'output_tokens':0,'total_tokens':0,'estimated_usd':0,'cached':True,'occurred_at':datetime.utcnow().isoformat(timespec='seconds')+'Z'}
    return out

def vision(prompt,tokens=500,multiple=False):
    files=request.files.getlist('images') if multiple else [request.files.get('image')]
    files=[f for f in files if f]
    if not files: return None,('사진 파일이 없습니다.',400)
    content=[{'type':'input_text','text':prompt}]
    digest=hashlib.sha256(prompt.encode('utf-8'))
    blobs=[]
    for f in files[:6]:
        mime=f.mimetype or 'image/jpeg'
        if mime not in ALLOWED:return None,('JPG, PNG, WEBP만 지원합니다.',400)
        blob=f.read();blobs.append((mime,blob));digest.update(blob)
    cache_key='vision:'+digest.hexdigest()
    cached=_cache_get(cache_key,90*86400)
    if cached is not None:return _cached_result(cached),None
    for mime,blob in blobs:
        url=f'data:{mime};base64,{base64.b64encode(blob).decode()}'
        detail=os.getenv('OPENAI_IMAGE_DETAIL','auto').strip().lower()
        if detail not in {'low','high','auto'}:detail='auto'
        content.append({'type':'input_image','image_url':url,'detail':detail})
    reserved_user,access_error=_analysis_access()
    if access_error:
        response,status=access_error
        try:message=response.get_json().get('error')
        except Exception:message='분석 제공량을 확인해 주세요.'
        return None,(message,status)
    try:
        configured=os.getenv('OPENAI_VISION_MODEL','').strip()
        candidates=[x.strip() for x in configured.split(',') if x.strip()] if configured else []
        candidates += [os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'gpt-4.1-mini']
        seen=set();last_error=None
        for model in candidates:
            if not model or model in seen:continue
            seen.add(model)
            try:
                with _AI_SEMAPHORE:
                    r=cli().responses.create(model=model,input=[{'role':'user','content':content}],max_output_tokens=tokens)
                data=parse(r.output_text);data['_api_usage']=_api_usage_meta(r,model,'vision');_cache_set(cache_key,data);return data,None
            except Exception as exc:
                last_error=exc
                logging.warning('vision model failed model=%s error=%s',model,str(exc)[:180])
        raise last_error or RuntimeError('사진 분석 모델을 사용할 수 없습니다.')
    except Exception as exc:
        if reserved_user:_rollback_analysis(reserved_user['id'])
        return None,_friendly_openai_error(exc)
@app.errorhandler(413)
def too_large(_):
    if request.path.startswith('/api/'): return jsonify(error='사진 용량이 너무 큽니다. 최신 앱은 전송 전 자동 압축합니다. 새로고침 후 다시 시도하세요.'),413
    return '파일 용량이 너무 큽니다.',413

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'): return jsonify(error='분석 API를 찾지 못했습니다. 최신 버전이 정상 배포됐는지 확인하세요.'),404
    return send_from_directory('.', 'index.html')

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/api/'): return jsonify(error='분석 서버 내부 오류가 발생했습니다. 잠시 후 다시 시도하세요.'),500
    return '서버 오류',500

@app.get('/')
def home():return send_from_directory('.','index.html')
@app.get('/<path:p>')
def static_file(p):return send_from_directory('.',p)
GENERAL_PRODUCT_PROMPT = '''일반상품 소싱용 사진을 정밀 분석한다.

중요: 사진에 앱이 자동으로 넣은 워터마크, 촬영앱 이름, 화면 상단·하단의 UI 글자는 상품 정보가 아니다. 특히 '올데이픽', 'ALLDAYPICK', 'ALL DAY PICK', 'AI 소싱계산기' 문구는 brand, product_name, variant, visible_text, design_features, coupang_query 등 모든 결과에서 완전히 제외한다. 워터마크와 실제 포장 인쇄를 혼동하지 않는다. 상품 본체, 포장 앞면/뒷면, 가격표, 신발 박스 라벨 중 하나일 수 있다. 사진에 실제로 보이는 정보만 사용하고 추측하지 않는다.

반드시 확인할 항목:
1. 브랜드와 정확한 상품명. 포장에 적힌 핵심 제품명, 라인명, 맛/향/색상/종류를 분리한다.
2. 용량·중량·규격·입수·묶음 수량. 예: 210g, 500ml, 30매, 6입.
3. 바코드 숫자(EAN/UPC/GTIN). 바코드 아래 숫자를 정확히 읽되 모델번호와 혼동하지 않는다.
4. 제조사 또는 수입자, 원산지, 제품 유형이 보이면 기록한다.
5. 디자인 식별정보: 포장 주색상, 로고 위치, 캐릭터, 용기 형태, 전면에 보이는 핵심 문구를 짧게 정리한다. 검색어 보조용일 뿐 보이지 않는 특징은 만들지 않는다.
6. 신발·의류·가전 등 모델번호가 있는 상품은 product_code에 정확히 넣는다. 신발은 브랜드 스타일코드(예: U9060ECA, DD1391-100, IF6490)를 바코드나 내부 일련번호보다 우선한다. 뉴발란스 라벨에서 NBPDFS로 시작하는 코드는 내부 관리번호이므로 product_code로 절대 선택하지 않는다. 예시 사진처럼 NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 보이면 product_code는 반드시 U9060ECA, internal_code는 NBPDFS193I이며 긴 문자열은 barcode_text다.
7. 신발이면 한국/JP 사이즈(mm), US 사이즈, 색상을 각각 구분한다.
8. 판매가·소싱가·정상가·할인가를 자동 입력하지 않는다. 가격표가 보이더라도 가격 숫자는 결과에 사용하지 않고 price와 list_price는 항상 0으로 둔다.
9. 쿠팡 검색에 가장 적합한 짧고 정확한 검색어를 coupang_query에 만든다. 브랜드 + 상품명 + 모델번호(있을 때) + 용량/수량 순으로 구성하고, 광고문구·가격·바코드는 넣지 않는다. 상품명이 불명확할 때만 바코드를 fallback_query에 넣는다.

설명·마크다운 없이 완전한 JSON 하나만 반환한다. 값이 안 보이면 0 또는 빈 문자열을 쓰고 항목을 생략하지 않는다:
{"category":"식품|생활용품|뷰티|완구|반려동물|유아용품|의류|신발|가전|기타","brand":"","product_name":"","variant":"","product_code":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","barcode":"","manufacturer":"","origin":"","volume":"","count":0,"size_mm":0,"us_size":"","color":"","design_features":[""],"list_price":0,"price":0,"promotion":"","coupang_query":"","fallback_query":"","visible_text":[""],"confidence":"높음|보통|낮음","warnings":[""]}'''


@app.post('/api/recognize-general-universal')
def general_universal():
    try:
        files=request.files.getlist('images')
        multiple=bool(files)
        prompt=GENERAL_PRODUCT_PROMPT + """

추가 지시: 입력 사진은 상품 본체, 포장 앞면/뒷면, 바코드, 신발 박스 라벨 중 하나 또는 여러 장이다. 사진 종류를 자동 분류하고 여러 장이면 같은 상품의 정보로 합쳐라. 판매가와 소싱가는 사용자가 직접 입력하므로 price와 list_price는 항상 0으로 둔다. 서로 충돌하는 값은 임의로 확정하지 말고 warnings에 적어라. 신발은 내부 관리번호가 아니라 실제 브랜드 스타일코드를 product_code로 선택한다.
"""
        d,e=vision(prompt,560,multiple=multiple)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_general_result(d))
    except Exception as x:return jsonify(error=f'일반상품 통합 인식 오류: {x}'),502

@app.post('/api/recognize-product')
def product():
    try:
        d,e=vision(GENERAL_PRODUCT_PROMPT,520)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_general_result(d))
    except Exception as x:return jsonify(error=f'상품 정밀 인식 오류: {x}'),502
@app.post('/api/recognize-price-tag')
def price():
    try:
        d,e=vision(GENERAL_PRODUCT_PROMPT + '\n상품명·모델번호·바코드만 확인한다. 가격은 자동 입력하지 않고 price와 list_price는 항상 0으로 둔다.',520)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_general_result(d))
    except Exception as x:return jsonify(error=f'가격표 정밀 인식 오류: {x}'),502
@app.post('/api/recognize-receipt')
def receipt():
    try:
        d,e=vision('한국 마트 영수증을 분석한다. 실제로 확인되는 내용만 사용한다. JSON 하나만 반환: {"store":"","date":"","total":0,"items":[{"name":"","qty":1,"amount":0}],"confidence":"높음|보통|낮음"}',420)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'영수증 인식 오류: {x}'),502
@app.post('/api/recognize-sneaker-label')
def sneaker():
    try:
        prompt='''신발 박스 라벨 또는 택을 분석한다. 화면에 실제 보이는 글자만 사용한다. 가장 중요한 작업은 모델번호와 사이즈를 정확히 구분하는 것이다.

모델번호 선택 규칙:
1. 브랜드의 실제 스타일코드 형태를 최우선으로 선택한다. 예: 뉴발란스 U9060ECA, ML725R, M2002RCC, BB550WWW / 나이키 DD1391-100, DV0833-100 / 아디다스 IF6490, IG6199.
2. 바코드 바로 아래의 긴 문자열, EAN/UPC, 내부 물류번호, 일련번호는 모델번호로 선택하지 않는다.
3. 같은 사진에 'NBPDFS193I', 'U9060ECA', 'NBPDFS193I39240'가 함께 있으면 모델번호는 반드시 U9060ECA이며, NBPDFS193I는 internal_code, NBPDFS193I39240는 barcode다.
4. 한국/JP mm 사이즈를 우선한다. 큰 숫자 220~320 범위가 보이면 size에 넣고, US 6 같은 해외 사이즈와 혼동하지 않는다.
5. OCR 문자 I/1, O/0를 임의로 바꾸지 말고 라벨 글자를 그대로 유지한다.
6. 모델번호 후보를 위치와 함께 model_candidates에 모두 반환한다. 바코드 아래 후보는 role을 barcode_text로 표시한다.

설명·마크다운 없이 완전한 JSON 하나만 반환한다. 값이 안 보이면 0 또는 빈 문자열을 쓰고 항목을 생략하지 않는다:
{"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","product_name":"","size":0,"us_size":"","color":"","barcode":"","confidence":"높음|보통|낮음"}'''
        d,e=vision(prompt,600)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'신발 라벨 인식 오류: {x}'),502


@app.post('/api/recognize-sneaker-batch')
def sneaker_batch():
    try:
        prompt='여러 장의 사진을 하나의 스니커즈 소싱 건으로 통합 분석한다. 사진들은 신발 박스 라벨, 아울렛 가격표, KREAM 체결 거래, 판매입찰, 구매입찰 화면이 섞여 있을 수 있다. 먼저 각 사진 유형을 분류한 뒤 같은 상품·같은 사이즈의 정보만 합친다. 실제 화면에 보이는 값만 사용하고 추측하지 않는다.\n\n모델번호 규칙: 브랜드 스타일코드를 최우선으로 선택한다. 바코드 아래 긴 문자열·EAN·UPC·내부 물류번호는 모델번호가 아니다. NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 있으면 model_no는 U9060ECA, internal_code는 NBPDFS193I, barcode는 NBPDFS193I39240이다.\n사이즈 규칙: 한국/JP mm 220~320을 우선하고 US 사이즈와 혼동하지 않는다.\n가격표 규칙: 가격표에 이미 할인 적용되어 크게 표시된 현재 판매가를 sale_price에 넣는다. 정상가는 list_price다. 가격표의 기존 할인율은 shown_discount_rate이며 사용자의 추가 할인율과 합산하지 않는다.\nKREAM 규칙: 실제 체결 거래만 trades에 넣고 날짜는 YYYY-MM-DD, 가격은 원 단위 정수로 한다. 판매입찰은 lowest_ask, 구매입찰은 highest_bid로 분리한다. 중복 체결은 제거하고 최신순 최대 10건으로 반환한다.\n서로 다른 모델이 섞이면 가장 많은 사진에서 일치하는 모델을 대표로 선택하고 conflicts에 경고를 넣는다.\n설명·마크다운 없이 완전한 JSON 하나만 반환한다. 값이 안 보이면 0 또는 빈 문자열을 쓰고 항목을 생략하지 않는다:\n{"image_types":["박스라벨","가격표","체결거래","판매입찰","구매입찰"],"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[],"internal_code":"","barcode":"","product_name":"","color":"","size":0,"us_size":"","list_price":0,"sale_price":0,"shown_discount_rate":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"trades":[{"date":"YYYY-MM-DD","price":0}],"visible_trade_count":0,"conflicts":[],"confidence":"높음|보통|낮음"}'
        d,e=vision(prompt,850,multiple=True)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'통합 사진 분석 오류: {x}'),502

def _to_int(v):
    try:
        if isinstance(v,str):
            v=re.sub(r'[^0-9.-]','',v)
        return max(0,int(float(v or 0)))
    except Exception:
        return 0

def _normalize_capture_result(d, wanted_size=0):
    """AI가 조금 다른 구조로 답해도 가격·사이즈를 최대한 살려 앱 표준 구조로 변환한다."""
    d=dict(d or {})
    rows_in=d.get('rows') or d.get('sizes') or d.get('options') or []
    sizes=[]
    all_prices=[]
    for row in rows_in:
        if not isinstance(row,dict):
            continue
        size=_to_int(row.get('size') or row.get('size_mm') or row.get('option_size'))
        prices=[]
        for value in row.get('prices') or row.get('trade_prices') or []:
            price=_to_int(value)
            if price: prices.append(price)
        trades=[]
        for t in row.get('trades') or row.get('transactions') or []:
            if isinstance(t,dict):
                price=_to_int(t.get('price') or t.get('amount'))
                if price:
                    prices.append(price)
                    trades.append({'date':str(t.get('date') or t.get('trade_date') or ''),'price':price})
            else:
                price=_to_int(t)
                if price:
                    prices.append(price)
                    trades.append({'date':'','price':price})
        # AI가 prices 배열만 반환하는 경우에도 최근 거래 입력칸이 비지 않도록 거래 행으로 보존한다.
        existing_prices=[_to_int(t.get('price')) for t in trades]
        for price in prices:
            if price and price not in existing_prices:
                trades.append({'date':'','price':price})
                existing_prices.append(price)
        for key in ('recent_price','avg_price','high_price','low_price'):
            price=_to_int(row.get(key))
            if price: prices.append(price)
        high=_to_int(row.get('high_price')) or (max(prices) if prices else 0)
        low=_to_int(row.get('low_price')) or (min(prices) if prices else 0)
        avg=_to_int(row.get('avg_price')) or (round(sum(prices)/len(prices)) if prices else 0)
        recent=_to_int(row.get('recent_price')) or (prices[0] if prices else avg or low or high)
        all_prices.extend(prices or [x for x in (high,avg,low,recent) if x])
        sizes.append({
            'size':size,
            'trade_count':_to_int(row.get('trade_count') or row.get('visible_trade_count')) or len(trades) or len(prices),
            'recent_price':recent,'avg_price':avg,'high_price':high,'low_price':low,
            'recent_date':str(row.get('recent_date') or row.get('date') or ''),
            'days_since_last_trade':_to_int(row.get('days_since_last_trade')) if row.get('days_since_last_trade') not in (None,'') else 999,
            'lowest_ask':_to_int(row.get('lowest_ask') or row.get('sell_bid') or row.get('ask')),
            'highest_bid':_to_int(row.get('highest_bid') or row.get('buy_bid') or row.get('bid')),
            'demand':str(row.get('demand') or '자료부족'),
            'recommendation_reason':str(row.get('recommendation_reason') or ''),
            'trades':trades[:12]
        })
    summary=d.get('visible_summary') or d.get('summary') or {}
    overall_high=_to_int(d.get('overall_high_price') or summary.get('high') or summary.get('high_price'))
    overall_avg=_to_int(d.get('overall_avg_price') or summary.get('avg') or summary.get('average') or summary.get('avg_price'))
    overall_low=_to_int(d.get('overall_low_price') or summary.get('low') or summary.get('low_price'))
    if all_prices:
        overall_high=overall_high or max(all_prices)
        overall_avg=overall_avg or round(sum(all_prices)/len(all_prices))
        overall_low=overall_low or min(all_prices)
    if not sizes and any((overall_high,overall_avg,overall_low)):
        sizes=[{'size':_to_int(wanted_size),'trade_count':_to_int(d.get('visible_trade_count')),
                'recent_price':overall_avg or overall_low or overall_high,'avg_price':overall_avg,
                'high_price':overall_high,'low_price':overall_low,'recent_date':'','days_since_last_trade':999,
                'lowest_ask':_to_int(d.get('lowest_ask')),'highest_bid':_to_int(d.get('highest_bid')),
                'demand':'자료부족','recommendation_reason':'가격은 인식했으나 사이즈 표시는 확인하지 못함','trades':[]}]
    # 단일 화면에서 사이즈 숫자를 못 읽었지만 사용자가 제품 사이즈를 이미 입력한 경우 그 값을 연결한다.
    if len(sizes)==1 and not _to_int(sizes[0].get('size')) and _to_int(wanted_size):
        sizes[0]['size']=_to_int(wanted_size)
        sizes[0]['recommendation_reason']=sizes[0].get('recommendation_reason') or '캡처에서 사이즈가 흐려 현재 상품 입력 사이즈를 적용함'
    visible_count=_to_int(d.get('visible_trade_count')) or sum(_to_int(x.get('trade_count')) for x in sizes)
    valid_sizes=[x for x in sizes if x.get('size')]
    mode='all' if len(valid_sizes)>1 else 'single'
    return {
        'analysis_mode':mode,
        'platform':str(d.get('platform') or ''),
        'model_no':str(d.get('model_no') or d.get('model') or ''),
        'product_name':str(d.get('product_name') or d.get('product_title') or ''),
        'capture_types':d.get('capture_types') or ([d.get('screen_type')] if d.get('screen_type') else []),
        'overall_high_price':overall_high,'overall_avg_price':overall_avg,'overall_low_price':overall_low,
        'sizes':sizes,
        'comparison_note':str(d.get('comparison_note') or ('단일옵션으로 사이즈 간 비교 불가' if mode=='single' else '')),
        'visible_trade_count':visible_count,
        'conflicts':d.get('conflicts') or [],
        'confidence':str(d.get('confidence') or '보통')
    }

@app.post('/api/recognize-kream-captures')
def kream_captures():
    try:
        scope=str(request.form.get('scope','auto') or 'auto').lower()
        wanted_size=_to_int(request.form.get('wanted_size'))
        scope_note={'all':'여러 사이즈가 보이면 각 사이즈를 별도 행으로 분리한다.','single':'현재 선택된 한 사이즈 화면만 읽는다.'}.get(scope,'화면에 보이는 구조에 따라 단일 사이즈 또는 여러 사이즈를 판단한다.')
        prompt=f'''KREAM 또는 POIZON 앱의 시세 스크린샷 한 장을 OCR처럼 정확히 읽어라. {scope_note}
가장 중요한 것은 화면에 보이는 숫자를 빠뜨리지 않는 것이다. 체결거래 가격, 사이즈, 날짜, 판매입찰 최저가, 구매입찰 최고가를 보이는 그대로 추출한다.
추측하거나 보이지 않는 숫자를 만들지 않는다. 쉼표가 포함된 원화 가격은 정수로 바꾼다. 사이즈가 안 보이면 size는 0이어도 되며 가격 데이터는 반드시 반환한다.
한 화면에 체결 가격이 여러 개 보이면 prices 배열과 trades 배열에 위에서 아래 순서로 모두 넣는다. 날짜가 안 보이면 date는 빈 문자열로 두되 price는 반드시 보존한다. 각 거래 행에 사이즈가 같이 보이면 반드시 해당 size 행에 묶고, 여러 사이즈가 섞인 화면이면 사이즈별 rows를 따로 만든다. 최고·평균·최저는 서버가 계산하므로 억지로 계산하지 않아도 된다.
설명이나 마크다운 없이 JSON 하나만 반환한다:
{{"platform":"KREAM|POIZON|기타","screen_type":"체결거래|판매입찰|구매입찰|시세요약|혼합","model_no":"","product_name":"","rows":[{{"size":0,"prices":[0],"trades":[{{"date":"YYYY-MM-DD","price":0}}],"lowest_ask":0,"highest_bid":0}}],"visible_summary":{{"high":0,"avg":0,"low":0}},"visible_trade_count":0,"confidence":"높음|보통|낮음"}}'''
        d,e=vision(prompt,700,multiple=True)
        if e:return jsonify(error=e[0]),e[1]
        out=_normalize_capture_result(d,wanted_size)
        has_price=any((out.get('overall_high_price'),out.get('overall_avg_price'),out.get('overall_low_price')))
        has_rows=any(any(_to_int(x.get(k)) for k in ('recent_price','avg_price','high_price','low_price','lowest_ask','highest_bid')) for x in out.get('sizes') or [])
        if not (has_price or has_rows):
            return jsonify(error='가격 숫자를 읽지 못했습니다. 가격과 사이즈가 보이는 화면 전체 캡처를 올려 주세요.'),422
        return jsonify(out)
    except Exception:
        app.logger.exception('KREAM/POIZON capture analysis failure')
        return jsonify(error='캡처 분석 중 오류가 발생했습니다. 잠시 후 같은 사진으로 다시 시도해 주세요.'),502

@app.post('/api/recognize-sneaker-outlet-tag')
def sneaker_outlet_tag():
    try:
        prompt='''아울렛 신발 가격표 또는 신발 박스 라벨 사진을 분석한다. 모델번호는 브랜드 스타일코드 형식을 우선하고 바코드 아래의 긴 문자열·일련번호를 모델번호로 선택하지 않는다. 예를 들어 NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 있으면 model_no는 U9060ECA, internal_code는 NBPDFS193I, barcode는 NBPDFS193I39240이다. 사진에 함께 보이는 브랜드, 모델번호, 상품명, 색상, 한국/JP 사이즈(mm), 바코드, 정상가, 가격표에 이미 할인이 적용되어 표시된 현재 판매가, 가격표의 1차 할인율을 추출한다. 가장 중요한 값은 고객이 매장에서 추가 할인을 받기 전 가격표에 적힌 할인 적용 판매가이며 반드시 sale_price에 넣는다. 정상가와 할인가가 모두 보이면 정상가는 list_price, 이미 할인 적용된 표시가는 sale_price로 정확히 구분한다. 취소선 가격·권장소비자가·정상가는 sale_price로 넣지 않는다. 여러 가격이 있으면 'SALE', '할인가', '회원가', '판매가', 가장 크거나 강조된 결제 가격 등의 문맥으로 실제 표시 할인가를 판단한다. 가격표에 적힌 할인율은 shown_discount_rate이며 이것은 이미 sale_price에 반영된 1차 할인율이다. 사용자가 별도로 적용할 추가 할인율과 혼동하거나 합산하지 않는다. 한 가격만 보여 할인가인지 확실하지 않으면 price_type을 unknown으로 하고 확인된 가격을 list_price에 넣는다. 보이지 않는 값은 0 또는 빈 문자열로 둔다. 임의 추측 금지. JSON 하나만 반환: {"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","product_name":"","size":0,"color":"","barcode":"","list_price":0,"sale_price":0,"shown_discount_rate":0,"price_type":"normal|sale|unknown","confidence":"높음|보통|낮음"}'''
        d,e=vision(prompt,600)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'아울렛 가격표 인식 오류: {x}'),502

@app.post('/api/analyze-kream-url')
def analyze_kream_url():
    try:
        body=request.get_json(silent=True) or {}
        url=str(body.get('url','')).strip()
        wanted_size=int(body.get('size') or 0)
        if not re.match(r'^https://(?:www\.)?kream\.co\.kr/products/\d+(?:[/?#].*)?$',url,re.I):
            return jsonify(error='올바른 KREAM 상품 주소가 아닙니다.'),400
        prompt=f"""오늘 날짜는 {datetime.now().astimezone().strftime('%Y-%m-%d')}이다. 다음 KREAM 상품 URL의 공개적으로 확인 가능한 정보를 웹 검색으로 조사한다.
URL: {url}
사용자가 관심 있는 사이즈: {wanted_size if wanted_size else '미지정'}

KREAM 페이지, 검색엔진에 노출된 KREAM 결과, 신뢰할 만한 공개 페이지에서 실제로 확인되는 내용만 사용한다. 추측하지 않는다.
상품명, 브랜드, 모델번호, 발매가, 사이즈별 최근 거래가격과 거래일, 공개된 최고 구매입찰가와 최저 판매입찰가, 확인 가능한 최근 거래 여러 건을 수집한다.
값이 공개되지 않으면 0 또는 빈 배열로 둔다. 가격은 원 단위 정수, 날짜는 YYYY-MM-DD 형식이다.
사이즈별 데이터는 확인 가능한 모든 사이즈를 반환하고, 같은 사이즈의 거래가 여러 개면 trades에 최신순으로 최대 10건 넣는다.
반드시 설명이나 마크다운 없이 JSON 하나만 반환한다.
{{"product_name":"","brand":"Nike|New Balance|Adidas|Under Armour|기타","model_no":"","color":"","release_price":0,"sizes":[{{"size":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"recent_date":"","trade_count":0,"trades":[{{"date":"YYYY-MM-DD","price":0}}]}}],"summary":"공개정보 기반 핵심 판단 한두 문장","confidence":"높음|보통|낮음"}}"""
        client=cli()
        last_error=None
        response=None
        for tool_type in ('web_search','web_search_preview'):
            try:
                model=os.getenv('OPENAI_WEB_MODEL',os.getenv('OPENAI_MODEL','gpt-4.1-mini'))
                response=client.responses.create(
                    model=model,
                    tools=[{'type':tool_type}],
                    input=prompt,
                    max_output_tokens=1800
                )
                break
            except Exception as exc:
                last_error=exc
        if response is None:
            raise last_error or RuntimeError('웹 검색 도구를 사용할 수 없습니다.')
        d=parse(response.output_text)
        d['_api_usage']=_api_usage_meta(response,model,'web_search')
        d['checked_at']=datetime.now().astimezone().strftime('%Y-%m-%d')
        d['source_url']=url
        return jsonify(d)
    except Exception as x:
        return jsonify(error=f'KREAM 링크 분석 오류: {x}'),502



def _naver_searchad_ready():
    return all(os.getenv(k,'').strip() for k in ('NAVER_SEARCHAD_API_KEY','NAVER_SEARCHAD_SECRET_KEY','NAVER_SEARCHAD_CUSTOMER_ID'))

def _naver_count_range(value):
    """네이버 검색광고의 숫자 또는 '< 10' 값을 (최소, 최대, 정확여부)로 변환한다."""
    if isinstance(value,(int,float)):
        n=max(0,int(value));return n,n,True
    raw=str(value or '').strip().replace(',','')
    if not raw:return 0,0,False
    if '<' in raw:
        m=re.search(r'(\d+)',raw);upper=max(0,int(m.group(1))-1) if m else 9
        return 0,upper,False
    try:
        n=max(0,int(float(raw)));return n,n,True
    except Exception:
        return 0,0,False

def _naver_searchad_keyword(keyword):
    """네이버 검색광고 키워드도구 공식 API에서 월간 검색수와 경쟁도를 가져온다."""
    if not _naver_searchad_ready():return None
    path='/keywordstool';method='GET';timestamp=str(int(time.time()*1000))
    api_key=os.getenv('NAVER_SEARCHAD_API_KEY').strip();secret=os.getenv('NAVER_SEARCHAD_SECRET_KEY').strip();customer=os.getenv('NAVER_SEARCHAD_CUSTOMER_ID').strip()
    signature=base64.b64encode(hmac.new(secret.encode(),f'{timestamp}.{method}.{path}'.encode(),hashlib.sha256).digest()).decode()
    hint=re.sub(r'\s+','',str(keyword or '').strip())[:100]
    if not hint:return None
    query=urllib.parse.urlencode({'hintKeywords':hint,'showDetail':'1'})
    req=urllib.request.Request('https://api.searchad.naver.com'+path+'?'+query,headers={
        'X-Timestamp':timestamp,'X-API-KEY':api_key,'X-Customer':customer,'X-Signature':signature,'Accept':'application/json'
    },method='GET')
    with urllib.request.urlopen(req,timeout=8) as response:
        payload=json.loads(response.read().decode('utf-8'))
    rows=payload.get('keywordList') or []
    if not rows:return None
    norm=lambda v:re.sub(r'\s+','',str(v or '').lower())
    exact=next((r for r in rows if norm(r.get('relKeyword'))==norm(hint)),rows[0])
    pc_min,pc_max,pc_exact=_naver_count_range(exact.get('monthlyPcQcCnt'))
    mo_min,mo_max,mo_exact=_naver_count_range(exact.get('monthlyMobileQcCnt'))
    related=[]
    for row in rows:
        kw=str(row.get('relKeyword') or '').strip()
        if kw and norm(kw)!=norm(exact.get('relKeyword')) and kw not in related:related.append(kw)
        if len(related)>=6:break
    return {
        'keyword':str(exact.get('relKeyword') or keyword).strip(),
        'monthly_pc_min':pc_min,'monthly_pc_max':pc_max,
        'monthly_mobile_min':mo_min,'monthly_mobile_max':mo_max,
        'monthly_min':pc_min+mo_min,'monthly_max':pc_max+mo_max,
        'exact':bool(pc_exact and mo_exact),
        'competition_index':str(exact.get('compIdx') or ''),
        'average_depth':exact.get('plAvgDepth'),
        'monthly_pc_clicks':exact.get('monthlyAvePcClkCnt'),
        'monthly_mobile_clicks':exact.get('monthlyAveMobileClkCnt'),
        'related_keywords':related,
        'source':'네이버 검색광고 키워드도구 공식 API',
        'checked_at':datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')
    }

def _competition_score_from_naver(value):
    raw=str(value or '').strip().lower()
    if raw in {'높음','high'}:return 80
    if raw in {'중간','보통','medium'}:return 55
    if raw in {'낮음','low'}:return 30
    return 0



@app.post('/api/analyze-market-keyword')
def analyze_market_keyword():
    """공식 검색량이 연결되면 그 값을 우선하고, 공개 웹 검색은 추세·경쟁 신호 교차검증에만 사용한다."""
    body=request.get_json(silent=True) or {}
    raw_keyword=str(body.get('keyword') or '').strip()[:160]
    if not raw_keyword:return jsonify(error='분석할 키워드가 없습니다.'),400
    keyword_cache_key='keyword:v690:'+re.sub(r'\s+',' ',raw_keyword.lower()).strip()
    cached=_cache_get(keyword_cache_key,3*86400)
    if cached is not None:return jsonify(_cached_result(cached))
    context={k:body.get(k) for k in ('brand','product_name','category','sale_price','cost_price','margin','roi')}
    official=None;official_error=''
    try:official=_naver_searchad_keyword(raw_keyword)
    except Exception as exc:
        official_error=str(exc)[:180]
        logging.warning('naver searchad keyword lookup failed: %s',official_error)
    official_note=json.dumps(official,ensure_ascii=False) if official else '공식 검색량 API 미연결 또는 조회 실패'
    prompt=f"""한국 온라인 쇼핑 상품의 수요와 경쟁을 보수적으로 분석한다.

입력 검색어: {raw_keyword}
상품정보: {json.dumps(context,ensure_ascii=False)}
네이버 검색광고 공식 키워드 자료: {official_note}

규칙:
1. 소비자가 실제로 검색할 대표 키워드(main_keyword)를 만든다. 일반상품은 브랜드+핵심상품명+규격, 스니커즈는 브랜드+모델번호를 우선한다.
2. 공개 웹 검색으로 최근 90일 이내의 검색 노출, 리뷰·구매 신호, 가격 경쟁, 품절·재입고 신호를 서로 다른 출처 3곳 이상에서 교차검증한다.
3. 공식 자료가 제공되면 월간 검색량과 경쟁도는 절대로 임의 수정하지 않는다. 공식 자료가 없으면 월간 검색량·판매자수·상품수를 추측하거나 만들어내지 말고 0으로 둔다.
4. evidence에는 실제 확인한 출처의 이름과 무엇을 확인했는지 적는다. 확인하지 않은 숫자를 사실처럼 쓰지 않는다.
5. demand_score와 competition_score는 공식 검색량, 공식 경쟁도, 최근 웹 노출·리뷰·구매·가격 신호를 종합한 0~100 평가 점수이며 절대 검색량 자체가 아니다.
6. 공개 정보가 충분하지 않으면 confidence를 낮음으로 하고 recommendation은 자료부족 또는 소량 테스트로 둔다.

설명·마크다운 없이 JSON 하나만 반환한다:
{{"main_keyword":"","related_keywords":[],"demand_score":0,"competition_score":0,"sourcing_score":0,"turnover":"빠름|보통|느림|자료부족","recommendation":"적극 소싱|마진 확보 시 소싱|소량 테스트|비추천|자료부족","seller_count":0,"product_count":0,"search_trend":"상승|보합|하락|판단어려움","trend_reason":"","review_signal":"","purchase_signal":"","price_competition":"낮음|보통|높음|판단어려움","price_range_note":"","confidence":"높음|보통|낮음","confidence_reason":"","evidence":[],"cautions":[]}}"""
    reserved_user,access_error=_analysis_access()
    if access_error:return access_error
    response=None;last_error=None;model=''
    try:
        search_models=[]
        for item in (os.getenv('OPENAI_SEARCH_MODEL',''),os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'gpt-4.1-mini'):
            for candidate in str(item).split(','):
                candidate=candidate.strip()
                if candidate and candidate not in search_models:search_models.append(candidate)
        for model_candidate in search_models:
            for tool_type in ('web_search','web_search_preview'):
                try:
                    model=model_candidate
                    with _AI_SEMAPHORE:
                        response=cli().responses.create(model=model,tools=[{'type':tool_type}],input=prompt,max_output_tokens=1300)
                    break
                except Exception as exc:last_error=exc
            if response is not None:break
        if response is None:raise last_error or RuntimeError('웹 검색 도구를 사용할 수 없습니다.')
        d=parse(response.output_text)
        d['_api_usage']=_api_usage_meta(response,model,'keyword_search')
    except Exception as exc:
        if reserved_user:_rollback_analysis(reserved_user['id'])
        d={'main_keyword':raw_keyword,'related_keywords':[],'demand_score':0,'competition_score':0,'sourcing_score':0,
           'turnover':'자료부족','recommendation':'자료부족','seller_count':0,'product_count':0,'search_trend':'판단어려움',
           'trend_reason':'공개 웹 교차검증에 일시적으로 연결하지 못했습니다.','review_signal':'','purchase_signal':'',
           'price_competition':'판단어려움','price_range_note':'','confidence':'낮음',
           'confidence_reason':'공개 웹 자료를 확인하지 못해 공식 키워드 자료만 반영했습니다.' if official else '공식 데이터와 공개 웹 자료를 모두 확인하지 못했습니다.',
           'evidence':[],'cautions':['공개 웹 검색 연결 오류: '+str(exc)[:120]]}
    for k in ('demand_score','competition_score','sourcing_score'):
        try:d[k]=max(0,min(100,int(float(d.get(k) or 0))))
        except:d[k]=0
    for k in ('seller_count','product_count'):
        try:d[k]=max(0,int(float(d.get(k) or 0)))
        except:d[k]=0
    d['main_keyword']=str(d.get('main_keyword') or raw_keyword).strip()[:100]
    ai_related=[str(x).strip() for x in (d.get('related_keywords') or []) if str(x).strip()]
    d['keyword']=d['main_keyword'];d['checked_at']=datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')
    d['official_data_available']=bool(official);d['data_sources']=[]
    if official:
        d['monthly_pc_search_min']=official['monthly_pc_min'];d['monthly_pc_search_max']=official['monthly_pc_max']
        d['monthly_mobile_search_min']=official['monthly_mobile_min'];d['monthly_mobile_search_max']=official['monthly_mobile_max']
        d['monthly_search_min']=official['monthly_min'];d['monthly_search_max']=official['monthly_max']
        d['monthly_search_volume']=official['monthly_min'] if official['exact'] else 0
        d['search_volume_type']='확인값' if official['exact'] else '공식 범위'
        d['exact_search_volume_available']=bool(official['exact'])
        d['competition_index']=official['competition_index']
        d['search_volume_estimate']=official['monthly_min'] if official['exact'] else round((official['monthly_min']+official['monthly_max'])/2)
        if not d.get('competition_score'):
            d['competition_score']=_competition_score_from_naver(official['competition_index'])
        merged=[]
        for x in official.get('related_keywords',[])+ai_related:
            if x and x not in merged:merged.append(x)
        d['related_keywords']=merged[:6]
        d['official_source']=official['source'];d['data_scope']='네이버 검색광고 공식 검색량 + 공개 웹 교차검증'
        d['data_sources'].append({'name':official['source'],'type':'공식 월간 검색수·경쟁도','checked_at':official['checked_at']})
        ev=[str(x) for x in (d.get('evidence') or []) if str(x).strip()]
        ev.insert(0,f"네이버 검색광고 공식 API: 월간 검색수 {'정확값 '+str(official['monthly_min'])+'회' if official['exact'] else str(official['monthly_min'])+'~'+str(official['monthly_max'])+'회'}, 경쟁도 {official['competition_index'] or '미제공'}")
        d['evidence']=ev[:8]
    else:
        d['monthly_search_volume']=0;d['monthly_search_min']=0;d['monthly_search_max']=0;d['search_volume_estimate']=0
        d['search_volume_type']='자료부족';d['exact_search_volume_available']=False;d['competition_index']=''
        d['related_keywords']=ai_related[:4];d['official_source']='';d['data_scope']='공개 웹 교차검증(공식 절대 검색량 미연결)'
        cautions=[str(x) for x in (d.get('cautions') or []) if str(x).strip()]
        cautions.insert(0,'네이버 검색광고 공식 API가 연결되지 않아 월간 검색량은 표시하지 않습니다. 추정 숫자를 만들지 않았습니다.')
        if official_error:cautions.append('공식 키워드 API 조회 오류: '+official_error)
        d['cautions']=cautions[:8]
    d['search_trend']=d.get('search_trend') or '판단어려움';d['price_competition']=d.get('price_competition') or '판단어려움'
    d['confidence']=d.get('confidence') or ('보통' if official else '낮음')
    if not d.get('confidence_reason'):
        d['confidence_reason']='공식 월간 검색수와 공개 웹 자료를 함께 확인했습니다.' if official else '공식 절대 검색량이 연결되지 않아 공개 웹 신호만 반영했습니다.'
    if not d.get('turnover'):d['turnover']='자료부족'
    if not d.get('recommendation'):d['recommendation']='자료부족'
    _cache_set(keyword_cache_key,d)
    return jsonify(d)


@app.post('/api/export-excel')
def export_excel():
    """브라우저 localStorage의 저장 데이터를 실제 .xlsx 파일로 내보낸다."""
    try:
        body=request.get_json(silent=True) or {}
        general=(body.get('general') or [])[:2000]
        sneakers=(body.get('sneakers') or [])[:2000]
        cart=(body.get('cart') or [])[:3000]
        receipts=(body.get('receipts') or [])[:3000]
        receipt_only=bool(body.get('receiptOnly'))
        settings=body.get('settings') or {}

        out=BytesIO()
        wb=xlsxwriter.Workbook(out, {'in_memory': True})
        title=wb.add_format({'bold':True,'font_size':16,'font_color':'#173f70'})
        head=wb.add_format({'bold':True,'bg_color':'#173f70','font_color':'#FFFFFF','border':1,'align':'center','valign':'vcenter'})
        text=wb.add_format({'border':1,'valign':'top'})
        integer=wb.add_format({'border':1,'num_format':'#,##0','valign':'top'})
        percent=wb.add_format({'border':1,'num_format':'0.0"%"','valign':'top'})
        dtfmt=wb.add_format({'border':1,'num_format':'yyyy-mm-dd hh:mm','valign':'top'})
        money=wb.add_format({'border':1,'num_format':'#,##0"원"','valign':'top'})
        note=wb.add_format({'font_color':'#666666','italic':True})

        def parse_dt(value):
            try:
                return datetime.fromisoformat(str(value).replace('Z','+00:00')).replace(tzinfo=None)
            except Exception:
                return str(value or '')

        # 요약
        ws=wb.add_worksheet('요약')
        ws.write('A1','올데이픽 AI 소싱 저장 데이터',title)
        ws.write('A3','구분',head); ws.write('B3','건수',head)
        ws.write('A4','일반상품 기록',text); ws.write_number('B4',len(general),integer)
        ws.write('A5','스니커즈 기록',text); ws.write_number('B5',len(sneakers),integer)
        ws.write('A6','영수증 기록',text); ws.write_number('B6',len(receipts),integer)
        ws.write('A6','장바구니',text); ws.write_number('B6',len(cart),integer)
        ws.write('A8','다운로드 일시',head); ws.write_datetime('B8',datetime.now(),dtfmt)
        ws.write('A10','세금 계산 기준',head); ws.write('B10','설정값',head)
        labels=[('예상 소득세율',settings.get('incomeTaxRate',0),'percent'),('부가세 계산',settings.get('useVat','yes'),'text'),('일반상품 판매 수수료율',settings.get('fee',11.8),'percent'),('일반상품 배송비',settings.get('ship',4000),'money'),('스니커즈 판매 수수료율',settings.get('sFee',6),'percent'),('스니커즈 배송비',settings.get('sShip',3000),'money')]
        for r,(lab,val,kind) in enumerate(labels,11):
            ws.write(r-1,0,lab,text)
            fmt=money if kind=='money' else percent
            ws.write_number(r-1,1,float(val or 0),fmt)
        ws.write('A19','※ 앱에 저장된 예상 계산값이며 실제 신고세액과 다를 수 있습니다.',note)
        ws.set_column('A:A',24); ws.set_column('B:B',20)

        # 일반상품 기록
        ws=wb.add_worksheet('일반상품 기록')
        headers=['저장일시','분류','브랜드','상품명','옵션/종류','상품코드','바코드','용량·규격','인식수량','색상','제조사','원산지','소싱매장','내 판매 구성','쿠팡 상품 구성','쿠팡 검색어','핵심 키워드','예상 수요점수','경쟁강도점수','AI 소싱지수','예상 회전율','키워드 추천결과','키워드 분석 신뢰도','키워드 분석 근거','쿠팡 판매가','개당 소싱가','총 소싱가','쿠팡 수수료율','쿠팡 수수료','배송비','기타비용','매출 부가세','추정 매입 부가세','예상 납부 부가세','종소세 전 이익','종소세·지방소득세율','예상 종소세·지방소득세','세후 최종 순이익','실마진율','ROI','인식 신뢰도','포장·디자인 특징','인식된 원문','확인사항','메모']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        for r,x in enumerate(general,1):
            scan=x.get('scan') or {}; vals=[parse_dt(x.get('date')),x.get('category') or scan.get('category',''),x.get('brand') or scan.get('brand',''),x.get('productName') or scan.get('product_name') or x.get('name',''),x.get('variant') or scan.get('variant',''),x.get('productCode') or scan.get('product_code',''),x.get('barcode') or scan.get('barcode',''),x.get('volume') or scan.get('volume',''),x.get('count') or scan.get('count',0),x.get('color') or scan.get('color',''),x.get('manufacturer') or scan.get('manufacturer',''),x.get('origin') or scan.get('origin',''),x.get('store',''),x.get('bundle',0),x.get('marketBundle',0),x.get('coupangQuery') or scan.get('coupang_query',''),x.get('keyword',''),(x.get('marketAnalysis') or {}).get('demand_score',0),(x.get('marketAnalysis') or {}).get('competition_score',0),(x.get('marketAnalysis') or {}).get('sourcing_score',0),(x.get('marketAnalysis') or {}).get('turnover',''),(x.get('marketAnalysis') or {}).get('recommendation',''),(x.get('marketAnalysis') or {}).get('confidence',''),' · '.join((x.get('marketAnalysis') or {}).get('evidence') or []),x.get('sale',0),x.get('unitCost',0),x.get('cost',0),x.get('feeRate',settings.get('fee',11.8)),x.get('fee',0),x.get('ship',0),x.get('other',0),x.get('outputVat',0),x.get('inputVat',0),x.get('vat',0),x.get('profitBeforeIncomeTax',0),x.get('incomeTaxRate',0),x.get('incomeTax',0),x.get('profit',0),x.get('margin',0),x.get('roi',0),x.get('confidence') or scan.get('confidence',''),' · '.join(x.get('designFeatures') or scan.get('design_features') or []),' | '.join(x.get('visibleText') or scan.get('visible_text') or []),' · '.join(x.get('warnings') or scan.get('warnings') or []),x.get('memo','')]
            for c,v in enumerate(vals):
                fmt=dtfmt if c==0 and isinstance(v,datetime) else (money if headers[c] in ('쿠팡 판매가','개당 소싱가','총 소싱가','쿠팡 수수료','배송비','기타비용','매출 부가세','추정 매입 부가세','예상 납부 부가세','종소세 전 이익','예상 종소세·지방소득세','세후 최종 순이익') else (percent if headers[c] in ('쿠팡 수수료율','종소세·지방소득세율','실마진율','ROI') else (integer if headers[c] in ('인식수량','내 판매 구성','쿠팡 상품 구성','예상 수요점수','경쟁강도점수','AI 소싱지수') else text)))
                if isinstance(v,(int,float)) and c not in (0,): ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,datetime): ws.write_datetime(r,c,v,fmt)
                else: ws.write(r,c,v,fmt)
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,len(general)),len(headers)-1)
        ws.set_column(0,0,18); ws.set_column(1,15,18); ws.set_column(16,31,15); ws.set_column(32,36,32)

        # 스니커즈 기록
        ws=wb.add_worksheet('스니커즈 기록')
        headers=['저장일시','브랜드','모델번호','내부코드','바코드','사이즈','색상','원산지','소싱매장','가격표 표시가','추가 할인율','할인금액','최종 매입가','최고 체결가','평균 체결가','최저 체결가','수요','수요 판단근거','확인 거래수','거래 추세','최근거래 경과일','최고가 순이익','평균가 순이익','최저가 순이익','최고가 마진율','평균가 마진율','최저가 마진율','최고가 ROI','평균가 ROI','최저가 ROI','KREAM 분석 모드','캡처 화면 종류','비교 참고사항','충돌/경고','상품사진 인식 신뢰도','KREAM 분석 신뢰도','사진 자동분류','판매수수료율','기본수수료','수수료 부가세율','판매자 배송비','평균가 기준 예상 부가세','평균가 기준 종소세 전 이익','평균가 기준 예상 종소세','평균가 기준 정산금액','핵심 키워드','예상 수요점수','경쟁강도점수','AI 소싱지수','예상 회전율','추천 결과','키워드 분석 신뢰도','분석 근거']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        for r,x in enumerate(sneakers,1):
            ta=x.get('tradeAnalysis') or {}
            avgd=x.get('avgDetail') or {}; vals=[parse_dt(x.get('date')),x.get('brand',''),x.get('model',''),x.get('internalCode',''),x.get('barcode',''),x.get('size',0),x.get('color',''),x.get('origin',''),x.get('store',''),x.get('listPrice',0),x.get('discount',0),x.get('discountAmount',0),x.get('buy',0),x.get('highSale',0),x.get('avgSale',0),x.get('lowSale',0),x.get('demand',''),x.get('demandReason',''),x.get('visibleTradeCount',ta.get('count',0)),ta.get('trend',''),ta.get('days',0),x.get('highProfit',0),x.get('avgProfit',0),x.get('lowProfit',0),x.get('highMargin',0),x.get('avgMargin',0),x.get('lowMargin',0),x.get('highROI',0),x.get('avgROI',0),x.get('lowROI',0),x.get('kreamMode',''),' · '.join(x.get('captureTypes') or []),x.get('comparisonNote',''),' · '.join(x.get('conflicts') or []),x.get('confidence',''),x.get('kreamConfidence',''),' · '.join(x.get('imageTypes') or []),settings.get('sFee',6),settings.get('sBaseFee',2500),settings.get('sFeeVat',10),settings.get('sShip',3000),avgd.get('vat',0),avgd.get('profitBeforeIncomeTax',0),avgd.get('incomeTax',0),avgd.get('settlement',0),x.get('keyword',''),(x.get('marketAnalysis') or {}).get('demand_score',0),(x.get('marketAnalysis') or {}).get('competition_score',0),(x.get('marketAnalysis') or {}).get('sourcing_score',0),(x.get('marketAnalysis') or {}).get('turnover',''),(x.get('marketAnalysis') or {}).get('recommendation',''),(x.get('marketAnalysis') or {}).get('confidence',''),' · '.join((x.get('marketAnalysis') or {}).get('evidence') or [])]
            for c,v in enumerate(vals):
                fmt=dtfmt if c==0 and isinstance(v,datetime) else (percent if c in (10,24,25,26,27,28,29,37,39) else (money if c in (9,11,12,13,14,15,21,22,23,38,40,41,42,43,44) else (integer if c in (5,18,20) else text)))
                if isinstance(v,(int,float)) and c!=0: ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,datetime): ws.write_datetime(r,c,v,fmt)
                else: ws.write(r,c,v,fmt)
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,len(sneakers)),len(headers)-1)
        ws.set_column(0,0,18); ws.set_column(1,8,18); ws.set_column(9,29,15); ws.set_column(30,36,26); ws.set_column(37,44,16); ws.set_column(45,52,18)

        # KREAM 사이즈별 분석
        ws=wb.add_worksheet('KREAM 사이즈별 분석')
        headers=['저장일시','브랜드','모델번호','기준 매입가','사이즈','거래량','최근 체결가','평균 체결가','최고 체결가','최저 체결가','최근 거래일','최근거래 경과일','판매입찰 최저가','구매입찰 최고가','수요','추천 근거','기준 예상 순이익','기준 ROI','체결거래 원문']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        rr=1
        for x in sneakers:
            rows=x.get('sizeRows') or (x.get('kreamAnalysis') or {}).get('sizes') or []
            for row in rows:
                ref=float(row.get('avg_price') or row.get('recent_price') or row.get('lowest_ask') or 0)
                buy=float(x.get('buy') or 0); fee=ref*float(settings.get('sFee',6))/100+float(settings.get('sBaseFee',2500)); fee_vat=fee*float(settings.get('sFeeVat',10))/100; ship=float(settings.get('sShip',3000)); output_vat=ref/11; input_vat=buy/11+fee_vat+ship/11; vat=max(0,output_vat-input_vat); pre=ref-fee-fee_vat-ship-buy-vat; tax=max(0,pre)*float(settings.get('incomeTaxRate',42))/100; profit=pre-tax; roi=(profit/buy*100) if buy else 0
                vals=[parse_dt(x.get('date')),x.get('brand',''),x.get('model',''),buy,row.get('size',0),row.get('trade_count',0),row.get('recent_price',0),row.get('avg_price',0),row.get('high_price',0),row.get('low_price',0),row.get('recent_date',''),row.get('days_since_last_trade',0),row.get('lowest_ask',0),row.get('highest_bid',0),row.get('demand',''),row.get('recommendation_reason',''),profit,roi,' | '.join(f"{t.get('date','')} {t.get('price',0)}" for t in (row.get('trades') or []))]
                for c,v in enumerate(vals):
                    fmt=dtfmt if c==0 and isinstance(v,datetime) else (money if c in (3,6,7,8,9,12,13,16) else (percent if c==17 else (integer if c in (4,5,11) else text)))
                    if isinstance(v,(int,float)): ws.write_number(rr,c,float(v),fmt)
                    elif isinstance(v,datetime): ws.write_datetime(rr,c,v,fmt)
                    else: ws.write(rr,c,v,fmt)
                rr+=1
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,rr-1),len(headers)-1)
        ws.set_column(0,3,18); ws.set_column(4,17,15); ws.set_column(18,18,38)

        # 영수증 기록 (인식 완료 즉시 브라우저에 자동 저장된 데이터)
        ws=wb.add_worksheet('영수증 기록')
        headers=['자동 저장일시','영수증 구매일','매장명','영수증 총액','상품 순번','상품명','수량','상품금액','인식 신뢰도','원본 파일명']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        rr=1
        for x in receipts:
            items=x.get('items') or [{}]
            for seq,item in enumerate(items,1):
                vals=[parse_dt(x.get('savedAt') or x.get('date')),x.get('purchaseDate',''),x.get('store',''),x.get('total',0),seq,item.get('name',''),item.get('qty',1),item.get('amount',0),x.get('confidence',''),x.get('sourceFile','')]
                for c,v in enumerate(vals):
                    fmt=dtfmt if c==0 and isinstance(v,datetime) else (money if c in (3,7) else (integer if c in (4,6) else text))
                    if isinstance(v,(int,float)) and c!=0: ws.write_number(rr,c,float(v),fmt)
                    elif isinstance(v,datetime): ws.write_datetime(rr,c,v,fmt)
                    else: ws.write(rr,c,v,fmt)
                rr+=1
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,rr-1),len(headers)-1)
        ws.set_column(0,0,19); ws.set_column(1,2,18); ws.set_column(3,4,14); ws.set_column(5,5,38); ws.set_column(6,7,14); ws.set_column(8,9,18)

        # 장바구니
        ws=wb.add_worksheet('장바구니')
        headers=['담은 순서','구분','상품명','소싱매장','수량','개당 매입가','총 매입액','개당 예상 순이익','예상 총이익','판매가/평균체결가','저장일시']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        for r,x in enumerate(cart,1):
            qty=float(x.get('qty') or 0); unit=float(x.get('unitCost') or x.get('buy') or 0); profit=float(x.get('profit') or 0)
            sale=float(x.get('sale') or x.get('avgSale') or 0)
            vals=[r,x.get('type',''),x.get('name',''),x.get('store',''),qty,unit,qty*unit,profit,qty*profit,sale,parse_dt(x.get('date'))]
            for c,v in enumerate(vals):
                fmt=dtfmt if c==10 and isinstance(v,datetime) else (money if c in (5,6,7,8,9) else (integer if c in (0,4) else text))
                if isinstance(v,(int,float)): ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,datetime): ws.write_datetime(r,c,v,fmt)
                else: ws.write(r,c,v,fmt)
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,len(cart)),len(headers)-1)
        ws.set_column(0,1,12); ws.set_column(2,3,28); ws.set_column(4,9,16); ws.set_column(10,10,18)

        wb.close(); out.seek(0)
        filename=('픽셀_영수증기록_' if receipt_only else '픽셀_소싱데이터_')+datetime.now().strftime('%Y%m%d_%H%M')+'.xlsx'
        return send_file(out,as_attachment=True,download_name=filename,mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as x:
        return jsonify(error=f'엑셀 생성 오류: {x}'),500

@app.get('/health')
def health():
    try:
        with ENGINE.connect() as con:con.execute(text('SELECT 1')).scalar_one()
        return jsonify(ok=True,version='6.9.4',database='postgresql' if DB_URL.startswith('postgresql') else 'sqlite')
    except Exception as exc:
        logging.exception('health database check failed')
        return jsonify(ok=False,version='6.9.4',database='unavailable',error='database connection failed'),503

@app.get('/ready')
def ready():return health()

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
