import json
import base64
import sqlite3
import time
import os
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from aiohttp import web
import aiohttp
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'model_config.json'
PROMPT = BASE / 'systemprompt'
DB = BASE / 'conversations.sqlite3'
QQ_CONFIG = BASE / 'qqbot_config.json'
LEGACY_QQ_CONFIG = BASE / 'apikey.txt'
qq_config_lock = threading.RLock()

def qq_settings():
    with qq_config_lock:
        # Rename only after validating the legacy file; never overwrite a new config.
        path = QQ_CONFIG if QQ_CONFIG.exists() else LEGACY_QQ_CONFIG
        if not path.exists():
            return {'appid': '', 'secret': ''}
        cfg = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(cfg, dict) or not isinstance(cfg.get('appid'), str) or not isinstance(cfg.get('secret'), str):
            raise ValueError('QQ 配置格式错误')
        if path == LEGACY_QQ_CONFIG:
            path.rename(QQ_CONFIG)
        return {'appid': cfg['appid'], 'secret': cfg['secret']}

def save_qq_settings(appid, secret):
    with qq_config_lock:
        cfg = qq_settings()
        if not isinstance(appid, str) or not appid.isascii() or not appid.isdigit() or len(appid) > 32:
            raise ValueError('AppID 必须是数字')
        if not isinstance(secret, str) or len(secret) > 512 or any(c.isspace() for c in secret):
            raise ValueError('AppSecret 格式不正确')
        if not secret:
            if appid != cfg['appid'] or not cfg['secret']:
                raise ValueError('首次配置或更换 AppID 时，请同时填写 AppSecret')
            secret = cfg['secret']
        # Atomic replacement prevents a partial JSON file if a write is interrupted.
        fd, name = tempfile.mkstemp(prefix='.qqbot-', suffix='.tmp', dir=QQ_CONFIG.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump({'appid': appid, 'secret': secret}, out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, QQ_CONFIG)
        finally:
            if os.path.exists(name):
                os.unlink(name)

DEFAULTS = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'vision': False, 'api_key': '', 'requests_per_minute': 10, 'private_context_limit': 20, 'group_context_limit': 100}

def context_limit(chat, cfg=None):
    cfg = cfg or settings()
    return cfg['group_context_limit'] if chat.startswith('群聊:') else cfg['private_context_limit']

class RateLimiter:
    def __init__(self):
        self.requests = {}
        self.last_cleanup = 0

    def allow(self, chat, limit, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_cleanup >= 60:
            self.requests = {key: queue for key, queue in self.requests.items() if queue and queue[-1] > now - 60}
            self.last_cleanup = now
        queue = self.requests.setdefault(chat, deque())
        while queue and queue[0] <= now - 60:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
        return True

def settings():
    if not CONFIG.exists(): return DEFAULTS.copy()
    return {**DEFAULTS, **json.loads(CONFIG.read_text(encoding='utf-8'))}

def prompt():
    return PROMPT.read_text(encoding='utf-8') if PROMPT.exists() else '你是一位友善的猫娘助手，请自然、简洁地回答。'

@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=10)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)')
        yield conn
        conn.commit()
    finally:
        conn.close()

def history(chat):
    with db() as conn:
        return list(reversed(conn.execute('SELECT role, content FROM messages WHERE chat=? ORDER BY id DESC LIMIT ?', (chat, context_limit(chat) - 1)).fetchall()))

def save_turn(chat, content, response):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.executemany('INSERT INTO messages(chat,role,content) VALUES(?,?,?)', [
            (chat, 'user', json.dumps(content, ensure_ascii=False)),
            (chat, 'assistant', json.dumps(response, ensure_ascii=False)),
        ])
        conn.execute('DELETE FROM messages WHERE chat=? AND id NOT IN (SELECT id FROM messages WHERE chat=? ORDER BY id DESC LIMIT ?)', (chat, chat, context_limit(chat)))

