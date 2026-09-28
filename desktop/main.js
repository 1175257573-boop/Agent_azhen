'use strict';

/**
 * Atlas 控制台 · Electron 主进程
 * =============================================================================
 * 这个文件的职责只有一件：把「Python 后端 + Web 界面」包装成一个双击即用的桌面应用。
 *
 *   Electron 启动
 *     ├─ 探测 127.0.0.1:8000 是否已有 Atlas 服务
 *     │    ├─ 有  → 直接复用（退出时不关它，因为不是我们起的）
 *     │    └─ 无  → 找个能用的 Python 解释器，spawn `main.py web`
 *     ├─ 先显示本地「启动中」页（loading.html），同时轮询 /api/health
 *     ├─ 就绪后切到 http://127.0.0.1:8000/client
 *     └─ 窗口关闭 / 退出应用 → 连同后端进程一起结束
 *
 * 设计约束：
 *   1. 「关窗口 = 停服务」。这是用户理解的开关语义，不能让后端偷偷留在后台。
 *      因此只杀我们自己 spawn 的进程（owned），且用同步方式，保证退出前一定执行到。
 *   2. 后端起不来时要让人看得见原因。所以有 loading 页 + 日志文件，而不是白屏。
 *   3. 渲染层一律 sandbox + contextIsolation，preload 只暴露必要通道。
 * =============================================================================
 */

const { app, BrowserWindow, ipcMain, Menu, shell, nativeImage } = require('electron');
const { spawn, execFile, execFileSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');

// ---------- 路径与常量 -------------------------------------------------------

const REPO_ROOT = path.resolve(__dirname, '..');
const APP_DIR = path.join(__dirname, 'app');
const ASSETS_DIR = path.join(__dirname, 'assets');

/** 本机覆盖配置（记录上次可用的解释器，不入库） */
const SETTINGS_FILE = path.join(__dirname, '.atlas-desktop.json');
/** 运行日志，出问题时让人有地方可查 */
const LOG_FILE = path.join(os.homedir(), '.atlas', 'desktop.log');

const HOST = '127.0.0.1';
const PORT = Number(process.env.ATLAS_PORT || 8000);
const BASE_URL = `http://${HOST}:${PORT}`;
const CLIENT_URL = `${BASE_URL}/client`;

/** 首次启动最多等后端多久（依赖冷启动 + SQLite 建表，给足余量） */
const READY_TIMEOUT_MS = 120_000;
const IS_DEV = process.argv.includes('--dev');

// ---------- 运行时状态 -------------------------------------------------------

let win = null;
/** { owned: boolean, proc: ChildProcess|null, error?: string } */
let backend = { owned: false, proc: null };
let quitting = false;
let logStream = null;

// ---------- 日志 -------------------------------------------------------------

function initLog() {
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  } catch {
    logStream = null; // 日志写不了不该影响主流程
  }
}

function log(...parts) {
  const line = `[${new Date().toISOString()}] ${parts.join(' ')}`;
  console.log(line);
  try {
    logStream?.write(line + '\n');
  } catch {
    /* ignore */
  }
  if (win && !win.isDestroyed()) {
    win.webContents.send('desktop:log', line);
  }
}

// ---------- 本地覆盖配置 -----------------------------------------------------

function readSettings() {
  try {
    return JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8'));
  } catch {
    return {};
  }
}

function writeSettings(patch) {
  try {
    fs.writeFileSync(
      SETTINGS_FILE,
      JSON.stringify({ ...readSettings(), ...patch }, null, 2),
      'utf8',
    );
  } catch {
    /* 记不住也无所谓，下次重新探测 */
  }
}

// ---------- Python 解释器探测 -------------------------------------------------

/** 探测顺序：人工指定 → 上次成功 → 仓库内虚拟环境 → 受管环境 → PATH */
function interpreterCandidates() {
  const list = [];
  const settings = readSettings();

  if (settings.python) {
    list.push({ cmd: settings.python, args: [], label: '上次成功使用的解释器' });
  }
  if (process.env.ATLAS_PYTHON) {
    list.push({ cmd: process.env.ATLAS_PYTHON, args: [], label: '环境变量 ATLAS_PYTHON' });
  }

  const venvs = [
    path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(REPO_ROOT, '.venv', 'bin', 'python'),
    path.join(REPO_ROOT, 'venv', 'Scripts', 'python.exe'),
    path.join(REPO_ROOT, 'env', 'Scripts', 'python.exe'),
  ];
  for (const p of venvs) {
    if (fs.existsSync(p)) {
      list.push({ cmd: p, args: [], label: `仓库内虚拟环境（${path.relative(REPO_ROOT, p)}）` });
    }
  }

  const managed = path.join(
    os.homedir(), '.workbuddy', 'binaries', 'python', 'envs', 'default', 'Scripts', 'python.exe',
  );
  if (fs.existsSync(managed)) {
    list.push({ cmd: managed, args: [], label: '受管虚拟环境' });
  }

  if (process.platform === 'win32') {
    list.push({ cmd: 'py', args: ['-3'], label: 'py -3' });
    list.push({ cmd: 'py', args: [], label: 'py' });
  }
  list.push({ cmd: 'python', args: [], label: 'PATH 中的 python' });
  list.push({ cmd: 'python3', args: [], label: 'PATH 中的 python3' });

  return list;
}

