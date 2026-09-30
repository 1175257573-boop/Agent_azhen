"""部署编排验证 —— 阶段五。

本机跑不了 docker（`docker` 命令不存在），所以验证分两层：

**静态层**：解析 `docker-compose.yml` 与 `Dockerfile`，把「改了端口忘了改 URL」
「Agent 和记忆服务没共享同一个卷」「健康检查会级联下游」这类只有上线才炸的
配置错误，在 CI 里就拦下来。

**运行层**：按 compose 里声明的环境变量，把 gateway / agent / memory **三个真实
子进程**拉起来（域名换成 127.0.0.1、端口换成空闲端口，其余变量照抄），
真正验证跨进程拓扑与共享存储。这层不依赖 docker，但验证的是同一套配置。

零 API Key、零外部服务（SQLite 后端），离线可复现。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
STARTUP_TIMEOUT = 180.0

APP_SERVICES = ("gateway", "agent", "memory")


# ---------------------------------------------------------------------------
# 静态：compose
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _env_of(service: dict) -> dict:
    env = service.get("environment") or {}
    return {str(k): ("" if v is None else str(v)) for k, v in env.items()}


_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str, environ: dict | None = None) -> str:
    """按 compose 的规则展开 `${VAR}` / `${VAR:-默认}`。

    不展开就会把字面量 `${LONG_TERM_BACKEND:-sqlite}` 当成后端名传进去，
    于是长期存储静默降级成内存 —— 线上表现为「偏好存了但重启就没」，
    而日志里只有一行 warn。部署配置必须按真实语义解析才验得出来。
    """
    environ = os.environ if environ is None else environ

    def repl(match: re.Match) -> str:
        name, default = match.group(1), match.group(2) or ""
        return str(environ.get(name) or default)

    return _INTERPOLATION.sub(repl, value)


def test_compose_declares_the_three_app_services(compose):
    """三个应用服务都在，且共用同一个构建（一个镜像三种角色）。"""
    services = compose["services"]
    for name in APP_SERVICES:
        assert name in services, f"compose 里缺少服务 {name}"

    builds = {services[n]["build"]["dockerfile"] for n in APP_SERVICES}
    assert builds == {"Dockerfile"}, f"三个服务应该共用同一个 Dockerfile，实际 {builds}"

    modules = {_env_of(services[n]).get("APP_MODULE") for n in APP_SERVICES}
    assert modules == {
        "server.gateway:app",
        "server.agent_app:app",
        "server.memory_app:app",
    }, f"角色模块不对：{modules}"


def test_gateway_urls_point_at_real_services_and_matching_ports(compose):
    """网关的下游地址必须是 compose 里的服务名，端口要和该服务实际监听的一致。

    这是最容易漂的地方：改了 `agent` 的 PORT，忘了改 `AGENT_SERVICE_URL`，
    起起来是 502，而且看日志只会看到「连接被拒绝」。
    """
    services = compose["services"]
    gw_env = _env_of(services["gateway"])

    for key, target in (("AGENT_SERVICE_URL", "agent"), ("MEMORY_SERVICE_URL", "memory")):
        url = gw_env.get(key, "")
        assert url.startswith(f"http://{target}:"), \
            f"{key}={url} 应该指向 compose 服务名 {target}"

        port_in_url = url.rsplit(":", 1)[-1]
        port_declared = _env_of(services[target]).get("PORT")
        assert port_in_url == port_declared, \
            f"{key} 里的端口 {port_in_url} 与 {target} 的 PORT={port_declared} 不一致"
        assert port_in_url in [str(p) for p in services[target].get("expose", [])], \
            f"{target} 的 {port_in_url} 没有写进 expose，网关连不上"


def test_agent_and_memory_share_the_state_volume(compose):
    """Agent 与记忆服务必须挂同一个卷到同一个 ATLAS_HOME。

    Agent 写的会话流水存在这里，记忆服务读的也是这里 —— 卷不共享，
    会话列表就是空的，而且两个进程各写一份，谁也看不见谁。
    """
    services = compose["services"]

    def mounts(name: str) -> tuple[str, str]:
        home = _env_of(services[name]).get("ATLAS_HOME")
        entries = services[name].get("volumes") or []
        for entry in entries:
            vol, _, path = str(entry).partition(":")
            if path == home:
                return vol, path
        pytest.fail(f"{name} 没有把任何卷挂到 ATLAS_HOME={home}")

    agent_vol, agent_home = mounts("agent")
    memory_vol, memory_home = mounts("memory")

    assert agent_vol == memory_vol, f"卷不一样：{agent_vol} vs {memory_vol}"
    assert agent_home == memory_home, f"ATLAS_HOME 不一样：{agent_home} vs {memory_home}"
    assert agent_vol in compose["volumes"], f"卷 {agent_vol} 没有在顶层 volumes 里声明"


def test_only_the_gateway_publishes_ports(compose):
    """只有网关对宿主发布端口；Agent / 记忆服务只在容器网络内可达。"""
    services = compose["services"]
    assert services["gateway"].get("ports"), "网关必须发布端口，否则外部访问不到"
    for name in ("agent", "memory"):
        assert not services[name].get("ports"), \
            f"{name} 不该对宿主发布端口（对外只允许经网关进入）"


def test_gateway_waits_for_healthy_downstreams(compose):
    """网关要等下游 healthy 再启动，否则前几秒的请求全是 502。"""
    deps = compose["services"]["gateway"].get("depends_on") or {}
    for name in ("agent", "memory"):
        assert deps.get(name, {}).get("condition") == "service_healthy", \
            f"gateway 对 {name} 的依赖没有用 service_healthy：{deps.get(name)}"


def test_healthchecks_do_not_cascade_to_downstreams(compose):
    """容器健康检查只查自己。

    网关若把 `/api/health` 当探针，下游一挂它就把自己标成不健康，
    编排器跟着重启一个本来健康的网关 —— 局部故障被放大成整体抖动。
    """
    for name in APP_SERVICES:
        env = _env_of(compose["services"][name])
        path = env.get("HEALTH_PATH", "")
        assert path, f"{name} 没有配置 HEALTH_PATH"
        if name == "gateway":
            assert "health" not in path, \
                f"网关的健康检查不能走 /api/health（会级联下游），实际 {path}"
        else:
            assert "probe_backends=false" in path or "health" not in path, \
                f"{name} 的健康检查不该探测后端，实际 {path}"


def test_no_hardcoded_secrets_in_compose(compose):
    """密钥只从宿主环境 / .env 注入：compose 里出现的一律是 ${...} 引用。"""
    secret_keys = ("API_KEY", "PASSWORD", "SECRET", "TOKEN")

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and any(s in key.upper() for s in secret_keys):
                    # 允许空串，也允许 ${...} 插值；不允许明文
                    assert value == "" or "${" in value, \
                        f"compose 里疑似硬编码了密钥：{key}={value}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(compose)


def test_default_backends_resolve_to_persistent_sqlite(compose):
    """默认后端展开后必须是 sqlite —— 内存后端一重启就什么都不剩。

    按 compose 语义展开（不是看字面量）：字面量是 `${...:-sqlite}`，
    真正生效的值才决定进程行为。
    """
    common = _env_of(compose["x-app"])
    empty: dict[str, str] = {}  # 宿主没设任何变量时的展开结果
    assert _expand(common["SHORT_TERM_BACKEND"], empty) == "sqlite"
    assert _expand(common["LONG_TERM_BACKEND"], empty) == "sqlite"
    # 显式覆盖也要能穿透
    assert _expand(common["SHORT_TERM_BACKEND"],
                   {"SHORT_TERM_BACKEND": "redis"}) == "redis"


def test_optional_backends_are_behind_profiles(compose):
    """Redis / PostgreSQL 默认不起 —— 默认 SQLite 后端，不该为一个用不上的 PG 拉镜像。"""
    services = compose["services"]
    for name in ("redis", "postgres"):
        assert name in services[name].get("profiles", []), \
            f"{name} 应该放在 profile 后面，默认不启动"


# ---------------------------------------------------------------------------
# 静态：Dockerfile / .dockerignore
# ---------------------------------------------------------------------------
def test_dockerfile_builds_frontend_then_runtime():
    """两阶段构建：node 编前端 → python 只装运行时，且把产物拷进来。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    stages = [line.split()[1] for line in text.splitlines()
              if line.startswith("FROM ") and " AS " in line]
    assert len(stages) == 2, f"应该是两阶段构建，实际 {stages}"

    frontend_stage, runtime_stage = stages
    assert "node:" in text.split(f"AS {frontend_stage}")[0], "第一个阶段应该是 node 镜像"
    assert "python:" in text.split(f"AS {runtime_stage}")[0].split(f"AS {frontend_stage}")[-1], \
        "第二个阶段应该是 python 镜像"
    assert "web/dist" in text, "运行时没有把前端产物拷进来"