def save_group_message(chat, user, text, attachments):
    cfg = settings()
    content = message_content(f'[群成员 {user}] {text.strip()}' if text.strip() else '', attachments, cfg['vision'])
    if not content:
        return
    if isinstance(content, list) and not text.strip():
        content.insert(0, {'type': 'text', 'text': f'[群成员 {user}]'})
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO messages(chat,role,content) VALUES(?,?,?)', (chat, 'user', json.dumps(content, ensure_ascii=False)))
        conn.execute('DELETE FROM messages WHERE chat=? AND id NOT IN (SELECT id FROM messages WHERE chat=? ORDER BY id DESC LIMIT ?)', (chat, chat, context_limit(chat, cfg)))

def clear_context(chat):
    with db() as conn:
        conn.execute('DELETE FROM messages WHERE chat=?', (chat,))

def initialize_memory():
    cfg = settings()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat, id)')
        conn.execute('DELETE FROM messages WHERE id IN (SELECT id FROM (SELECT id, chat, ROW_NUMBER() OVER (PARTITION BY chat ORDER BY id DESC) AS n FROM messages) WHERE n > CASE WHEN substr(chat, 1, 3) = ? THEN ? ELSE ? END)', ('群聊:', cfg['group_context_limit'], cfg['private_context_limit']))

def clean_content(content, vision):
    if isinstance(content, str): return content
    return [block for block in content if block.get('type') == 'text' or (vision and block.get('type') == 'image_url')]

def message_content(text, attachments, vision):
    text = text.strip()
    blocks = ([{'type': 'text', 'text': text}] if text else [])
    if vision:
        for item in attachments[:4]:
            url = getattr(item, 'url', '') or ''
            typ = getattr(item, 'content_type', '') or ''
            if url.startswith('//'): url = 'https:' + url
            if typ.startswith('image/') and url.startswith('https://'):
                blocks.append({'type': 'image_url', 'image_url': {'url': url}})
    if not blocks: return None
    return blocks if any(b['type'] == 'image_url' for b in blocks) else text

async def prepare_images(content, session):
    if isinstance(content, str):
        return content
    prepared = []
    for block in content:
        if block.get('type') != 'image_url':
            prepared.append(block)
            continue
        url = block.get('image_url', {}).get('url', '')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or not (parsed.hostname == 'qq.com' or parsed.hostname.endswith('.qq.com') or parsed.hostname == 'qq.com.cn' or parsed.hostname.endswith('.qq.com.cn')):
            continue
        try:
            async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
                mime = response.headers.get('Content-Type', '').split(';')[0].lower()
                if response.status != 200 or mime not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
                    continue
                if response.content_length is not None and response.content_length > 8 * 1024 * 1024:
                    continue
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        break
                if len(raw) > 8 * 1024 * 1024 or not raw:
                    continue
                encoded = base64.b64encode(raw).decode('ascii')
                prepared.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}})
        except (aiohttp.ClientError, TimeoutError):
            continue
    return prepared

async def answer(chat, text, attachments):
    cfg = settings()
    current_content = message_content(text, attachments, cfg['vision'])
    if not current_content: return '当前没有可处理的文字或图片。'
    if not cfg['api_key']: return '管理员尚未配置模型 API Key。'
    messages = [SystemMessage(content=prompt())]
    async with aiohttp.ClientSession() as session:
        for role, raw in history(chat):
            content = clean_content(json.loads(raw), cfg['vision'])
            if isinstance(content, list): content = await prepare_images(content, session)
            if content: messages.append(HumanMessage(content=content) if role == 'user' else AIMessage(content=content))
        prepared_current = await prepare_images(current_content, session)
    if not prepared_current:
        return '这张图片暂时无法读取，请重新发送。'
    messages.append(HumanMessage(content=prepared_current))
    model = ChatOpenAI(model=cfg['model'], base_url=cfg['base_url'], api_key=cfg['api_key'], timeout=60, max_retries=1)
    result = await model.ainvoke(messages)
    response = result.content if isinstance(result.content, str) else ''.join(x.get('text', '') for x in result.content if isinstance(x, dict))
    if not response.strip(): raise ValueError('empty model response')
    save_turn(chat, current_content, response)
    return response

