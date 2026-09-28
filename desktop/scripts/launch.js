'use strict';

/**
 * 启动器（由普通 Node 执行，不是由 Electron 执行）
 * =============================================================================
 * 为什么需要它：如果宿主环境（WorkBuddy / VS Code 等本身是 Electron 的应用）
 * 派生了终端，shell 里会带着 ELECTRON_RUN_AS_NODE=1 与被注入的 NODE_OPTIONS。
 * 此时直接跑 electron.exe，它会退化成「纯 Node 模式」，require('electron')
 * 拿到的只是二进制路径而不是 API，主进程当场崩溃。
 *
 * 这个文件由普通 Node 执行 —— 此时 require('electron') 恰好返回 electron.exe
 * 的路径，正好用它来拉起真正的 Electron，并把污染变量从子进程环境里剥掉。
 *
 * 用户双击（从 explorer 启动）本来就没有这些变量，但启动器让两条路径行为一致。
 * =============================================================================
 */

const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

// 本文件位于 desktop/scripts/ 下，工程根是它的上一级
const desktopDir = path.resolve(__dirname, '..');

// ---- 环境净化：只删确定有害的，其余原样透传 ----
const env = { ...process.env };
const POLLUTED = [
  'ELECTRON_RUN_AS_NODE',     // 让 electron.exe 退化成纯 Node
  'NODE_OPTIONS',             // 宿主注入的 --require shim 会进 Electron 主进程
];
const removed = [];
for (const key of POLLUTED) {
  if (key in env) {
    delete env[key];
    removed.push(key);
  }
}

// ---- 依赖检查：给出人话，而不是一堆 ENOENT ----
const exe = require('electron'); // 普通 Node 下 = electron.exe 绝对路径
const exeExists = typeof exe === 'string' && fs.existsSync(exe);
if (!exeExists) {
  console.error('[atlas-desktop] 未找到 Electron 二进制。请先在 desktop/ 目录执行：npm install');
  process.exit(1);
}

// 实测（别改回去）：electron.exe 收到相对路径 '.' 且父进程是 Node spawn 时，
// 主进程会静默挂起（无窗口无报错，仅 1 个进程）；换成绝对路径则一切正常。
// 因此这里不用 '.'，直接传 desktop 目录的绝对路径。
const args = [desktopDir, ...process.argv.slice(2)];
if (removed.length) {
  console.log(`[atlas-desktop] 已从启动环境剥离污染变量：${removed.join(', ')}`);
}
console.log(`[atlas-desktop] 启动 ${exe} ${args.join(' ')}`);

// 注意两个 Windows 上的实测结论（别改回去）：
//   · stdio 不能用 'inherit' —— GUI 子系统进程继承控制台句柄后，主进程会
//     静默卡死（无窗口、无报错、单进程）。用 pipe 收集再转发。
//   · 不要传 windowsHide: true —— 它会给 GUI 进程加 CREATE_NO_WINDOW，
//     同样导致主进程静默挂起。GUI 程序本来就没有控制台窗口，这个选项无意义且有害。
const child = spawn(exe, args, {
  cwd: desktopDir,
  env,
  stdio: ['ignore', 'pipe', 'pipe'],
});

child.stdout?.on('data', (d) => process.stdout.write(d));
child.stderr?.on('data', (d) => process.stderr.write(d));

child.on('exit', (code, signal) => {
  if (signal) process.exit(1);
  process.exit(code ?? 0);
});

// Ctrl+C 透传：让 electron 一起退，而不是留一个孤儿
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    try { child.kill(); } catch { /* ignore */ }
    process.exit(0);
  });
}