def test_runtime_does_not_run_as_root_and_has_healthcheck():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER atlas" in text or "USER 10001" in text, "容器不该以 root 运行"
    assert "HEALTHCHECK" in text, "镜像缺少 HEALTHCHECK"


def test_image_defaults_match_the_gateway_role(compose):
    """镜像默认角色 = 网关：PORT 与 HEALTH_PATH 要和 compose 里 gateway 的一致。

    否则 `docker run` 单独拉一个容器（不传环境变量）会跑出一个和编排不一致的角色。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")

    def default_of(name: str) -> str:
        # ENV A=1 \ B=2 续行写法：把所有 ENV 块拼成一整行再找
        joined = " ".join(
            line.strip().rstrip("\\").strip()
            for line in text.splitlines()
            if line.strip().startswith(("ENV ", "ATLAS_HOME=", "APP_MODULE=", "PORT=", "HEALTH_PATH="))
        )
        match = re.search(rf"\b{name}=(\S+)", joined)
        assert match, f"Dockerfile 的 ENV 里没有 {name}"
        return match.group(1)

    gw_env = _env_of(compose["services"]["gateway"])
    assert default_of("APP_MODULE") == gw_env["APP_MODULE"]
    assert default_of("PORT") == gw_env["PORT"]
    assert default_of("HEALTH_PATH") == gw_env["HEALTH_PATH"]


def test_container_healthcheck_script_reports_self_status():
    """健康检查脚本：活着返回 0，路径错 / 端口没人听 都返回 1。

    探针把 404 或连接失败当成「健康」，编排器就永远发现不了故障容器。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # 只有 /api/info 是 200，其他一律 404 —— 探针要是把 404 当活着就露馅
            self.send_response(200 if self.path == "/api/info" else 404)
            self.end_headers()

        def log_message(self, *args):  # 别把测试输出弄脏
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def probe(port_value: str, path: str) -> int:
            env = {**os.environ, "PORT": port_value, "HEALTH_PATH": path}
            return subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "deploy" / "healthcheck.py")],
                env=env, capture_output=True,
            ).returncode

        assert probe(str(port), "/api/info") == 0, "活着却报不健康"
        assert probe(str(port), "/nope") == 1, "404 被当成了健康"
        assert probe(str(_free_port()), "/api/info") == 1, "端口没人听却报健康"
    finally:
        server.shutdown()


