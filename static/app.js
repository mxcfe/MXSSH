const status = document.querySelector('#status');
const message = document.querySelector('#message');
const disconnectButton = document.querySelector('#disconnect');
const sessions = [];
const terminalThemes = {
  dark: { background: '#0a0f15', foreground: '#e8edf5', cursor: '#a1eac5', selectionBackground: '#496c5c' },
  light: {
    background: '#ffffff', foreground: '#25343b', cursor: '#176c4a', selectionBackground: '#c6ead6',
    black: '#27343a', red: '#b43d45', green: '#267947', yellow: '#8a6500',
    blue: '#315fba', magenta: '#8c4c9c', cyan: '#157b85', white: '#68777d',
    brightBlack: '#627078', brightRed: '#bc3f4a', brightGreen: '#267947', brightYellow: '#8a6500',
    brightBlue: '#315fba', brightMagenta: '#8c4c9c', brightCyan: '#157b85', brightWhite: '#404e55',
  },
};
const themeToggle = document.querySelector('#theme-toggle');
function applyTheme(theme) {
  const isLight = theme === 'light';
  document.documentElement.dataset.theme = isLight ? 'light' : 'dark';
  themeToggle.textContent = isLight ? '☾ 深色主题' : '☼ 浅色主题';
  themeToggle.setAttribute('aria-pressed', String(isLight));
  for (const session of sessions) session.term.options.theme = terminalThemes[isLight ? 'light' : 'dark'];
}
applyTheme(document.documentElement.dataset.theme);
themeToggle.addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  applyTheme(theme);
  try { localStorage.setItem('mxssh-theme', theme); } catch (_) { /* Theme still applies for this page. */ }
});
let activeSession = null;
const terminalContainer = document.querySelector('#terminal');
const terminalColumn = document.querySelector('#terminal-column');
const terminalKeys = document.querySelector('#terminal-keys');
const keysToggle = document.querySelector('#keys-toggle');
const terminalWindow = document.querySelector('#terminal-window');
const fullscreenToggle = document.querySelector('#fullscreen-toggle');
const sessionTabs = document.querySelector('#session-tabs');
const terminalEmpty = document.querySelector('#terminal-empty');
const savedForm = document.querySelector('#saved-form');
const connectionsList = document.querySelector('#connections-list');
const editorOverlay = document.querySelector('#editor-overlay');
const editorMessage = document.querySelector('#editor-message');
const connectionSearch = document.querySelector('#connection-search');
const clearTerminal = document.querySelector('#clear-terminal');
const terminalStatus = document.querySelector('#terminal-status');
const filePanel = document.querySelector('#files-view');
const terminalBody = document.querySelector('#terminal-body');
const filePanelToggle = document.querySelector('#file-panel-toggle');
const terminalPanelToggle = document.querySelector('#terminal-panel-toggle');
let splitFilesOpen = false;
const sessionStorageKey = 'mxssh-live-sessions';
const sharedSessionPrefix = 'mxssh-session:';
let restoringSessions = true;
function saveSharedSession(session) {
  try { localStorage.setItem(sharedSessionPrefix + session.resumeToken, JSON.stringify({ name: session.name, token: session.resumeToken, mode: session.initialMode })); } catch (_) {}
}
function removeSharedSession(token) {
  if (!token) return;
  try { localStorage.removeItem(sharedSessionPrefix + token); } catch (_) {}
}
function persistSessions() {
  if (restoringSessions) return;
  try {
    sessionStorage.setItem(sessionStorageKey, JSON.stringify({
      sessions: sessions.filter(session => session.resumeToken).map(session => ({ name: session.name, token: session.resumeToken, filesOpen: session.filesOpen, terminalOpen: session.terminalOpen, filePath: session.filePath, keysOpen: session.keysOpen })),
      active: activeSession?.resumeToken || null,
      view: currentView,
    }));
  } catch (_) { /* The current session still works without browser storage. */ }
}
let savedConnections = [];
let terminalFontSize = 14;
const pageNames = { connections: '连接管理', 'terminal-view': '终端会话', 'settings-view': '设置' };
let currentView = 'connections';
let editingId = null;
let editorBusy = false;

function filePanelVisible() {
  return currentView === 'terminal-view' && splitFilesOpen;
}

function setSplitFiles(open) {
  splitFilesOpen = open;
  filePanel.hidden = !open;
  terminalBody.classList.toggle('split', open);
  filePanelToggle.setAttribute('aria-expanded', String(open));
}

