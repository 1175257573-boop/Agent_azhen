'use strict';

/**
 * 启动页逻辑
 * =============================================================================
 * 这个页面只在「后端还没起来」的窗口期存在，作用是：
 *   1. 告诉用户现在在干什么（而不是白屏或转圈无解释）
 *   2. 起不来的时候，把原因和**下一步该做什么**直接摆在眼前
 * =============================================================================
 */

const bridge = window.atlasDesktop;
const $ = (id) => document.getElementById(id);

const el = {
  stage: $('stage'),
  spinner: $('spinner'),
  status: $('status'),
  hint: $('hint'),
  fail: $('fail'),
  failTitle: $('failTitle'),
  failBody: $('failBody'),
  failSuggest: $('failSuggest'),
  gone: $('gone'),
  logBox: $('logBox'),
  log: $('log'),
  logCount: $('logCount'),
};

// ---------- 日志 ----------

let logLines = 0;

function appendLog(line) {
  logLines += 1;
  el.logCount.textContent = String(logLines);
  el.log.textContent += line + '\n';
  // 自动滚到底，除非用户自己往上翻了
  const nearBottom = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 40;
  if (nearBottom) el.log.scrollTop = el.log.scrollHeight;
}

// ---------- 失败指引 ----------

const FAILURE_GUIDE = {
  NO_PYTHON: {
    title: '找不到可用的 Python 环境',
    body: '这个后端需要 Python 3.10+，并且把依赖装齐（fastapi / uvicorn / langchain）。',
    suggest: (ctx) => {
      const tried = [
        '已按顺序尝试过下列位置，都没有成功：',
        '  1) 上次成功使用的解释器（记在用户目录，不入库）',
        '  2) 环境变量 ATLAS_PYTHON',
        '  3) 后端目录下的虚拟环境 .venv/',
        '  4) 受管虚拟环境',
        '  5) 系统 PATH 中的 python / py',
        '',
      ];

      // 安装版：外面没有仓库，指引必须能照着做，不能让人去找 .venv
      if (ctx.packaged) {
        return [
          ...tried,
          '这是安装版，应用自带的只有后端代码，Python 环境需要本机自备。',
          '',
          '【方式 A】装好 Python 后，用应用自带的依赖清单装依赖',
          '    python -m pip install -r "' + (ctx.backendRoot || '') + '\\requirements.txt"',
          '',
          '【方式 B】如果已有装好依赖的环境，直接指定',
          '    setx ATLAS_PYTHON "D:\\path\\to\\python.exe"',
          '    然后重新启动本应用',
          '',
          '后端代码目录：' + (ctx.backendRoot || '(未知)'),
        ].join('\n');
      }

      return [
        ...tried,
        '任选一种处理方式：',
        '',
        '【方式 A】在仓库根目录建虚拟环境并装依赖（推荐）',
        '    python -m venv .venv',
        '    .venv\\Scripts\\python.exe -m pip install -r requirements.txt',
        '',
        '【方式 B】已经有装好依赖的环境，直接告诉应用用哪个',
        '    set ATLAS_PYTHON=D:\\path\\to\\python.exe',
        '    然后重新启动本应用',
      ].join('\n');
    },
  },
  TIMEOUT: {
    title: '等待本地服务就绪超时',
    body: '后端进程已启动，但 120 秒内没能响应健康检查。',
    suggest: () => [
      '常见原因：',
      '  · 依赖没装齐 —— 后端一启动就抛异常退出，日志末尾会看到 traceback',
      '  · 8000 端口被别的程序占用 —— 换端口后重启：set ATLAS_PORT=8010',
      '  · 首次运行要建 SQLite 表，机器较慢时偶尔超时',
      '',
      '展开下方「运行日志」，最后几行通常就能定位问题。',
    ].join('\n'),
  },
  BOOTSTRAP_ERROR: {
    title: '启动流程异常',
    body: '桌面壳自身在启动过程中抛了异常。',
    suggest: () => '展开下方「运行日志」查看完整堆栈。',
  },
};

function showFailure(payload) {
  const code = payload?.code;
  // 打包与否决定指引怎么说：安装版外面没有仓库，不能让人去建 .venv
  const ctx = {
    packaged: Boolean(payload?.packaged),
    backendRoot: payload?.backendRoot || '',
  };

  const g = FAILURE_GUIDE[code] || {
    title: '启动失败',
    body: `未知错误（${code}）`,
    suggest: () => '展开下方「运行日志」查看详情。',
  };

  el.stage.hidden = true;
  el.fail.hidden = false;
  el.failTitle.textContent = g.title;
  el.failBody.textContent = g.body;
  el.failSuggest.textContent = typeof g.suggest === 'function' ? g.suggest(ctx) : g.suggest;
  el.logBox.open = true;
}

// ---------- 事件接线 ----------

if (!bridge) {
  el.status.textContent = '桥接层未加载';
  el.hint.textContent = 'preload 脚本没有生效，请检查 desktop/preload.js 是否存在。';
} else {
  bridge.onLog(appendLog);

  bridge.onReady(({ baseUrl }) => {
    el.spinner.classList.add('done');
    el.status.textContent = '服务已就绪';
    el.hint.textContent = `正在打开界面 · ${baseUrl}/client`;
  });

  bridge.onFailed((payload) => showFailure(payload));

  bridge.onBackendExit(({ code, signal }) => {
    if (el.fail.hidden === false) return; // 已经在报错了，别叠一层
    el.stage.hidden = true;
    el.gone.hidden = false;
    el.logBox.open = true;
    appendLog(`--- 后端进程退出：code=${code} signal=${signal} ---`);
  });

  $('btnRetry').addEventListener('click', async () => {
    el.fail.hidden = true;
    el.stage.hidden = false;
    el.status.textContent = '正在重试…';
    el.hint.textContent = '重新寻找解释器并启动后端';
    el.spinner.classList.remove('done');
    await bridge.restart();
  });

  $('btnRestart').addEventListener('click', async () => {
    el.gone.hidden = true;
    el.stage.hidden = false;
    el.status.textContent = '正在重启服务…';
    el.spinner.classList.remove('done');
    await bridge.restart();
  });

  for (const id of ['btnLog', 'btnLog2']) {
    $(id).addEventListener('click', () => bridge.revealLog());
  }
}