def test_dockerignore_excludes_dependencies_and_secrets():
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for pattern in ("web/node_modules", "__pycache__", ".git", ".env"):
        assert pattern in text, f".dockerignore 应该排除 {pattern}"


# ---------------------------------------------------------------------------
# 运行层：按 compose 的配置真起三个进程
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _compose_env(compose: dict, home: Path, **overrides) -> dict:
    """照抄 compose 的公共环境变量，只把路径与模型换成离线可跑的。"""
    common = {k: _expand(v) for k, v in _env_of(compose["x-app"]).items()}
    env = os.environ.copy()
    env.update({
        "ATLAS_HOME": str(home),
        "SHORT_TERM_BACKEND": common["SHORT_TERM_BACKEND"],
        "LONG_TERM_BACKEND": common["LONG_TERM_BACKEND"],
        "MEMORY_STRICT": common["MEMORY_STRICT"],
        "LLM_PROVIDER": "fake",          # 零 Key 冒烟
        "PYTHONIOENCODING": "utf-8",     # Windows 默认 GBK，中文日志会让子进程写挂
    })
    for name in ("REDIS_URL", "PG_DSN", "POSTGRES_URL", "DATABASE_URL",
                 "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
                 "DASHSCOPE_API_KEY", "SQLITE_PATH"):
        env.pop(name, None)
    env.update(overrides)
    return env


def _wait_up(proc: subprocess.Popen, port: int, log_file: Path, path: str = "/api/health") -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"服务进程提前退出（exit={proc.returncode}），日志：\n"
                + log_file.read_text(encoding="utf-8", errors="replace")[-3000:]
            )
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"服务 {port} 启动超时，日志：\n"
                       + log_file.read_text(encoding="utf-8", errors="replace")[-3000:])


