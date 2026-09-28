'use strict';

/**
 * 渲染层安全桥（preload）
 * =============================================================================
 * 窗口以 sandbox: true + contextIsolation: true 运行，页面里没有 Node、没有 require。
 * 界面能做的事，全部由这里白名单式暴露 —— 不多给一个口子。
 *
 * 刻意不暴露的能力（想加之前先想清楚）：
 *   · 任意文件读写、任意命令执行
 *   · 把密钥明文从主进程读到页面（密钥只在后端进程里，桌面壳完全不经手）
 * =============================================================================
 */

const { contextBridge, ipcRenderer } = require('electron');

/** 事件订阅表：页面可以注册多个回调，但只能经由固定通道进来 */
const channels = {
  log: new Set(),
  ready: new Set(),
  failed: new Set(),
  backendExit: new Set(),
};

const CHANNEL_MAP = {
  'desktop:log': 'log',
  'desktop:ready': 'ready',
  'desktop:failed': 'failed',
  'desktop:backend-exit': 'backendExit',
};

for (const [channel, key] of Object.entries(CHANNEL_MAP)) {
  ipcRenderer.on(channel, (_event, payload) => {
    for (const cb of channels[key]) {
      try {
        cb(payload);
      } catch {
        /* 单个回调出错不该影响其他订阅者 */
      }
    }
  });
}

const subscribe = (key) => (cb) => {
  if (typeof cb === 'function') channels[key].add(cb);
};

contextBridge.exposeInMainWorld('atlasDesktop', {
  /** 让界面能判断自己是不是跑在桌面壳里，从而微调文案 */
  isDesktop: true,

  // ---- 事件订阅 ----
  onLog: subscribe('log'),
  onReady: subscribe('ready'),
  onFailed: subscribe('failed'),
  onBackendExit: subscribe('backendExit'),

  // ---- 请求 ----
  status: () => ipcRenderer.invoke('desktop:status'),
  restart: () => ipcRenderer.invoke('desktop:restart'),
  revealLog: () => ipcRenderer.invoke('desktop:reveal-log'),
  openExternal: (url) => ipcRenderer.invoke('desktop:open-external', url),
});