/**
 * 试跑一个解释器，确认它 **不仅能启动，而且依赖装齐了**。
 * 只验证 `python --version` 是不够的 —— 那只能证明有 Python，不能证明有 FastAPI。
 */
function probeInterpreter(cmd, args) {
  return new Promise((resolve) => {
    execFile(
      cmd,
      [...args, '-c', 'import sys, uvicorn, fastapi; print(sys.version.split()[0])'],
      { timeout: 25_000, windowsHide: true },
      (err, stdout) => {
        if (err) return resolve(null);
        const out = String(stdout).trim().split(/\r?\n/).filter(Boolean).pop();
        return resolve(out || null);
      },
    );
  });
}

async function pickInterpreter() {
  const seen = new Set();
  for (const c of interpreterCandidates()) {
    const key = `${c.cmd}|${c.args.join(' ')}`.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);

    const version = await probeInterpreter(c.cmd, c.args);
    if (version) {
      log(`解释器可用：${c.label} → Python ${version}`);
      writeSettings({ python: c.cmd, pythonArgs: c.args, lastUsedAt: new Date().toISOString() });
      return { ...c, version };
    }
    log(`解释器不可用（缺依赖或无法启动）：${c.label}`);
  }
  return null;
}

// ---------- 后端进程 ---------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function pingHealth(timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}/api/health`, { timeout }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function startBackend() {
  if (await pingHealth()) {
    log(`端口 ${PORT} 上已有 Atlas 服务在运行，直接复用（退出时不会关掉它）`);
    backend = { owned: false, proc: null };
    return backend;
  }

  const py = await pickInterpreter();
  if (!py) {
    backend = { owned: false, proc: null, error: 'NO_PYTHON' };
    return backend;
  }

  const argv = [...py.args, 'main.py', 'web'];
  log(`启动后端：${py.cmd} ${argv.join(' ')}（cwd=${REPO_ROOT}）`);

  const proc = spawn(py.cmd, argv, {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      ATLAS_DESKTOP: '1',        // 让后端知道自己在桌面壳里
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  proc.stdout?.on('data', (b) => log('后端 |', String(b).trimEnd()));
  proc.stderr?.on('data', (b) => log('后端 !', String(b).trimEnd()));
  proc.on('error', (err) => log(`后端进程启动失败：${err.message}`));
  proc.on('exit', (code, signal) => {
    log(`后端进程退出：code=${code} signal=${signal}`);
    if (backend.proc === proc) backend.proc = null;
    if (!quitting && win && !win.isDestroyed()) {
      win.webContents.send('desktop:backend-exit', { code, signal });
    }
  });

  backend = { owned: true, proc };
  return backend;
}

async function waitReady(timeoutMs = READY_TIMEOUT_MS) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await pingHealth()) return true;
    // 后端进程已经死了就别再空等
    if (backend.owned && !backend.proc) return false;
    await sleep(600);
  }
  return false;
}

/**
 * 同步停止后端。必须是同步的 —— 放在 before-quit / process.on('exit') 里，
 * 异步的 taskkill 还没执行完进程就退出了，后端会变成孤儿进程继续占着 8000 端口。
 */
function stopBackendSync() {
  const proc = backend?.proc;
  if (!proc || !backend.owned) return;
  const pid = proc.pid;
  log(`正在停止后端进程（pid=${pid}）…`);

  if (process.platform === 'win32') {
    try {
      // /T 连子进程一起杀，避免 uvicorn 派生的 worker 残留
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
    } catch {
      try { proc.kill(); } catch { /* ignore */ }
    }
  } else {
    try { proc.kill('SIGTERM'); } catch { /* ignore */ }
  }

  backend.proc = null;
  backend.owned = false;
}

// ---------- 窗口 -------------------------------------------------------------

