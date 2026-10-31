"""Local admin routes for per-conversation workspace settings and execution logs."""
import json
from aiohttp import web
import backend
import coding_agent as agent
import proactive
import agent_images
import group_info
from workspace_agent.errors import AppError
from workspace_agent.folder_picker import pick_folder


async def script(request):
    return web.FileResponse(backend.BASE / 'agent_admin.js')


def valid_chat(chat):
    if not proactive.valid_chat(chat):
        raise web.HTTPBadRequest(text='请选择有效的群或私聊用户')
    return chat


def members(conn, chat):
    if chat.startswith('私聊:'):
        return [{'id':chat[3:], 'name':'当前私聊用户'}]
    return [{'id':m['id'], 'name':m['name'], 'platform_name':m['platform_name'], 'alias':m['alias']}
            for m in group_info.profile(conn,chat[3:])['members']]


def check_users(conn, chat, users):
    people = members(conn,chat)
    known_ids = {m['id'] for m in people}
    for user in users:
        if user in known_ids:
            continue
        matches = [m for m in people if user in (m['name'],m.get('platform_name'),m.get('alias'))]
        if matches:
            raise ValueError(f'“{user}”是昵称或备注，不能作为执行权限。请从成员列表选择对应 OpenID。')
        if chat.startswith('私聊:'):
            raise ValueError('私聊授权请填写当前会话的用户 OpenID，可以从成员列表选择')


async def get_settings(request):
    chat = valid_chat(request.query.get('chat'))
    with backend.db() as conn:
        cfg = agent.config(conn, chat)
        people = members(conn,chat)
        recent = [agent.run_info(conn, r[0], False) for r in conn.execute('SELECT id FROM agent_runs WHERE chat=? ORDER BY created DESC LIMIT 20', (chat,)).fetchall()]
    return web.json_response(dict(config=cfg, members=people, recent=recent, active=agent.manager.snapshot()), headers={'Cache-Control': 'no-store'})


async def save_settings(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请提交设置对象')
        chat = valid_chat(data.get('chat'))
        cfg = agent.validate_config(chat, data.get('config'))
        with agent.manager.guard:
            active = agent.manager.snapshot()
            if active and active['chat'] == chat:
                raise web.HTTPConflict(text='此会话任务仍在执行，请先停止任务再修改工作目录或权限')
            with backend.db() as conn:
                check_users(conn,chat,cfg['allowed_users'])
                conn.execute('INSERT INTO agent_config(chat,config) VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET config=excluded.config',
                             (chat, json.dumps(cfg, ensure_ascii=False)))
    except AppError as exc:
        raise web.HTTPBadRequest(text=exc.message)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({'ok': True})


async def folder(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请提交路径对象')
        initial = data.get('initial_path', '')
        if not isinstance(initial, str) or len(initial) > 4096:
            raise ValueError('路径格式无效')
        path = await pick_folder(initial)
        return web.json_response({'path': path, 'cancelled': path is None})
    except AppError as exc:
        return web.json_response({'error': '文件夹选择窗口不可用或已超时，请手动填写路径。', 'code': exc.code}, status=exc.status_code)
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text='请填写有效的初始路径')


async def get_run(request):
    with backend.db() as conn:
        data = agent.run_info(conn, request.match_info['run_id'])
    if not data:
        raise web.HTTPNotFound(text='任务不存在')
    return web.json_response(data, headers={'Cache-Control': 'no-store'})


async def image(request):
    try:
        with backend.db() as conn:
            raw,mime = agent_images.read_image(conn,request.match_info['image_id'])
    except (ValueError,OSError):
        raise web.HTTPNotFound(text='图片不存在或已被修改')
    return web.Response(body=raw,content_type=mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


async def stop(request):
    data = await request.json()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='请提交任务对象')
    chat = valid_chat(data.get('chat'))
    run_id = data.get('run_id')
    if not isinstance(run_id, str) or not run_id:
        raise web.HTTPBadRequest(text='请填写任务 ID')
    return web.json_response({'ok': True, 'stopping': agent.manager.cancel(chat, run_id)})


def register(app):
    app.add_routes([web.get('/agent-admin.js', script), web.get('/api/agent', get_settings),
                    web.post('/api/agent', save_settings), web.post('/api/agent/pick-folder', folder),
                    web.get('/api/agent/runs/{run_id}', get_run), web.get('/api/agent/images/{image_id}', image), web.post('/api/agent/stop', stop)])
