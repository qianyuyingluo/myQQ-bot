"""Group-scoped display names and mention metadata; no platform API calls."""
import json
import re


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) else ''


def name(value):
    return ' '.join(value.split())[:120] if isinstance(value, str) else ''


def user_id(data):
    return identifier(data.get('member_openid')) or identifier(data.get('id'))


def nickname(data):
    return name(data.get('card')) or name(data.get('nickname')) or name(data.get('username'))


def event_metadata(data):
    author = data.get('author') if isinstance(data.get('author'), dict) else {}
    group = data.get('group') if isinstance(data.get('group'), dict) else {}
    mentions = []
    items = data.get('mentions')
    for item in (items if isinstance(items, list) else [])[:50]:
        if not isinstance(item, dict):
            continue
        mid = user_id(item)
        if mid:
            mentions.append({'id': mid, 'name': nickname(item), 'bot': item.get('bot') is True,
                             'is_self': item.get('is_you') is True or item.get('isYou') is True})
    # Retain explicit protocol mentions even when the event lacks a mentions array.
    text = data.get('content') if isinstance(data.get('content'), str) else ''
    ids = re.findall(r'<@!?([A-Za-z0-9_-]{1,128})>', text)
    ids += re.findall(r'<qqbot-at-user\b[^>]*\bid=["\']([A-Za-z0-9_-]{1,128})["\']', text)
    known = {m['id'] for m in mentions}
    for mid in ids[:50]:
        if mid not in known:
            mentions.append({'id': mid, 'name': '', 'bot': False, 'is_self': False})
            known.add(mid)
    return {'group_id': identifier(data.get('group_openid')),
            'group_name': name(data.get('group_name')) or name(group.get('name')),
            'author_id': user_id(author), 'author_name': nickname(author), 'author_bot': author.get('bot') is True, 'mentions': mentions[:50]}


def create_tables(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS group_profiles (group_id TEXT PRIMARY KEY, platform_name TEXT NOT NULL DEFAULT \'\', alias TEXT NOT NULL DEFAULT \'\')')
    conn.execute('CREATE TABLE IF NOT EXISTS group_members (group_id TEXT NOT NULL, member_id TEXT NOT NULL, platform_name TEXT NOT NULL DEFAULT \'\', alias TEXT NOT NULL DEFAULT \'\', updated TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(group_id, member_id))')


def remember(conn, meta, robot_id=''):
    gid = identifier(meta.get('group_id'))
    if not gid:
        return
    conn.execute('INSERT INTO group_profiles(group_id, platform_name) VALUES(?, ?) ON CONFLICT(group_id) DO UPDATE SET platform_name=CASE WHEN excluded.platform_name != \'\' THEN excluded.platform_name ELSE group_profiles.platform_name END', (gid, name(meta.get('group_name'))))
    people = [{'id': meta.get('author_id'), 'name': meta.get('author_name')}]
    people += [m for m in meta.get('mentions', []) if not m.get('is_self') and not m.get('bot') and m.get('id') != robot_id]
    for person in people:
        mid = identifier(person.get('id'))
        if mid:
            conn.execute('INSERT INTO group_members(group_id, member_id, platform_name) VALUES(?, ?, ?) ON CONFLICT(group_id, member_id) DO UPDATE SET platform_name=CASE WHEN excluded.platform_name != \'\' THEN excluded.platform_name ELSE group_members.platform_name END, updated=CURRENT_TIMESTAMP', (gid, mid, name(person.get('name'))))


def profile(conn, gid, limit=None):
    row = conn.execute('SELECT platform_name, alias FROM group_profiles WHERE group_id=?', (gid,)).fetchone() or ('', '')
    sql = 'SELECT member_id, platform_name, alias FROM group_members WHERE group_id=? ORDER BY updated DESC, member_id'
    args = (gid,)
    if limit is not None:
        sql += ' LIMIT ?'
        args += (limit,)
    members = [{'id': mid, 'platform_name': platform, 'alias': alias, 'name': alias or platform}
               for mid, platform, alias in conn.execute(sql, args)]
    return {'group_id': gid, 'platform_name': row[0], 'alias': row[1], 'name': row[1] or row[0], 'members': members}


def format_message(conn, gid, uid, text, mentions=(), robot_id=''):
    # Keep the stable ID prefix for old history and distinguish it from user text.
    prefix = f'[群成员 {uid}]'
    row = conn.execute('SELECT platform_name, alias FROM group_members WHERE group_id=? AND member_id=?', (gid, uid)).fetchone()
    if row and (row[1] or row[0]):
        prefix += ' 昵称=' + json.dumps(row[1] or row[0], ensure_ascii=False)
    targets = []
    for item in mentions:
        mid = identifier(item.get('id'))
        if not mid:
            continue
        row = conn.execute('SELECT platform_name, alias FROM group_members WHERE group_id=? AND member_id=?', (gid, mid)).fetchone()
        display = (row[1] or row[0]) if row else name(item.get('name'))
        targets.append({'id': mid, 'name': display or '未知', 'is_bot': bool(item.get('is_self') or item.get('bot') or mid == robot_id)})
    if targets:
        prefix += '\n本条消息明确 @ 的对象（资料）：' + json.dumps(targets, ensure_ascii=False)
    # Do not guess identities from plain @nickname text: names can be duplicated.
    return prefix + '\n消息正文：' + text.strip()


def seed_history(conn):
    for chat, raw in conn.execute("SELECT chat, content FROM messages WHERE role='user' AND chat LIKE '群聊:%'").fetchall():
        gid = identifier(chat[3:])
        if not gid:
            continue
        try:
            content = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(content, list):
            content = next((b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text'), '')
        match = re.match(r'\[群成员 ([A-Za-z0-9_-]{1,128})\]', content) if isinstance(content, str) else None
        remember(conn, {'group_id': gid, 'author_id': match[1] if match else ''})
