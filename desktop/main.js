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

const { app, BrowserWindow, dialog, ipcMain, Menu, Tray, shell, nativeImage } = require('electron');
const { spawn, execFile, execFileSync } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');

// ---------- 路径与常量 -------------------------------------------------------

const DEV_ROOT = path.resolve(__dirname, '..');
const APP_DIR = path.join(__dirname, 'app');
const ASSETS_DIR = path.join(__dirname, 'assets');

/**
 * 后端代码目录。
 *
 *   开发（electron . / npm start）→ 仓库根目录
 *   打包后                        → 安装目录/resources/backend
 *
 * 打包后外面根本没有仓库，所以必须带上后端源码（见 package.json 的 extraResources）。
 * 仓库那点代码才 1MB 出头，塞进包的代价可以忽略。
 */
function resolveBackendRoot() {
  if (app.isPackaged) {
    const bundled = path.join(process.resourcesPath, 'backend');
    if (fs.existsSync(path.join(bundled, 'main.py'))) return bundled;
    log('警告：未找到随包携带的后端代码，回退到安装目录附近查找');
  }
  return DEV_ROOT;
}

const BACKEND_ROOT = resolveBackendRoot();

/**
 * 本机覆盖配置（记录上次可用的解释器）。
 *
 * 打包后 `SETTINGS_FILE` 不能放在 __dirname —— 那是只读的 app.asar，写盘必然失败，
 * 而失败又会被 readSettings/writeSettings 的 try 静默吞掉，表现为「每次都重新探测解释器」。
 * 必须放到可写的用户目录。
 */
function resolveSettingsFile() {
  try {
    return path.join(app.getPath('userData'), '.atlas-desktop.json');
  } catch {
    return path.join(os.homedir(), '.atlas', 'desktop-settings.json');
  }
}

const SETTINGS_FILE = resolveSettingsFile();
/** 运行日志，出问题时让人有地方可查 */
const LOG_FILE = path.join(os.homedir(), '.atlas', 'desktop.log');

const HOST = '127.0.0.1';

/**
 * 端口在启动时解析，不写死。
 *
 * 写死 8000 的问题：它是最容易被别的程序占用的端口之一，撞了之后后端
 * 会以 `OSError: [Errno 10048]` 秒退，而症状出现在很远的地方
 * （健康检查一直连不上 → 「等待服务就绪超时」），排查成本很高。
 *
 * 优先级：
 *   1) ATLAS_PORT —— 显式指定（调试、写文档、CI 都靠它）
 *   2) 0 —— 交给操作系统分配一个当前空闲的端口，等于天然随机且永不冲突
 *
 * 所以下面三个都是 `let`：解析之前它们还没有值。
 */
let PORT = 0;
let BASE_URL = '';
// 新前端（web/dist 构建产物）挂在根路径，SPA 自己做路由；
// 没构建过时后端会退回旧的 static/index.html，两种情况下根路径都能开。
let CLIENT_URL = '';
/** 旧版三栏控制台，作为备用入口保留（后端 /client 一直挂着） */
let LEGACY_URL = '';

/** 让操作系统给一个当前空闲的端口（随即释放，race 窗口极小）。 */
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, HOST, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function resolvePort() {
  const explicit = process.env.ATLAS_PORT;
  PORT = explicit ? Number(explicit) : await pickFreePort();
  BASE_URL = `http://${HOST}:${PORT}`;
  CLIENT_URL = `${BASE_URL}/`;
  LEGACY_URL = `${BASE_URL}/client`;
  return PORT;
}

/** 首次启动最多等后端多久（依赖冷启动 + SQLite 建表，给足余量） */
const READY_TIMEOUT_MS = 120_000;
const IS_DEV = process.argv.includes('--dev');

// ---------- 运行时状态 -------------------------------------------------------

