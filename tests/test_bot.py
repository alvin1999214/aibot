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

    def test_reply_to_bot_triggers_once_without_mention(self):
        from instagrapi.types import ReplyMessage
        message = self.message('1', 20, '請再解釋一下', 102)
        message.reply = ReplyMessage(id='previous', user_id='10',
                                    timestamp=datetime.fromtimestamp(90, timezone.utc), text='先前回覆')
        self.bot.client.direct_threads.return_value = [NS(id='100', is_group=True, messages=[message])]
        self.bot.tick()
        self.bot.tick()
        self.bot.reply.assert_called_once()
        self.assertIn('請再解釋一下', self.bot.reply.call_args.args[0][-1]['content'])
        self.bot.client.direct_send.assert_called_once_with('answer', thread_ids=[100])

    def test_reply_trigger_ignores_other_users_self_history_private_and_nontext(self):
        for target, sender, ts, is_group, body in [
            ('20', 30, 102, True, '回覆別人'),
            (None, 20, 102, True, '未知發送者'),
            ('10', 10, 102, True, '自己回覆'),
            ('10', 20, 90, True, '舊訊息'),
            ('10', 20, 102, False, '私訊'),
            ('10', 20, 102, True, None),
            ('10', 20, 102, True, '   '),
        ]:
            with self.subTest(target=target, sender=sender, ts=ts, is_group=is_group, body=body):
                message = self.message('1', sender, body, ts)
                message.reply = NS(id='previous', user_id=target)
                self.bot.client.direct_threads.return_value = [
                    NS(id='100', is_group=is_group, messages=[message])]
                self.bot.tick()
        self.bot.reply.assert_not_called()
        self.bot.client.direct_send.assert_not_called()

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

    def test_model_request_enables_google_search(self):
        from app.main import Bot
        with patch.dict(os.environ, {'BASE_URL': 'https://model.example/v1', 'API_KEY': 'secret',
                                     'MODEL': 'test-model', 'WEB_SEARCH': 'true'}):
            with patch('app.main.httpx.Client') as client:
                response = client.return_value.__enter__.return_value.post.return_value
                response.json.return_value = {'choices': [{'message': {'content': '已查詢'}}]}
                self.assertEqual(Bot.reply(self.bot, [{'role': 'user', 'content': '查一下最新消息'}]), '已查詢')
                payload = client.return_value.__enter__.return_value.post.call_args.kwargs['json']
                self.assertIn({'google_search': {}}, payload['tools'])

    def test_import_saves_session_and_releases_lock(self):
        client = Mock(user_id=10)
        client.account_info.return_value = NS(username='bot')
        client.dump_settings.side_effect = lambda path: Path(path).write_text('{"session": "test"}')
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client):
            self.bot.import_session('session-token')
        client.login_by_sessionid.assert_called_once_with('session-token')
        client.login.assert_not_called()
        self.assertIs(self.bot.client, client)
        self.assertFalse(self.bot.lock.locked())
        session = Path(self.temp.name) / 'session.json'
        self.assertTrue(session.exists())
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)

    def test_rejected_session_never_activates(self):
        from instagrapi.exceptions import TwoFactorRequired, ChallengeRequired, LoginRequired
        for error in [TwoFactorRequired(), ChallengeRequired(), LoginRequired(), TimeoutError()]:
            with self.subTest(error=type(error).__name__):
                client = Mock()
                client.login_by_sessionid.side_effect = error
                self.bot.lock.acquire()
                with patch.object(self.bot, 'new_client', return_value=client), patch.object(self.bot, 'activate') as activate:
                    self.bot.import_session('secret-token')
                    activate.assert_not_called()
                self.assertIsNone(self.bot.client)
                self.assertIsNone(self.bot.username)
                self.assertFalse(self.bot.lock.locked())
                self.assertEqual(self.bot.error, type(error).__name__)
                self.assertNotIn('secret-token', self.bot.state)
                self.assertFalse((Path(self.temp.name) / 'session.json').exists())

    def test_library_challenge_is_propagated_without_interaction(self):
        from instagrapi.exceptions import ChallengeRequired
        client = self.bot.new_client()
        with self.assertRaises(ChallengeRequired):
            client.handle_exception(client, ChallengeRequired())

    def test_saved_session_restores_without_password_login(self):
        (Path(self.temp.name) / 'session.json').write_text('{}')
        client = Mock()
        self.bot.stop.set()
        with patch.object(self.bot, 'new_client', return_value=client), patch.object(self.bot, 'activate') as activate:
            self.bot.run()
            activate.assert_called_once_with(client)
        client.load_settings.assert_called_once_with(Path(self.temp.name) / 'session.json')
        client.login.assert_not_called()

    def test_restart_ignores_pending_history_but_replies_to_new_messages(self):
        from app.main import Bot
        (Path(self.temp.name) / 'session.json').write_text('{}')
        old_mention = self.message('old-mention', 20, '@bot 舊要求', 102)
        old_reply = self.message('old-reply', 20, '先前追問', 110)
        old_reply.reply = NS(id='bot-message', user_id='10')
        boundary = self.message('boundary', 20, '@bot 啟動時的訊息', 150)
        self.bot.store.claim('10', '100', 'already-sent')
        self.bot.store.finish('10', '100', 'already-sent', 'sent')
        with patch.dict(os.environ, {'DATA_DIR': self.temp.name}):
            restarted = Bot()
        client = self.bot.client
        client.account_info.return_value = NS(username='bot')
        restarted.stop.set()
        with patch.object(restarted, 'new_client', return_value=client), \
                patch.object(restarted, 'save'), patch('app.main.time.time', return_value=150):
            restarted.run()
        restarted.reply = Mock(return_value='answer')
        thread = NS(id='100', is_group=True, messages=[old_mention, old_reply, boundary])
        client.direct_threads.return_value = [thread]
        restarted.tick()
        restarted.tick()
        restarted.reply.assert_not_called()
        client.direct_send.assert_not_called()
        client.direct_send_photo.assert_not_called()
        self.assertTrue(restarted.store.done('10', '100', 'already-sent'))
        thread.messages.append(self.message('new', 20, '@bot 新要求', 151))
        restarted.tick()
        restarted.tick()
        restarted.reply.assert_called_once()
        client.direct_send.assert_called_once_with('answer', thread_ids=[100])
        context = restarted.reply.call_args.args[0]
        self.assertTrue(any('舊要求' in item['content'] for item in context))

    def test_session_reimport_refreshes_cutoff(self):
        client = Mock(user_id=10)
        client.account_info.return_value = NS(username='bot')
        self.bot.lock.acquire()
        with patch.object(self.bot, 'new_client', return_value=client), \
                patch.object(self.bot, 'save'), patch('app.main.time.time', return_value=300):
            self.bot.import_session('session-token')
        self.assertEqual(self.bot.store.since('10', 400), 300)
        self.assertFalse(self.bot.lock.locked())