@web.middleware
async def local_only(request, handler):
    if request.host not in ('127.0.0.1:8766', 'localhost:8766'): raise web.HTTPForbidden()
    origin = request.headers.get('Origin')
    if origin and origin not in ('http://127.0.0.1:8766', 'http://localhost:8766'): raise web.HTTPForbidden()
    if request.method != 'GET' and request.content_type != 'application/json': raise web.HTTPUnsupportedMediaType()
    return await handler(request)

async def index(request): return web.FileResponse(BASE / 'admin.html')

async def get_qq_settings(request):
    cfg = qq_settings()
    return web.json_response({'appid': cfg['appid'], 'has_secret': bool(cfg['secret'])}, headers={'Cache-Control': 'no-store'})

async def update_qq_settings(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请提交 QQ 配置对象')
        save_qq_settings(data.get('appid', ''), data.get('secret', ''))
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text='请填写数字 AppID 和有效的 AppSecret；首次配置或更换 AppID 时必须填写 AppSecret')
    return web.json_response({'ok': True, 'restart_required': True})

async def get_settings(request):
    cfg = settings()
    return web.json_response({'base_url': cfg['base_url'], 'model': cfg['model'], 'vision': cfg['vision'], 'has_key': bool(cfg['api_key']), 'prompt': prompt(), 'requests_per_minute': cfg['requests_per_minute'], 'private_context_limit': cfg['private_context_limit'], 'group_context_limit': cfg['group_context_limit']})

async def update_settings(request):
    data = await request.json()
    url = str(data.get('base_url', '')).strip().rstrip('/')
    parsed = urlparse(url)
    model = str(data.get('model', '')).strip()
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or not model: raise web.HTTPBadRequest(text='请填写有效的 API 地址和模型名称')
    cfg = settings()
    limit = data.get('requests_per_minute', cfg['requests_per_minute'])
    if type(limit) is not int or not 1 <= limit <= 1000: raise web.HTTPBadRequest(text='每分钟请求上限必须是 1 到 1000 的整数')
    limits = {}
    for name in ('private_context_limit', 'group_context_limit'):
        value = data.get(name, cfg[name])
        if type(value) is not int or not 2 <= value <= 1000: raise web.HTTPBadRequest(text='上下文上限必须是 2 到 1000 的整数')
        limits[name] = value
    cfg.update(base_url=url, model=model, vision=bool(data.get('vision')), requests_per_minute=limit, **limits)
    key = str(data.get('api_key', '')).strip()
    if key: cfg['api_key'] = key
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    PROMPT.write_text(str(data.get('prompt', '')), encoding='utf-8')
    initialize_memory()
    return web.json_response({'ok': True})

async def chats(request):
    with db() as conn:
        rows = conn.execute('SELECT chat, COUNT(*), MAX(created) FROM messages GROUP BY chat ORDER BY MAX(id) DESC').fetchall()
    return web.json_response([{'chat': x[0], 'count': x[1], 'updated': x[2]} for x in rows])

async def messages(request):
    with db() as conn:
        rows = conn.execute('SELECT role, content, created FROM messages WHERE chat=? ORDER BY id', (request.query.get('chat', ''),)).fetchall()
    return web.json_response([{'role': r, 'content': json.loads(c), 'created': t} for r,c,t in rows])

def run_admin():
    app = web.Application(middlewares=[local_only], client_max_size=65536)
    app.add_routes([web.get('/', index), web.get('/api/settings', get_settings), web.post('/api/settings', update_settings), web.get('/api/chats', chats), web.get('/api/messages', messages)])
    app.add_routes([web.get('/api/qqbot-config', get_qq_settings), web.post('/api/qqbot-config', update_qq_settings)])
    print('管理页面：http://127.0.0.1:8766', flush=True)
    web.run_app(app, host='127.0.0.1', port=8766, handle_signals=False, print=None)
