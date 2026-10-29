import asyncio
import json
import sys
import threading
import re
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / '.deps'))
import botpy
from botpy.connection import ConnectionState
from botpy.message import GroupMessage
from backend import answer, clear_context, initialize_memory, run_admin, save_group_message, settings, qq_settings, remember_group, RateLimiter, db, proactive_draft, save_proactive_message, observe_activity, mark_replied
import group_info
import proactive

locks = {}
rate_limiter = RateLimiter()
seen_messages = {}

class DetailedGroupMessage(GroupMessage):
    __slots__ = ('metadata',)

    def __init__(self, api, event_id, data):
        # Keep the raw metadata before botpy 1.2.1 reduces users to OpenIDs.
        self.metadata = group_info.event_metadata(data)
        clean = dict(data)
        clean['mentions'] = [m for m in (data.get('mentions') or []) if isinstance(m, dict)]
        super().__init__(api, event_id, clean)
        self.author.member_openid = self.metadata['author_id']

def parse_group_at_message_create(self, payload):
    self._dispatch('group_at_message_create', DetailedGroupMessage(self.api, payload.get('id'), payload.get('d') or {}))

# Both event paths must preserve the same metadata.
ConnectionState.parse_group_at_message_create = parse_group_at_message_create

def parse_group_message_create(self, payload):
    self._dispatch('group_message_create', payload)

# botpy 1.2.1 does not register the full-group event.
if not hasattr(ConnectionState, 'parse_group_message_create'):
    ConnectionState.parse_group_message_create = parse_group_message_create

def is_bot_mentioned(payload, robot):
    data = payload.get('d') or {}
    robot_id = str(getattr(robot, 'id', '') or '')
    for mention in data.get('mentions') or []:
        if not isinstance(mention, dict):
            continue
        if mention.get('is_you') is True or mention.get('isYou') is True:
            return True
        if robot_id and group_info.user_id(mention) == robot_id:
            return True
    return bool(robot_id and any(m['id'] == robot_id for m in group_info.event_metadata(data)['mentions']))

def first_delivery(group, message_id):
    if not message_id:
        return True
    now = time.monotonic()
    for key, stamp in list(seen_messages.items()):
        if now - stamp > 300:
            del seen_messages[key]
    key = (group, message_id)
    if key in seen_messages:
        return False
    seen_messages[key] = now
    return True

class Catgirl(botpy.Client):
    async def on_ready(self):
        print(f'已连接 QQ：{self.robot.name}', flush=True)
        if not getattr(self, '_proactive_task', None) or self._proactive_task.done():
            self._proactive_scheduler = proactive.Scheduler(self.api, db, proactive_draft, save_proactive_message, locks)
            self._proactive_task = asyncio.create_task(self._proactive_scheduler.run())

    async def close(self):
        task = getattr(self, '_proactive_task', None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await super().close()

    async def handle(self, message, scope):
        user = message.author.user_openid if scope == '私聊' else message.author.member_openid
        if not user or (scope == '群聊' and not message.group_openid):
            print('消息缺少用户或群标识，已忽略', flush=True)
            return
        chat = f'私聊:{user}' if scope == '私聊' else f'群聊:{message.group_openid}'
        activity = observe_activity(chat, directed=True)
        robot_id = str(getattr(getattr(self, 'robot', None), 'id', '') or '')
        meta = getattr(message, 'metadata', {})
        if scope == '群聊':
            remember_group({**meta, 'group_id': message.group_openid, 'author_id': user}, robot_id)
        rate_key = f'私聊:{user}' if scope == '私聊' else f'群聊:{message.group_openid}:{user}'
        if not rate_limiter.allow(rate_key, settings()['requests_per_minute']):
            try:
                await message.reply(msg_type=0, content='樱洛繁忙中')
            except Exception:
                print('QQ 限流提示发送失败', flush=True)
            return
        lock = locks.setdefault(chat, asyncio.Lock())
        async with lock:
            try:
                if scope == '私聊' and (message.content or '').strip().lower() == '/clean context':
                    clear_context(chat)
                    response = '上下文已清除，我们重新开始吧。'
                elif (message.content or '').strip().lower() == '/auto reply':
                    response = '樱洛来啦，有什么想聊的喵？'
                    try:
                        if scope == '私聊':
                            await self.api.post_c2c_message(openid=user, msg_type=0, content=response)
                        else:
                            await self.api.post_group_message(group_openid=message.group_openid, msg_type=0, content=response)
                        mark_replied(chat, activity)
                        return
                    except Exception as exc:
                        print(f'主动消息发送失败，改用指令回复: {type(exc).__name__}', flush=True)
                else:
                    text = message.content or ''
                    if scope == '群聊':
                        response = await answer(chat, text, message.attachments or [], speaker=user, mentions=meta.get('mentions', []), robot_id=robot_id)
                    else:
                        response = await answer(chat, text, message.attachments or [])
                await message.reply(msg_type=0, content=response[:1800])
                mark_replied(chat, activity)
            except Exception as exc:
                print(f'处理消息失败: {type(exc).__name__}', flush=True)
                try:
                    await message.reply(msg_type=0, content='暂时无法回复，请稍后再试。')
                except Exception:
                    print('QQ 回复发送失败', flush=True)

    async def on_c2c_message_create(self, message):
        await self.handle(message, '私聊')

    async def on_group_at_message_create(self, message):
        if getattr(message, 'metadata', {}).get('author_bot'):
            return
        if first_delivery(message.group_openid, message.id):
            await self.handle(message, '群聊')
        elif getattr(message, 'metadata', None):
            remember_group(message.metadata, str(getattr(self.robot, 'id', '') or ''))

    async def on_group_message_create(self, payload):
        data = payload.get('d') or {}
        author = data.get('author') or {}
        if author.get('bot') is True:
            return
        message = DetailedGroupMessage(self.api, payload.get('id'), data)
        if not message.group_openid or not message.author.member_openid:
            return
        if message.author.member_openid == str(getattr(self.robot, 'id', '') or ''):
            return
        if not first_delivery(message.group_openid, message.id):
            remember_group(message.metadata, str(getattr(self.robot, 'id', '') or ''))
            return
        if is_bot_mentioned(payload, self.robot) or (message.content or '').strip().lower() == '/auto reply':
            await self.handle(message, '群聊')
        else:
            chat = f'群聊:{message.group_openid}'
            # Record arrival before waiting for the conversation lock to invalidate stale drafts.
            observe_activity(chat)
            async with locks.setdefault(chat, asyncio.Lock()):
                try:
                    robot_id = str(getattr(self.robot, 'id', '') or '')
                    remember_group(message.metadata, robot_id)
                    save_group_message(chat, message.author.member_openid, message.content or '', message.attachments or [], message.metadata['mentions'], robot_id)
                except Exception as exc:
                    print(f'群消息保存失败: {type(exc).__name__}', flush=True)

if __name__ == '__main__':
    initialize_memory()
    config = qq_settings()
    admin = threading.Thread(target=run_admin, daemon=True)
    admin.start()
    if config['appid'] and config['secret']:
        Catgirl(intents=botpy.Intents(public_messages=True)).run(appid=config['appid'], secret=config['secret'])
    else:
        print('请在管理页面保存 QQ 配置，然后重启机器人。', flush=True)
        admin.join()
