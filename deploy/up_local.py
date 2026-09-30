"""本地三进程部署 —— 不依赖 Docker 的等价启动器。

docker compose 是正式部署方式；但在没有 Docker 的机器上（或只是想改一行 Python
立刻看效果、不想等镜像构建），用这个脚本可以拉起**同一套拓扑**：

    python deploy/up_local.py

    :8000  gateway  对外唯一入口（含前端）
    :8001  agent    Agent 执行服务
    :8002  memory   记忆服务
    共享 ATLAS_HOME —— Agent 写的会话，记忆服务看得到

环境变量与 docker-compose.yml 的 x-app 段保持一致
（ATLAS_HOME / SHORT_TERM_BACKEND / LONG_TERM_BACKEND / 模型 Key），
所以「脚本里能跑、容器里跑不了」这类偏差不会出现。

Ctrl-C 一次退出，三个进程一起收干净，不留孤儿占端口。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

# 角色 → 默认端口。与 docker-compose.yml 保持一致
ROLES = {
    "agent": ("server.agent_app", 8001),
    "memory": ("server.memory_app", 8002),
    "gateway": ("server.gateway", 8000),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(port: int, path: str, timeout: float, proc: subprocess.Popen, log: Path) -> bool:
    """轮询就绪。trust_env=False：本机请求绝不走 HTTP_PROXY（否则会被代理劫持）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with httpx.Client(timeout=2.0, trust_env=False) as client:
                if client.get(f"http://127.0.0.1:{port}{path}").status_code == 200:
                    return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


def build_env(home: Path, args) -> dict:
    env = os.environ.copy()
    env.update({
        "ATLAS_HOME": str(home),
        "SHORT_TERM_BACKEND": args.short_term,
        "LONG_TERM_BACKEND": args.long_term,
        "MEMORY_STRICT": "0",
        "PYTHONIOENCODING": "utf-8",  # Windows 默认 GBK，中文日志会让子进程写挂
    })
    if args.provider:
        env["LLM_PROVIDER"] = args.provider
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas 本地三进程部署（无需 Docker）")
    parser.add_argument("--home", default=None,
                        help="运行态目录（默认 ~/.atlas）。三个进程共享，等价于 compose 的 atlas-data 卷")
    parser.add_argument("--provider", default=None,
                        help="模型 provider；填 fake 可零 API Key 冒烟")
    parser.add_argument("--short-term", default="sqlite", choices=["memory", "sqlite", "redis"])
    parser.add_argument("--long-term", default="sqlite", choices=["memory", "sqlite", "postgres"])
    parser.add_argument("--gateway-port", type=int, default=8000)
    parser.add_argument("--agent-port", type=int, default=8001)
    parser.add_argument("--memory-port", type=int, default=8002)
    parser.add_argument("--log-dir", default=None, help="子进程日志目录（默认不落盘，直接打到终端）")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待就绪的秒数")
    args = parser.parse_args()

    home = Path(args.home).expanduser() if args.home else Path.home() / ".atlas"
    home.mkdir(parents=True, exist_ok=True)
    log_dir = Path(args.log_dir).expanduser() if args.log_dir else None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

    ports = {"agent": args.agent_port, "memory": args.memory_port, "gateway": args.gateway_port}
    env = build_env(home, args)
    # 网关要知道下游在哪；这与 compose 里的 AGENT_SERVICE_URL / MEMORY_SERVICE_URL 同义
    env["AGENT_SERVICE_URL"] = f"http://127.0.0.1:{ports['agent']}"
    env["MEMORY_SERVICE_URL"] = f"http://127.0.0.1:{ports['memory']}"

    procs: list[tuple[str, subprocess.Popen, Path | None]] = []

    def shutdown(*_):
        print("\n正在关闭三个服务…", flush=True)
        for _name, proc, fh in procs:
            if proc.poll() is None:
                proc.terminate()
        for _name, proc, fh in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if fh:
                fh.close()
        print("已全部退出。")

    # Windows 上只有 SIGINT / SIGBREAK 真正会被投递（SIGTERM 是硬杀，处理器跑不到），
    # Linux/macOS 则靠 SIGTERM。三个都注册，哪个先来都能把子进程收干净。
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):  # 非主线程 / 平台不支持，跳过即可
            pass

    print(f"运行态目录（三个进程共享）：{home}")
    print(f"记忆后端：短期={args.short_term} 长期={args.long_term}\n")

    for name, (module, _default) in ROLES.items():
        port = ports[name]
        fh = None
        stdout = None
        if log_dir:
            fh = (log_dir / f"{name}.log").open("w", encoding="utf-8")
            stdout = fh
        proc = subprocess.Popen(
            [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", str(port)],
            env=env, cwd=str(PROJECT_ROOT), stdout=stdout, stderr=subprocess.STDOUT,
        )
        procs.append((name, proc, fh))
        print(f"  {name:<8} 已拉起 pid={proc.pid}  http://127.0.0.1:{port}")

    print("\n等待就绪…", flush=True)
    for name, proc, fh in procs:
        # 网关走 /api/info：本地应答，不等下游， readiness 与容器健康检查口径一致
        path = "/api/info" if name == "gateway" else "/api/health"
        if not wait_up(ports[name], path, args.timeout, proc, log_dir / f"{name}.log" if log_dir else Path(".")):
            print(f"\n[失败] {name} 没能在 {args.timeout}s 内就绪", file=sys.stderr)
            if log_dir:
                print((log_dir / f"{name}.log").read_text(encoding="utf-8", errors="replace")[-2000:],
                      file=sys.stderr)
            shutdown()
            return 1
        print(f"  {name:<8} ready")

    print(f"""
Atlas 已启动（三进程）

  前端 / API 入口   http://127.0.0.1:{ports['gateway']}
  健康聚合          http://127.0.0.1:{ports['gateway']}/api/health
  接口文档          http://127.0.0.1:{ports['gateway']}/docs

  agent  :{ports['agent']}   memory  :{ports['memory']}（仅本机可达，对外只走网关）

Ctrl-C 停止全部服务。""", flush=True)

    try:
        while True:
            time.sleep(1)
            if any(proc.poll() is not None for _, proc, _ in procs):
                gone = [n for n, p, _ in procs if p.poll() is not None]
                print(f"\n[异常] 服务退出：{gone}", file=sys.stderr)
                shutdown()
                return 1
    except KeyboardInterrupt:
        shutdown()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
