import hmac
import base64
import json
import logging
import os
from pathlib import Path
import threading
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urljoin, urlsplit

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
from .images import GeneratedImage, IMAGE_TOOL, decode_image, normalize_reference_image

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


def latest_user_text(messages):
    for message in reversed(messages):
        if message.get('role') == 'user' and isinstance(message.get('content'), str):
            return message['content']
    return ''


def requests_image(text):
    lowered = text.casefold()
    return any(marker in lowered for marker in (
        '生成圖片', '生成一張', '產生圖片', '產生一張', '生圖', '畫一張', '畫張',
        '繪製', '做一張圖', '做張圖', 'generate an image', 'generate image',
        'create an image', 'create image', 'draw an image', 'draw a picture',
        '改圖', '修圖', '修改圖片', '修改照片', '修改這張', '把這張', '將這張',
        'edit image', 'edit this', 'edit the photo', '重畫',
    ))


def requests_profile_reference(text):
    lowered = text.casefold()
    return any(marker in lowered for marker in (
        '頭像', '大頭貼', '個人照片', 'profile pic', 'profile picture', 'avatar',
    ))


def refers_to_image(text):
    return any(marker in text.casefold() for marker in (
        '圖片', '照片', '這張', '那張', '上圖', '剛才的圖', '改圖', '修圖',
        '這個改', '改成', '換成', '改一下', 'image', 'photo', 'picture', 'edit',
    ))


def message_image_url(message):
    # Only ordinary still photos; never treat a video thumbnail as an uploaded photo.
    media = getattr(message, 'media', None) or getattr(message, 'media_share', None)
    if media is not None and getattr(media, 'media_type', None) == 1:
        url = getattr(media, 'thumbnail_url', None)
        return str(url) if url else None
    return None


def image_parts(references):
    parts = []
    for label, jpeg in references:
        parts.extend([
            {'type': 'text', 'text': label},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode('ascii')}},
        ])
    return parts


def requests_web_search(text):
    lowered = text.casefold()
    return any(marker in lowered for marker in (
        '搜尋', '搜索', '查詢', '查一下', '上網查', '搵資料', '今日', '今天', '而家',
        '現在', '最新', '即時', '實時', '天氣', '日期', '幾月幾日', '幾點', '新聞',
        '股價', '匯率', '賽果', 'search the web', 'search online', 'look up', 'today',
        'current', 'latest', 'weather', 'news', 'exchange rate', 'stock price',
    ))


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


