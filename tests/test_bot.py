import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from app.core import Store, mentioned


class CoreTests(unittest.TestCase):
    def test_mentions(self):
        for text in ['@my.bot 你好', '嗨 @MY.BOT！', '(@my.bot)']:
            self.assertTrue(mentioned(text, 'my.bot'))
        for text in ['@my.bot2', '@my.bot.other', 'x@my.bot', '@myXbot', 'hi']:
            self.assertFalse(mentioned(text, 'my.bot'))

    def test_context_and_claim_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            store = Store(path)
            store.add('bot', 'a', '1', 1, 'alice', 'first')
            store.add('bot', 'b', '2', 2, 'eve', 'secret')
            store.add('other', 'a', '3', 2, 'eve', 'other account')
            store.add('bot', 'a', '4', 3, 'bot', 'answer')
            store.add('bot', 'a', '5', 4, 'alice', 'future')
            context = store.context('bot', 'a', 3, 40, 1000)
            self.assertEqual([x['content'] for x in context], ['[user alice] first', 'answer'])
            self.assertEqual(context[1]['role'], 'assistant')
            self.assertTrue(store.claim('bot', 'a', '1'))
            self.assertFalse(Store(path).claim('bot', 'a', '1'))
            self.assertEqual(store.since('bot', 100), 100)
            self.assertEqual(Store(path).since('bot', 200), 100)
            self.assertEqual(len(store.context('bot', 'a', 10, 1, 2)[0]['content']), len('[user alice] fu'))


class BotTests(unittest.TestCase):
    def setUp(self):
        from app.main import Bot
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'DATA_DIR': self.temp.name}):
            self.bot = Bot()
        self.bot.username = 'bot'
        self.bot.client = Mock(user_id=10)
        self.bot.store.since('10', 100)
        self.bot.reply = Mock(return_value='answer')
        self.bot.client.direct_send.return_value = self.message('sent', 10, 'answer', 200)

    def tearDown(self):
        self.temp.cleanup()

    def message(self, mid, uid, text, ts):
        return NS(id=mid, user_id=uid, text=text, timestamp=datetime.fromtimestamp(ts, timezone.utc))

    def test_group_mention_context_dedup_and_old_history(self):
        messages = [self.message('1', 20, '@bot old', 90),
                    self.message('2', 20, 'context', 101),
                    self.message('3', 20, '@bot hello', 102),
                    self.message('4', 10, '@bot self', 103)]
        self.bot.client.direct_threads.return_value = [NS(id='100', is_group=True, messages=messages),
            NS(id='200', is_group=False, messages=[self.message('5', 20, '@bot private', 110)])]
        self.bot.tick()
        self.bot.tick()
        self.bot.reply.assert_called_once()
        self.bot.client.direct_send.assert_called_once_with('answer', thread_ids=[100])
        content = str(self.bot.reply.call_args)
        self.assertIn('context', content)
        self.assertNotIn('self', content)
        self.assertNotIn('private', content)

    def test_uncertain_send_not_retried(self):
        self.bot.client.direct_threads.return_value = [NS(id='100', is_group=True,
            messages=[self.message('1', 20, '@bot hello', 102)])]
        self.bot.client.direct_send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.bot.tick()
        self.bot.tick()
        self.assertEqual(self.bot.client.direct_send.call_count, 1)

    def test_model_failure_can_retry(self):
        self.bot.client.direct_threads.return_value = [NS(id='100', is_group=True,
            messages=[self.message('1', 20, '@bot hello', 102)])]
        self.bot.reply.side_effect = [TimeoutError(), 'answer']
        with self.assertRaises(TimeoutError):
            self.bot.tick()
        self.bot.tick()
        self.bot.client.direct_send.assert_called_once()

    def test_model_request(self):
        from app.main import Bot
        with patch.dict(os.environ, {'BASE_URL': 'https://model.example/v1/', 'API_KEY': 'secret', 'MODEL': 'test-model'}):
            with patch('app.main.httpx.Client') as client:
                response = client.return_value.__enter__.return_value.post.return_value
                response.json.return_value = {'choices': [{'message': {'content': 'hello'}}]}
                self.assertEqual(Bot.reply(self.bot, [{'role': 'user', 'content': 'hi'}]), 'hello')
                call = client.return_value.__enter__.return_value.post.call_args
                self.assertEqual(call.args[0], 'https://model.example/v1/chat/completions')
                self.assertEqual(call.kwargs['json']['model'], 'test-model')

    def test_login_saves_session_and_releases_lock(self):
        from app.main import Login
        client = Mock(user_id=10)
        client.account_info.return_value = NS(username='bot')
        client.dump_settings.side_effect = lambda path: Path(path).write_text('{"session": "test"}')
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client):
            self.bot.login(Login(username='bot', password='password', code='123456'))
        client.login.assert_called_once_with('bot', 'password', verification_code='123456')
        self.assertIs(self.bot.client, client)
        self.assertFalse(self.bot.lock.locked())
        session = Path(self.temp.name) / 'session.json'
        self.assertTrue(session.exists())
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)

    def test_two_factor_failure_and_session_import(self):
        from app.main import Login
        from instagrapi.exceptions import TwoFactorRequired
        client = Mock()
        client.login.side_effect = TwoFactorRequired()
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client):
            self.bot.login(Login(username='bot', password='password'))
        self.assertIn('雙重驗證', self.bot.state)
        self.assertIsNone(self.bot.client)
        self.assertFalse(self.bot.lock.locked())
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client), patch.object(self.bot, 'activate') as activate:
            self.bot.login(Login(sessionid='session-token'))
            client.login_by_sessionid.assert_called_once_with('session-token')
            activate.assert_called_once_with(client)

    def test_approval_resume_reuses_client_and_acknowledges_checkpoint(self):
        from app.main import Login, ContinueLogin
        from instagrapi.exceptions import ChallengeRequired
        client = Mock()
        client.last_json = {'bloks_action': 'com.bloks.www.ig.challenge.redirect.async',
                            'challenge_context': 'private-context'}
        client.login.side_effect = [ChallengeRequired(), True]
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client) as factory:
            self.bot.login(Login(username='bot', password='password'))
            self.assertIs(self.bot.pending.client, client)
            self.assertFalse((Path(self.temp.name) / 'session.json').exists())
            self.bot.lock.acquire()
            with patch.object(self.bot, 'activate') as activate:
                self.bot.login(ContinueLogin(), resume=True)
                activate.assert_called_once_with(client)
            factory.assert_called_once()
        client.challenge_bloks_redirect_dismiss.assert_called_once()
        self.assertEqual(client.login.call_count, 2)
        self.assertIsNone(self.bot.pending)
        self.assertFalse(self.bot.lock.locked())

    def test_two_factor_resume_uses_code_and_original_device(self):
        from app.main import Login, ContinueLogin
        from instagrapi.exceptions import TwoFactorRequired
        client = Mock(last_json={})
        client.login.side_effect = [TwoFactorRequired(), True]
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client) as factory:
            self.bot.login(Login(username='bot', password='password'))
            self.bot.lock.acquire()
            with patch.object(self.bot, 'activate'):
                self.bot.login(ContinueLogin(code='123456'), resume=True)
            factory.assert_called_once()
        client.login.assert_called_with('bot', 'password', verification_code='123456')
        client.challenge_bloks_redirect_dismiss.assert_not_called()

    def test_expired_approval_does_not_attempt_login(self):
        from app.main import Login, ContinueLogin, PendingLogin
        client = Mock()
        self.bot.pending = PendingLogin(client, Login(username='bot', password='password'), 0)
        self.bot.lock.acquire()
        self.bot.login(ContinueLogin(), resume=True)
        client.login.assert_not_called()
        self.assertIsNone(self.bot.pending)
        self.assertFalse(self.bot.lock.locked())

    def test_unapproved_checkpoint_does_not_activate(self):
        from app.main import Login, ContinueLogin, PendingLogin
        from instagrapi.exceptions import ChallengeRequired
        client = Mock(last_json={'bloks_action': 'com.bloks.www.ig.challenge.redirect.async',
                                'challenge_context': 'private-context'})
        client.challenge_bloks_redirect_dismiss.side_effect = ChallengeRequired()
        self.bot.pending = PendingLogin(client, Login(username='bot', password='password'), float('inf'))
        self.bot.lock.acquire()
        with patch.object(self.bot, 'activate') as activate:
            self.bot.login(ContinueLogin(), resume=True)
            activate.assert_not_called()
        client.login.assert_not_called()
        self.assertIs(self.bot.pending.client, client)
        self.assertFalse(self.bot.lock.locked())


