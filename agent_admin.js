(() => {
  const el = id => document.getElementById(id);
  let loaded = '', busy = false, generation = 0, selectedRun = '', activeRun = '', lastDetail = '', timer, disconnected = false;
  const labels = {running:'执行中', completed:'已完成', failed:'失败', cancelled:'已停止',
    timeout:'超时', limit:'达到上限', interrupted:'重启中断', done:'成功', unknown:'结果未知',
    pending:'等待发送', uploading:'正在上传', sending:'正在发送', sent:'已发送', skipped:'未发送'};
  async function request(url, data) {
    let r;
    try { r = await fetch(url, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}); }
    catch { disconnected = true; throw Error('无法连接机器人后端。请启动“启动机器人.bat”，确认管理页面地址为 http://127.0.0.1:8766，再刷新页面。未成功保存的设置不会生效。'); }
    if (!r.ok) { const text = await r.text(); throw Error(text); }
    if (disconnected) { disconnected = false; el('agentStatus').textContent = '已重新连接后端，请确认设置已保存。'; }
    return r.json();
  }
  function lock(value) {
    busy = value;
    for (const control of document.querySelectorAll('.agent-panel input,.agent-panel select,.agent-panel textarea,.agent-panel button')) control.disabled = value;
    el('agentFields').disabled = value || !loaded;
    el('agentStop').disabled = value || !activeRun;
  }
  function reset() {
    loaded = ''; selectedRun = ''; activeRun = ''; lastDetail = ''; generation++; clearTimeout(timer);
    el('agentFields').disabled = true; el('agentStop').disabled = true;
    el('agentRecent').replaceChildren(); el('agentDetails').replaceChildren();
    el('agentStatus').textContent = '请点击“读取设置”后再编辑当前会话。';
  }
  async function targets() {
    const data = await request('/api/proactive/targets');
    const selected = el('agentPicker').value;
    el('agentPicker').replaceChildren(new Option('请选择会话，也可手动填写下方会话 ID', ''));
    for (const item of data.targets) el('agentPicker').append(new Option(item.label, item.chat));
    el('agentPicker').value = selected;
  }
  async function details(id, token = generation) {
    const run = await request('/api/agent/runs/'+encodeURIComponent(id));
    if (token !== generation || selectedRun !== id) return;
    const signature = JSON.stringify(run);
    if (signature === lastDetail) return;
    lastDetail = signature;
    const out = el('agentDetails'); out.replaceChildren();
    const heading = document.createElement('p');
    heading.textContent = `任务 ${run.id.slice(0,8)} · ${labels[run.status] || run.status} · 工作目录：${run.workspace}`;
    const task = document.createElement('pre'); task.textContent = '任务：'+run.request;
    out.append(heading, task);
    for (const call of run.calls) {
      const fold = document.createElement('details'), title = document.createElement('summary'), body = document.createElement('pre');
      title.textContent = `${call.step}. ${call.tool} · ${labels[call.status] || call.status}`;
      body.textContent = JSON.stringify({参数:call.arguments, 结果:call.result}, null, 2);
      fold.append(title, body); out.append(fold);
    }
    const result = document.createElement('pre'); result.textContent = run.response || '正在处理；工具执行完成后显示返回结果。';
    out.append(result);
    for (const item of run.images || []) {
      const figure = document.createElement('figure'), img = document.createElement('img'), caption = document.createElement('figcaption');
      img.src = '/api/agent/images/'+encodeURIComponent(item.id); img.alt = item.name; img.loading = 'lazy';
      img.style.cssText = 'max-width:100%;max-height:360px;object-fit:contain';
      figure.style.margin = '16px 0';
      caption.textContent = `${item.name} · ${labels[item.status] || item.status}${item.detail ? ' · '+item.detail : ''}`;
      figure.append(img,caption); out.append(figure);
    }
    if (run.delivery === 'failed') {
      const delivery = document.createElement('p'); delivery.textContent = 'QQ 结果发送失败，完整任务结果已保存在这里。'; out.append(delivery);
    }
  }
  function renderRuns(data) {
    activeRun = data.active?.chat === loaded ? data.active.id : '';
    el('agentStop').disabled = busy || !activeRun;
    el('agentRunning').textContent = activeRun ? '当前会话正在执行任务 '+activeRun.slice(0,8) : data.active ? '另一会话正在执行任务，结束后可发起新任务。' : '当前没有正在执行的 Agent 任务。';
    el('agentRecent').replaceChildren();
    for (const run of data.recent) {
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = `${run.id.slice(0,8)} · ${labels[run.status] || run.status} · ${new Date(run.created*1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false})}（北京时间）`;
      button.onclick = () => { selectedRun = run.id; details(run.id).catch(showError); };
      el('agentRecent').append(button);
    }
    if (!data.recent.length) el('agentRecent').textContent = '暂无任务。保存设置后，在 QQ 私聊或群里 @ 机器人交代任务。';
    if (!selectedRun && data.recent.length) selectedRun = data.recent[0].id;
  }
  function showError(error) { el('agentStatus').textContent = error.message; }
  async function status() {
    if (!loaded || busy) return;
    const chat = loaded, token = generation;
    try {
      const data = await request('/api/agent?chat='+encodeURIComponent(chat));
      if (token !== generation || chat !== loaded) return;
      renderRuns(data);
      if (selectedRun) await details(selectedRun, token);
    } catch (e) { if (token === generation) showError(e); }
    finally { if (token === generation && loaded) { clearTimeout(timer); timer = setTimeout(status, 2500); } }
  }
  async function load() {
    if (busy) return;
    reset(); const chat = el('agentChat').value.trim();
    if (!/^(私聊|群聊):[A-Za-z0-9_-]{1,128}$/.test(chat)) { showError(Error('请填写 私聊:用户OpenID 或 群聊:群OpenID')); return; }
    lock(true);
    try {
      const data = await request('/api/agent?chat='+encodeURIComponent(chat)); loaded = chat;
      const cfg = data.config;
      el('agentMemberPicker').replaceChildren(new Option('选择成员，将真实 OpenID 加入授权列表',''));
      for (const member of data.members || []) el('agentMemberPicker').append(new Option((member.name || '未命名成员')+' · '+member.id,member.id));
      el('agentEnabled').checked = cfg.enabled; el('agentTrigger').value = cfg.trigger;
      el('agentWorkspace').value = cfg.workspace;
      el('agentUsers').value = cfg.allowed_users.length ? cfg.allowed_users.join('\n') : chat.startsWith('私聊:') ? chat.slice(3) : '';
      el('agentSteps').value = cfg.max_steps; el('agentTimeout').value = cfg.task_timeout;
      renderRuns(data); if (selectedRun) await details(selectedRun);
      el('agentStatus').textContent = '已读取设置。修改后点击“保存 Agent 设置”生效。';
    } catch(e) { showError(e); } finally { lock(false); if (loaded) timer = setTimeout(status, 2500); }
  }
  el('agentPicker').onchange = () => { el('agentChat').value = el('agentPicker').value; load(); };
  el('agentChat').oninput = () => { el('agentPicker').value = ''; reset(); };
  el('agentLoad').onclick = load;
  el('agentTargets').onclick = () => targets().catch(showError);
  el('agentAddMember').onclick = () => {
    const id = el('agentMemberPicker').value;
    if (!id) return;
    const users = new Set(el('agentUsers').value.split(/[\s,，]+/).filter(Boolean)); users.add(id);
    el('agentUsers').value = [...users].join('\n');
    el('agentStatus').textContent = '已加入真实 OpenID，请保存 Agent 设置。';
  };
  el('agentPickFolder').onclick = async () => {
    if (busy || !loaded) return;
    lock(true); el('agentStatus').textContent = '请在运行机器人的电脑上选择文件夹；也可以取消后手动输入路径。';
    try {
      const data = await request('/api/agent/pick-folder', {initial_path:el('agentWorkspace').value.trim()});
      if (data.path) el('agentWorkspace').value = data.path;
      el('agentStatus').textContent = data.cancelled ? '已取消选择，原路径保留。' : '已选择文件夹，请保存 Agent 设置。';
    } catch(e) { showError(e); } finally { lock(false); timer = setTimeout(status, 2500); }
  };
  el('agentForm').onsubmit = async event => {
    event.preventDefault(); if (busy || !loaded || el('agentChat').value.trim() !== loaded) return;
    const config = {enabled:el('agentEnabled').checked, trigger:el('agentTrigger').value, workspace:el('agentWorkspace').value.trim(),
      allowed_users:el('agentUsers').value.split(/[\s,，]+/).filter(Boolean), max_steps:Number(el('agentSteps').value), task_timeout:Number(el('agentTimeout').value)};
    lock(true);
    try { await request('/api/agent', {chat:loaded, config}); el('agentStatus').textContent = '已保存，下一条 QQ 消息生效。'; await targets(); }
    catch(e) { showError(e); } finally { lock(false); timer = setTimeout(status, 2500); }
  };
  el('agentStop').onclick = async () => {
    if (!activeRun || busy) return;
    try { const result = await request('/api/agent/stop', {chat:loaded, run_id:activeRun}); el('agentStatus').textContent = result.stopping ? '正在停止命令和任务，已经写入的文件会保留。' : '任务已经结束。'; await status(); }
    catch(e) { showError(e); }
  };
  el('agentRefresh').onclick = status;
  targets().catch(showError);
})();
