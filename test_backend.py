import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent / '.deps'))
os.environ['NO_PROXY'] = '127.0.0.1,localhost'
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import backend
import bot
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = backend.CONFIG, backend.PROMPT, backend.DB
        root = Path(self.temp.name)
        backend.CONFIG, backend.PROMPT, backend.DB = root/'config.json', root/'systemprompt', root/'db.sqlite3'
        self.received = []
        async def mock(request):
            self.received.append(await request.json())
            return web.json_response({'id':'test', 'object':'chat.completion','created':1,'model':'test','choices':[{'index':0,'message':{'role':'assistant','content':'收到'},'finish_reason':'stop'}]})
        app = web.Application()
        app.router.add_post('/chat/completions', mock)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.cfg = {'base_url': f'http://127.0.0.1:{port}', 'model':'test','api_key':'test','vision':False}
        self.write_config()

    def write_config(self): backend.CONFIG.write_text(json.dumps(self.cfg), encoding='utf-8')

    async def test_group_names_mentions_and_time_reach_model(self):
        bot.rate_limiter = backend.RateLimiter()
        sent = []
        state = bot.ConnectionState(lambda *args: sent.append(args), None)
        payload = {'id': 'event', 'd': {'id': 'message', 'group_openid': 'G', 'group_name': '猫娘',
            'author': {'member_openid': 'A', 'username': '千羽'},
            'mentions': [{'id': 'B', 'username': 'oi'}, {'id': 'robot', 'is_you': True}],
            'content': '你问 <@B>'}}
        state.parsers['group_at_message_create'](payload)
        message = sent[0][1]
        self.assertEqual(message.metadata['author_name'], '千羽')
        message_api = SimpleNamespace(post_group_message=AsyncMock())
        message._api = message_api
        fake = SimpleNamespace(robot=SimpleNamespace(id='robot'))
        with patch.object(backend, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 28, 18, 19, 0, tzinfo=timezone(timedelta(hours=8)))
            await bot.Catgirl.handle(fake, message, '群聊')
        messages = self.received[-1]['messages']
        self.assertIn('2026-10-28 18:19:00', messages[0]['content'])
        self.assertIn('星期三', messages[0]['content'])
        self.assertIn('UTC+08:00', messages[0]['content'])
        current = messages[-1]['content']
        for text in ('猫娘', '千羽', 'oi', '群成员 A', '明确 @', '"id": "B"'):
            self.assertIn(text, current)
        with backend.db() as conn:
            ids = [m['id'] for m in backend.group_info.profile(conn, 'G')['members']]
            self.assertEqual(set(ids), {'A', 'B'})
        with patch.object(backend, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 29, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
            await backend.answer('私聊:A', '你好', [])
        self.assertIn('2026-10-29 01:00:00', self.received[-1]['messages'][0]['content'])
        self.assertNotIn('oi', json.dumps(self.received[-1], ensure_ascii=False))
        self.assertNotIn('当前群资料', json.dumps(self.received[-1], ensure_ascii=False))
        with backend.db() as conn:
            saved = str(conn.execute('SELECT content FROM messages').fetchall())
            self.assertNotIn('当前服务器时间', saved)
            self.assertNotIn('当前群资料', saved)

    async def test_group_profile_alias_validation_and_isolation(self):
        backend.remember_group({'group_id': 'G', 'group_name': '原群名', 'author_id': 'A', 'author_name': '平台昵称'})
        backend.remember_group({'group_id': 'H', 'group_name': '另一群', 'author_id': 'A', 'author_name': '别群昵称'})
        app = web.Application(middlewares=[backend.local_only])
        app.add_routes([web.get('/api/group-profile', backend.get_group_profile), web.post('/api/group-profile', backend.update_group_profile), web.get('/api/groups', backend.get_groups)])
        async with TestClient(TestServer(app), headers={'Host': '127.0.0.1:8766'}) as client:
            data = {'group_id': 'G', 'alias': '猫娘', 'members': [{'id': 'A', 'alias': 'oi'}]}
            self.assertEqual((await client.post('/api/group-profile', json=data)).status, 200)
            # Incoming platform data must not overwrite an administrator alias.
            backend.remember_group({'group_id': 'G', 'group_name': '新群名', 'author_id': 'A', 'author_name': '新昵称'})
            backend.remember_group({'group_id': 'G', 'author_id': 'A'})
            profile = await (await client.get('/api/group-profile?group_id=G')).json()
            self.assertEqual(profile['name'], '猫娘')
            self.assertEqual(profile['platform_name'], '新群名')
            self.assertEqual(profile['members'][0]['name'], 'oi')
            self.assertEqual(profile['members'][0]['platform_name'], '新昵称')
            for bad in ([], {'group_id': '../G'}, {'group_id': 'G', 'alias': 'x'*121}, {'group_id': 'G', 'members': [{'id': 'A'}, {'id': 'A'}]}):
                self.assertEqual((await client.post('/api/group-profile', json=bad)).status, 400)
            self.assertEqual((await client.post('/api/group-profile', json=data, headers={'Origin': 'https://example.com'})).status, 403)
            await backend.answer('群聊:G', '你好', [])
            request = json.dumps(self.received[-1], ensure_ascii=False)
            self.assertIn('oi', request)
            self.assertNotIn('别群昵称', request)
            data['alias'], data['members'][0]['alias'] = '', ''
            await client.post('/api/group-profile', json=data)
            profile = await (await client.get('/api/group-profile?group_id=G')).json()
            self.assertEqual(profile['name'], '新群名')
            self.assertEqual(profile['members'][0]['name'], '新昵称')

    async def test_full_group_metadata_protocol_mentions_and_history_seed(self):
        bot.seen_messages.clear()
        fake = SimpleNamespace(robot=SimpleNamespace(id='robot'), api=None, handle=AsyncMock())
        raw = {'id': 'event', 'd': {'id': 'm1', 'group_openid': 'G', 'author': {'member_openid': 'B', 'username': 'oi'},
               'content': '你好 <qqbot-at-user id="A" />'}}
        await bot.Catgirl.on_group_message_create(fake, raw)
        fake.handle.assert_not_awaited()
        with backend.db() as conn:
            profile = backend.group_info.profile(conn, 'G')
            self.assertEqual({m['id'] for m in profile['members']}, {'A', 'B'})
        self.assertIn('oi', backend.history('群聊:G')[0][1])
        self.assertIn('明确 @', backend.history('群聊:G')[0][1])
        self.assertTrue(bot.is_bot_mentioned({'d': {'content': '<qqbot-at-user id="robot" />'}}, fake.robot))
        self.assertFalse(bot.is_bot_mentioned(raw, fake.robot))
        backend.save_turn('群聊:OLD', '[群成员 OLDUSER] 旧消息', '旧回复')
        backend.initialize_memory()
        with backend.db() as conn:
            self.assertEqual(backend.group_info.profile(conn, 'OLD')['members'][0]['id'], 'OLDUSER')

    async def test_group_image_failure_does_not_send_only_metadata(self):
        self.cfg['vision'] = True
        self.write_config()
        result = await backend.answer('群聊:G', '', [SimpleNamespace(content_type='image/png', url='https://example.com/expired.png')], speaker='A')
        self.assertIn('无法读取', result)
        self.assertEqual(self.received, [])
        self.cfg['vision'] = False
        self.write_config()
        result = await backend.answer('群聊:G', '', [SimpleNamespace(content_type='image/png', url='https://example.com/expired.png')], speaker='A')
        self.assertEqual(self.received, [])
        self.assertEqual(backend.history('群聊:G'), [])

    async def test_qq_config_migration_and_api(self):
        root = Path(self.temp.name)
        target, legacy = root / 'qqbot_config.json', root / 'apikey.txt'
        with patch.object(backend, 'QQ_CONFIG', target), patch.object(backend, 'LEGACY_QQ_CONFIG', legacy):
            self.assertEqual(backend.qq_settings(), {'appid': '', 'secret': ''})
            legacy.write_text(json.dumps({'appid': '123456', 'secret': 'old-test-secret'}), encoding='utf-8')
            self.assertEqual(backend.qq_settings()['secret'], 'old-test-secret')
            self.assertTrue(target.exists())
            self.assertFalse(legacy.exists())
            app = web.Application(middlewares=[backend.local_only])
            app.add_routes([web.get('/api/qqbot-config', backend.get_qq_settings), web.post('/api/qqbot-config', backend.update_qq_settings)])
            async with TestClient(TestServer(app), headers={'Host': '127.0.0.1:8766'}) as client:
                response = await client.get('/api/qqbot-config')
                self.assertEqual(await response.json(), {'appid': '123456', 'has_secret': True})
                response = await client.post('/api/qqbot-config', json={'appid': '123456', 'secret': ''})
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(target.read_text())['secret'], 'old-test-secret')
                before = target.read_bytes()
                for invalid in ({'appid': '654321', 'secret': ''}, {'appid': 'invalid', 'secret': 'test'}, {'appid': '123456', 'secret': 123}, []):
                    response = await client.post('/api/qqbot-config', json=invalid)
                    self.assertEqual(response.status, 400)
                    self.assertEqual(target.read_bytes(), before)
                response = await client.post('/api/qqbot-config', json={'appid': '654321', 'secret': 'new-test-secret'})
                self.assertEqual(await response.json(), {'ok': True, 'restart_required': True})
                self.assertEqual(json.loads(target.read_text()), {'appid': '654321', 'secret': 'new-test-secret'})
                response = await client.get('/api/qqbot-config')
                self.assertNotIn('new-test-secret', await response.text())
                response = await client.post('/api/qqbot-config', json={'appid': '123', 'secret': 'test'}, headers={'Origin': 'https://example.com'})
                self.assertEqual(response.status, 403)
            legacy.write_text(json.dumps({'appid': '123456', 'secret': 'stale-test-secret'}), encoding='utf-8')
            self.assertEqual(backend.qq_settings()['appid'], '654321')

    async def asyncTearDown(self):
        await self.runner.cleanup()
        backend.CONFIG, backend.PROMPT, backend.DB = self.old
        self.temp.cleanup()

    async def test_images_and_context(self):
        image = SimpleNamespace(content_type='image/png', url='https://example.com/image.png')
        await backend.answer('one', '你好', [image])
        self.assertNotIn('image_url', json.dumps(self.received[-1]))
        self.assertEqual(len(backend.history('one')), 2)
        await backend.answer('one', '', [image])
        self.assertEqual(len(backend.history('one')), 2)
        self.cfg['vision'] = True
        self.write_config()
        async def fake_prepare(content, session): return content
        with patch.object(backend, 'prepare_images', side_effect=fake_prepare):
            await backend.answer('one', '看看', [image])
        self.assertIn('image_url', json.dumps(self.received[-1]))
        self.cfg['vision'] = False
        self.write_config()
        await backend.answer('one', '继续', [])
        self.assertNotIn('image_url', json.dumps(self.received[-1]))
        await backend.answer('two', '另一个用户', [])
        self.assertNotIn('你好', json.dumps(self.received[-1], ensure_ascii=False))

    async def test_expired_image_skipped_from_history(self):
        self.cfg['vision'] = True
        self.write_config()
        content = [{'type':'text', 'text':'之前的问题'}, {'type':'image_url', 'image_url':{'url':'https://example.com/expired.png'}}]
        backend.save_turn('one', content, '之前的回答')
        await backend.answer('one', '继续', [])
        sent = json.dumps(self.received[-1], ensure_ascii=False)
        self.assertIn('之前的问题', sent)
        self.assertNotIn('image_url', sent)
        result = await backend.answer('one', '', [SimpleNamespace(content_type='image/png', url='https://example.com/expired.png')])
        self.assertIn('无法读取', result)

    async def test_limit_isolation_and_migration(self):
        for i in range(18):
            backend.save_turn('私聊:A', f'A{i}', f'答{i}')
        backend.save_turn('私聊:B', 'B的消息', 'B的回复')
        backend.save_turn('群聊:G:A', '群消息', '群回复')
        with backend.db() as conn:
            rows = conn.execute('SELECT role, content FROM messages WHERE chat=? ORDER BY id', ('私聊:A',)).fetchall()
            self.assertEqual(len(rows), 20)
            self.assertEqual(json.loads(rows[0][1]), 'A8')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('私聊:B',)).fetchone()[0], 2)

            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('群聊:G:A',)).fetchone()[0], 2)
            for i in range(35):
                conn.execute('INSERT INTO messages(chat,role,content) VALUES(?,?,?)', ('旧会话', 'user', json.dumps(str(i))))
        backend.initialize_memory()
        with backend.db() as conn:
            rows = conn.execute('SELECT content FROM messages WHERE chat=? ORDER BY id', ('旧会话',)).fetchall()
            self.assertEqual(len(rows), 20)
            self.assertEqual(json.loads(rows[0][0]), '15')
        await backend.answer('私聊:A', '当前消息', [])
        sent = self.received[-1]['messages']
        self.assertEqual(len(sent), 21)  # 系统提示词 + 最多 20 条聊天消息
        self.assertEqual(sent[-1]['content'], '当前消息')
        with backend.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('私聊:A',)).fetchone()[0], 20)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('私聊:B',)).fetchone()[0], 2)

    async def test_rate_limit_arrival_and_isolation(self):
        limiter = backend.RateLimiter()
        for i in range(10): self.assertTrue(limiter.allow('A', 10, now=float(i)))
        self.assertFalse(limiter.allow('A', 10, now=10))
        self.assertTrue(limiter.allow('B', 10, now=10))
        self.assertTrue(limiter.allow('A', 10, now=60))
        self.assertFalse(limiter.allow('A', 1, now=60))
        self.assertTrue(limiter.allow('A', 20, now=60))

        bot.rate_limiter = backend.RateLimiter()
        self.cfg['requests_per_minute'] = 1
        self.write_config()
        msg = SimpleNamespace(author=SimpleNamespace(user_openid='A'), content='你好', attachments=[], reply=AsyncMock())
        with patch.object(bot, 'answer', new=AsyncMock(return_value='正常')) as mocked:
            await bot.Catgirl.handle(None, msg, '私聊')
            await bot.Catgirl.handle(None, msg, '私聊')
        self.assertEqual(mocked.await_count, 1)
        self.assertEqual(msg.reply.await_args_list[1].kwargs['content'], '樱洛繁忙中')
        self.assertEqual(backend.history('私聊:A'), [])

    async def test_admin_limit_settings(self):
        app = web.Application()
        app.add_routes([web.get('/api/settings', backend.get_settings), web.post('/api/settings', backend.update_settings)])
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/api/settings')
            initial = await response.json()
            self.assertEqual(initial['requests_per_minute'], 10)
            self.assertEqual((initial['private_context_limit'], initial['group_context_limit']), (20, 100))
            data = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'prompt': '测试', 'requests_per_minute': 3, 'private_context_limit': 4, 'group_context_limit': 6}
            for i in range(5):
                backend.save_turn('私聊:A', str(i), '回复')
                backend.save_group_message('群聊:G', 'A', str(i), [])
            response = await client.post('/api/settings', json=data)
            self.assertEqual(response.status, 200)
            self.assertEqual(backend.settings()['requests_per_minute'], 3)
            with backend.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('私聊:A',)).fetchone()[0], 4)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('群聊:G',)).fetchone()[0], 5)
            self.assertEqual(len(backend.history('群聊:G')), 5)
            backend.save_group_message('群聊:G', 'A', '新群消息', [])
            backend.save_group_message('群聊:G', 'A', '再一条', [])
            with backend.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('群聊:G',)).fetchone()[0], 6)
            self.assertEqual(len(backend.history('群聊:G')), 5)

            data['requests_per_minute'] = 0
            response = await client.post('/api/settings', json=data)
            self.assertEqual(response.status, 400)
            self.assertEqual(backend.settings()['requests_per_minute'], 3)
            data['requests_per_minute'] = 3
            data['group_context_limit'] = 1
            response = await client.post('/api/settings', json=data)
            self.assertEqual(response.status, 400)
            self.assertEqual(backend.settings()['group_context_limit'], 6)

    async def test_private_clean_command(self):
        bot.rate_limiter = backend.RateLimiter()
        backend.save_turn('私聊:A', '旧消息', '旧回复')
        backend.save_turn('私聊:B', '其他用户', '其他回复')
        backend.save_turn('群聊:G:A', '群消息', '群回复')
        msg = SimpleNamespace(author=SimpleNamespace(user_openid='A'), content=' /clean context ', attachments=[], reply=AsyncMock())
        with patch.object(bot, 'answer', new=AsyncMock()) as mocked:
            await bot.Catgirl.handle(None, msg, '私聊')
        mocked.assert_not_awaited()
        self.assertEqual(backend.history('私聊:A'), [])
        self.assertEqual(len(backend.history('私聊:B')), 2)
        self.assertEqual(len(backend.history('群聊:G:A')), 2)
        self.assertIn('已清除', msg.reply.await_args.kwargs['content'])

    async def test_auto_reply_push_and_fallback(self):
        bot.rate_limiter = backend.RateLimiter()
        api = SimpleNamespace(post_c2c_message=AsyncMock(), post_group_message=AsyncMock())
        client = SimpleNamespace(api=api)
        msg = SimpleNamespace(author=SimpleNamespace(user_openid='A'), content='/auto reply', attachments=[], reply=AsyncMock())
        with patch.object(bot, 'answer', new=AsyncMock()) as model:
            await bot.Catgirl.handle(client, msg, '私聊')
            model.assert_not_awaited()
        api.post_c2c_message.assert_awaited_once()
        self.assertNotIn('msg_id', api.post_c2c_message.await_args.kwargs)
        msg.reply.assert_not_awaited()
        api.post_c2c_message.side_effect = RuntimeError('push rejected')
        await bot.Catgirl.handle(client, msg, '私聊')
        msg.reply.assert_awaited_once()
        self.assertEqual(backend.history('私聊:A'), [])

    async def test_group_shared_context_and_full_event(self):
        bot.rate_limiter = backend.RateLimiter()
        bot.seen_messages.clear()
        a = SimpleNamespace(author=SimpleNamespace(member_openid='A'), group_openid='G', content='你好', attachments=[], reply=AsyncMock())
        b = SimpleNamespace(author=SimpleNamespace(member_openid='B'), group_openid='G', content='他说什么', attachments=[], reply=AsyncMock())
        await bot.Catgirl.handle(None, a, '群聊')
        await bot.Catgirl.handle(None, b, '群聊')
        sent = self.received[-1]['messages']
        self.assertTrue(any('群成员 A' in str(m.get('content')) for m in sent))
        self.assertIn('群成员 B', str(sent[-1]['content']))
        self.assertEqual(len(backend.history('群聊:G')), 4)
        self.assertEqual(backend.history('私聊:A'), [])
        dispatched = []
        state = bot.ConnectionState(lambda *args: dispatched.append(args), None)
        self.assertIn('group_message_create', state.parsers)
        raw = {'id':'evt', 'd':{'id':'msg', 'group_openid':'G', 'author':{'member_openid':'A'}, 'content':'你好', 'mentions':[{'is_you':True}]}}
        state.parsers['group_message_create'](raw)
        self.assertEqual(dispatched[0][0], 'group_message_create')
        self.assertTrue(bot.is_bot_mentioned(raw, SimpleNamespace(id=123)))
        self.assertFalse(bot.is_bot_mentioned({'d':{'mentions':[{'id':'other'}], 'content':'普通消息'}}, SimpleNamespace(id=123)))
        fake = SimpleNamespace(robot=SimpleNamespace(id=123), api=None, handle=AsyncMock())
        await bot.Catgirl.on_group_message_create(fake, raw)
        await bot.Catgirl.on_group_message_create(fake, raw)
        self.assertEqual(fake.handle.await_count, 1)
        await bot.Catgirl.on_group_message_create(fake, {'d':{'content':'普通消息'}})
        self.assertEqual(fake.handle.await_count, 1)

    async def test_full_group_messages_become_context_without_reply(self):
        bot.seen_messages.clear()
        fake = SimpleNamespace(robot=SimpleNamespace(id=123), api=None, handle=AsyncMock())
        def payload(mid, group, user, content, attachments=None):
            return {'id':'event', 'd':{'id':mid, 'group_openid':group, 'author':{'member_openid':user}, 'content':content, 'attachments':attachments or []}}
        one = payload('one', 'G', 'A', '我喜欢猫')
        await bot.Catgirl.on_group_message_create(fake, one)
        await bot.Catgirl.on_group_message_create(fake, one)
        await bot.Catgirl.on_group_message_create(fake, payload('two', 'G', 'B', '我也喜欢'))
        await bot.Catgirl.on_group_message_create(fake, payload('other', 'H', 'C', '另一群的消息'))
        image = {'content_type':'image/png', 'url':'https://example.com/test.png'}
        await bot.Catgirl.on_group_message_create(fake, payload('image', 'G', 'A', '', [image]))
        fake.handle.assert_not_awaited()
        self.assertEqual(len(backend.history('群聊:G')), 2)
        await backend.answer('群聊:G', '[群成员 B] 上面聊了什么', [])
        sent = json.dumps(self.received[-1]['messages'], ensure_ascii=False)
        self.assertIn('我喜欢猫', sent)
        self.assertIn('我也喜欢', sent)
        self.assertNotIn('另一群的消息', sent)
        self.assertNotIn('image_url', sent)
        for i in range(120):
            await bot.Catgirl.on_group_message_create(fake, payload(f'extra{i}', 'G', 'A', str(i)))
        with backend.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE chat=?', ('群聊:G',)).fetchone()[0], 100)

if __name__ == '__main__': unittest.main()