@contextmanager
def typing_activity(client, thread_id):
    stop = threading.Event()
    ready = threading.Event()

    def publish():
        realtime = None
        try:
            # Use an independent realtime client so the polling client remains
            # exclusively owned by the main Bot worker.
            realtime = client.realtime_client()
            realtime.connect()
            while not stop.is_set():
                realtime.direct_indicate_activity(thread_id, is_active=True)
                ready.set()
                if stop.wait(5):
                    break
        except Exception as exc:
            ready.set()
            log.warning('Instagram typing indicator unavailable: %s', type(exc).__name__)
        finally:
            if realtime is not None:
                try:
                    if realtime.connected:
                        realtime.direct_indicate_activity(thread_id, is_active=False)
                except Exception:
                    pass
                try:
                    realtime.disconnect()
                except Exception:
                    pass

    worker = threading.Thread(target=publish, name='instagram-typing', daemon=True)
    worker.start()
    # Usually completes immediately; never make model work depend on MQTT.
    ready.wait(timeout=1)
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2)


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
        self.count = max(1, min(10, int(os.getenv('CONTEXT_MESSAGES', '10'))))
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

    def reply(self, messages, participants=None, sender_id=None, source_images=None):
        participants = participants or []
        source_images = source_images or []
        system = os.getenv('SYSTEM_PROMPT', '請用繁體中文簡潔回答群組問題。')
        web_search = os.getenv('WEB_SEARCH', '').strip().lower() in {'1', 'true', 'yes', 'on'}
        image_model = os.getenv('IMAGE_MODEL', '').strip()
        latest = latest_user_text(messages)
        if web_search and requests_web_search(latest) and not requests_image(latest) and not source_images:
            return self.search_web(messages, latest[:4000])
        if web_search:
            if image_model:
                system += (
                    '\n你具備網路搜尋能力。需要即時、最新或網路上的資料時，呼叫 search_web；'
                    '不可聲稱自己無法連網。'
                    '若回應中有可用來源資訊，請在回答中簡短列出。'
                )
            else:
                system += (
                    '\n你具備網路搜尋能力。需要即時、最新或網路上的資料時，使用 Google Search；'
                    '不可聲稱自己無法連網。'
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
                '\n當最新使用者要求生成、畫圖、修改、換背景或依照圖片變化時，呼叫 generate_image，'
                '把上下文整理為完整的圖片描述。工具會直接把圖片發送至群組；'
                '一般聊天不需呼叫工具。每次最多生成一張圖片。'
                '若描繪群組成員，reference_usernames 必須包含所有人物的精確 username，不能只填一人。'
                '多人必須各自出現，保留各人的外觀，不可合併成一人；排除僅用於呼叫你的 bot 標註。'
                '若已附上來源圖片，修改要求必須以來源圖片為基礎，保留未要求更動的內容。'
                '若使用者要修改既有圖片但沒有提供來源圖片，請要求重新上傳或回覆該圖片，'
                '不可只憑先前提示詞重畫並聲稱已修改原圖。'
                '只問圖片內容時回答問題，不必生成。')
            member_descriptions = [
                f"@{member['username']} ({member.get('full_name') or '未設定名稱'})"
                for member in participants if member.get('username')
            ]
            sender = next((member for member in participants
                           if member.get('id') == str(sender_id)), None)
            if member_descriptions:
                payload['messages'][0]['content'] += '\n目前群組成員：' + '、'.join(member_descriptions)
            if sender and sender.get('username'):
                payload['messages'][0]['content'] += f"\n最新發言者是 @{sender['username']}。"
            # Gemini/Antigravity can reject a built-in search tool mixed with
            # function declarations. Route search as a function first, then make
            # a separate request containing only the server-side search tool.
            payload['tools'] = ([WEB_SEARCH_TOOL] if web_search else []) + [IMAGE_TOOL]
            payload['tool_choice'] = 'auto'
        if source_images:
            payload['messages'] = [dict(item) for item in payload['messages']]
            for item in reversed(payload['messages']):
                if item['role'] == 'user':
                    item['content'] = [{'type': 'text', 'text': item['content']}] + image_parts(source_images)
                    break
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
                usernames = arguments.get('reference_usernames', [])
                legacy = arguments.get('reference_username')
                if (not isinstance(usernames, list) or
                        any(not isinstance(name, str) or not name.strip() for name in usernames) or
                        (legacy is not None and not isinstance(legacy, str))):
                    raise ValueError('Invalid reference usernames')
                usernames = list(usernames)
                if legacy:
                    usernames.append(legacy)
                # Recover all explicitly tagged members even if the model only supplied one.
                # A supplied photo is the primary source unless avatars are explicitly requested.
                if not source_images or requests_profile_reference(latest):
                    usernames.extend(member['username'] for member in participants
                                     if member.get('username') and mentioned(latest, member['username'])
                                     and member['username'].casefold() != (self.username or '').casefold())
                if requests_profile_reference(latest) and any(word in latest.casefold() for word in (
                        '我的', '我嘅', 'my ')):
                    sender = next((m for m in participants if m.get('id') == str(sender_id)), None)
                    if sender:
                        usernames.append(sender['username'])
                usernames = list(dict.fromkeys(name.strip().lstrip('@').casefold() for name in usernames))
                if len(usernames) > 10:
                    return '一次最多可使用 10 位群組成員的頭像，請減少人數後再試。'
                references = list(source_images)
                for username in usernames:
                    member = next((m for m in participants
                                   if m.get('username', '').casefold() == username), None)
                    if member is None:
                        return f'找不到群組成員 @{username} 的頭像，請確認 username 後再試一次。'
                    if not member.get('profile_pic_url'):
                        return f'群組成員 @{username} 暫時沒有可用的頭像。'
                    try:
                        jpeg = self.download_profile_picture(member['profile_pic_url'])
                    except Exception as exc:
                        log.warning('Profile picture download failed: %s', type(exc).__name__)
                        return f'暫時無法取得 @{username} 的頭像，請稍後再試。'
                    references.append((f'人物 @{username} 的頭像參考', jpeg))
                if requests_profile_reference(latest) and not references:
                    return '找不到指定的群組成員頭像，請標註要參考的成員。'
                if references:
                    prompt = prompt.strip() + '\n使用者原始要求：' + latest
                    if usernames:
                        prompt += ('\n人物參考對照：' + '、'.join('@' + name for name in usernames)
                                   + '。依要求描繪每位人物，各自保留可辨識特徵，不可遺漏或合併人物。')
                    return self.generate_image(prompt, references=references)
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

    def download_profile_picture(self, profile_pic_url):
        current = str(profile_pic_url)
        headers = {
            'User-Agent': getattr(self.client, 'user_agent', 'Mozilla/5.0'),
            'Referer': 'https://www.instagram.com/',
        }
        with httpx.Client(timeout=20, follow_redirects=False) as http:
            for _ in range(4):
                parsed = urlsplit(current)
                host = (parsed.hostname or '').lower()
                if parsed.scheme != 'https' or not (
                        host == 'instagram.com' or host.endswith('.instagram.com')
                        or host.endswith('.cdninstagram.com') or host.endswith('.fbcdn.net')):
                    raise ValueError('Unsupported profile picture URL')
                with http.stream('GET', current, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get('location')
                        if not location:
                            raise ValueError('Profile picture redirect has no location')
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get('content-type', '').split(';', 1)[0]
                    if not content_type.startswith('image/'):
                        raise ValueError('Profile picture response is not an image')
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 20 * 1024 * 1024:
                            raise ValueError('Profile picture exceeds 20 MiB')
                        chunks.append(chunk)
                    return normalize_reference_image(b''.join(chunks))
        raise ValueError('Too many profile picture redirects')

    def search_web(self, messages, query):
        system = os.getenv('SYSTEM_PROMPT', '請用繁體中文簡潔回答群組問題。')
        system += (
            '\n你具備網路搜尋能力。本次必須使用 Google Search 查詢後再回答，'
            '不可聲稱自己無法連網。'
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

    def generate_image(self, prompt, reference_image=None, references=None):
        references = list(references or [])
        if reference_image is not None:
            references.append(('頭像參考', reference_image))
        content = prompt
        if references:
            content = [{'type': 'text', 'text': (
                prompt + '\nUse each labeled reference for its corresponding person or source image. '
                'For edits, modify the source image and preserve details not requested to change. '
                'Keep distinct people recognizable; do not merge their identities.')}]
            content.extend(image_parts(references))
        with httpx.Client(timeout=180) as http:
            response = http.post(os.environ['BASE_URL'].rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + os.environ['API_KEY']},
                json={
                    'model': os.environ['IMAGE_MODEL'],
                    'messages': [{'role': 'user', 'content': content}],
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

    def source_image(self, account, tid, message):
        ts = message.timestamp.timestamp()
        current = self.store.image(account, tid, ts, mid=str(message.id))
        if current:
            return current
        reply = getattr(message, 'reply', None)
        if reply is not None:
            # An explicit reply always takes precedence over a recent unrelated upload.
            return self.store.image(account, tid, ts, mid=str(reply.id))
        if refers_to_image(message.text or ''):
            return self.store.image(account, tid, ts, sender=str(message.user_id))
        return None

    def tick(self):
        client = self.client
        account = str(client.user_id)
        since = self.store.since(account, time.time())
        threads = client.direct_threads(amount=self.limit, thread_message_limit=self.count)
        for thread in threads:
            if not thread.is_group:
                continue
            tid = str(thread.id)
            participants = []
            for user in getattr(thread, 'users', []):
                if not getattr(user, 'username', None):
                    continue
                profile_pic_url = (getattr(user, 'profile_pic_url_hd', None)
                                   or getattr(user, 'profile_pic_url', None))
                participants.append({
                    'id': str(user.pk),
                    'username': user.username,
                    'full_name': getattr(user, 'full_name', '') or '',
                    'profile_pic_url': str(profile_pic_url) if profile_pic_url else None,
                })
            messages = sorted(thread.messages, key=lambda m: (m.timestamp, str(m.id)))
            for message in messages:
                body = message.text or ''
                url = message_image_url(message)
                if url:
                    self.store.add_image(account, tid, str(message.id), message.timestamp.timestamp(),
                                         str(message.user_id), url=url)
                    body = (body + '\n[圖片附件]').strip()
                replied_to = getattr(message, 'reply', None)
                reply_url = message_image_url(replied_to)
                if reply_url:
                    self.store.add_image(account, tid, str(replied_to.id),
                                         replied_to.timestamp.timestamp(), str(replied_to.user_id),
                                         url=reply_url)
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
                    with typing_activity(client, tid):
                        source = self.source_image(account, tid, message)
                        source_images = []
                        source_error = False
                        if source:
                            try:
                                jpeg = source['jpeg'] or self.download_profile_picture(source['url'])
                                source_images = [(f"來源圖片（訊息 {source['id']}）", jpeg)]
                            except Exception as exc:
                                log.warning('Source image download failed: %s', type(exc).__name__)
                                source_error = True
                        if source_error:
                            answer = '無法讀取這張圖片，可能已過期。請重新上傳圖片，再 @我說明要修改的內容。'
                        else:
                            answer = self.reply(context, participants=participants,
                                                sender_id=str(message.user_id), source_images=source_images)
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
                if is_image:
                    self.store.add_image(account, tid, str(sent.id), sent.timestamp.timestamp(),
                                         account, jpeg=normalize_reference_image(answer.jpeg))
            self.store.prune(account, tid, self.count)

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
