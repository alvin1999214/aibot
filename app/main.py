import hmac
import logging
import os
from pathlib import Path
import queue
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, TwoFactorRequired
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import Store, mentioned

log = logging.getLogger('bot')


@dataclass
class PendingLogin:
    client: Client
    payload: object
    expires: float


class Bot:
    def __init__(self):
        self.data = Path(os.getenv('DATA_DIR', './data'))
        self.data.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data, 0o700)
        self.store = Store(self.data / 'bot.sqlite3')
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.codes = queue.Queue()
        self.client = None
        self.state = '尚未登入'
        self.error = None
        self.username = None
        self.challenge = False
        self.pending = None
        self.poll = max(10, int(os.getenv('POLL_SECONDS', '20')))
        self.count = max(1, min(200, int(os.getenv('CONTEXT_MESSAGES', '40'))))
        self.chars = max(1000, int(os.getenv('CONTEXT_CHARS', '16000')))
        self.limit = max(1, int(os.getenv('THREAD_LIMIT', '50')))

    def new_client(self):
        client = Client()
        if os.getenv('IG_PROXY'):
            client.set_proxy(os.environ['IG_PROXY'])
        client.challenge_code_handler = self.challenge_code
        return client

    def challenge_code(self, username, choice):
        self.challenge = True
        self.state = '請輸入 Instagram 寄送的驗證碼（5 分鐘內）'
        try:
            return self.codes.get(timeout=300)
        finally:
            self.challenge = False

    def save(self, client):
        temp = self.data / 'session.tmp'
        client.dump_settings(temp)
        os.chmod(temp, 0o600)
        temp.replace(self.data / 'session.json')

    def activate(self, client):
        user = client.account_info()
        self.save(client)
        self.store.since(str(client.user_id), time.time())
        self.username = user.username
        self.client = client
        self.state = '運行中'
        self.error = None

    def expire_pending(self):
        # Called while holding the client lock; credentials only live in memory.
        if self.pending and time.monotonic() >= self.pending.expires:
            self.pending = None
            self.state = '待核准登入已逾時，請重新輸入帳密'

    def login(self, payload, resume=False):
        # The request handler holds this lock before starting the thread.
        self.client = None
        self.state = '登入中'
        self.username = None
        self.error = None
        while not self.codes.empty():
            self.codes.get_nowait()
        client = None
        deadline = time.monotonic() + 600
        try:
            self.expire_pending()
            pending = self.pending
            if resume and not pending:
                self.state = '沒有待核准登入或已逾時，請重新輸入帳密'
                return
            if pending and (resume or (not payload.sessionid
                    and payload.username == pending.payload.username
                    and payload.password == pending.payload.password)):
                client = pending.client
                deadline = pending.expires
                if resume:
                    payload = pending.payload.model_copy(update={'code': payload.code})
            else:
                client = self.new_client()
            self.pending = None
            # Only acknowledge this supported checkpoint after the user explicitly approves.
            context = client.last_json
            if (resume and isinstance(context, dict)
                    and context.get('bloks_action') == 'com.bloks.www.ig.challenge.redirect.async'
                    and context.get('challenge_context')):
                client.challenge_bloks_redirect_dismiss()
            if payload.sessionid:
                client.login_by_sessionid(payload.sessionid)
            else:
                client.login(payload.username, payload.password, verification_code=payload.code)
            self.activate(client)
        except (TwoFactorRequired, ChallengeRequired) as exc:
            if client is not None:
                self.pending = PendingLogin(client, payload, deadline)
            self.error = type(exc).__name__
            self.state = ('需要雙重驗證或登入核准：請在 Instagram App 按 Approve，'
                          '再按「已在 Instagram 核准，繼續登入」（10 分鐘內）。'
                          '若 Instagram 要求驗證碼，可在繼續登入欄填入。'
                          '若核准後仍停在此處，套件可能不支援該推播流程，可改用 sessionid 匯入。')
            if isinstance(exc, TwoFactorRequired):
                self.state = ('Instagram 要求雙重驗證。目前套件沒有推播核准輪詢，手機 Approve 後不會自動完成登入。'
                              '可按「已在 Instagram 核准，繼續登入」以原裝置狀態重試；'
                              '若仍回到此訊息，請從已登入的瀏覽器匯入 sessionid，'
                              '或使用 Instagram 提供的驗證碼。原登入狀態保留 10 分鐘。')
        except Exception as exc:
            self.state = '登入失敗；請檢查資料或先在 Instagram App 完成安全驗證'
            self.error = type(exc).__name__
        finally:
            self.lock.release()

    def reply(self, messages):
        with httpx.Client(timeout=90) as http:
            response = http.post(os.environ['BASE_URL'].rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + os.environ['API_KEY']},
                json={'model': os.environ['MODEL'], 'messages': [
                    {'role': 'system', 'content': os.getenv('SYSTEM_PROMPT', '請用繁體中文簡潔回答群組問題。')}
                ] + messages})
            response.raise_for_status()
            text = response.json()['choices'][0]['message']['content']
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Empty model response')
            return text.strip()[:900]

    def tick(self):
        client = self.client
        account = str(client.user_id)
        since = self.store.since(account, time.time())
        threads = client.direct_threads(amount=self.limit, thread_message_limit=self.count)
        for thread in threads:
            if not thread.is_group:
                continue
            tid = str(thread.id)
            messages = sorted(thread.messages, key=lambda m: (m.timestamp, str(m.id)))
            for message in messages:
                body = message.text or ''
                if body:
                    self.store.add(account, tid, str(message.id), message.timestamp.timestamp(),
                                   str(message.user_id), body)
            for message in messages:
                mid = str(message.id)
                ts = message.timestamp.timestamp()
                if (ts <= since or str(message.user_id) == account
                        or not mentioned(message.text or '', self.username)
                        or self.store.done(account, tid, mid)):
                    continue
                context = self.store.context(account, tid, ts, self.count, self.chars)
                answer = self.reply(context)
                # Claim before sending: uncertain network outcomes must not cause duplicate replies.
                if not self.store.claim(account, tid, mid):
                    continue
                try:
                    sent = client.direct_send(answer, thread_ids=[int(tid)])
                except Exception:
                    self.store.finish(account, tid, mid, 'uncertain')
                    raise
                self.store.finish(account, tid, mid, 'sent')
                self.store.add(account, tid, str(sent.id), sent.timestamp.timestamp(), account, answer)
            self.store.prune(account, tid, self.count * 3)

    def run(self):
        with self.lock:
            if (self.data / 'session.json').exists():
                try:
                    client = self.new_client()
                    client.load_settings(self.data / 'session.json')
                    self.activate(client)
                except Exception as exc:
                    self.state = 'Session 已失效，請重新登入'
                    self.error = type(exc).__name__
        failures = 0
        while not self.stop.is_set():
            if self.lock.acquire(blocking=False):
                try:
                    self.expire_pending()
                    if self.client:
                        self.tick()
                        self.error = None
                        failures = 0
                except (LoginRequired, ChallengeRequired) as exc:
                    self.client = None
                    self.state = '需要重新登入或在 Instagram App 完成驗證'
                    self.error = type(exc).__name__
                except Exception as exc:
                    failures += 1
                    self.error = type(exc).__name__
                    log.warning('Polling failed: %s', type(exc).__name__)
                finally:
                    self.lock.release()
            self.stop.wait(min(300, self.poll * 2 ** min(failures, 4)))


