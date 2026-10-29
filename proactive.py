"""Persistent proactive-message policy and a scheduler on the QQ event loop."""
import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
DEFAULTS = {'auto_enabled': False, 'periodic_enabled': False, 'interval_seconds': 30,
            'debounce_seconds': 10, 'cooldown_seconds': 300, 'hourly_limit': 3,
            'daily_limit': 10, 'quiet_enabled': True, 'quiet_start': '23:00',
            'quiet_end': '09:00', 'topic': ''}
heartbeat = 0.0


def valid_chat(chat):
    return isinstance(chat, str) and re.fullmatch(r'(群聊|私聊):[A-Za-z0-9_-]{1,128}', chat) is not None


def create_tables(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS proactive_config (chat TEXT PRIMARY KEY, config TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)')
    conn.execute('CREATE TABLE IF NOT EXISTS proactive_state (chat TEXT PRIMARY KEY, activity INTEGER NOT NULL DEFAULT 0, handled INTEGER NOT NULL DEFAULT 0, human_at REAL NOT NULL DEFAULT 0, last_reply REAL NOT NULL DEFAULT 0, last_auto REAL NOT NULL DEFAULT 0, last_periodic REAL NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0, blocked TEXT NOT NULL DEFAULT \'\', status TEXT NOT NULL DEFAULT \'等待消息\')')
    conn.execute('CREATE TABLE IF NOT EXISTS proactive_jobs (id TEXT PRIMARY KEY, chat TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL DEFAULT \'\', created REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT \'\')')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_proactive_jobs_chat_time ON proactive_jobs(chat, created)')


def state(conn, chat):
    conn.execute('INSERT OR IGNORE INTO proactive_state(chat) VALUES(?)', (chat,))
    cursor = conn.execute('SELECT * FROM proactive_state WHERE chat=?', (chat,))
    return dict(zip([c[0] for c in cursor.description], cursor.fetchone()))


def config(conn, chat):
    row = conn.execute('SELECT config, version FROM proactive_config WHERE chat=?', (chat,)).fetchone()
    return ({**DEFAULTS, **json.loads(row[0])}, row[1]) if row else (DEFAULTS.copy(), 0)


def save_config(conn, chat, data, now):
    if not valid_chat(chat) or not isinstance(data, dict):
        raise ValueError('请选择有效的群或私聊 OpenID')
    cfg, _ = config(conn, chat)
    for key in DEFAULTS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(DEFAULTS[key], bool):
            if type(value) is not bool:
                raise ValueError('开关必须为布尔值')
        elif isinstance(DEFAULTS[key], int):
            ranges = {'interval_seconds': (10, 86400), 'debounce_seconds': (1, 600),
                      'cooldown_seconds': (0, 86400), 'hourly_limit': (1, 100), 'daily_limit': (1, 1000)}
            low, high = ranges[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} 必须是 {low} 到 {high} 的整数')
        elif key.startswith('quiet_'):
            if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                raise ValueError('安静时段请使用 HH:MM 格式')
        elif not isinstance(value, str) or len(value) > 500:
            raise ValueError('话题最多500字')
        cfg[key] = value
    if chat.startswith('私聊:'):
        cfg['auto_enabled'] = False
    conn.execute('INSERT INTO proactive_config(chat, config) VALUES(?, ?) ON CONFLICT(chat) DO UPDATE SET config=excluded.config, version=version+1', (chat, json.dumps(cfg, ensure_ascii=False)))
    state(conn, chat)
    conn.execute("UPDATE proactive_state SET blocked='', retry_after=0, last_periodic=?, status='设置已保存' WHERE chat=?", (now, chat))
    return cfg


def observe(conn, chat, now, directed=False):
    state(conn, chat)
    conn.execute('UPDATE proactive_state SET activity=activity+1, human_at=? WHERE chat=?', (now, chat))
    if directed:
        conn.execute('UPDATE proactive_state SET handled=activity WHERE chat=?', (chat,))
    return state(conn, chat)['activity']


def replied(conn, chat, activity, now):
    state(conn, chat)
    conn.execute('UPDATE proactive_state SET handled=MAX(handled, ?), last_reply=? WHERE chat=?', (activity, now, chat))


def is_quiet(cfg, now):
    if not cfg['quiet_enabled'] or cfg['quiet_start'] == cfg['quiet_end']:
        return False
    clock = datetime.fromtimestamp(now, TZ).strftime('%H:%M')
    start, end = cfg['quiet_start'], cfg['quiet_end']
    return start <= clock < end if start < end else clock >= start or clock < end


def auto_reason(conn, chat, cfg, st, now):
    if not cfg['auto_enabled'] or not chat.startswith('群聊:'):
        return '自动接话未开启'
    if st['activity'] <= st['handled']:
        return '等待新的成员消息'
    if now - st['human_at'] < cfg['debounce_seconds']:
        return '等待连续消息结束'
    if now - max(st['last_auto'], st['last_reply']) < cfg['cooldown_seconds']:
        return '冷却中'
    day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    hour_count, day_count = conn.execute("SELECT SUM(created>=?), SUM(created>=?) FROM proactive_jobs WHERE chat=? AND kind='auto' AND status='sent'", (now-3600, day, chat)).fetchone()
    if (hour_count or 0) >= cfg['hourly_limit']:
        return '达到每小时上限'
    if (day_count or 0) >= cfg['daily_limit']:
        return '达到今日上限'
    return ''


def due(conn, chat, now):
    cfg, version = config(conn, chat)
    st = state(conn, chat)
    if st['blocked'] or now < st['retry_after']:
        return None
    queued = conn.execute("SELECT id FROM proactive_jobs WHERE chat=? AND status='queued' ORDER BY created LIMIT 1", (chat,)).fetchone()
    if queued:
        return 'manual', queued[0], version, st['activity']
    if is_quiet(cfg, now):
        reason = '安静时段'
    elif cfg['periodic_enabled'] and now - st['last_periodic'] >= cfg['interval_seconds']:
        return 'periodic', None, version, st['activity']
    else:
        reason = auto_reason(conn, chat, cfg, st, now)
        if not reason:
            return 'auto', None, version, st['activity']
        if cfg['periodic_enabled']:
            remaining = max(0, int(cfg['interval_seconds'] - (now - st['last_periodic'])))
            reason = f'等待定时发言（约{remaining}秒）；' + reason
    conn.execute('UPDATE proactive_state SET status=? WHERE chat=?', (reason, chat))
    return None


def enqueue(conn, chat, content, now):
    if not valid_chat(chat) or not isinstance(content, str) or not 1 <= len(content.strip()) <= 1800:
        raise ValueError('请选择收件人并填写1至1800字的消息')
    if conn.execute("SELECT 1 FROM proactive_jobs WHERE chat=? AND status IN ('queued','sending')", (chat,)).fetchone():
        raise ValueError('该会话已有待发送消息，请等待发送结果')
    job = uuid.uuid4().hex
    state(conn, chat)
    # A deliberate manual send retries the target once; automated attempts stay paused on failure.
    conn.execute("UPDATE proactive_state SET blocked='', retry_after=0 WHERE chat=?", (chat,))
    conn.execute("INSERT INTO proactive_jobs(id,chat,kind,content,created,status) VALUES(?,?,'manual',?,?,'queued')", (job, chat, content.strip(), now))
    return job


def recover(conn):
    # Delivery may have succeeded before a crash. Never replay uncertain sends.
    uncertain = conn.execute("SELECT DISTINCT chat FROM proactive_jobs WHERE status='sending'").fetchall()
    conn.execute("UPDATE proactive_jobs SET status='unknown', detail='进程中断，送达结果未知；未自动重发' WHERE status='sending'")
    conn.execute("UPDATE proactive_jobs SET status='cancelled', detail='进程重启，取消未发送的手动消息' WHERE status='queued'")
    for (chat,) in uncertain:
        conn.execute("UPDATE proactive_state SET blocked='存在送达结果未知的消息，请检查QQ后重新保存设置', status='已暂停' WHERE chat=?", (chat,))


class Scheduler:
    def __init__(self, api, db, draft, save_message, locks, now=time.time):
        self.api, self.db, self.draft, self.save_message = api, db, draft, save_message
        self.locks, self.now = locks, now
        self.workers = {}

    async def run(self):
        global heartbeat
        with self.db() as conn:
            recover(conn)
        last_cleanup = 0
        try:
            while True:
                heartbeat = time.time()
                if heartbeat - last_cleanup > 3600:
                    with self.db() as conn:
                        conn.execute("DELETE FROM proactive_jobs WHERE created<? AND status NOT IN ('sending','queued')", (heartbeat-7*86400,))
                    last_cleanup = heartbeat
                with self.db() as conn:
                    chats = [r[0] for r in conn.execute('SELECT chat FROM proactive_config UNION SELECT chat FROM proactive_jobs WHERE status=\'queued\'')]
                for chat in chats:
                    if len(self.workers) >= 2:
                        break
                    if chat in self.workers:
                        continue
                    with self.db() as conn:
                        ready = due(conn, chat, self.now())
                    if ready:
                        task = asyncio.create_task(self.attempt(chat))
                        self.workers[chat] = task
                        task.add_done_callback(lambda done, key=chat: self.finished(key, done))
                await asyncio.sleep(1)
        finally:
            heartbeat = 0
            tasks = list(self.workers.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def finished(self, chat, task):
        self.workers.pop(chat, None)
        if not task.cancelled() and task.exception():
            print('主动消息任务异常：' + type(task.exception()).__name__, flush=True)

    async def attempt(self, chat):
        async with self.locks.setdefault(chat, asyncio.Lock()):
            with self.db() as conn:
                choice = due(conn, chat, self.now())
                if not choice:
                    return
                kind, job, version, activity = choice
                cfg, _ = config(conn, chat)
                if kind == 'manual':
                    text = conn.execute('SELECT content FROM proactive_jobs WHERE id=?', (job,)).fetchone()[0]
                else:
                    conn.execute("UPDATE proactive_state SET retry_after=?, status='正在生成主动消息' WHERE chat=?", (self.now()+30, chat))
            if kind != 'manual':
                try:
                    text = await self.draft(chat, kind, cfg['topic'])
                except Exception as exc:
                    with self.db() as conn:
                        conn.execute('UPDATE proactive_state SET retry_after=?, status=? WHERE chat=?', (self.now()+60, '生成失败：'+type(exc).__name__, chat))
                    return
            with self.db() as conn:
                latest, revision = config(conn, chat)
                st = state(conn, chat)
                if kind != 'manual' and (revision != version or st['activity'] != activity or is_quiet(latest, self.now())):
                    conn.execute("UPDATE proactive_state SET retry_after=0, last_periodic=CASE WHEN ?='periodic' THEN ? ELSE last_periodic END, status='新消息或设置变化，已取消过时草稿' WHERE chat=?", (kind, self.now(), chat))
                    return
                if not text or not text.strip():
                    conn.execute("UPDATE proactive_state SET handled=?, retry_after=0, last_periodic=CASE WHEN ?='periodic' THEN ? ELSE last_periodic END, status='本轮保持沉默' WHERE chat=?", (activity, kind, self.now(), chat))
                    return
                if not job:
                    job = uuid.uuid4().hex
                    conn.execute("INSERT INTO proactive_jobs(id,chat,kind,content,created,status) VALUES(?,?,?,?,?,'sending')", (job, chat, kind, text[:1800], self.now()))
                else:
                    conn.execute("UPDATE proactive_jobs SET status='sending' WHERE id=? AND status='queued'", (job,))
            try:
                method = self.api.post_group_message if chat.startswith('群聊:') else self.api.post_c2c_message
                target = {'group_openid': chat[3:]} if chat.startswith('群聊:') else {'openid': chat[3:]}
                result = await asyncio.wait_for(method(**target, msg_type=0, content=text[:1800]), timeout=30)
                if not isinstance(result, dict) or not result.get('id'):
                    raise RuntimeError('QQ未返回消息ID，送达结果未知')
            except Exception as exc:
                code = getattr(exc, 'code', None)
                safe_code = str(code) if isinstance(code, int) else ''
                detail = 'QQ发送未确认：'+type(exc).__name__+(' / '+safe_code if safe_code else '')+'；已暂停，请检查权限与QQ消息后重试'
                with self.db() as conn:
                    conn.execute("UPDATE proactive_jobs SET status='failed',detail=? WHERE id=?", (detail, job))
                    conn.execute('UPDATE proactive_state SET blocked=?,status=? WHERE chat=?', (detail, detail, chat))
                return
            # Only confirmed sends become assistant history, never a fake user turn.
            with self.db() as conn:
                conn.execute("UPDATE proactive_jobs SET status='sent',created=? WHERE id=?", (self.now(), job))
                conn.execute("UPDATE proactive_state SET handled=MAX(handled,?), last_reply=?, last_auto=CASE WHEN ?='auto' THEN ? ELSE last_auto END, last_periodic=CASE WHEN ?='periodic' THEN ? ELSE last_periodic END, retry_after=0, status='发送成功' WHERE chat=?", (activity, self.now(), kind, self.now(), kind, self.now(), chat))
                self.save_message(conn, chat, text[:1800])
