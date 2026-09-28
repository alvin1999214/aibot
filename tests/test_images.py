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

from app.images import GeneratedImage, decode_image, normalize_reference_image
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

    def test_reference_image_is_normalized_to_jpeg(self):
        raw = base64.b64decode(inline_image().partition(',')[2])
        with Image.open(BytesIO(normalize_reference_image(raw))) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.mode, 'RGB')
            self.assertEqual(image.size, (32, 24))


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
        with patch.object(self.bot, 'reply', return_value=GeneratedImage('cat', decode_image({'content': inline_image()}))) as reply:
            with self.assertRaises(TimeoutError):
                self.bot.tick()
            self.bot.tick()
            reply.assert_called_once()
        self.assertFalse(paths[0].exists())
        self.bot.client.direct_send_photo.assert_called_once()

    def test_generation_failure_retries_before_claim(self):
        self.bot.client.direct_send_photo.return_value = self.sent
        with patch.object(self.bot, 'reply', side_effect=[ValueError('no image'), GeneratedImage('cat', decode_image({'content': inline_image()}))]):
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

    def test_group_profile_picture_is_sent_as_image_reference(self):
        chat = Mock()
        chat.json.return_value = {'choices': [{'message': {'tool_calls': [{
            'function': {
                'name': 'generate_image',
                'arguments': '{"prompt":"變成一隻狐狸","reference_username":"alice"}',
            },
        }]}}]}
        generated = Mock()
        generated.json.return_value = {'choices': [{'message': {
            'images': [{'image_url': {'url': inline_image()}}],
        }}]}
        reference = b'normalized-profile-jpeg'
        participants = [{
            'id': '20', 'username': 'alice', 'full_name': 'Alice',
            'profile_pic_url': 'https://scontent.cdninstagram.com/alice.jpg',
        }]
        with patch('app.main.httpx.Client') as client, \
                patch.object(self.bot, 'download_profile_picture', return_value=reference) as download:
            client.return_value.__enter__.return_value.post.side_effect = [chat, generated]
            result = self.bot.reply(
                [{'role': 'user', 'content': '@bot 根據我的頭像生成一隻動物'}],
                participants=participants, sender_id='20')
        self.assertIsInstance(result, GeneratedImage)
        download.assert_called_once_with('https://scontent.cdninstagram.com/alice.jpg')
        image_payload = client.return_value.__enter__.return_value.post.call_args_list[1].kwargs['json']
        content = image_payload['messages'][0]['content']
        self.assertEqual(content[0]['type'], 'text')
        self.assertIn('@alice', content[1]['text'])
        self.assertEqual(content[2]['type'], 'image_url')
        self.assertEqual(content[2]['image_url']['url'],
                         'data:image/jpeg;base64,' + base64.b64encode(reference).decode('ascii'))

    def test_profile_reference_must_be_a_group_member(self):
        with patch('app.main.httpx.Client') as client, \
                patch.object(self.bot, 'download_profile_picture') as download:
            client.return_value.__enter__.return_value.post.return_value.json.return_value = {
                'choices': [{'message': {'tool_calls': [{
                    'function': {
                        'name': 'generate_image',
                        'arguments': '{"prompt":"新頭像","reference_username":"stranger"}',
                    },
                }]}}]}
            result = self.bot.reply(
                [{'role': 'user', 'content': '@bot 根據 @stranger 生成一個頭像'}],
                participants=[{'id': '20', 'username': 'alice', 'full_name': 'Alice',
                               'profile_pic_url': 'https://scontent.cdninstagram.com/alice.jpg'}],
                sender_id='20')
        self.assertIn('找不到群組成員 @stranger', result)
        download.assert_not_called()

    def test_profile_picture_url_rejects_non_instagram_hosts(self):
        with self.assertRaises(ValueError):
            self.bot.download_profile_picture('https://example.com/avatar.jpg')