security = HTTPBasic()
bot = None


def auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    expected = os.environ['ADMIN_PASSWORD']
    if not (hmac.compare_digest(credentials.username.encode(), b'admin') and
            hmac.compare_digest(credentials.password.encode(), expected.encode())):
        raise HTTPException(401, '登入失敗', headers={'WWW-Authenticate': 'Basic'})
    if request.method != 'GET' and request.headers.get('x-bot-admin') != '1':
        raise HTTPException(403, '缺少管理請求標記')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '不允許跨站請求')


@asynccontextmanager
async def lifespan(app):
    global bot
    if len(os.getenv('ADMIN_PASSWORD', '')) < 16 or os.getenv('ADMIN_PASSWORD') == 'replace-with-a-long-random-password':
        raise RuntimeError('請在 .env 設定至少 16 字元的 ADMIN_PASSWORD')
    for key in ('API_KEY', 'BASE_URL', 'MODEL'):
        if not os.getenv(key):
            raise RuntimeError(f'缺少 {key}')
    bot = Bot()
    worker = threading.Thread(target=bot.run, daemon=True)
    worker.start()
    yield
    bot.stop.set()
    worker.join(timeout=3)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    host.strip() for host in os.getenv('ALLOWED_HOSTS', '*').split(',') if host.strip()
])


@app.middleware('http')
async def headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'"
    return response


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/', dependencies=[Depends(auth)], response_class=HTMLResponse)
def index():
    return Path(__file__).with_name('index.html').read_text()


@app.get('/ui.js', dependencies=[Depends(auth)])
def script():
    from fastapi.responses import Response
    return Response(Path(__file__).with_name('ui.js').read_text(), media_type='application/javascript')


@app.get('/style.css', dependencies=[Depends(auth)])
def stylesheet():
    from fastapi.responses import Response
    return Response(Path(__file__).with_name('style.css').read_text(), media_type='text/css')


@app.get('/status', dependencies=[Depends(auth)])
def status():
    return {'state': bot.state, 'username': bot.username, 'error': bot.error,
            'challenge': bot.challenge, 'approval': bool(bot.pending and time.monotonic() < bot.pending.expires)}


class Login(BaseModel):
    username: str = Field(default='', max_length=100)
    password: str = Field(default='', max_length=500)
    code: str = Field(default='', max_length=20)
    sessionid: str = Field(default='', max_length=2000)


@app.post('/login', dependencies=[Depends(auth)])
def login(payload: Login):
    if not payload.sessionid and not (payload.username and payload.password):
        raise HTTPException(400, '請輸入帳密或 sessionid')
    if not bot.lock.acquire(blocking=False):
        raise HTTPException(409, '正在登入或處理訊息，請稍後再試')
    threading.Thread(target=bot.login, args=(payload,), daemon=True).start()
    return {'message': '登入已開始，請查看狀態'}


class Code(BaseModel):
    code: str = Field(pattern=r'^\d{6,8}$')


class ContinueLogin(BaseModel):
    code: str = Field(default='', pattern=r'^(?:\d{6,8})?$')


@app.post('/continue-login', dependencies=[Depends(auth)])
def continue_login(payload: ContinueLogin):
    if not bot.lock.acquire(blocking=False):
        raise HTTPException(409, '正在登入或處理訊息，請稍後再試')
    bot.expire_pending()
    if not bot.pending:
        bot.lock.release()
        raise HTTPException(409, '沒有待核准登入或已逾時，請重新輸入帳密')
    threading.Thread(target=bot.login, args=(payload, True), daemon=True).start()
    return {'message': '正在沿用原登入狀態繼續驗證，請查看狀態'}


@app.post('/challenge', dependencies=[Depends(auth)])
def challenge(payload: Code):
    if not bot.challenge:
        raise HTTPException(409, '目前沒有等待中的驗證')
    bot.codes.put(payload.code)
    return {'message': '驗證碼已送出'}
