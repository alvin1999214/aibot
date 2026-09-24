import base64
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from PIL import Image

from app.images import GeneratedImage, decode_image
from app.main import Bot


def inline_image():
    output = BytesIO()
    Image.new('RGBA', (32, 24), (255, 0, 0, 128)).save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()


class ImageTests(unittest.TestCase):
    def test_supported_inline_formats_become_jpeg(self):
        url = inline_image()
        for message in [
            {'images': [{'image_url': {'url': url}}]},
            {'content': [{'type': 'image_url', 'image_url': {'url': url}}]},
            {'content': f'![generated]({url})'},
        ]:
            with self.subTest(message_format=list(message)):
                with Image.open(BytesIO(decode_image(message))) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.mode, 'RGB')
                    self.assertEqual(image.size, (32, 24))

    def test_missing_external_or_invalid_image_is_rejected(self):
        for message in [
            {'content': 'Unable to generate image'},
            {'images': [{'image_url': {'url': 'https://example.com/image.png'}}]},
            {'images': [{'image_url': {'url': 'data:image/png;base64,invalid!'}}]},
            {'images': [{'image_url': {'url': 'data:image/png;base64,aGVsbG8='}}]},
        ]:
            with self.subTest(message=message), self.assertRaises((ValueError, OSError)):
                decode_image(message)

    def test_image_size_limits(self):
        with patch('app.images.MAX_IMAGE_BYTES', 1), self.assertRaises(ValueError):
            decode_image({'images': [{'image_url': {'url': inline_image()}}]})


class ImageBotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            'DATA_DIR': self.directory.name, 'BASE_URL': 'https://model.example/v1/',
            'API_KEY': 'secret', 'MODEL': 'chat-model', 'IMAGE_MODEL': 'image-model',
            'IMAGE_ASPECT_RATIO': '4:3', 'IMAGE_SIZE': '2K',
        })
        env.start()
        self.addCleanup(env.stop)
        self.bot = Bot()
        self.bot.username = 'bot'
        self.bot.client = Mock(user_id=10)
        self.bot.store.since('10', 100)
        message = NS(id='request', user_id='20', text='@bot 畫一隻貓', reply=None,
                     timestamp=datetime.fromtimestamp(102, timezone.utc))
        self.bot.client.direct_threads.return_value = [NS(id='100', is_group=True, messages=[message])]
        self.sent = NS(id='photo', timestamp=datetime.fromtimestamp(105, timezone.utc))

    def test_tool_generation_sends_photo_once_logs_and_cleans_up(self):
        chat = Mock()
        chat.json.return_value = {'choices': [{'message': {'content': None, 'tool_calls': [
            {'function': {'name': 'generate_image', 'arguments': '{"prompt":"一隻可愛的貓"}'}}
        ]}}]}
        generated = Mock()
        url = inline_image()
        generated.json.return_value = {'choices': [{'message': {'images': [{'image_url': {'url': url}}]}}]}
        paths = []

        def send(path, thread_ids):
            paths.append(path)
            self.assertEqual(thread_ids, [100])
            with Image.open(path) as image:
                self.assertEqual(image.format, 'JPEG')
            return self.sent

        self.bot.client.direct_send_photo.side_effect = send
        with patch('app.main.httpx.Client') as client, self.assertLogs('bot', level='INFO') as logs:
            post = client.return_value.__enter__.return_value.post
            post.side_effect = [chat, generated]
            self.bot.tick()
            self.bot.tick()
            self.assertEqual(post.call_count, 2)
            request, image_request = post.call_args_list
            self.assertEqual(request.kwargs['json']['tools'][0]['function']['name'], 'generate_image')
            self.assertEqual(image_request.args[0], 'https://model.example/v1/chat/completions')
            self.assertEqual(image_request.kwargs['headers']['Authorization'], 'Bearer secret')
            payload = image_request.kwargs['json']
            self.assertEqual(payload['model'], 'image-model')
            self.assertEqual(payload['messages'], [{'role': 'user', 'content': '一隻可愛的貓'}])
            self.assertEqual(payload['image_config'], {'aspect_ratio': '4:3', 'image_size': '2K'})
            self.assertEqual(payload['modalities'], ['image', 'text'])
        self.bot.client.direct_send_photo.assert_called_once()
        self.bot.client.direct_send.assert_not_called()
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].parent.exists())
        self.assertIn('已生成圖片', self.bot.store.context('10', '100', 200, 40, 16000)[-1]['content'])
        self.assertIn('"media_type": "image"', '\n'.join(logs.output))
        self.assertNotIn(url, '\n'.join(logs.output))

    def test_uncertain_photo_send_is_not_retried_and_file_is_removed(self):
        paths = []

        def fail(path, thread_ids):
            paths.append(path)
            raise TimeoutError()

        self.bot.client.direct_send_photo.side_effect = fail
        with patch.object(self.bot, 'reply', return_value=GeneratedImage('cat', b'jpeg')) as reply:
            with self.assertRaises(TimeoutError):
                self.bot.tick()
            self.bot.tick()
            reply.assert_called_once()
        self.assertFalse(paths[0].exists())
        self.bot.client.direct_send_photo.assert_called_once()

    def test_generation_failure_retries_before_claim(self):
        self.bot.client.direct_send_photo.return_value = self.sent
        with patch.object(self.bot, 'reply', side_effect=[ValueError('no image'), GeneratedImage('cat', b'jpeg')]):
            with self.assertRaises(ValueError):
                self.bot.tick()
            self.assertFalse(self.bot.store.done('10', '100', 'request'))
            self.bot.tick()
        self.bot.client.direct_send_photo.assert_called_once()

    def test_text_chat_with_and_without_image_model(self):
        for image_model in ['', 'image-model']:
            with self.subTest(image_model=image_model), patch.dict(os.environ, {'IMAGE_MODEL': image_model}), \
                    patch('app.main.httpx.Client') as client:
                post = client.return_value.__enter__.return_value.post
                post.return_value.json.return_value = {'choices': [{'message': {'content': '你好'}}]}
                self.assertEqual(self.bot.reply([{'role': 'user', 'content': 'hi'}]), '你好')
                self.assertEqual('tools' in post.call_args.kwargs['json'], bool(image_model))
                post.assert_called_once()

    def test_invalid_tool_call_does_not_generate(self):
        for function in [
            {'name': 'unknown', 'arguments': '{"prompt":"cat"}'},
            {'name': 'generate_image', 'arguments': '{"prompt":"   "}'},
            {'name': 'generate_image', 'arguments': '{"prompt":12}'},
            {'name': 'generate_image', 'arguments': 'invalid JSON'},
        ]:
            with self.subTest(function=function), patch('app.main.httpx.Client') as client, \
                    patch.object(self.bot, 'generate_image') as generate:
                client.return_value.__enter__.return_value.post.return_value.json.return_value = {
                    'choices': [{'message': {'tool_calls': [{'function': function}]}}]}
                with self.assertRaises(ValueError):
                    self.bot.reply([{'role': 'user', 'content': 'draw a cat'}])
                generate.assert_not_called()