@pytest.fixture(scope="module")
def stack(compose):
    """按 compose 的拓扑拉起 gateway / agent / memory 三个真实子进程。"""
    work = Path(tempfile.mkdtemp(prefix="deploy-stack-"))
    home = work / "atlas-home"
    home.mkdir(parents=True)

    agent_port, memory_port, gw_port = _free_port(), _free_port(), _free_port()
    base = _compose_env(compose, home)

    def spawn(module: str, port: int, extra: dict, ready_path: str):
        log_file = work / f"{module}.log"
        fh = log_file.open("w", encoding="utf-8")
        env = {**base, **extra}
        proc = subprocess.Popen(
            [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", str(port)],
            env=env, cwd=str(PROJECT_ROOT), stdout=fh, stderr=subprocess.STDOUT,
        )
        proc._log_fh = fh  # type: ignore[attr-defined]
        return proc, log_file, port, ready_path

    agent = spawn("server.agent_app", agent_port, {}, "/api/health")
    memory = spawn("server.memory_app", memory_port, {}, "/api/health")
    gw = spawn(
        "server.gateway", gw_port,
        {"AGENT_SERVICE_URL": f"http://127.0.0.1:{agent_port}",
         "MEMORY_SERVICE_URL": f"http://127.0.0.1:{memory_port}"},
        # 网关的容器健康检查就是这个路径， readiness 也用它
        _env_of(compose["services"]["gateway"]).get("HEALTH_PATH", "/api/info"),
    )

    procs = [agent, memory, gw]
    try:
        # 并行预热：串行等会把 langchain 的导入时间乘以三
        for proc, log, port, path in procs:
            _wait_up(proc, port, log, path)
        yield {"gateway": gw_port, "agent": agent_port, "memory": memory_port, "home": home}
    finally:
        for proc, *_ in procs:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc._log_fh.close()  # type: ignore[attr-defined]


def test_stack_comes_up_with_all_three_processes_healthy(stack):
    """三个进程都活着，且网关的聚合健康检查报全绿。"""
    health = httpx.get(f"http://127.0.0.1:{stack['gateway']}/api/health", timeout=10.0).json()
    assert health["ok"] is True, f"健康聚合格：{health}"
    assert health["agent"]["ok"] and health["memory"]["ok"], f"下游没起来：{health}"

    info = httpx.get(f"http://127.0.0.1:{stack['gateway']}/api/info", timeout=10.0)
    assert info.status_code == 200, "网关本地接口（容器健康检查用的那个）必须应答"


def test_chat_streams_through_gateway_to_agent_process(stack):
    """对话 SSE 穿过网关打到 Agent 子进程：能收到多帧，且以 done 收尾。"""
    payload = {"message": "你好", "thread_id": "deploy-1", "mode": "chat",
               "provider": "fake", "user_id": "deploy", "role": "admin", "enable_mcp": False}

    async def scenario():
        frames = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", f"http://127.0.0.1:{stack['gateway']}/api/chat/stream",
                                     json=payload) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        frames.append(json.loads(line[len("data:"):].strip()))
        return frames

    frames = asyncio.run(scenario())
    types = [f["type"] for f in frames]
    assert len(frames) > 1, f"SSE 被缓冲成了一块：{len(frames)} 帧"
    assert "token" in types and types[-1] == "done", f"事件流不完整：{types}"


