import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import httpx

from app.images import GeneratedImage
from app.main import Bot, model_retry_seconds
import test_images as image_tests


class CooldownTests(unittest.TestCase):
    setUp = image_tests.ImageBotTests.setUp

    def response(self, status=429, **kwargs):
        return httpx.Response(status, request=httpx.Request('POST', 'https://model.example/v1/chat/completions'),
                              **kwargs)

    def test_structured_cooldown_and_retry_after(self):
        response = self.response(json={'error': {'code': 'model_cooldown', 'reset_seconds': 17087,
                                               'message': 'Quota resets after 4h56m8s'}},
                                 headers={'Retry-After': '20'})
        self.assertEqual(model_retry_seconds(response, 100), 17087)
        self.assertEqual(model_retry_seconds(self.response(json={'error': {'reset_seconds': 10}},
                                                         headers={'Retry-After': '120'}), 100), 120)
        self.assertEqual(model_retry_seconds(self.response(text='limited', headers={
            'Retry-After': 'Thu, 01 Jan 1970 00:03:20 GMT'}), 100), 100)

    def test_missing_or_invalid_delay_uses_short_fallback(self):
        for body in [None, [], {'error': 'limited'}, {'error': {'reset_seconds': -1}},
                     {'error': {'reset_seconds': 'nan'}}, {'error': {'reset_seconds': True}}]:
            with self.subTest(body=body):
                response = self.response(json=body, headers={'Retry-After': 'invalid'})
                self.assertEqual(model_retry_seconds(response, 100), 60)

    def test_image_429_is_persisted_and_skips_requests_until_expiry(self):
        with patch('app.main.time.time', return_value=100), patch('app.main.httpx.Client') as client:
            post = client.return_value.__enter__.return_value.post
            post.return_value = self.response(json={'error': {'reset_seconds': 17087}})
            notice = self.bot.generate_image('cat')
            self.assertIn('4 小時 45 分鐘', notice)
            self.assertIn('這次沒有生成圖片', notice)
            self.assertEqual(Bot().generate_image('cat'), notice)
            post.assert_called_once()
            with patch.dict(os.environ, {'IMAGE_MODEL': 'other-image'}):
                self.assertIsNone(self.bot.image_cooldown_notice())
            with patch.dict(os.environ, {'BASE_URL': 'https://other.example/v1'}):
                self.assertIsNone(self.bot.image_cooldown_notice())
        with patch('app.main.time.time', return_value=17188), patch('app.main.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value = self.response(
                200, json={'choices': [{'message': {'content': image_tests.inline_image()}}]})
            self.assertIsInstance(self.bot.generate_image('cat'), GeneratedImage)

    def test_other_http_failures_still_raise_without_cooldown(self):
        with patch('app.main.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value = self.response(500, text='error')
            with self.assertRaises(httpx.HTTPStatusError):
                self.bot.generate_image('cat')
        self.assertIsNone(self.bot.image_cooldown_notice())

    def test_cooldown_does_not_block_chat_or_download_more_avatars(self):
        self.bot.store.set_model_cooldown('https://model.example/v1', 'image-model', 1000)
        with patch('app.main.time.time', return_value=100), patch('app.main.httpx.Client') as client, \
                patch.object(self.bot, 'download_profile_picture') as download:
            post = client.return_value.__enter__.return_value.post
            post.return_value.json.return_value = {'choices': [{'message': {'content': '你好'}}]}
            self.assertEqual(self.bot.reply([{'role': 'user', 'content': '你好'}]), '你好')
            post.return_value.json.return_value = {'choices': [{'message': {'tool_calls': [{
                'function': {'name': 'generate_image', 'arguments': '{"prompt":"cat","reference_usernames":["alice"]}'},
            }]}}]}
            self.assertIn('稍後重新提出要求', self.bot.reply([{'role': 'user', 'content': '畫 @alice'}]))
            download.assert_not_called()
            self.assertEqual(post.call_count, 2)

    def test_tick_sends_one_notice_and_continues_to_next_message(self):
        thread = self.bot.client.direct_threads.return_value[0]
        thread.messages.append(NS(id='chat', user_id='20', text='@bot 你好', reply=None,
                                  timestamp=datetime.fromtimestamp(103, timezone.utc)))
        self.bot.client.direct_send.return_value = self.sent
        chat_image = Mock()
        chat_image.json.return_value = {'choices': [{'message': {'tool_calls': [{
            'function': {'name': 'generate_image', 'arguments': '{"prompt":"cat"}'},
        }]}}]}
        chat_text = Mock()
        chat_text.json.return_value = {'choices': [{'message': {'content': '你好'}}]}
        with patch('app.main.httpx.Client') as client:
            post = client.return_value.__enter__.return_value.post
            post.side_effect = [chat_image, self.response(json={'error': {'reset_seconds': 17087}}), chat_text]
            self.bot.tick()
            self.bot.tick()
        self.assertEqual(post.call_count, 3)
        replies = [call.args[0] for call in self.bot.client.direct_send.call_args_list]
        self.assertEqual(len(replies), 2)
        self.assertIn('額度不足', replies[0])
        self.assertEqual(replies[1], '你好')
        self.assertTrue(self.bot.store.done('10', '100', 'request'))
        self.bot.client.direct_send_photo.assert_not_called()