class ImageReferenceFlowTests(unittest.TestCase):
    setUp = ImageBotTests.setUp
    def tool_response(self, arguments):
        import json
        result = Mock()
        result.json.return_value = {'choices': [{'message': {'tool_calls': [{
            'function': {'name': 'generate_image', 'arguments': json.dumps(arguments)},
        }]}}]}
        return result

    def test_multiple_mentions_recover_omitted_person_and_do_not_match_prefix(self):
        participants = [{'id': str(i), 'username': name, 'profile_pic_url': name + '.jpg'}
                        for i, name in enumerate(['alice', 'bob', 'bobby', 'bot'])]
        with patch('app.main.httpx.Client') as client, \
                patch.object(self.bot, 'download_profile_picture', return_value=b'jpeg') as download, \
                patch.object(self.bot, 'generate_image') as generate:
            client.return_value.__enter__.return_value.post.return_value = self.tool_response(
                {'prompt': '兩人合照', 'reference_username': 'ALICE'})
            self.bot.reply([{'role': 'user', 'content': '@bot 畫 @Alice 和 @bob 合照'}],
                           participants=participants)
        self.assertEqual([c.args[0] for c in download.call_args_list], ['alice.jpg', 'bob.jpg'])
        references = generate.call_args.kwargs['references']
        self.assertEqual(len(references), 2)
        self.assertIn('@alice', references[0][0])
        self.assertIn('@bob', references[1][0])

    def test_missing_one_person_does_not_generate_partial_group(self):
        with patch('app.main.httpx.Client') as client, patch.object(self.bot, 'generate_image') as generate:
            client.return_value.__enter__.return_value.post.return_value = self.tool_response(
                {'prompt': '合照', 'reference_usernames': ['alice', 'bob']})
            result = self.bot.reply([{'role': 'user', 'content': '@bot 畫合照'}])
        self.assertIn('找不到', result)
        generate.assert_not_called()

    def test_photo_goes_to_both_chat_and_image_models_without_mutating_context(self):
        messages = [{'role': 'user', 'content': '@bot 把照片背景改成今天的晴天'}]
        refs = [('來源照片', b'photo')]
        with patch.dict(os.environ, {'WEB_SEARCH': 'true'}), patch('app.main.httpx.Client') as client, \
                patch.object(self.bot, 'generate_image') as generate, \
                patch.object(self.bot, 'search_web') as search:
            client.return_value.__enter__.return_value.post.return_value = self.tool_response(
                {'prompt': '改成晴天'})
            self.bot.reply(messages, source_images=refs)
        search.assert_not_called()
        content = client.return_value.__enter__.return_value.post.call_args.kwargs['json']['messages'][-1]['content']
        self.assertEqual(content[-1]['image_url']['url'], 'data:image/jpeg;base64,cGhvdG8=')
        self.assertEqual(generate.call_args.kwargs['references'], refs)
        self.assertIsInstance(messages[0]['content'], str)

    def photo(self, mid='upload', uid='20', ts=101):
        return NS(id=mid, user_id=uid, text=None, reply=None,
                  timestamp=datetime.fromtimestamp(ts, timezone.utc),
                  media=NS(media_type=1, thumbnail_url='https://scontent.cdninstagram.com/photo.jpg'))

    def test_upload_then_edit_uses_photo_and_keeps_binary_out_of_logs(self):
        thread = self.bot.client.direct_threads.return_value[0]
        thread.messages.insert(0, self.photo())
        thread.messages[-1].text = '@bot 把這張照片改成水彩'
        self.bot.client.direct_send.return_value = self.sent
        with patch.object(self.bot, 'download_profile_picture', return_value=b'photo') as download, \
                patch.object(self.bot, 'reply', return_value='完成') as reply, \
                self.assertLogs('bot', 'INFO') as logs:
            self.bot.tick()
        download.assert_called_once()
        self.assertEqual(reply.call_args.kwargs['source_images'][0][1], b'photo')
        self.assertNotIn('base64', '\n'.join(logs.output))

    def test_explicit_reply_photo_overrides_recent_photo(self):
        thread = self.bot.client.direct_threads.return_value[0]
        request = thread.messages[0]
        request.reply = self.photo('older', uid='30', ts=99)
        thread.messages.insert(0, self.photo())
        request.text = '@bot 修改背景'
        self.bot.client.direct_send.return_value = self.sent
        with patch.object(self.bot, 'download_profile_picture', return_value=b'photo'), \
                patch.object(self.bot, 'reply', return_value='完成') as reply:
            self.bot.tick()
        self.assertIn('older', reply.call_args.kwargs['source_images'][0][0])

    def test_reference_selection_is_scoped_and_never_uses_future_or_other_sender(self):
        store = self.bot.store
        for account, thread, mid, ts, sender in [
                ('other', '100', 'other-account', 101, '20'),
                ('10', 'other', 'other-thread', 101, '20'),
                ('10', '100', 'other-user', 101, '30'),
                ('10', '100', 'future', 103, '20')]:
            store.add_image(account, thread, mid, ts, sender, jpeg=b'photo')
        request = self.bot.client.direct_threads.return_value[0].messages[0]
        request.text = '@bot 修改這張圖片'
        self.assertIsNone(self.bot.source_image('10', '100', request))

    def test_generated_image_survives_restart_and_is_used_for_reply(self):
        jpeg = decode_image({'content': inline_image()})
        self.bot.client.direct_send_photo.return_value = self.sent
        with patch.object(self.bot, 'reply', return_value=GeneratedImage('貓', jpeg)):
            self.bot.tick()
        restarted = Bot()
        request = NS(id='edit', user_id='20', text='改成藍色',
                     timestamp=datetime.fromtimestamp(106, timezone.utc), reply=self.sent)
        source = restarted.source_image('10', '100', request)
        self.assertEqual(source['id'], 'photo')
        self.assertTrue(source['jpeg'])
        restarted.store.prune('10', '100', 0)
        self.assertIsNone(restarted.source_image('10', '100', request))

    def test_failed_source_download_does_not_generate_from_text_only(self):
        request = self.bot.client.direct_threads.return_value[0].messages[0]
        request.media = self.photo().media
        self.bot.client.direct_send.return_value = self.sent
        with patch.object(self.bot, 'download_profile_picture', side_effect=ValueError()), \
                patch.object(self.bot, 'reply') as reply:
            self.bot.tick()
        reply.assert_not_called()
        self.assertIn('重新上傳', self.bot.client.direct_send.call_args.args[0])

    def test_image_api_receives_every_labeled_reference(self):
        with patch('app.main.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value.json.return_value = {
                'choices': [{'message': {'content': inline_image()}}]}
            self.bot.generate_image('合照', references=[('@alice', b'alice'), ('@bob', b'bob')])
        content = client.return_value.__enter__.return_value.post.call_args.kwargs['json']['messages'][0]['content']
        self.assertEqual([part['text'] for part in content[1:] if part['type'] == 'text'],
                         ['@alice', '@bob'])
        self.assertEqual([part['image_url']['url'] for part in content if part['type'] == 'image_url'],
                         ['data:image/jpeg;base64,YWxpY2U=', 'data:image/jpeg;base64,Ym9i'])

    def test_malformed_reference_arrays_are_rejected(self):
        for names in ['alice', [1], [' '], None]:
            with self.subTest(names=names), patch('app.main.httpx.Client') as client, \
                    patch.object(self.bot, 'generate_image') as generate:
                client.return_value.__enter__.return_value.post.return_value = self.tool_response(
                    {'prompt': '合照', 'reference_usernames': names})
                with self.assertRaises(ValueError):
                    self.bot.reply([{'role': 'user', 'content': '畫合照'}])
                generate.assert_not_called()

    def test_current_attachment_takes_priority_and_video_is_not_a_photo(self):
        from app.main import message_image_url
        photo = self.photo()
        self.assertIsNotNone(message_image_url(photo))
        photo.media.media_type = 2
        self.assertIsNone(message_image_url(photo))
        request = self.bot.client.direct_threads.return_value[0].messages[0]
        request.reply = self.photo('older')
        self.bot.store.add_image('10', '100', 'older', 101, '20', jpeg=b'older')
        self.bot.store.add_image('10', '100', 'request', 102, '20', jpeg=b'current')
        self.assertEqual(self.bot.source_image('10', '100', request)['jpeg'], b'current')