function iconImage() {
  const p = path.join(ASSETS_DIR, 'icon.png');
  return fs.existsSync(p) ? nativeImage.createFromPath(p) : undefined;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1040,
    minHeight: 660,
    show: false,
    title: 'Atlas 控制台',
    backgroundColor: '#f7f7f5',
    autoHideMenuBar: true,
    icon: iconImage(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  win.once('ready-to-show', () => {
    win.show();
    if (IS_DEV) win.webContents.openDevTools({ mode: 'detach' });
  });

  // 界面里的 <title> 不许改窗口标题，否则切到远程页面后标题会跳
  win.on('page-title-updated', (e) => e.preventDefault());

  // 站内链接留在窗口里，站外链接交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(BASE_URL)) return { action: 'allow' };
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BASE_URL) && !url.startsWith('file://')) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  win.on('closed', () => {
    win = null;
  });

  win.loadFile(path.join(APP_DIR, 'loading.html'));
}

function buildMenu() {
  const template = [
    {
      label: '应用',
      submenu: [
        { label: '重新加载界面', accelerator: 'CmdOrCtrl+R', click: () => win?.webContents.reload() },
        { label: '重启后端服务', click: () => restartBackend() },
        { type: 'separator' },
        { label: '在浏览器中打开', click: () => shell.openExternal(CLIENT_URL) },
        { label: '查看运行日志', click: () => shell.showItemInFolder(LOG_FILE) },
        { type: 'separator' },
        { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { role: 'resetZoom', label: '实际大小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
        { role: 'toggleDevTools', label: '开发者工具' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------- 启动流程 ---------------------------------------------------------

async function bootstrap() {
  createWindow();
  buildMenu();

  if (IS_DEV) log('开发者模式：已打开 DevTools');

  const state = await startBackend();

  if (state.error === 'NO_PYTHON') {
    log('未找到可用的 Python 解释器（需要已安装 langchain / fastapi / uvicorn 的环境）');
    notYet('NO_PYTHON');
    return;
  }

  const ok = await waitReady();
  if (!ok) {
    log(`等待服务就绪超时（${READY_TIMEOUT_MS / 1000}s）`);
    notYet('TIMEOUT');
    return;
  }

  log(`服务已就绪，载入界面：${CLIENT_URL}`);
  if (win && !win.isDestroyed()) {
    win.webContents.send('desktop:ready', { baseUrl: BASE_URL });
    await sleep(350); // 让启动页把「已就绪」显示完整，避免一闪而过
    win.loadURL(CLIENT_URL);
  }
}

async function restartBackend() {
  log('收到重启后端请求');
  stopBackendSync();
  await sleep(900);
  const state = await startBackend();
  if (state.error === 'NO_PYTHON') return notYet('NO_PYTHON');
  const ok = await waitReady();
  if (win && !win.isDestroyed()) win.loadURL(CLIENT_URL);
  return ok;
}

function notYet(code) {
  if (win && !win.isDestroyed()) {
    win.webContents.send('desktop:failed', {
      code,
      logFile: LOG_FILE,
      repoRoot: REPO_ROOT,
    });
  }
}

// ---------- IPC --------------------------------------------------------------

ipcMain.handle('desktop:status', async () => ({
  ready: await pingHealth(),
  baseUrl: BASE_URL,
  clientUrl: CLIENT_URL,
  backendOwned: backend.owned,
  backendPid: backend.proc?.pid ?? null,
  logFile: LOG_FILE,
  repoRoot: REPO_ROOT,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
}));

ipcMain.handle('desktop:restart', async () => restartBackend());

ipcMain.handle('desktop:reveal-log', () => {
  try {
    shell.showItemInFolder(LOG_FILE);
  } catch { /* ignore */ }
});

ipcMain.handle('desktop:open-external', (_e, url) => {
  if (typeof url === 'string' && /^https?:\/\//.test(url)) shell.openExternal(url);
});

// ---------- 生命周期 ---------------------------------------------------------

// 单实例：第二次双击时激活已有窗口，而不是再起一个后端
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    initLog();
    log(`=== Atlas 控制台启动（Electron ${process.versions.electron} / ${process.platform}）===`);
    bootstrap().catch((err) => {
      log(`启动流程异常：${err?.stack || err}`);
      notYet('BOOTSTRAP_ERROR');
    });
  });
}

app.on('window-all-closed', () => {
  app.quit();
});

app.on('before-quit', () => {
  quitting = true;
  stopBackendSync();
});

// 兜底：无论从哪条路径退出，都不该留下孤儿后端
process.on('exit', () => {
  quitting = true;
  stopBackendSync();
});
