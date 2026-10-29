import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / '.deps'))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import backend
import proactive


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = backend.DB, backend.CONFIG, backend.PROMPT
        root = Path(self.temp.name)
        backend.DB, backend.CONFIG, backend.PROMPT = root/'db.sqlite3', root/'model.json', root/'prompt'
        backend.CONFIG.write_text(json.dumps({'api_key': 'dummy', 'model': 'test'}))
        self.now = datetime(2026, 10, 28, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.api = SimpleNamespace(post_group_message=AsyncMock(return_value={'id': 'sent'}), post_c2c_message=AsyncMock(return_value={'id': 'sent'}))
        self.draft = AsyncMock(return_value='一起聊聊吧')
        self.engine = proactive.Scheduler(self.api, backend.db, self.draft, backend.save_proactive_message, {}, now=lambda: self.now)

    def tearDown(self):
        backend.DB, backend.CONFIG, backend.PROMPT = self.old
        self.temp.cleanup()

    def configure(self, chat='群聊:G', **values):
        with backend.db() as conn:
            return proactive.save_config(conn, chat, values, self.now)

    def observe(self, chat='群聊:G', directed=False):
        with backend.db() as conn:
            return proactive.observe(conn, chat, self.now, directed)

    def snapshot(self, chat='群聊:G'):
        with backend.db() as conn:
            return proactive.state(conn, chat)

    async def test_debounce_cooldown_and_no_followup_without_humans(self):
        self.configure(auto_enabled=True)
        self.observe()
        self.now += 9
        await self.engine.attempt('群聊:G')
        self.draft.assert_not_awaited()
        self.observe()  # A burst resets the ten-second wait.
        self.now += 10
        await self.engine.attempt('群聊:G')
        self.api.post_group_message.assert_awaited_once()
        self.now += 30
        self.observe()
        self.now += 10
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 1)
        self.now += 260
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 2)
        self.now += 3600
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 2)
        with backend.db() as conn:
            rows = conn.execute('SELECT role FROM messages').fetchall()
            self.assertEqual(rows, [('assistant',), ('assistant',)])

    async def test_periodic_ignores_cooldown_limits_and_no_reply_rule(self):
        self.configure(periodic_enabled=True, interval_seconds=30, hourly_limit=1, daily_limit=1, cooldown_seconds=86400)
        for step in (29, 1, 30, 30):
            self.now += step
            await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 3)
        self.assertEqual(self.snapshot()['activity'], 0)
        self.assertNotIn('msg_id', self.api.post_group_message.await_args.kwargs)
        # Persisted timers also protect a newly constructed scheduler from immediate repeats.
        restarted = proactive.Scheduler(self.api, backend.db, self.draft, backend.save_proactive_message, {}, now=lambda: self.now)
        await restarted.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 3)

    async def test_quiet_hours_cross_midnight_and_disable(self):
        self.configure(periodic_enabled=True)
        for hour in (23, 0, 8):
            self.now = datetime(2026, 10, 29, hour, tzinfo=proactive.TZ).timestamp()
            await self.engine.attempt('群聊:G')
        self.draft.assert_not_awaited()
        self.now = datetime(2026, 10, 29, 9, tzinfo=proactive.TZ).timestamp()
        await self.engine.attempt('群聊:G')
        self.api.post_group_message.assert_awaited_once()
        self.now = datetime(2026, 10, 29, 23, tzinfo=proactive.TZ).timestamp()
        self.configure(quiet_enabled=False)
        self.now += 30
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 2)

    async def test_auto_quotas_per_group_and_beijing_day(self):
        for chat in ('群聊:G', '群聊:H'):
            self.configure(chat, auto_enabled=True, hourly_limit=1, daily_limit=2, cooldown_seconds=0, quiet_enabled=False)
            self.observe(chat)
        self.now += 10
        await self.engine.attempt('群聊:G')
        self.observe()
        self.now += 10
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 1)
        await self.engine.attempt('群聊:H')
        self.assertEqual(self.api.post_group_message.await_count, 2)
        self.now += 3601
        await self.engine.attempt('群聊:G')
        self.observe()
        self.now += 3601
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 3)
        self.now = datetime(2026, 10, 29, 0, 1, tzinfo=proactive.TZ).timestamp()
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 4)

    async def test_new_message_and_config_change_cancel_stale_draft(self):
        self.configure(auto_enabled=True)
        self.observe()
        self.now += 10
        async def changed(*args):
            self.observe()
            return '过时内容'
        self.draft.side_effect = changed
        await self.engine.attempt('群聊:G')
        self.api.post_group_message.assert_not_awaited()
        self.now += 10
        async def disabled(*args):
            self.configure(auto_enabled=False)
            return '已经关闭'
        self.draft.side_effect = disabled
        await self.engine.attempt('群聊:G')
        self.api.post_group_message.assert_not_awaited()

    async def test_send_failure_pauses_without_context_or_secret_leak(self):
        self.configure(periodic_enabled=True)
        self.api.post_group_message.side_effect = RuntimeError('a secret payload must not leak')
        self.now += 30
        await self.engine.attempt('群聊:G')
        self.assertIn('RuntimeError', self.snapshot()['blocked'])
        self.assertNotIn('secret payload', self.snapshot()['blocked'])
        self.now += 120
        await self.engine.attempt('群聊:G')
        self.api.post_group_message.assert_awaited_once()
        self.assertEqual(backend.history('群聊:G'), [])
        self.api.post_group_message.side_effect = None
        self.configure(periodic_enabled=True)
        self.now += 30
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.api.post_group_message.await_count, 2)

    async def test_manual_private_target_and_dedup(self):
        self.now = datetime(2026, 10, 28, 23, tzinfo=proactive.TZ).timestamp()
        with backend.db() as conn:
            proactive.enqueue(conn, '私聊:USER', '晚安', self.now)
            with self.assertRaises(ValueError):
                proactive.enqueue(conn, '私聊:USER', '重复点击', self.now)
        await asyncio.gather(self.engine.attempt('私聊:USER'), self.engine.attempt('私聊:USER'))
        self.api.post_c2c_message.assert_awaited_once_with(openid='USER', msg_type=0, content='晚安')
        self.api.post_group_message.assert_not_awaited()
        self.assertEqual(backend.history('私聊:OTHER'), [])
        self.draft.assert_not_awaited()

    async def test_silence_and_generation_failure(self):
        self.configure(auto_enabled=True)
        self.observe()
        self.now += 10
        self.draft.return_value = ''
        await self.engine.attempt('群聊:G')
        self.now += 60
        await self.engine.attempt('群聊:G')
        self.draft.assert_awaited_once()
        self.observe()
        self.now += 10
        self.draft.side_effect = ValueError('invalid model JSON')
        await self.engine.attempt('群聊:G')
        self.assertEqual(self.snapshot()['retry_after'], self.now+60)
        self.api.post_group_message.assert_not_awaited()

    async def test_recovery_does_not_replay_unknown_delivery(self):
        with backend.db() as conn:
            first = proactive.enqueue(conn, '私聊:A', '测试', self.now)
            conn.execute("UPDATE proactive_jobs SET status='sending' WHERE id=?", (first,))
            proactive.enqueue(conn, '私聊:B', '未发送', self.now)
            proactive.recover(conn)
            statuses = dict(conn.execute('SELECT chat,status FROM proactive_jobs').fetchall())
            self.assertEqual(statuses, {'私聊:A': 'unknown', '私聊:B': 'cancelled'})
        await self.engine.attempt('私聊:A')
        self.api.post_c2c_message.assert_not_awaited()
        self.configure('私聊:A', periodic_enabled=True)
        with backend.db() as conn:
            proactive.recover(conn)
            self.assertEqual(proactive.state(conn, '私聊:A')['blocked'], '')

    async def test_admin_settings_and_offline_queue(self):
        app = web.Application(middlewares=[backend.local_only])
        app.add_routes([web.get('/api/proactive', backend.get_proactive), web.post('/api/proactive', backend.update_proactive), web.post('/api/proactive/send', backend.send_proactive)])
        async with TestClient(TestServer(app), headers={'Host':'127.0.0.1:8766'}) as client:
            d = await (await client.get('/api/proactive?chat=群聊:G')).json()
            self.assertEqual(d['config']['interval_seconds'], 30)
            data = {'chat':'私聊:A','config':{'periodic_enabled':True,'interval_seconds':45,'quiet_enabled':False}}
            self.assertEqual((await client.post('/api/proactive', json=data)).status, 200)
            bad = {'chat':'私聊:A','config':{'interval_seconds':0}}
            self.assertEqual((await client.post('/api/proactive', json=bad)).status, 400)
            self.assertEqual((await client.post('/api/proactive', json=data, headers={'Origin':'https://example.com'})).status, 403)
            with patch.object(proactive, 'heartbeat', 0):
                self.assertEqual((await client.post('/api/proactive/send', json={'chat':'私聊:A','content':'你好'})).status, 409)
            with patch.object(proactive, 'heartbeat', proactive.time.time()):
                response = await client.post('/api/proactive/send', json={'chat':'私聊:A','content':'你好'})
                self.assertEqual(response.status, 202)
            with backend.db() as conn:
                self.assertEqual(proactive.config(conn,'私聊:A')[0]['interval_seconds'],45)

    async def test_model_draft_reads_context_without_saving_fake_turn(self):
        backend.save_turn('群聊:G','[群成员 A] 今天学Python','一起学习吧')
        backend.save_turn('私聊:A','私人内容','私人回复')
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='{"send":true,"content":"有什么想练习的？"}')))
        with patch.object(backend, 'ChatOpenAI', return_value=model):
            result = await backend.proactive_draft('群聊:G','auto','编程')
        self.assertEqual(result,'有什么想练习的？')
        text = str(model.ainvoke.await_args.args)
        self.assertIn('Python',text)
        self.assertIn('当前服务器时间',text)
        self.assertNotIn('私人内容',text)
        self.assertEqual(len(backend.history('群聊:G')),2)

    async def test_live_scheduler_loop_dispatches_and_stops(self):
        self.configure(periodic_enabled=True)
        self.now += 30
        sent = asyncio.Event()
        async def send(**kwargs):
            sent.set()
            return {'id': 'confirmed'}
        self.api.post_group_message.side_effect = send
        task = asyncio.create_task(self.engine.run())
        try:
            await asyncio.wait_for(sent.wait(), timeout=3)
            # Wait for the send confirmation to be committed by the worker.
            for _ in range(20):
                if self.snapshot()['status'] == '发送成功':
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.snapshot()['status'], '发送成功')
            self.assertGreater(proactive.heartbeat, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(proactive.heartbeat, 0)
        self.api.post_group_message.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