class WebTests(unittest.TestCase):
    def test_admin_and_csrf(self):
        from fastapi.testclient import TestClient
        from app.main import app, Bot
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'DATA_DIR': directory, 'ADMIN_PASSWORD': 'a-secure-test-password',
            'API_KEY': 'test', 'BASE_URL': 'https://model.example/v1', 'MODEL': 'test'}), patch.object(Bot, 'run'):
            with TestClient(app) as client:
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.get('/status').status_code, 401)
                client.auth = ('admin', 'a-secure-test-password')
                self.assertEqual(client.get('/').status_code, 200)
                self.assertNotIn('sessionid', client.get('/status').text)
                self.assertEqual(client.post('/login', json={}).status_code, 403)
                self.assertEqual(client.post('/continue-login', json={}).status_code, 403)
                self.assertEqual(client.post('/continue-login', json={}, headers={'X-Bot-Admin': '1'}).status_code, 409)
                self.assertEqual(client.post('/login', json={}, headers={
                    'X-Bot-Admin': '1', 'Origin': 'https://evil.example'}).status_code, 403)
                self.assertEqual(client.post('/login', json={}, headers={'X-Bot-Admin': '1'}).status_code, 400)
                self.assertEqual(client.get('/', headers={'Host': '192.168.1.10:8001'}).status_code, 200)
                self.assertEqual(client.post('/login', json={}, headers={
                    'Host': '192.168.1.10:8001', 'Origin': 'http://192.168.1.10:8001',
                    'X-Bot-Admin': '1'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
