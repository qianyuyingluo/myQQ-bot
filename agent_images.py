"""Collect generated images and deliver them through QQ's official media API."""
import asyncio
import base64
import hashlib
from pathlib import Path
import time
from uuid import uuid4

from botpy.http import Route
from workspace_agent import workspace_tools
from workspace_agent.errors import AppError

MAX_IMAGES = 3
MAX_BYTES = 4 * 1024 * 1024
IMAGE_TOOL = {'type':'function', 'function':{'name':'publish_image',
    'description':'Register a PNG/JPG image from the workspace to send to the current QQ conversation when the task finishes. Use for generated images or an image the user explicitly asks to receive. At most 3 images per task, 4 MiB per image. This queues delivery, not a success receipt.',
    'parameters':{'type':'object','properties':{'path':{'type':'string','description':'Workspace-relative image file path'}},
                  'required':['path'],'additionalProperties':False}}}


class TaskReply:
    """Select active delivery before a long task's passive reply window expires."""
    def __init__(self, api, message, chat, received_at):
        self.api, self.message, self.chat, self.received_at = api,message,chat,received_at

    async def reply(self, **kwargs):
        if time.monotonic()-self.received_at < 240:
            return await self.message.reply(**kwargs)
        # Do not fall back after a failed/uncertain send: that could duplicate it.
        if self.chat.startswith('群聊:'):
            return await self.api.post_group_message(group_openid=self.chat[3:], **kwargs)
        return await self.api.post_c2c_message(openid=self.chat[3:], **kwargs)


def create_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_images (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL, local_path TEXT NOT NULL,
        mime TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '',
        UNIQUE(run_id,sha256))''')


def snapshot(tools):
    result = {}
    try:
        for path, is_dir in tools._entries(tools.root, True):
            if not is_dir and path.suffix.lower() in ('.png','.jpg','.jpeg'):
                stat = path.stat()
                result[tools._relative(path)] = (stat.st_mtime_ns, stat.st_size)
    except (AppError, OSError):
        # An incomplete baseline must never cause unchanged old images to be sent.
        return None
    return result


def _db():
    import backend
    return backend.db()


def register(tools, run_id, path):
    try:
        file = tools.resolve(path, must_exist=True)
        if not file.is_file() or file.suffix.lower() not in ('.png','.jpg','.jpeg'):
            raise ValueError('只发送 PNG/JPG 图片')
        if file.stat().st_size > MAX_BYTES:
            raise ValueError('图片超过4 MiB，请缩小图片后再发送')
        with file.open('rb') as stream:
            raw = stream.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise ValueError('图片超过4 MiB')
        mime = 'image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else ''
        if not mime:
            raise ValueError('文件内容不是 PNG/JPG 图片')
        digest = hashlib.sha256(raw).hexdigest()
        with _db() as conn:
            old = conn.execute('SELECT id FROM agent_images WHERE run_id=? AND sha256=?',(run_id,digest)).fetchone()
            if old:
                return {'ok':True,'image_id':old[0],'status':'already_registered'}
            if conn.execute('SELECT COUNT(*) FROM agent_images WHERE run_id=?',(run_id,)).fetchone()[0] >= MAX_IMAGES:
                raise ValueError('每个任务最多发送3张图片；其余图片保留在工作目录中')
            id = uuid4().hex
            folder = workspace_tools.RUNTIME_DIR / 'agent_runs' / run_id / 'images'
            folder.mkdir(parents=True,exist_ok=True)
            stored = folder / (id+('.png' if mime=='image/png' else '.jpg'))
            stored.write_bytes(raw)
            name = tools._relative(file)
            conn.execute('INSERT INTO agent_images(id,run_id,name,local_path,mime,sha256,size) VALUES(?,?,?,?,?,?,?)',
                         (id,run_id,name,str(stored),mime,digest,len(raw)))
        return {'ok':True,'image_id':id,'path':name,'status':'queued_for_delivery'}
    except (ValueError,AppError,OSError) as exc:
        return {'ok':False,'error':str(exc)[:400]}


def collect(tools, run_id, before):
    if before is None:
        return '目录过大或扫描不完整，未自动收集图片；可用 publish_image 指定图片。'
    after = snapshot(tools)
    if after is None:
        return '图片扫描未完成，只有已登记的图片会发送。'
    errors = []
    for name, stamp in after.items():
        if before.get(name) != stamp:
            result = register(tools,run_id,name)
            if not result['ok']:
                errors.append(name+': '+result['error'])
    return '\n'.join(errors[:5])


def records(conn, run_id):
    rows = conn.execute('SELECT id,name,mime,size,status,detail FROM agent_images WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
    return [dict(zip(('id','name','mime','size','status','detail'),row)) for row in rows]


def read_image(conn, id):
    row = conn.execute('SELECT local_path,mime,sha256 FROM agent_images WHERE id=?',(id,)).fetchone()
    if not row:
        raise ValueError('图片不存在')
    file = Path(row[0]).resolve(strict=True)
    if not file.is_relative_to(workspace_tools.RUNTIME_DIR.resolve()) or file.stat().st_size > MAX_BYTES:
        raise ValueError('图片路径或大小无效')
    with file.open('rb') as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != row[2]:
        raise ValueError('图片快照已被修改')
    return raw,row[1]


async def upload(api, chat, raw):
    # botpy 1.2.1 lacks a file_data argument; reuse its token/session transport.
    if chat.startswith('群聊:'):
        route = Route('POST','/v2/groups/{group_openid}/files',group_openid=chat[3:])
    else:
        route = Route('POST','/v2/users/{openid}/files',openid=chat[3:])
    result = await asyncio.wait_for(api._http.request(route,json={
        'file_type':1,'file_data':base64.b64encode(raw).decode('ascii'),'srv_send_msg':False}),timeout=30)
    if not isinstance(result,dict) or not isinstance(result.get('file_info'),str) or not result['file_info']:
        raise ValueError('QQ 未返回图片文件信息')
    return result['file_info']


async def deliver(api, message, chat, run_id, seq):
    with _db() as conn:
        images = records(conn,run_id)
    notes = []
    for item in images:
        if item['status'] != 'pending':
            continue
        status, detail = 'failed', ''
        try:
            with _db() as conn:
                raw,_ = read_image(conn,item['id'])
                conn.execute("UPDATE agent_images SET status='uploading' WHERE id=?",(item['id'],))
            file_info = await upload(api,chat,raw)
            with _db() as conn:
                conn.execute("UPDATE agent_images SET status='sending' WHERE id=?",(item['id'],))
            # Sending can succeed remotely even if a response times out. Never retry.
            status = 'unknown'
            sent = await asyncio.wait_for(message.reply(msg_type=7,msg_seq=seq,media={'file_info':file_info}),timeout=30)
            if not isinstance(sent,dict) or not sent.get('id'):
                raise ValueError('QQ 未确认图片发送结果')
            status = 'sent'
        except asyncio.CancelledError:
            detail = '发送中断，未自动重发'
            raise
        except Exception as exc:
            detail = f'{type(exc).__name__}：未确认发送，图片保留在本机执行记录中'
            notes.append(item['name']+'：'+detail)
        finally:
            seq += 1
            with _db() as conn:
                conn.execute('UPDATE agent_images SET status=?,detail=? WHERE id=?',(status,detail,item['id']))
    return seq, '\n'.join(notes)