function setSessionPanels(session) {
  const terminalOpen = !session || session.terminalOpen;
  const filesOpen = Boolean(session && session.filesOpen);
  terminalColumn.hidden = !terminalOpen;
  terminalWindow.classList.toggle('terminal-hidden', !terminalOpen);
  const keysOpen = Boolean(session && session.keysOpen);
  terminalKeys.hidden = !keysOpen;
  keysToggle.disabled = !session;
  keysToggle.setAttribute('aria-expanded', String(keysOpen));
  setSplitFiles(filesOpen);
  terminalPanelToggle.setAttribute('aria-expanded', String(terminalOpen));
  terminalPanelToggle.disabled = !session || (terminalOpen && !filesOpen);
  filePanelToggle.disabled = !session || (filesOpen && !terminalOpen);
  terminalPanelToggle.title = terminalPanelToggle.disabled && session ? '至少保留一个面板' : '显示或隐藏终端';
  filePanelToggle.title = filePanelToggle.disabled && session ? '至少保留一个面板' : '显示或隐藏文件';
  if (session) updateSessionLabel(session);
}

function fitVisibleTerminal(focus = false) {
  if (!activeSession || currentView !== 'terminal-view' || !activeSession.terminalOpen) return;
  activeSession.fit.fit();
  if (focus) activeSession.term.focus();
}

function showView(id) {
  if (!pageNames[id]) return;
  if (id !== 'terminal-view') setSplitFiles(false);
  if (id !== 'terminal-view' && document.fullscreenElement === terminalWindow) {
    document.exitFullscreen().catch(() => {});
  }
  currentView = id;
  persistSessions();
  document.querySelector('.main-area').classList.toggle('terminal-active', id === 'terminal-view');
  document.querySelectorAll('.view').forEach(view => { view.hidden = view.id !== id; });
  if (id === 'terminal-view') setSessionPanels(activeSession);
  document.querySelectorAll('.nav-item').forEach(button => {
    const selected = button.dataset.view === id;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  for (const session of sessions) {
    const selected = id === 'terminal-view' && session === activeSession;
    session.tab.classList.toggle('active', selected);
    if (selected) session.tabLabel.setAttribute('aria-current', 'page');
    else session.tabLabel.removeAttribute('aria-current');
  }
  document.querySelector('#page-name').textContent = pageNames[id];
  if (id === 'settings-view') loadSettings();
  if (filePanelVisible()) refreshFilePanel();
  if (id === 'terminal-view') fitVisibleTerminal(true);
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => showView(button.dataset.go)));
showView('connections');

function closeEditor() {
  if (editorBusy) return;
  editorOverlay.hidden = true;
  savedForm.hidden = true;
  savedForm.reset();
  editorMessage.textContent = '';
  editingId = null;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : '请求失败，请检查填写内容');
  }
  return response.json();
}

const settingsForm = document.querySelector('#settings-form');
const settingsPort = document.querySelector('#settings-port');
const settingsSave = document.querySelector('#settings-save');
const settingsMessage = document.querySelector('#settings-message');
let settingsRequestVersion = 0;
let settingsSaveInFlight = false;

function displaySettings(settings, saved = false) {
  settingsPort.value = settings.port;
  document.querySelector('#settings-active-port').textContent = settings.active_port;
  settingsMessage.dataset.state = settings.restart_required ? 'pending' : 'success';
  settingsMessage.textContent = settings.restart_required
    ? `已保存端口 ${settings.port}，请退出并重新启动 MXSSH 后生效。`
    : (saved ? '设置已保存，当前端口无需重启。' : '');
}

async function loadSettings() {
  if (settingsSaveInFlight) return;
  const version = ++settingsRequestVersion;
  settingsPort.disabled = true;
  settingsSave.disabled = true;
  settingsMessage.dataset.state = '';
  settingsMessage.textContent = '正在读取设置…';
  try {
    const settings = await api('/api/settings');
    if (version !== settingsRequestVersion) return;
    displaySettings(settings);
    settingsPort.disabled = false;
    settingsSave.disabled = false;
  } catch (error) {
    if (version !== settingsRequestVersion) return;
    settingsMessage.dataset.state = 'error';
    settingsMessage.textContent = `读取设置失败：${error.message}`;
  }
}

settingsForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!settingsForm.reportValidity() || settingsSave.disabled) return;
  const port = Number(settingsPort.value);
  const version = ++settingsRequestVersion;
  settingsSaveInFlight = true;
  settingsPort.disabled = true;
  settingsSave.disabled = true;
  settingsMessage.dataset.state = '';
  settingsMessage.textContent = '正在保存设置…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ port }),
    });
    if (version === settingsRequestVersion) displaySettings(settings, true);
  } catch (error) {
    if (version === settingsRequestVersion) {
      settingsMessage.dataset.state = 'error';
      settingsMessage.textContent = `保存设置失败：${error.message}`;
    }
  } finally {
    settingsSaveInFlight = false;
    if (version === settingsRequestVersion) { settingsPort.disabled = false; settingsSave.disabled = false; }
  }
});