let win = null;
/** { owned: boolean, proc: ChildProcess|null, error?: string } */
let backend = { owned: false, proc: null };
let quitting = false;
/** 关窗确认框是否正在显示，防止连点 X 弹出多个对话框 */
let closeDialogOpen = false;
/** 上面那把锁的兜底定时器：界面没回传时自动解锁，避免关闭键从此失灵 */
let closeLockTimer = null;
const CLOSE_LOCK_TIMEOUT_MS = 60_000;
/** 系统托盘实例（关窗选「最小化」后靠它找回窗口） */
let tray = null;
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
    path.join(BACKEND_ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(BACKEND_ROOT, '.venv', 'bin', 'python'),
    path.join(BACKEND_ROOT, 'venv', 'Scripts', 'python.exe'),
    path.join(BACKEND_ROOT, 'env', 'Scripts', 'python.exe'),
  ];
  for (const p of venvs) {
    if (fs.existsSync(p)) {
      list.push({ cmd: p, args: [], label: `仓库内虚拟环境（${path.relative(BACKEND_ROOT, p)}）` });
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
function probeInterpreter(cmd, args, { light = false } = {}) {
  return new Promise((resolve) => {
    // 轻量模式只验证「解释器能跑」，不 import 依赖；完整模式才验证依赖齐全。
    const script = light
      ? 'import sys; print(sys.version.split()[0])'
      : 'import sys, uvicorn, fastapi; print(sys.version.split()[0])';
    execFile(cmd, [...args, '-c', script], { timeout: 25_000, windowsHide: true }, (err, stdout) => {
      if (err) return resolve(null);
      const out = String(stdout).trim().split(/\r?\n/).filter(Boolean).pop();
      return resolve(out || null);
    });
  });
}

async function pickInterpreter() {
  // 快路径：上次成功用过的解释器，只做「能不能跑起来」的轻量验证（~250ms），
  // 不再 import uvicorn/fastapi（那要 ~880ms，而这份结果马上就用不上了——
  // 后端是另一个进程，导入成果不共享）。依赖真缺失时后端会秒退，
  // 由 BACKEND_EXITED 指引告诉用户怎么修。
  if (!process.env.ATLAS_PYTHON) {
    const remembered = readSettings().python;
    if (remembered && fs.existsSync(remembered)) {
      const version = await probeInterpreter(remembered, [], { light: true });
      if (version) {
        log(`解释器快路径：复用 ${remembered} → Python ${version}（跳过依赖探测）`);
        return { cmd: remembered, args: [], label: '上次成功使用的解释器', version };
      }
    }
  }

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

function pingHealth(timeout = 3000) {
  return new Promise((resolve) => {
    // probe_backends=false 是关键：默认 true 时后端会真去拨 Redis / 连 PostgreSQL，
    // 连不上就卡到 7 秒以上（实测 7100ms），而这里 timeout 只有几秒 →
    // 每次探测都在后端返回前就判失败，waitReady 一直重试到 120 秒超时。
    // 启动轮询只关心「进程活着没」，后端连通性由界面上的状态展示负责。
    const req = http.get(`${BASE_URL}/api/health?probe_backends=false`, { timeout }, (res) => {
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

  // --port 必须显式传：端口现在是运行时挑的（ATLAS_PORT 或系统分配的空闲端口），
  // 不传的话后端会退回自己写死的 8000，两边对不上 → 探活永远连不上。
  const argv = [...py.args, 'main.py', 'web', '--port', String(PORT)];
  log(`启动后端：${py.cmd} ${argv.join(' ')}（cwd=${BACKEND_ROOT}）`);

  const proc = spawn(py.cmd, argv, {
    cwd: BACKEND_ROOT,
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

/**
 * 端口是否已空出来。
 * 杀进程 ≠ 端口立刻可绑定：taskkill 返回时子进程可能还在收尾，
 * 新后端导入完依赖（~1s）再去 bind，往往正好撞上 10048。
 */
function probePortFree() {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port: PORT });
    const done = (free) => {
      try {
        s.destroy();
      } catch {
        /* ignore */
      }
      resolve(free);
    };
    s.setTimeout(400, () => done(true)); // 连不上即视为已释放
    s.once('connect', () => done(false)); // 还能连上说明仍被占用
    s.once('error', () => done(true));
  });
}

async function waitPortFree(timeoutMs = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await probePortFree()) return true;
    await sleep(100);
  }
  return false;
}

async function waitReady(timeoutMs = READY_TIMEOUT_MS) {
  const t0 = Date.now();
  // 退避而不是固定 600ms：后端通常 1.3s 就绪，600ms 的格子会白等半秒以上。
  let delay = 60;
  while (Date.now() - t0 < timeoutMs) {
    if (await pingHealth()) return true;
    // 后端进程已经死了就别再空等
    if (backend.owned && !backend.proc) return false;
    await sleep(delay);
    delay = Math.min(Math.round(delay * 1.5), 300);
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

/**
 * 关窗询问：优先用应用内那套自定义弹窗（样式统一、能讲清后果），
 * 只有界面还没加载出来（启动页/启动失败）时才退回系统原生对话框。
 */
function askHowToClose() {
  const inApp = win && !win.isDestroyed() && win.webContents.getURL().startsWith(CLIENT_URL);

  const done = (decision) => {
    clearCloseLock();
    if (decision === 'minimize') {
      // 最小化到托盘：窗口连任务栏一起藏起来，后端继续跑，
      // 靠托盘图标（或托盘菜单「显示主界面」）回来
      createTray();
      win?.hide();
      log('已最小化到托盘，后端继续运行');
    } else if (decision === 'quit') {
      quitting = true;
      app.quit(); // before-quit / exit 钩子会负责停后端
    }
  };

  if (inApp) {
    win.webContents.send('desktop:confirm-close');
    // 兜底：万一界面没回传（回调丢失、渲染进程异常、用户开着弹窗不管），
    // 锁不能一直挂着 —— 否则之后点关闭键都毫无反应。每分钟自动解锁一次。
    clearTimeout(closeLockTimer);
    closeLockTimer = setTimeout(() => {
      closeDialogOpen = false;
      closeLockTimer = null;
    }, CLOSE_LOCK_TIMEOUT_MS);
    return;
  }

  dialog
    .showMessageBox(win, {
      type: 'question',
      buttons: ['最小化到托盘', '退出（同时停止后端）', '取消'],
      defaultId: 0,
      cancelId: 2,
      title: 'Atlas 控制台',
      message: '要最小化，还是退出？',
      detail: '最小化：窗口收进系统托盘，后端继续运行。\n退出：会一并停掉本地后端服务。',
      noLink: true,
    })
    .then(({ response }) => done(['minimize', 'quit', 'cancel'][response]))
    .catch(() => done('cancel'));
}

function clearCloseLock() {
  clearTimeout(closeLockTimer);
  closeLockTimer = null;
  closeDialogOpen = false;
}

/**
 * 系统托盘。
 *
 * 为什么必须有：关窗时选「最小化」后窗口是隐藏的（任务栏也不留），
 * 没有托盘图标的话用户就再也回不来了 —— 只能杀进程。
 * 托盘是这个"最小化"语义唯一的恢复入口。
 */
function createTray() {
  if (tray) return;
  const icon = iconImage();
  if (!icon) {
    log('未找到可用图标，跳过托盘（此时「最小化」仍可用，窗口只是缩到任务栏）');
    return;
  }
  tray = new Tray(icon);
  tray.setToolTip('Atlas 控制台 · 运行中');
  refreshTrayMenu();
  tray.on('click', () => showMainWindow());
  tray.on('double-click', () => showMainWindow());
  log('系统托盘已就绪');
}

function refreshTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: '显示主界面', click: () => showMainWindow() },
      {
        label: '最小化到托盘',
        click: () => {
          win?.hide();
        },
      },
      { type: 'separator' },
      {
        label: '完全退出（同时停止后端）',
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
}

function showMainWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function iconImage() {
  // 打包后优先用 resources 下的真实文件：asar 内路径在部分系统 API 里不可读，
  // 会导致任务栏/Alt-Tab 图标回退成 Electron 默认图标。
  if (app.isPackaged) {
    const p = path.join(process.resourcesPath, 'icon.ico');
    if (fs.existsSync(p)) return nativeImage.createFromPath(p);
  }
  for (const name of ['icon.ico', 'icon.png']) {
    const p = path.join(ASSETS_DIR, name);
    if (fs.existsSync(p)) return nativeImage.createFromPath(p);
  }
  return undefined;
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

  win.on('close', (event) => {
    // 真正退出（菜单里选了退出、或正在退出流程中）时不拦截
    if (quitting) return;
    // 其余情况一律先问一句：最小化还是关闭。
    // 不拦截的话窗口会被销毁，任务栏就没了，用户只能重新双击启动，
    // 而后端也一并被 before-quit 杀掉 —— 每次开关都白等一次冷启动。
    event.preventDefault();
    if (closeDialogOpen) return; // 连点关闭键时只弹一个
    closeDialogOpen = true;
    askHowToClose();
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
        {
          label: '最小化到托盘',
          click: () => {
            createTray();
            win?.hide();
          },
        },
        {
          label: '完全退出（同时停止后端）',
          accelerator: 'CmdOrCtrl+Q',
          click: () => {
            quitting = true;
            app.quit();
          },
        },
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
  createTray();

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
  // 固定等 900ms 是不够的：端口没真正释放时，新后端 bind 会撞 10048 然后秒退。
  const freed = await waitPortFree();
  if (!freed) log('端口仍被占用，继续启动（可能仍会失败，日志里有原因）');
  const state = await startBackend();
  if (state.error === 'NO_PYTHON') return notYet('NO_PYTHON');
  const ok = await waitReady();
  if (!ok) {
    // 这里以前不看 ok 就 loadURL：后端没起来时把窗口甩到没人监听的端口，
    // Chromium 直接显示 ERR_CONNECTION_REFUSED（用户看到的「连接不上服务器」）。
    const code = backend.owned && !backend.proc ? 'BACKEND_EXITED' : 'TIMEOUT';
    log(`重启后未就绪（${code}），保留启动页并给出指引`);
    if (code === 'BACKEND_EXITED' && readSettings().python) {
      // 后端秒退多半是记住的解释器已经失效（venv 重建 / 换了机器），
      // 清掉记忆，下一次启动才会走完整的候选列表重新探测。
      writeSettings({ python: '' });
      log('已清除记住的解释器，下次启动将重新探测');
    }
    return notYet(code);
  }
  if (win && !win.isDestroyed()) win.loadURL(CLIENT_URL);
  return true;
}

function notYet(code) {
  if (win && !win.isDestroyed()) {
    win.webContents.send('desktop:failed', {
      code,
      logFile: LOG_FILE,
      backendRoot: BACKEND_ROOT,
      // 打包后外面没有仓库，失败指引得换一套说法（见 app/loading.js）
      packaged: app.isPackaged,
    });
  }
}

// ---------- IPC --------------------------------------------------------------

ipcMain.handle('desktop:status', async () => ({
  ready: await pingHealth(),
  baseUrl: BASE_URL,
  clientUrl: CLIENT_URL,
  legacyUrl: LEGACY_URL,
  backendOwned: backend.owned,
  backendPid: backend.proc?.pid ?? null,
  logFile: LOG_FILE,
  backendRoot: BACKEND_ROOT,
  packaged: app.isPackaged,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
}));

ipcMain.handle('desktop:restart', async () => restartBackend());

// 界面上那个自定义弹窗做完选择后回传
ipcMain.handle('desktop:close-decision', async (_e, decision) => {
  // 无论收到什么都要先解锁 —— 'cancel' 也要解锁：
  // 那是用户点了遮罩/Esc 走掉的路径，早先前端在这里直接 return 不回传，
  // 结果锁一直挂着，之后点关闭键全被 if (closeDialogOpen) return 挡掉。
  clearCloseLock();
  if (decision === 'minimize') {
    createTray();
    win?.hide();
    log('已最小化到托盘，后端继续运行');
  } else if (decision === 'quit') {
    quitting = true;
    app.quit();
  }
  return true;
});

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
  // 复用托盘的恢复逻辑：窗口现在是 hide 状态，光 restore() 唤不醒，
  // 必须 show() + focus()，否则最小化到托盘后再双击图标会毫无反应
  app.on('second-instance', () => showMainWindow());

  app.whenReady().then(() => {
    initLog();
    log(`=== Atlas 控制台启动（Electron ${process.versions.electron} / ${process.platform}）===`);
    // 端口必须在建窗、探活、拼 URL 之前定下来
    return resolvePort()
      .then((p) => {
        log(
          process.env.ATLAS_PORT
            ? `使用指定端口 ATLAS_PORT=${p}`
            : `未指定 ATLAS_PORT，选用空闲端口 ${p}（避免与本机其他服务冲突）`,
        );
      })
      .then(() => bootstrap())
      .catch((err) => {
        log(`启动流程异常：${err?.stack || err}`);
        notYet('BOOTSTRAP_ERROR');
      });
  });
}

app.on('window-all-closed', () => {
  app.quit();
});

/**
 * 退出时要不要顺手停掉后端。默认**停**——
 * 关窗弹窗里的「退出（同时停止后端）」就是这个语义，不留孤儿进程。
 *
 * 想让它留在后台（频繁开关、或做 CI / 开发调试）：
 *     set ATLAS_KEEP_BACKEND=1
 * 正常用不到这个开关：想快就选「最小化」，窗口还在，后端自然也还在。
 */
function shouldKeepBackend() {
  if (process.env.ATLAS_KEEP_BACKEND === '1') return true;
  if (process.env.ATLAS_KEEP_BACKEND === '0') return false;
  return false;
}

app.on('before-quit', () => {
  quitting = true;
  if (tray) {
    tray.destroy();
    tray = null;
  }
  if (shouldKeepBackend()) {
    log('退出但按 ATLAS_KEEP_BACKEND=1 保留后端服务');
    return;
  }
  stopBackendSync();
});

// 兜底：无论从哪条路径退出，都不该留下孤儿后端
process.on('exit', () => {
  quitting = true;
  if (shouldKeepBackend()) return;
  stopBackendSync();
});
