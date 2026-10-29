(() => {
  const numbers = ['debounce_seconds', 'cooldown_seconds', 'hourly_limit', 'daily_limit', 'interval_seconds'];
  const switches = ['auto_enabled', 'periodic_enabled', 'quiet_enabled'];
  let loaded = '', busy = false, timer = null;
  const target = () => $('proactiveScope').value + ':' + $('proactiveId').value.trim();
  const tell = text => { $('proactiveStatus').textContent = text; };
  function lock(value) {
    busy = value;
    for (const el of document.querySelectorAll('.proactive-panel button,.proactive-panel select')) el.disabled = value;
    $('proactiveId').disabled = value;
    $('proactiveFields').disabled = value || !loaded;
    $('proactiveManual').disabled = value || !loaded;
    $('auto_enabled').disabled = $('proactiveScope').value === '私聊';
  }
  function online(value) { $('proactiveOnline').textContent = value ? '主动发言任务正在运行' : '主动发言任务未运行：设置可保存，发送需先启动机器人并连接QQ。'; }
  async function targets() {
    const d = await api('/api/proactive/targets');
    online(d.online);
    const selected = $('proactivePicker').value;
    $('proactivePicker').replaceChildren(new Option('请选择会话，或手动填写目标 OpenID', ''));
    for (const item of d.targets) $('proactivePicker').append(new Option(item.label, item.chat));
    $('proactivePicker').value = selected;
  }
  function renderStatus(d) {
    online(d.online);
    tell(d.state.blocked || d.state.status);
    $('proactiveRecent').replaceChildren();
    const kinds = {auto: '自动接话', periodic: '定时发言', manual: '手动发送'};
    const states = {queued:'等待发送', sending:'正在发送', sent:'发送成功', failed:'发送未确认', unknown:'送达未知', cancelled:'已取消'};
    for (const item of d.recent) {
      const row = document.createElement('div');
      row.textContent = new Date(item.time * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) + ' · ' + kinds[item.kind] + ' · ' + (states[item.status] || item.status) + (item.detail ? ' · ' + item.detail : '');
      $('proactiveRecent').append(row);
    }
  }
  function invalidate() {
    loaded = ''; clearTimeout(timer); lock(false);
    $('proactiveRecent').replaceChildren(); $('proactiveContent').value = '';
    tell('请读取当前会话设置后再保存或发送。');
  }
  async function read() {
    if (busy) return;
    const chat = target();
    lock(true);
    try {
      const d = await api('/api/proactive?chat=' + encodeURIComponent(chat));
      loaded = chat;
      for (const id of numbers) $(id).value = d.config[id];
      for (const id of switches) $(id).checked = d.config[id];
      $('quiet_start').value = d.config.quiet_start;
      $('quiet_end').value = d.config.quiet_end;
      $('proactiveTopic').value = d.config.topic;
      renderStatus(d);
    } catch (e) { loaded = ''; tell('读取失败：' + e.message); }
    finally { lock(false); }
  }
  async function status() {
    if (!loaded || busy) return;
    const chat = loaded;
    try {
      const d = await api('/api/proactive?chat=' + encodeURIComponent(chat));
      if (loaded !== chat) return;
      renderStatus(d);
      // Poll only while an explicitly submitted message is still in flight.
      if (d.recent.some(item => ['queued', 'sending'].includes(item.status))) timer = setTimeout(status, 2000);
    } catch (e) { tell('状态读取失败：' + e.message); }
  }
  $('proactivePicker').onchange = async () => {
    invalidate();
    const chat = $('proactivePicker').value;
    if (!chat) return;
    $('proactiveScope').value = chat.slice(0, 2); $('proactiveId').value = chat.slice(3);
    await read();
  };
  $('proactiveScope').onchange = invalidate;
  $('proactiveId').oninput = invalidate;
  $('proactiveLoad').onclick = read;
  $('proactiveRefreshTargets').onclick = () => targets().catch(e => tell(e.message));
  $('proactiveRefreshStatus').onclick = status;
  $('proactiveForm').onsubmit = async e => {
    e.preventDefault(); if (busy || !loaded || loaded !== target()) return;
    const config = {};
    for (const id of numbers) config[id] = Number($(id).value);
    for (const id of switches) config[id] = $(id).checked;
    config.quiet_start = $('quiet_start').value; config.quiet_end = $('quiet_end').value;
    config.topic = $('proactiveTopic').value.trim();
    lock(true);
    try {
      await api('/api/proactive', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat:loaded,config})});
      tell('已保存，运行中的机器人将自动应用；定时模式从现在起重新计时。');
      await targets();
    } catch (e) { tell('保存失败：' + e.message); }
    finally { lock(false); }
  };
  $('proactiveSend').onclick = async () => {
    if (busy || !loaded || loaded !== target()) return;
    const content = $('proactiveContent').value.trim();
    if (!content) { tell('请填写发送内容'); return; }
    lock(true);
    try {
      await api('/api/proactive/send', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat:loaded,content})});
      $('proactiveContent').value = '';
      tell('已排队，正在等待QQ确认发送结果…');
      clearTimeout(timer); timer = setTimeout(status, 1000);
    } catch (e) { tell('发送失败：' + e.message); }
    finally { lock(false); }
  };
  targets().catch(e => tell('会话列表读取失败：' + e.message));
})();
