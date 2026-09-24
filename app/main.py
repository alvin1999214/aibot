import hmac
import json
import logging
import os
from pathlib import Path
import threading
import tempfile
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, TwoFactorRequired
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import Store, mentioned
from .images import GeneratedImage, IMAGE_TOOL, decode_image

WEB_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_web',
        'description': ('Search the web before answering. Use for current, recent, changing, '
                        'or externally verifiable information, and when the user asks to search.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'A concise web search query.'},
            },
            'required': ['query'],
            'additionalProperties': False,
        },
    },
}

log = logging.getLogger('bot')
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    log.addHandler(handler)
log.propagate = False


def conversation_log(event, **fields):
    # Keep Unicode readable and escape newlines so each event occupies one log line.
    log.info('%s', json.dumps({'event': event, **fields}, ensure_ascii=False))


def raise_for_model_status(response):
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        # Provider errors usually contain the actionable cause. Keep the log bounded
        # and on one line; request headers (including the API key) are never logged.
        body = response.text[:2000].replace('\r', '\\r').replace('\n', '\\n')
        log.warning('Model API rejected request: status=%s body=%s', response.status_code, body)
        raise


class Bot:
    def __init__(self):
        self.data = Path(os.getenv('DATA_DIR', './data'))
        self.data.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data, 0o700)
        self.store = Store(self.data / 'bot.sqlite3')
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.client = None
        self.state = '尚未匯入 Session'
        self.error = None
        self.username = None
        self.poll = max(10, int(os.getenv('POLL_SECONDS', '20')))
        self.count = max(1, min(200, int(os.getenv('CONTEXT_MESSAGES', '40'))))
        self.chars = max(1000, int(os.getenv('CONTEXT_CHARS', '16000')))
        self.limit = max(1, int(os.getenv('THREAD_LIMIT', '50')))

    def new_client(self):
        client = Client()
        if os.getenv('IG_PROXY'):
            client.set_proxy(os.environ['IG_PROXY'])
        # Propagate verification failures; never enter the library's interactive login flow.
        client.handle_exception = self.reject_verification
        return client

    @staticmethod
    def reject_verification(client, exc):
        raise exc

    def save(self, client):
        temp = self.data / 'session.tmp'
        client.dump_settings(temp)
        os.chmod(temp, 0o600)
        temp.replace(self.data / 'session.json')

    def activate(self, client):
        user = client.account_info()
        self.save(client)
        self.store.start_session(str(client.user_id), time.time())
        self.username = user.username
        self.client = client
        self.state = '運行中'
        self.error = None

    def import_session(self, sessionid):
        # The request handler holds this lock before starting the thread.
        self.client = None
        self.state = '正在驗證 Session'
        self.username = None
        self.error = None
        try:
            client = self.new_client()
            if not client.login_by_sessionid(sessionid):
                raise LoginRequired('Session import rejected')
            self.activate(client)
        except (TwoFactorRequired, ChallengeRequired, LoginRequired) as exc:
            self.state = 'Session 已失效或需要驗證；請在 Instagram 網站完成登入與驗證，再重新取得 sessionid 匯入'
            self.error = type(exc).__name__
        except Exception as exc:
            self.state = 'Session 匯入失敗，請確認 sessionid、網路連線及 Instagram 帳號狀態'
            self.error = type(exc).__name__
        finally:
            self.lock.release()

    def reply(self, messages):
        system = os.getenv('SYSTEM_PROMPT', '請用繁體中文簡潔回答群組問題。')
        web_search = os.getenv('WEB_SEARCH', '').strip().lower() in {'1', 'true', 'yes', 'on'}
        image_model = os.getenv('IMAGE_MODEL', '').strip()
        if web_search:
            if image_model:
                system += (
                    '\n需要即時、最新或網路上的資料時，呼叫 search_web；'
                    '若回應中有可用來源資訊，請在回答中簡短列出。'
                )
            else:
                system += (
                    '\n需要即時、最新或網路上的資料時，使用 Google Search；'
                    '若回應中有可用來源資訊，請在回答中簡短列出。'
                )
        payload = {'model': os.environ['MODEL'], 'messages': [
            {'role': 'system', 'content': system}
        ] + messages}
        if web_search and not image_model:
            # CLIProxyAPI maps this OpenAI-compatible extension to Gemini's
            # native {"googleSearch": {}} grounding tool.
            payload['tools'] = [{'google_search': {}}]
        if image_model:
            payload['messages'][0]['content'] += (
                '\n當最新使用者要求生成或畫圖片時，呼叫 generate_image，'
                '把上下文整理為完整的圖片描述。工具會直接把圖片發送至群組；'
                '一般聊天不需呼叫工具。每次最多生成一張圖片。')
            # Gemini/Antigravity can reject a built-in search tool mixed with
            # function declarations. Route search as a function first, then make
            # a separate request containing only the server-side search tool.
            payload['tools'] = ([WEB_SEARCH_TOOL] if web_search else []) + [IMAGE_TOOL]
            payload['tool_choice'] = 'auto'
        with httpx.Client(timeout=90) as http:
            response = http.post(os.environ['BASE_URL'].rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + os.environ['API_KEY']},
                json=payload)
            raise_for_model_status(response)
            message = response.json()['choices'][0]['message']
        calls = message.get('tool_calls') or []
        if calls:
            if len(calls) != 1 or not payload.get('tools'):
                raise ValueError('Unexpected tool calls')
            function = calls[0]['function']
            arguments = json.loads(function['arguments'])
            if not isinstance(arguments, dict):
                raise ValueError('Invalid tool arguments')
            if function['name'] == 'generate_image':
                prompt = arguments.get('prompt')
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
                    raise ValueError('Invalid image prompt')
                return self.generate_image(prompt.strip())
            if function['name'] == 'search_web' and web_search:
                query = arguments.get('query')
                if not isinstance(query, str) or not query.strip() or len(query) > 4000:
                    raise ValueError('Invalid web search query')
                return self.search_web(messages, query.strip())
            raise ValueError('Unknown model tool')
        text = message.get('content')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Empty model response')
        return text.strip()[:900]

    def search_web(self, messages, query):
        system = os.getenv('SYSTEM_PROMPT', '請用繁體中文簡潔回答群組問題。')
        system += (
            '\n本次必須使用 Google Search 查詢後再回答。'
            '請根據搜尋結果簡潔回答；若回應中有可用來源資訊，請列出來源。'
            f'\n搜尋主題：{query}'
        )
        with httpx.Client(timeout=90) as http:
            response = http.post(os.environ['BASE_URL'].rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + os.environ['API_KEY']},
                json={
                    'model': os.environ['MODEL'],
                    'messages': [{'role': 'system', 'content': system}] + messages,
                    'tools': [{'google_search': {}}],
                })
            raise_for_model_status(response)
            message = response.json()['choices'][0]['message']
        text = message.get('content')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Empty web search response')
        return text.strip()[:900]

    def generate_image(self, prompt):
        with httpx.Client(timeout=180) as http:
            response = http.post(os.environ['BASE_URL'].rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + os.environ['API_KEY']},
                json={
                    'model': os.environ['IMAGE_MODEL'],
                    'messages': [{'role': 'user', 'content': prompt}],
                    'modalities': ['image', 'text'],
                    'image_config': {
                        'aspect_ratio': os.getenv('IMAGE_ASPECT_RATIO', '1:1'),
                        'image_size': os.getenv('IMAGE_SIZE', '1K'),
                    },
                })
            raise_for_model_status(response)
            message = response.json()['choices'][0]['message']
        return GeneratedImage(prompt=prompt, jpeg=decode_image(message))

    def send_answer(self, client, tid, answer):
        if isinstance(answer, GeneratedImage):
            with tempfile.TemporaryDirectory(prefix='image-', dir=self.data) as directory:
                path = Path(directory) / 'generated.jpg'
                path.write_bytes(answer.jpeg)
                return client.direct_send_photo(path, thread_ids=[int(tid)])
        return client.direct_send(answer, thread_ids=[int(tid)])

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
                body = message.text or ''
                replied_to = getattr(message, 'reply', None)
                replies_to_bot = (replied_to is not None
                                  and str(replied_to.user_id) == account)
                if (ts <= since or str(message.user_id) == account
                        or not body.strip()
                        or not (mentioned(body, self.username) or replies_to_bot)
                        or self.store.done(account, tid, mid)):
                    continue
                context = self.store.context(account, tid, ts, self.count, self.chars)
                details = {'account': account, 'thread_id': tid, 'message_id': mid}
                conversation_log('conversation.input', **details,
                                 sender_id=str(message.user_id), text=message.text, context=context,
                                 reply_to_message_id=str(replied_to.id) if replied_to else None)
                try:
                    answer = self.reply(context)
                except Exception as exc:
                    conversation_log('conversation.model_failed', **details, error=type(exc).__name__)
                    raise
                is_image = isinstance(answer, GeneratedImage)
                answer_text = '[已生成圖片] ' + answer.prompt if is_image else answer
                output = {'text': answer_text, 'media_type': 'image' if is_image else 'text'}
                # Claim before sending: uncertain network outcomes must not cause duplicate replies.
                if not self.store.claim(account, tid, mid):
                    continue
                try:
                    sent = self.send_answer(client, tid, answer)
                except Exception as exc:
                    self.store.finish(account, tid, mid, 'uncertain')
                    conversation_log('conversation.output', **details, **output,
                                     status='uncertain', error=type(exc).__name__)
                    raise
                self.store.finish(account, tid, mid, 'sent')
                conversation_log('conversation.output', **details, **output,
                                 status='sent', sent_message_id=str(sent.id))
                self.store.add(account, tid, str(sent.id), sent.timestamp.timestamp(), account, answer_text)
            self.store.prune(account, tid, self.count * 3)

    def run(self):
        with self.lock:
            if (self.data / 'session.json').exists():
                try:
                    client = self.new_client()
                    client.load_settings(self.data / 'session.json')
                    self.activate(client)
                except Exception as exc:
                    self.state = 'Session 無法恢復，請重新取得 sessionid 匯入'
                    self.error = type(exc).__name__
        failures = 0
        while not self.stop.is_set():
            if self.lock.acquire(blocking=False):
                try:
                    if self.client:
                        self.tick()
                        self.error = None
                        failures = 0
                except (LoginRequired, ChallengeRequired, TwoFactorRequired) as exc:
                    self.client = None
                    self.username = None
                    self.state = 'Session 需要驗證；請在 Instagram 網站完成驗證後，重新取得 sessionid 匯入'
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


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(status_code=422, content={'detail': '請只提交非空白的 sessionid（最多 2000 字元）'})


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
    return {'state': bot.state, 'username': bot.username, 'error': bot.error}


class SessionImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sessionid: str = Field(min_length=1, max_length=2000, repr=False)

    @field_validator('sessionid')
    @classmethod
    def trim_sessionid(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('請輸入 sessionid')
        return value


@app.post('/session', dependencies=[Depends(auth)])
def import_session(payload: SessionImport):
    if not bot.lock.acquire(blocking=False):
        raise HTTPException(409, '正在驗證 Session 或處理訊息，請稍後再試')
    threading.Thread(target=bot.import_session, args=(payload.sessionid,), daemon=True).start()
    return {'message': 'Session 匯入已開始，請查看狀態'}