def test_shared_state_makes_agent_writes_visible_to_memory_process(stack):
    """共享卷的效果：Agent 写的会话流水，记忆服务那一侧看得到。

    两个进程各写各的那份（rollout / long_term_store），但都落在
    ATLAS_HOME 指向的同一个库里 —— 卷不共享这条就不成立。
    """
    # 记忆服务写入长期偏好（走网关 → 记忆服务子进程）
    put = httpx.post(f"http://127.0.0.1:{stack['gateway']}/api/memory/preferences",
                     json={"user_id": "deploy", "key": "style", "value": "结构化"}, timeout=10.0)
    assert put.status_code == 200, put.text

    import sqlite3

    db = stack["home"] / "atlas.db"
    assert db.exists(), f"共享库没建起来：{db}"

    tables = {r[0] for r in sqlite3.connect(str(db)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "rollout" in tables, f"Agent 没有把会话流水写进共享库：{tables}"
    assert "long_term_store" in tables, f"记忆服务没有把偏好写进共享库：{tables}"

    rows = sqlite3.connect(str(db)).execute(
        "SELECT COUNT(*) FROM rollout WHERE thread_id='deploy-1'").fetchone()[0]
    assert rows > 0, "deploy-1 这个会话的流水不在共享库里"

    # 记忆服务确认自己用的就是这个库（sqlite[atlas.db] = ATLAS_HOME 下的库）
    status = httpx.get(f"http://127.0.0.1:{stack['memory']}/api/memory/status", timeout=10.0).json()
    assert "atlas.db" in json.dumps(status, ensure_ascii=False), \
        f"记忆服务的后端没有落在共享卷上：{status}"


def test_local_launcher_brings_up_the_whole_stack(compose):
    """`deploy/up_local.py` 真能把三进程栈拉起来，退出时收干净。

    这是没有 Docker 的机器上验证部署拓扑的手段：脚本用的环境变量与 compose 同源，
    所以「脚本能跑、容器跑不起来」这种偏差基本不存在。
    """
    work = Path(tempfile.mkdtemp(prefix="launcher-"))
    home = work / "home"
    log_dir = work / "logs"
    home.mkdir(parents=True)

    ports = {name: _free_port() for name in ("gateway", "agent", "memory")}
    log_file = work / "launcher.log"
    spawn_kwargs = {}
    if os.name == "nt":
        # Windows 上 terminate()=TerminateProcess（硬杀，信号处理器跑不到），
        # 必须建独立进程组后用控制台事件退出，才能验证「一起收干净」这条
        spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    with log_file.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "deploy" / "up_local.py"),
             "--provider", "fake", "--home", str(home), "--log-dir", str(log_dir),
             "--gateway-port", str(ports["gateway"]),
             "--agent-port", str(ports["agent"]),
             "--memory-port", str(ports["memory"])],
            cwd=str(PROJECT_ROOT), stdout=fh, stderr=subprocess.STDOUT, **spawn_kwargs,
        )

    try:
        # 等它自己报就绪（脚本内部已经探过三个进程）
        deadline = time.monotonic() + STARTUP_TIMEOUT
        started = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("启动器提前退出：" + log_file.read_text(encoding="utf-8", errors="replace")[-2000:])
            if "已启动" in log_file.read_text(encoding="utf-8", errors="replace"):
                started = True
                break
            time.sleep(0.5)
        assert started, "启动器没有在超时前报就绪：" + log_file.read_text(encoding="utf-8", errors="replace")[-2000:]

        base = f"http://127.0.0.1:{ports['gateway']}"
        with httpx.Client(timeout=15.0, trust_env=False) as client:
            health = client.get(f"{base}/api/health").json()
            assert health["ok"] is True, f"三进程栈健康聚合不合格：{health}"
            # 前端也得在（没构建过会退回旧页，同样是 200）
            assert client.get(f"{base}/").status_code == 200
    finally:
        if os.name == "nt":
            proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT"))
        else:
            proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 退出后不留孤儿占端口
    for name, port in ports.items():
        with socket.socket() as s:
            s.settimeout(1)
            assert s.connect_ex(("127.0.0.1", port)) != 0, \
                f"{name} 的端口 {port} 在启动器退出后仍然被占着"


def test_three_processes_hammering_one_sqlite_file_survive(stack):
    """部署态的并发：三个进程同时读写同一个 SQLite 库，不能报 database is locked。

    这正是阶段一修的那个坑（每次连接都跑 DDL + WAL，并发初始化会随机炸），
    在这里以真实进程拓扑再压一遍。
    """
    async def one_chat(i: int):
        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream(
                "POST", f"http://127.0.0.1:{stack['gateway']}/api/chat/stream",
                json={"message": f"第{i}条", "thread_id": f"deploy-conc-{i}", "mode": "chat",
                      "provider": "fake", "user_id": "deploy", "role": "admin",
                      "enable_mcp": False},
            ) as resp:
                frames = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        frames.append(json.loads(line[len("data:"):].strip()))
        return frames

    async def one_pref(i: int):
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{stack['gateway']}/api/memory/preferences",
                json={"user_id": "deploy", "key": f"k{i}", "value": f"v{i}"})
        return resp.status_code

    async def scenario():
        chats = [one_chat(i) for i in range(3)]
        prefs = [one_pref(i) for i in range(6)]
        return await asyncio.gather(*chats, *prefs)

    results = asyncio.run(scenario())
    frames_list, codes = results[:3], results[3:]

    for i, frames in enumerate(frames_list):
        types = [f["type"] for f in frames]
        assert "error" not in types, f"并发第 {i} 条报错：{[f for f in frames if f['type'] == 'error']}"
        assert types and types[-1] == "done", f"并发第 {i} 条没跑完：{types}"

    assert all(c == 200 for c in codes), f"并发写偏好失败：{codes}"