function showEditor(item = null) {
  editingId = item ? item.id : null;
  savedForm.reset();
  for (const field of ['name', 'host', 'port', 'username']) {
    if (item) savedForm.elements[field].value = item[field];
  }
  savedForm.elements.password.required = !item;
  document.querySelector('#password-hint').textContent = item
    ? '留空则保留原密码；密码以明文保存在本机数据库中。'
    : '密码以明文保存在本机数据库中。';
  document.querySelector('#editor-title').textContent = item ? '编辑连接' : '新建连接';
  editorMessage.textContent = '';
  editorMessage.dataset.state = '';
  editorOverlay.hidden = false;
  savedForm.hidden = false;
  savedForm.elements.name.focus();
}

async function loadConnections() {
  try {
    savedConnections = await api('/api/connections');
    renderConnections();
  } catch (error) { message.textContent = `加载连接失败：${error.message}`; }
}

function renderConnections() {
    const query = connectionSearch.value.trim().toLocaleLowerCase();
    const connections = savedConnections.filter(item =>
      [item.name, item.host, item.username, String(item.port)].some(value => value.toLocaleLowerCase().includes(query))
    );
    document.querySelector('#connection-count').textContent = query
      ? `${connections.length} / ${savedConnections.length} 个连接` : `${savedConnections.length} 个连接`;
    connectionsList.replaceChildren();
    if (!connections.length) {
      const empty = document.createElement('span');
      empty.className = 'empty';
      empty.textContent = query ? '没有匹配的连接。' : '暂无已保存的连接，点击“新建连接”开始。';
      connectionsList.append(empty);
    }
    for (const item of connections) {
      const row = document.createElement('div');
      row.className = 'connection-row';
      const info = document.createElement('div');
      info.className = 'connection-info';
      const avatar = document.createElement('span');
      avatar.className = 'connection-avatar';
      avatar.textContent = '>_';
      const copy = document.createElement('div');
      copy.className = 'connection-copy';
      const name = document.createElement('strong');
      name.textContent = item.name;
      const address = document.createElement('small');
      address.textContent = `${item.username}@${item.host}:${item.port}`;
      copy.append(name, address);
      info.append(avatar, copy);
      const actions = document.createElement('div');
      actions.className = 'row-actions';
      for (const [label, action] of [
        ['终端', () => startConnection({ connection_id: item.id }, item.name, 'terminal')],
        ['文件', () => startConnection({ connection_id: item.id }, item.name, 'files')],
        ['编辑', () => showEditor(item)],
        ['删除', async () => {
          if (!confirm(`确定删除连接“${item.name}”吗？`)) return;
          try {
            await api(`/api/connections/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
            if (editingId === item.id) closeEditor();
            await loadConnections();
          } catch (error) { message.textContent = error.message; }
        }],
      ]) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.addEventListener('click', action);
        actions.append(button);
      }
      row.append(info, actions);
      connectionsList.append(row);
    }
}
connectionSearch.addEventListener('input', renderConnections);

document.querySelector('#new-connection').addEventListener('click', () => showEditor());
document.querySelector('#cancel-edit').addEventListener('click', closeEditor);
document.querySelector('#close-editor').addEventListener('click', closeEditor);
editorOverlay.addEventListener('click', event => { if (event.target === editorOverlay) closeEditor(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !editorOverlay.hidden) closeEditor(); });
document.addEventListener('keydown', event => {
  if (!editorOverlay.hidden || event.target.closest('input, textarea, select, .xterm') || !event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const view = { '1': 'connections', '2': 'terminal-view' }[event.key];
  if (view) { event.preventDefault(); showView(view); }
});

function askHostKey(key) {
  const previous = key.previous && key.previous.length
    ? `\n\n警告：该主机已有不同的受信任密钥：\n${key.previous.join('\n')}` : '';
  return confirm(`服务器 ${key.host}:${key.port} 的主机密钥尚未受信任。${previous}\n\n算法：${key.algorithm}\n指纹：${key.fingerprint}\n\n请通过可信渠道核对指纹。确认信任此密钥并继续吗？`);
}

function setEditorBusy(busy) {
  editorBusy = busy;
  for (const element of savedForm.elements) element.disabled = busy;
  document.querySelector('#close-editor').disabled = busy;
}

async function testConnection(data, id) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const result = await api('/api/connections/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...data, connection_id: id }),
    });
    if (result.status !== 'host_key') return result;
    if (!askHostKey(result.host_key)) return { status: 'error', message: '未信任服务器主机密钥' };
    await api('/api/host-keys/confirm', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ challenge: result.host_key.challenge }),
    });
  }
  return { status: 'error', message: '服务器主机密钥反复变化，请核对后重试' };
}

document.querySelector('#test-connection').addEventListener('click', async () => {
  if (editorBusy || !savedForm.reportValidity()) return;
  const data = Object.fromEntries(new FormData(savedForm).entries());
  const id = editingId;
  setEditorBusy(true);
  editorMessage.textContent = '正在测试连接…';
  editorMessage.dataset.state = 'pending';
  try {
    const result = await testConnection(data, id);
    editorMessage.dataset.state = result.status === 'success' ? 'success' : 'error';
    editorMessage.textContent = result.status === 'success' ? '连接测试成功。' : `连接测试失败：${result.message || '未知错误'}`;
  } catch (error) { editorMessage.dataset.state = 'error'; editorMessage.textContent = `连接测试失败：${error.message}`; }
  finally { setEditorBusy(false); }
});

savedForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (editorBusy) return;
  const id = editingId;
  const data = Object.fromEntries(new FormData(savedForm).entries());
  setEditorBusy(true);
  editorMessage.textContent = '正在测试连接…';
  editorMessage.dataset.state = 'pending';
  try {
    let test;
    try { test = await testConnection(data, id); }
    catch (error) { test = { status: 'error', message: error.message }; }
    if (test.status !== 'success' && !confirm(`连接测试失败：${test.message || '未知错误'}\n\n是否仍然保存此连接？`)) {
      editorMessage.textContent = '已取消保存。';
      editorMessage.dataset.state = '';
      return;
    }
    await api(id ? `/api/connections/${encodeURIComponent(id)}` : '/api/connections', {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    setEditorBusy(false);
    closeEditor();
    message.textContent = '连接已保存。';
    await loadConnections();
  } catch (error) { editorMessage.dataset.state = 'error'; editorMessage.textContent = `保存失败：${error.message}`; }
  finally { setEditorBusy(false); }
});
loadConnections();

if (!window.Terminal || !window.FitAddon) {
  message.textContent = '终端组件加载失败，请检查浏览器是否能访问 jsDelivr。';
}

function updateSessionLabel(session) {
  const mode = session.terminalOpen ? (session.filesOpen ? '分屏' : '终端') : '文件';
  session.tabLabel.textContent = `${mode} · ${session.name} · ${session.state}`;
  session.tabLabel.title = session.tabLabel.textContent;
}

function refreshSession(session) {
  updateSessionLabel(session);
  if (session === activeSession) {
    terminalKeys.querySelectorAll('button').forEach(button => { button.disabled = session.state !== '已连接' || session.socket.readyState !== WebSocket.OPEN; });
    status.textContent = session.state;
    status.parentElement.dataset.state = session.state;
    message.textContent = session.message;
    terminalStatus.textContent = session.message.startsWith('连接错误：') || session.message.startsWith('WebSocket')
      ? session.message : `${session.name} · ${session.state}`;
    terminalStatus.title = terminalStatus.textContent;
    disconnectButton.disabled = session.socket.readyState >= WebSocket.CLOSING;
  }
}

function activateSession(session) {
  activeSession = session;
  persistSessions();
  if (currentView === 'terminal-view') setSessionPanels(session);
  for (const item of sessions) {
    item.pane.hidden = item !== session;
    const selected = item === session && currentView === 'terminal-view';
    item.tab.classList.toggle('active', selected);
    if (selected) item.tabLabel.setAttribute('aria-current', 'page');
    else item.tabLabel.removeAttribute('aria-current');
  }
  terminalEmpty.hidden = Boolean(session);
  if (filePanelVisible()) refreshFilePanel();
  if (session) {
    clearTerminal.disabled = false;
    refreshSession(session);
    fitVisibleTerminal(true);
  } else {
    status.textContent = '未连接';
    status.parentElement.dataset.state = '未连接';
    message.textContent = '请从连接管理选择服务器。';
    terminalStatus.textContent = '无活动会话';
    clearTerminal.disabled = true;
    disconnectButton.disabled = true;
  }
}

function closeSession(session) {
  const index = sessions.indexOf(session);
  if (index < 0) return;
  disconnectSession(session);
  sessions.splice(index, 1);
  persistSessions();
  session.term.dispose();
  session.tab.remove();
  session.pane.remove();
  if (activeSession === session) {
    activateSession(sessions[Math.min(index, sessions.length - 1)] || null);
    if (!sessions.length && currentView === 'terminal-view') showView('connections');
  }
  else if (filePanelVisible()) refreshFilePanel();
}

function startConnection(options, name, initialMode = 'terminal', layout = null) {
  if (!window.Terminal || !window.FitAddon) return;
  showView('terminal-view');
  // xterm must be opened in a measurable container even if the previous session hid it.
  terminalColumn.hidden = false;
  const url = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`;
  const socket = new WebSocket(url);
  socket.binaryType = 'arraybuffer';
  const pane = document.createElement('div');
  pane.className = 'terminal-pane';
  terminalContainer.append(pane);
  const term = new Terminal({ cursorBlink: true, fontSize: terminalFontSize, fontFamily: 'Consolas, monospace', theme: terminalThemes[document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'] });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(pane);
  const tab = document.createElement('div');
  tab.className = 'session-tab';
  const tabLabel = document.createElement('button');
  tabLabel.type = 'button';
  tabLabel.className = 'tab-label';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'close-tab';
  close.textContent = '×';
  close.setAttribute('aria-label', `关闭 ${name} 会话`);
  tab.append(tabLabel, close);
  sessionTabs.append(tab);
  const validLayout = layout && typeof layout.filesOpen === 'boolean' && typeof layout.terminalOpen === 'boolean' && (layout.filesOpen || layout.terminalOpen);
  const session = { name, socket, term, fit, pane, tab, tabLabel, initialMode, state: '连接中', message: '正在连接 SSH 服务器…', filePath: validLayout && typeof layout.filePath === 'string' ? layout.filePath : '.', filesOpen: validLayout ? layout.filesOpen : initialMode === 'files', terminalOpen: validLayout ? layout.terminalOpen : initialMode !== 'files', keysOpen: Boolean(validLayout && layout.keysOpen), terminalPath: null, fileToken: null, resumeToken: options.session_token || null, manualDisconnect: false };
  term.parser.registerOscHandler(7, data => {
    try {
      const url = new URL(data);
      if (url.protocol !== 'file:' || url.search || url.hash) return false;
      const path = decodeURIComponent(url.pathname);
      if (!path.startsWith('/') || path.includes('\0')) return false;
      session.terminalPath = path;
      if (session === activeSession && filePanelVisible()) updateCwdControls(session);
      return true;
    } catch (_) { return false; }
  });
  sessions.push(session);
  tabLabel.addEventListener('click', () => { showView('terminal-view'); activateSession(session); });
  close.addEventListener('click', () => closeSession(session));
  term.onData(data => {
    if (socket.readyState === WebSocket.OPEN) socket.send(new TextEncoder().encode(data));
  });
  term.onResize(({ cols, rows }) => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', cols, rows }));
  });
  activateSession(session);
  socket.onopen = () => {
    socket.send(JSON.stringify({ type: options.session_token ? 'resume' : 'connect', ...options, cols: term.cols, rows: term.rows }));
  };
  socket.onmessage = event => {
    if (!sessions.includes(session)) return;
    if (typeof event.data !== 'string') { term.write(new Uint8Array(event.data)); return; }
    const data = JSON.parse(event.data);
    if (data.type === 'host_key') {
      const trust = askHostKey(data.host_key);
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'host_key_response', trust }));
    } else if (data.type === 'connected') {
      session.fileToken = data.session_token;
      session.resumeToken = data.session_token;
      saveSharedSession(session);
      session.state = '已连接'; session.message = '连接成功。';
      persistSessions();
      if (session === activeSession && currentView === 'terminal-view' && session.terminalOpen) term.focus();
      if (session === activeSession && filePanelVisible()) refreshFilePanel(splitFilesOpen && !options.session_token);
    } else if (data.type === 'error') {
      session.message = `连接错误：${data.message}`;
      if (options.session_token) { removeSharedSession(session.resumeToken); session.resumeToken = null; persistSessions(); }
    } else if (data.type === 'session_closed') {
      removeSharedSession(session.resumeToken);
      session.resumeToken = null;
      session.fileToken = null;
      session.manualDisconnect = true;
      session.message = 'SSH 会话已结束。';
      persistSessions();
    }
    refreshSession(session);
  };
  socket.onerror = () => { if (sessions.includes(session)) { session.message = 'WebSocket 连接失败，请检查服务状态。'; refreshSession(session); } };
  socket.onclose = () => {
    session.state = '已断开';
    session.fileToken = null;
    if (session.message === '连接成功。' || session.message === '正在连接 SSH 服务器…') {
      session.message = session.manualDisconnect ? '连接已断开。' : '连接中断，可在 1 分钟内刷新页面恢复。';
    }
    if (sessions.includes(session)) refreshSession(session);
    if (sessions.includes(session) && filePanelVisible()) refreshFilePanel();
  };
}
function disconnectSession(session) {
  session.manualDisconnect = true;
  const token = session.resumeToken;
  removeSharedSession(token);
  session.resumeToken = null;
  session.fileToken = null;
  persistSessions();
  if (session.socket.readyState === WebSocket.OPEN) {
    session.socket.send(JSON.stringify({ type: 'disconnect' }));
  }
  session.socket.close();
  if (token) {
    fetch('/api/session/disconnect', { method: 'POST', headers: { 'X-MXSSH-Session': token }, keepalive: true }).catch(() => {});
  }
}
disconnectButton.addEventListener('click', () => {
  if (activeSession) { disconnectSession(activeSession); disconnectButton.disabled = true; }
});
keysToggle.addEventListener('click', () => {
  if (!activeSession) return;
  activeSession.keysOpen = !activeSession.keysOpen;
  setSessionPanels(activeSession);
  persistSessions();
  requestAnimationFrame(() => fitVisibleTerminal());
});
terminalKeys.addEventListener('pointerdown', event => {
  if (event.target.closest('button')) event.preventDefault();
});
terminalKeys.addEventListener('click', event => {
  const button = event.target.closest('button[data-key]');
  const session = activeSession;
  if (!button || !session || session.state !== '已连接' || session.socket.readyState !== WebSocket.OPEN) return;
  const application = session.term.modes.applicationCursorKeysMode;
  const cursor = application ? '\x1bO' : '\x1b[';
  const keys = {
    escape: '\x1b', tab: '\t', 'ctrl-c': '\x03', 'ctrl-d': '\x04', 'ctrl-z': '\x1a', 'ctrl-l': '\x0c',
    up: cursor + 'A', down: cursor + 'B', right: cursor + 'C', left: cursor + 'D',
    home: cursor + 'H', end: cursor + 'F', 'page-up': '\x1b[5~', 'page-down': '\x1b[6~',
    backspace: '\x7f', enter: '\r',
  };
  if (Object.prototype.hasOwnProperty.call(keys, button.dataset.key)) session.socket.send(new TextEncoder().encode(keys[button.dataset.key]));
});
clearTerminal.addEventListener('click', () => {
  if (activeSession) { activeSession.term.clear(); if (activeSession.terminalOpen) activeSession.term.focus(); }
});
function changeFontSize(delta) {
  terminalFontSize = Math.max(10, Math.min(24, terminalFontSize + delta));
  for (const session of sessions) session.term.options.fontSize = terminalFontSize;
  fitVisibleTerminal(true);
}
document.querySelector('#font-decrease').addEventListener('click', () => changeFontSize(-1));
document.querySelector('#font-increase').addEventListener('click', () => changeFontSize(1));
if (!document.fullscreenEnabled || !terminalWindow.requestFullscreen) {
  fullscreenToggle.disabled = true;
  fullscreenToggle.title = '当前浏览器不支持全屏';
} else {
  fullscreenToggle.addEventListener('click', async () => {
    try {
      if (document.fullscreenElement === terminalWindow) await document.exitFullscreen();
      else await terminalWindow.requestFullscreen();
    } catch (_) {
      terminalStatus.textContent = '无法切换全屏，请检查浏览器权限。';
      terminalStatus.title = terminalStatus.textContent;
    }
  });
}
document.addEventListener('fullscreenchange', () => {
  const fullscreen = document.fullscreenElement === terminalWindow;
  (fullscreen ? document.querySelector('#fullscreen-tabs') : document.querySelector('#workspace-tabs')).append(sessionTabs);
  fullscreenToggle.innerHTML = fullscreen ? '⛶ <span>退出全屏</span>' : '⛶ <span>全屏</span>';
  fullscreenToggle.title = fullscreen ? '退出全屏' : '全屏终端';
  fullscreenToggle.setAttribute('aria-pressed', String(fullscreen));
  requestAnimationFrame(() => {
    fitVisibleTerminal(true);
  });
});
if (window.ResizeObserver) {
  new ResizeObserver(() => fitVisibleTerminal()).observe(terminalContainer);
} else {
  window.addEventListener('resize', () => fitVisibleTerminal());
}

const filePath = document.querySelector('#file-path');
const fileList = document.querySelector('#file-list');
const fileMessage = document.querySelector('#file-message');
const fileUpload = document.querySelector('#file-upload');
const fileControls = ['#file-up', '#file-refresh', '#file-upload-button', '#file-path-form button', '#file-path'];
let fileListingVersion = 0;

filePanelToggle.addEventListener('click', () => {
  if (!activeSession) return;
  if (activeSession.filesOpen && !activeSession.terminalOpen) return;
  activeSession.filesOpen = !activeSession.filesOpen;
  setSessionPanels(activeSession);
  persistSessions();
  if (activeSession.filesOpen) refreshFilePanel(true);
  requestAnimationFrame(() => {
    fitVisibleTerminal();
  });
});

terminalPanelToggle.addEventListener('click', () => {
  if (!activeSession || (activeSession.terminalOpen && !activeSession.filesOpen)) return;
  const session = activeSession;
  session.terminalOpen = !session.terminalOpen;
  setSessionPanels(session);
  persistSessions();
  if (session.terminalOpen && session.filesOpen) moveTerminalToFileDirectory(session);
  requestAnimationFrame(() => fitVisibleTerminal(activeSession && activeSession.terminalOpen));
});

async function moveTerminalToFileDirectory(session) {
  const path = session.filePath;
  if (!session.fileToken || !path.startsWith('/')) {
    fileMessage.textContent = '无法切换终端目录：请先打开一个远程目录。';
    return;
  }
  fileMessage.textContent = '正在切换终端目录…';
  try {
    const response = await fileRequest(session, '/api/session/cd', '', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
    });
    const result = await response.json();
    if (activeSession === session && filePanelVisible()) {
      fileMessage.textContent = result.already_there ? '终端已在当前文件目录。' : '已向终端发送切换目录命令，请查看终端结果。';
    }
  } catch (error) {
    if (activeSession === session && filePanelVisible()) fileMessage.textContent = `终端目录未切换：${error.message}`;
  }
}

function updateCwdControls(session) {
  const ready = Boolean(session && session.fileToken && session.state === '已连接');
  document.querySelector('#file-cwd').disabled = !ready;
}

function refreshFilePanel(useTerminalPath = false) {
  fileListingVersion++;
  const ready = Boolean(activeSession && activeSession.fileToken && activeSession.state === '已连接');
  updateCwdControls(activeSession);
  fileControls.forEach(selector => { document.querySelector(selector).disabled = !ready; });
  fileList.replaceChildren();
  filePath.value = ready ? activeSession.filePath : '';
  if (ready && useTerminalPath) locateTerminalDirectory(activeSession);
  else if (ready) loadRemoteDirectory(activeSession, activeSession.filePath);
  else if (activeSession && activeSession.state === '已连接') {
    fileMessage.textContent = '当前会话不支持文件传输，请重启服务并重新连接 SSH。';
  } else {
    fileMessage.textContent = activeSession ? '当前会话尚未连接或已断开。' : '请先连接 SSH，再选择会话。';
  }
}

async function fileRequest(session, endpoint, path, options = {}) {
  const query = new URLSearchParams({ path });
  const response = await fetch(`${endpoint}?${query}`, {
    cache: 'no-store', ...options, headers: { ...(options.headers || {}), 'X-MXSSH-Session': session.fileToken },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (endpoint === '/api/session/cd' && response.status === 404 && body.detail === 'Not Found') {
      throw new Error('服务端尚未加载终端目录切换接口，请重启 MXSSH 服务并重新连接');
    }
    throw new Error(typeof body.detail === 'string' ? body.detail : '文件操作失败');
  }
  return response;
}

function remoteChild(directory, name) {
  return `${directory === '/' ? '' : directory.replace(/\/$/, '')}/${name}`;
}

function displaySize(size) {
  if (size == null) return '—';
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${(size / 1024 ** 3).toFixed(1)} GB`;
}

async function loadRemoteDirectory(session, path) {
  const version = ++fileListingVersion;
  fileMessage.textContent = '正在读取目录…';
  try {
    const response = await fileRequest(session, '/api/files', path);
    const result = await response.json();
    if (version !== fileListingVersion || activeSession !== session) return;
    session.filePath = result.path;
    persistSessions();
    filePath.value = result.path;
    fileList.replaceChildren();
    fileMessage.textContent = result.entries.length ? `${result.entries.length} 个项目` : '此目录为空。';
    for (const entry of result.entries) {
      const row = document.createElement('div');
      row.className = 'file-row';
      const name = document.createElement('button');
      name.type = 'button';
      name.className = 'file-name';
      name.textContent = `${entry.directory ? '▸' : '·'}  ${entry.name}`;
      name.title = entry.name;
      const childPath = remoteChild(result.path, entry.name);
      name.addEventListener('click', () => entry.directory ? loadRemoteDirectory(session, childPath) : downloadFile(session, childPath, entry.name));
      const details = document.createElement('div');
      details.className = 'file-details';
      const size = document.createElement('span');
      size.textContent = entry.directory ? '目录' : displaySize(entry.size);
      details.append(size);
      if (!entry.directory) {
        const download = document.createElement('button');
        download.type = 'button'; download.className = 'tool-button'; download.textContent = '下载';
        download.addEventListener('click', () => downloadFile(session, childPath, entry.name));
        details.append(download);
      }
      row.append(name, details);
      fileList.append(row);
    }
    return true;
  } catch (error) {
    if (version === fileListingVersion && activeSession === session) fileMessage.textContent = error.message;
    return false;
  }
}

async function locateTerminalDirectory(session) {
  const version = ++fileListingVersion;
  fileMessage.textContent = '正在定位终端当前目录…';
  try {
    const response = await fileRequest(session, '/api/files/cwd', '');
    const result = await response.json();
    if (version !== fileListingVersion || activeSession !== session || !filePanelVisible()) return;
    await loadRemoteDirectory(session, result.path);
  } catch (error) {
    if (version !== fileListingVersion || activeSession !== session || !filePanelVisible()) return;
    const fallback = session.terminalPath || session.filePath;
    const shown = await loadRemoteDirectory(session, fallback);
    if (shown && activeSession === session && filePanelVisible()) {
      fileMessage.textContent = `${error.message}；${session.terminalPath ? '已使用终端报告的目录' : '显示原目录'}。`;
    }
  }
}

async function downloadFile(session, path, name) {
  fileMessage.textContent = `正在下载 ${name}…`;
  try {
    const response = await fileRequest(session, '/api/files/download', path);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    if (activeSession === session) fileMessage.textContent = `下载完成：${name}`;
  } catch (error) { if (activeSession === session) fileMessage.textContent = error.message; }
}

document.querySelector('#file-cwd').addEventListener('click', () => {
  if (activeSession && activeSession.fileToken) locateTerminalDirectory(activeSession);
});
document.querySelector('#file-path-form').addEventListener('submit', event => {
  event.preventDefault();
  if (activeSession && activeSession.fileToken && filePath.value.trim()) loadRemoteDirectory(activeSession, filePath.value.trim());
});
document.querySelector('#file-up').addEventListener('click', () => {
  if (activeSession && activeSession.fileToken) {
    const path = activeSession.filePath.replace(/\/+$/, '');
    loadRemoteDirectory(activeSession, path.slice(0, path.lastIndexOf('/')) || '/');
  }
});
document.querySelector('#file-refresh').addEventListener('click', () => {
  if (activeSession && activeSession.fileToken) loadRemoteDirectory(activeSession, activeSession.filePath);
});
document.querySelector('#file-upload-button').addEventListener('click', () => fileUpload.click());
fileUpload.addEventListener('change', async () => {
  const session = activeSession;
  const files = Array.from(fileUpload.files);
  fileUpload.value = '';
  if (!session || !session.fileToken || !files.length) return;
  const directory = session.filePath;
  for (const file of files) {
    if (activeSession === session) fileMessage.textContent = `正在上传 ${file.name}…`;
    try {
      await fileRequest(session, '/api/files/upload', remoteChild(directory, file.name), { method: 'POST', body: file });
    } catch (error) {
      if (activeSession === session) fileMessage.textContent = error.message;
      return;
    }
  }
  if (activeSession === session && session.fileToken && session.filePath === directory) {
    const refreshed = await loadRemoteDirectory(session, directory);
    if (refreshed && activeSession === session && session.filePath === directory) fileMessage.textContent = `上传完成：${files.length} 个文件`;
  }
});

// Local storage shares the session capabilities across tabs of this origin.
try {
  const previous = JSON.parse(sessionStorage.getItem(sessionStorageKey) || '{}');
  let shared;
  try {
    shared = [];
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index);
      if (key && key.startsWith(sharedSessionPrefix)) {
        try { shared.push(JSON.parse(localStorage.getItem(key))); } catch (_) { /* Ignore invalid entries. */ }
      }
    }
  } catch (_) { shared = previous.sessions || []; }
  if (window.Terminal && window.FitAddon && Array.isArray(shared)) {
    for (const item of shared) {
      if (item && typeof item.name === 'string' && typeof item.token === 'string' && item.token) {
        const layout = Array.isArray(previous.sessions) ? previous.sessions.find(saved => saved.token === item.token) : null;
        startConnection({ session_token: item.token }, item.name, item.mode === 'files' ? 'files' : 'terminal', layout);
      }
    }
    const selected = sessions.find(session => session.resumeToken === previous.active);
    if (selected) activateSession(selected);
    showView(pageNames[previous.view] ? previous.view : 'connections');
  }
} catch (_) { /* Invalid or unavailable browser storage starts a fresh workspace. */ }
restoringSessions = false;
persistSessions();
window.addEventListener('storage', event => {
  if (!event.key || !event.key.startsWith(sharedSessionPrefix) || event.newValue !== null) return;
  const token = event.key.slice(sharedSessionPrefix.length);
  for (const session of sessions) {
    if (session.resumeToken !== token) continue;
    session.manualDisconnect = true;
    session.resumeToken = null;
    session.fileToken = null;
    session.message = '连接已在其他标签页断开。';
    session.socket.close();
    refreshSession(session);
  }
  persistSessions();
  if (filePanelVisible()) refreshFilePanel();
});