class WebTests(unittest.TestCase):
    def test_session_api_and_admin_protection(self):
        from fastapi.testclient import TestClient
        from app.main import app, Bot
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'DATA_DIR': directory, 'ADMIN_PASSWORD': 'a-secure-test-password',
            'API_KEY': 'test', 'BASE_URL': 'https://model.example/v1', 'MODEL': 'test'}), patch.object(Bot, 'run'):
            with TestClient(app) as client:
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.get('/status').status_code, 401)
                self.assertEqual(client.post('/session', json={'sessionid': 'secret'}).status_code, 401)
                client.auth = ('admin', 'a-secure-test-password')
                page = client.get('/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('如何取得 sessionid', page.text)
                for field in ['username', 'password', 'code']:
                    self.assertNotIn(f'name="{field}"', page.text)
                self.assertEqual(set(client.get('/status').json()), {'state', 'username', 'error'})
                for route in ['/login', '/continue-login', '/challenge']:
                    self.assertEqual(client.post(route, json={}).status_code, 404)
                self.assertEqual(client.post('/session', json={}).status_code, 403)
                self.assertEqual(client.post('/session', json={}, headers={
                    'X-Bot-Admin': '1', 'Origin': 'https://evil.example'}).status_code, 403)
                for payload in [{}, {'sessionid': '   '}, {'sessionid': 'x' * 2001},
                                {'sessionid': 'secret-token', 'password': 'secret-password'}]:
                    response = client.post('/session', json=payload, headers={'X-Bot-Admin': '1'})
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn('secret-token', response.text)
                    self.assertNotIn('secret-password', response.text)
                with patch('app.main.threading.Thread') as thread:
                    response = client.post('/session', json={'sessionid': '  secret-token  '}, headers={
                        'X-Bot-Admin': '1', 'Host': '192.168.1.10:8001', 'Origin': 'http://192.168.1.10:8001'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(thread.call_args.kwargs['args'], ('secret-token',))
                    thread.return_value.start.assert_called_once()
                    self.assertNotIn('secret-token', response.text)
                    self.assertEqual(client.post('/session', json={'sessionid': 'secret'},
                        headers={'X-Bot-Admin': '1'}).status_code, 409)
                    from app.main import bot
                    bot.lock.release()


if __name__ == '__main__':
    unittest.main()
