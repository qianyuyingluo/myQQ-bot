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
import coding_agent
import agent_images

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
        active = coding_agent.manager.snapshot()
        if active:
            coding_agent.manager.cancel(active['chat'], active['id'])
            with coding_agent.manager.guard:
                worker = coding_agent.manager.active['task'] if coding_agent.manager.active else None
            if worker:
                await asyncio.gather(worker, return_exceptions=True)
        task = getattr(self, '_proactive_task', None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await super().close()

    async def handle(self, message, scope):
        received_at = time.monotonic()
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
        text = coding_agent.command_text(message.content or '', robot_id, directed_group=scope == '群聊')
        with db() as conn:
            agent_cfg = coding_agent.config(conn, chat)
        agent_action = coding_agent.action(agent_cfg, user, text)
        # Status/stop bypass the conversation lock and ordinary rate limit, so a
        # long command can always be interrupted by an authorized sender.
        if agent_action in ('stop', 'status', 'help', 'disabled', 'denied'):
            if agent_action == 'stop':
                response = '正在停止任务，已写入的文件会保留。' if coding_agent.manager.cancel(chat) else '当前会话没有正在执行的 Agent 任务。'
            elif agent_action == 'status':
                with db() as conn:
                    row = conn.execute('SELECT id,status,response FROM agent_runs WHERE chat=? ORDER BY created DESC LIMIT 1', (chat,)).fetchone()
                response = f'任务 {row[0][:8]}：{row[1]}\n{row[2]}' if row else '当前会话还没有 Agent 任务。'
            elif agent_action == 'help':
                response = f'发送 /agent 加任务内容开始工作；/agent status 查看状态；/agent stop 停止执行。工作目录在管理页面设置。\n你的 OpenID：{user}'
            elif agent_action == 'disabled':
                response = f'请先在本机管理页面为此会话开启 Agent、选择工作文件夹并设置允许的用户。\n会话：{chat}\n你的 OpenID：{user}'
            else:
                response = f'你没有此会话的 Agent 执行权限。你的 OpenID：{user}'
            try:
                await message.reply(msg_type=0, content=response[:1800])
            except Exception:
                print('Agent 指令回复发送失败', flush=True)
            return
        if agent_action == 'run' and text.lower() not in ('/clean context', '/auto reply') and coding_agent.manager.snapshot():
            try:
                await message.reply(msg_type=0, content='Agent 正在执行任务，请稍后再发。可用 /agent status 查看状态，/agent stop 停止当前会话任务。')
            except Exception:
                print('Agent 忙碌提示发送失败', flush=True)
            return
        rate_key = f'私聊:{user}' if scope == '私聊' else f'群聊:{message.group_openid}:{user}'
        if not rate_limiter.allow(rate_key, settings()['requests_per_minute']):
            try:
                await message.reply(msg_type=0, content='樱洛繁忙中')
            except Exception:
                print('QQ 限流提示发送失败', flush=True)
            return
        lock = locks.setdefault(chat, asyncio.Lock())
        async with lock:
            agent_run_id = None
            reply_seq = 1
            try:
                if scope == '私聊' and text.lower() == '/clean context':
                    clear_context(chat)
                    response = '上下文已清除，我们重新开始吧。'
                elif text.lower() == '/auto reply':
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
                elif agent_action == 'run':
                    command = coding_agent.agent_command(text)
                    task_text = command if command is not None else text
                    async def started(run_id):
                        nonlocal reply_seq
                        # Distinct sequence numbers avoid QQ treating the final
                        # result as a duplicate reply to the same incoming message.
                        reply_seq = 2
                        try:
                            await asyncio.wait_for(message.reply(msg_type=0, msg_seq=1, content=f'已开始处理，任务 {run_id[:8]}。可用 /agent status 查看状态，/agent stop 停止。'), timeout=10)
                        except Exception:
                            pass
                    agent_run_id, response = await coding_agent.manager.run(chat, user, task_text, message.attachments or [], started, source_id=str(getattr(message, 'id', '') or ''))
                    if agent_run_id:
                        task_reply = agent_images.TaskReply(self.api, message, chat, received_at)
                        reply_seq, image_note = await agent_images.deliver(self.api, task_reply, chat, agent_run_id, reply_seq)
                        if image_note:
                            response += '\n\n图片发送提示：'+image_note
                            with db() as conn:
                                conn.execute('UPDATE agent_runs SET response=? WHERE id=?',(response,agent_run_id))
                        from backend import save_turn, group_text
                        remembered = group_text(chat[3:], user, task_text, meta.get('mentions', []), robot_id) if scope == '群聊' else task_text
                        save_turn(chat, remembered, response)
                else:
                    text = message.content or ''
                    if scope == '群聊':
                        response = await answer(chat, text, message.attachments or [], speaker=user, mentions=meta.get('mentions', []), robot_id=robot_id)
                    else:
                        response = await answer(chat, text, message.attachments or [])
                final = response if len(response) <= 1750 or not agent_run_id else response[:1650]+'\n（完整结果见管理页面的 Agent 执行记录）'
                if agent_run_id:
                    sent = await asyncio.wait_for(task_reply.reply(msg_type=0, msg_seq=reply_seq, content=final[:1800]),timeout=30)
                    if not isinstance(sent,dict) or not sent.get('id'):
                        raise ValueError('QQ 未确认 Agent 结果送达')
                else:
                    await message.reply(msg_type=0, msg_seq=reply_seq, content=final[:1800])
                if agent_run_id:
                    with db() as conn:
                        conn.execute("UPDATE agent_runs SET delivery='sent' WHERE id=?", (agent_run_id,))
                mark_replied(chat, activity)
            except Exception as exc:
                if agent_run_id:
                    with db() as conn:
                        conn.execute("UPDATE agent_runs SET delivery='failed' WHERE id=?", (agent_run_id,))
                    print(f'Agent 结果发送未确认，已保留在管理页面: {type(exc).__name__}', flush=True)
                    return
                print(f'处理消息失败: {type(exc).__name__}', flush=True)
                try:
                    await message.reply(msg_type=0, content='暂时无法回复，请稍后再试。')
                except Exception:
                    print('QQ 回复发送失败', flush=True)

    async def on_c2c_message_create(self, message):
        if first_delivery('私聊:'+str(message.author.user_openid), message.id):
            await self.handle(message, '私聊')

    async def on_group_at_message_create(self, message):
        if getattr(message, 'metadata', {}).get('author_bot'):
            return
        # An earlier full-group event may have only recorded the message because
        # READY's bot ID differs from its group OpenID. GROUP_AT is authoritative.
        if first_delivery('reply:'+message.group_openid, message.id):
            first_delivery(message.group_openid, message.id)
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
            if first_delivery('reply:'+message.group_openid, message.id):
                await self.handle(message, '群聊')
        else:
            if coding_agent.agent_command(coding_agent.command_text(message.content or '', directed_group=True)) is not None:
                # Wait for GROUP_AT; a slash command addressed to a different bot
                # must neither run here nor enter the ordinary model's history.
                remember_group(message.metadata, str(getattr(self.robot,'id','') or ''))
                return
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
    coding_agent.recover()
    config = qq_settings()
    admin = threading.Thread(target=run_admin, daemon=True)
    admin.start()
    if config['appid'] and config['secret']:
        Catgirl(intents=botpy.Intents(public_messages=True)).run(appid=config['appid'], secret=config['secret'])
    else:
        print('请在管理页面保存 QQ 配置，然后重启机器人。', flush=True)
        admin.join()
