"""运行时 API Key 凭据库 —— 让"进程已经跑起来了"之后还能换密钥。

为什么需要这一层：
    `config.py` 的密钥读取路径是 `os.environ`，这是**对**的：环境变量不落盘、
    不进口令、子进程可继承。但它有个前提——密钥必须在**进程启动前**就设好。
    而 Web 界面的场景恰恰相反：服务早起来了，用户才在页面上填 Key，
    总不能让他为了换个 Key 去 `setx` 然后重启服务。

    于是这里做一层"进程内可写"的凭据缓存，写入方式是把值注入 `os.environ`。
    好处是**下游完全无感知**：`build_chat_model` / `detect_provider` / 拉起的
    MCP 子进程，走的还是原来那条环境变量路径，一行都不用改。

保密设计（五条硬约束，按重要性排）：
    1. **默认不落盘**。`remember=False` 时密钥只活在进程内存里，进程一退就没了。
       这是默认值，因为"不落盘"永远比"落盘再想办法保护"更安全。
    2. 落盘必须显式勾选，位置 `ATLAS_HOME/credentials.json`，POSIX 下 chmod 0600，
       写入走"临时文件 + os.replace"原子替换，不留半截文件。
    3. **对外只给掩码**。`CredentialInfo.masked` 是唯一的展示出口；
       完整值只存在于进程内存与 `os.environ` 里，不写日志、不进浏览器存储。
    4. **清除时恢复被覆盖的原值**。否则用户在界面上清掉一个 Key，
       会连带把他原本在环境变量里配好的那份也抹掉——这是数据损坏，不是安全。
    5. 所有密钥值一进内存就登记到 `redact`，此后日志里出现即自动打码。

一个必须讲清楚的副作用：
    注入 `os.environ` 意味着 **MCP Server 子进程也会继承这个密钥**。
    本项目的 MCP Server 都是自己带的本地进程（stdio），这个继承是预期的；
    但如果你换成连第三方 MCP Server，就要重新评估要不要走这条路。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agent_kit.home import atlas_home
from agent_kit.logging_conf import get_logger
from agent_kit.redact import forget_secret, mask, register_secret

log = get_logger("agent.credentials")

CREDENTIALS_FILENAME = "credentials.json"
FILE_VERSION = 1

# 需要密钥的 provider → 对应的环境变量名。
# **顺序必须与 config.detect_provider() 一致**，否则"自动探测"和"界面展示"会打架。
ENV_NAME: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

MANAGED_PROVIDERS: tuple[str, ...] = tuple(ENV_NAME)

# 展示用的名字（前端直接用，省得两边各维护一份）
PROVIDER_LABEL: dict[str, str] = {
    "dashscope": "阿里云百炼 DashScope",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic Claude",
}

MIN_KEY_LENGTH = 8


def clean_key_input(raw: str) -> str:
    """容忍粘贴式输入：`DASHSCOPE_API_KEY=sk-xxx`、`export X="sk-xxx"`、带引号带换行。

    用户在终端里复制密钥时经常连着变量名、引号、`export` 一起拷过来。
    与其让他自己去删（然后失败一次再来一次），不如在这里剥干净。
    这不降低任何安全性——剥的都是包装，密钥本体一个字符没动。
    """
    text = (raw or "").strip()
    if "=" in text:
        head, _, tail = text.partition("=")
        name = head.strip()
        # 只在等号左边确实像「环境变量名」时才剥离。判据刻意收窄：
        # 要么带 `export ` 前缀，要么是全大写标识符。
        # 不能放宽到任意标识符——`abc=def=ghi` 这种密钥本体含 '=' 的会被误伤。
        looks_like_var = False
        if name.lower().startswith("export "):
            looks_like_var = name[7:].strip().isidentifier()
        else:
            looks_like_var = name.isidentifier() and name.isupper()
        if looks_like_var:
            text = tail.strip()
    return text.strip().strip('"').strip("'").strip()


@dataclass
class CredentialInfo:
    """一个 provider 的凭据**状态**。注意：这里永远不会出现完整密钥。"""

    provider: str
    label: str
    env_name: str
    configured: bool
    masked: str
    length: int
    origin: str          # env（启动前就有） | runtime（本次运行由界面注入） | none
    persistent: bool     # 是否已写入本机凭据文件
    shadowed: bool       # 界面输入是否覆盖了一个原本存在的环境变量

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "label": self.label,
            "env_name": self.env_name,
            "configured": self.configured,
            "masked": self.masked,
            "length": self.length,
            "origin": self.origin,
            "persistent": self.persistent,
            "shadowed": self.shadowed,
        }


class CredentialsStore:
    """进程内凭据库。线程安全靠"赋值即原子"+ 单次写盘，不引入额外锁。

    为什么不上锁：这里的操作只有几种——读环境变量、写字典、写文件。
    字典赋值与 `os.environ` 赋值在 CPython 里都是原子的，最坏情况是并发保存
    两个不同 provider 时写盘顺序交错，结果是文件里少存一个——用户重填一次即可，
    不会损坏已有数据。为这点概率引入全局锁，反而会让"换密钥"这种操作变复杂。
    """

    def __init__(self, home: Path | str | None = None) -> None:
        self._home = Path(home) if home is not None else None
        # 本次运行由界面注入的值
        self._runtime: dict[str, str] = {}
        # 首次接管某 provider 时记下它原本的值，清除时好还原
        self._pre_existing: dict[str, str | None] = {}
        # 已写入本机凭据文件的 provider
        self._persisted: set[str] = set()

    # ------------------------------------------------------------ 路径
    @property
    def path(self) -> Path:
        """凭据文件位置。`home` 固定则用它，否则动态解析 ATLAS_HOME（测试靠这个隔离）。"""
        root = self._home if self._home is not None else atlas_home()
        return root / CREDENTIALS_FILENAME

    def file_exists(self) -> bool:
        return self.path.is_file()

    # ------------------------------------------------------------ 写
    def set(self, provider: str, raw_key: str, *, remember: bool = False) -> CredentialInfo:
        """保存（并立即生效）一个 provider 的密钥。

        Args:
            remember: False（默认）只放内存，进程退出即消失。
                      True 额外写入本机凭据文件，下次启动自动载入。

        Raises:
            ValueError: provider 不认识，或密钥形态明显不对（过短 / 含空白）。
        """
        provider = (provider or "").strip().lower()
        if provider not in ENV_NAME:
            raise ValueError(f"未知 provider：{provider or '(空)'}，可选：{'、'.join(MANAGED_PROVIDERS)}")

        key = clean_key_input(raw_key)
        if len(key) < MIN_KEY_LENGTH:
            raise ValueError(f"密钥长度不足 {MIN_KEY_LENGTH} 个字符，请检查是否复制完整")
        if any(ch.isspace() for ch in key):
            raise ValueError("密钥中不应包含空白字符，请检查是否混入了换行或空格")

        env_name = ENV_NAME[provider]
        if provider not in self._runtime:
            # 首次由界面接管：先把原值存起来，清除时才能还原，而不是直接抹掉
            self._pre_existing[provider] = os.environ.get(env_name)

        self._runtime[provider] = key
        os.environ[env_name] = key
        register_secret(key)

        if remember:
            self._persisted.add(provider)
            self._write_file()

        log.info(
            "凭据已更新：provider=%s 值=%s 长度=%d 落盘=%s",
            provider, mask(key), len(key), "是" if remember else "否",
        )
        return self.info(provider)

    def clear(self, provider: str, *, forget: bool = False) -> bool:
        """移除内存中的密钥；`forget=True` 时同时从本机凭据文件里删掉。

        **如果这个 provider 原本就有环境变量，这里会把原值恢复回去**，
        而不是简单地把环境变量删掉——见模块开头第 4 条。
        """
        provider = (provider or "").strip().lower()
        if provider not in ENV_NAME:
            return False

        env_name = ENV_NAME[provider]
        removed = self._runtime.pop(provider, None)
        if removed:
            forget_secret(removed)

        original = self._pre_existing.pop(provider, None)
        if original:
            os.environ[env_name] = original
            register_secret(original)
        else:
            os.environ.pop(env_name, None)

        if forget:
            self._persisted.discard(provider)
            self._write_file()

        log.info("凭据已清除：provider=%s（落盘记录一并删除=%s）", provider, "是" if forget else "否")
        return True

    # ------------------------------------------------------------ 读
    def reveal(self, provider: str) -> str | None:
        """取回**完整**密钥值。仅供本机接口的"显式查看"使用。

        这里刻意记一条审计日志（不含值），因为"谁在什么时候把密钥掏出来看了"
        本身就是要留痕的事件——只有把动作记下来，事后才说得清。
        """
        provider = (provider or "").strip().lower()
        if provider not in ENV_NAME:
            return None
        value = self._runtime.get(provider) or os.environ.get(ENV_NAME[provider])
        if value:
            log.info("凭据被显式读取：provider=%s 值=%s（审计记录，不含明文）", provider, mask(value))
        return value

    def info(self, provider: str) -> CredentialInfo:
        provider = (provider or "").strip().lower()
        env_name = ENV_NAME.get(provider, "")
        value = self._runtime.get(provider) or (os.environ.get(env_name) if env_name else None) or ""

        if provider in self._runtime:
            origin = "runtime"
        elif value:
            origin = "env"
        else:
            origin = "none"

        return CredentialInfo(
            provider=provider,
            label=PROVIDER_LABEL.get(provider, provider),
            env_name=env_name,
            configured=bool(value),
            masked=mask(value),
            length=len(value),
            origin=origin,
            persistent=provider in self._persisted,
            shadowed=provider in self._runtime and bool(self._pre_existing.get(provider)),
        )

    def list(self) -> list[CredentialInfo]:
        """按 config.detect_provider 的优先级顺序列出全部受管 provider。"""
        return [self.info(p) for p in MANAGED_PROVIDERS]

    def active_provider(self) -> str:
        """当前会自动选中哪个 provider；一个都没配就是 `fake`（离线脚本模型）。

        判定顺序与 `config.detect_provider()` 保持一致——两处不一致的话，
        界面上写着"当前 dashscope"，实际跑起来却是 openai，这种偏差极难排查。
        """
        for provider in MANAGED_PROVIDERS:
            if os.environ.get(ENV_NAME[provider]):
                return provider
        return "fake"

    # ------------------------------------------------------------ 落盘
    def load_persisted(self) -> list[str]:
        """启动时把凭据文件里的密钥注入环境变量。返回实际载入的 provider 列表。

        **环境变量优先**：如果某个 provider 在系统环境里已经配了，
        文件里的那份就不覆盖它（环境变量是用户更明确的意图）。
        """
        data = self._read_file()
        if not data:
            return []

        loaded: list[str] = []
        for provider, item in data.items():
            if provider not in ENV_NAME or not isinstance(item, dict):
                continue
            key = clean_key_input(str(item.get("api_key") or ""))
            if len(key) < MIN_KEY_LENGTH:
                continue

            self._persisted.add(provider)
            register_secret(key)
            if provider in self._runtime or os.environ.get(ENV_NAME[provider]):
                continue  # 已有更高优先级的来源，不动

            self._runtime[provider] = key
            os.environ[ENV_NAME[provider]] = key
            loaded.append(provider)
        return loaded

    def storage_note(self) -> str:
        """凭据文件的可读权限现状，用于向用户交代"到底存哪、怎么保护的"。"""
        path = self.path
        if not path.is_file():
            return "尚未写入磁盘"
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            return "权限读取失败"
        if os.name == "nt":
            return "已写入；Windows 下 chmod 只影响只读位，实际保护来自用户目录 ACL"
        return f"已写入；权限 {oct(mode)}"

    # ------------------------------------------------------------ 内部
    def _read_file(self) -> dict:
        path = self.path
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 文件坏了不能把服务拖挂：当作没有凭据，让用户重填
            log.warning("凭据文件读取失败，已忽略：%s", exc)
            return {}
        items = raw.get("credentials") if isinstance(raw, dict) else None
        return items if isinstance(items, dict) else {}

    def _write_file(self) -> None:
        """原子写入。没有任何 provider 需要保留时直接删文件，不留空壳。"""
        path = self.path
        if not self._persisted:
            self._delete_file()
            return

        payload = {
            "version": FILE_VERSION,
            "note": "由 Atlas Web 界面写入的本机凭据。请勿提交到版本库，请勿拷贝到公共电脑。",
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "credentials": {
                p: {"api_key": self._runtime[p], "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                for p in sorted(self._persisted)
                if p in self._runtime
            },
        }

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            _restrict(path)
        except OSError as exc:
            log.error("凭据写盘失败：%s", exc)
            raise

    def _delete_file(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("凭据文件删除失败：%s", exc)

    def notes(self) -> list[str]:
        """给界面看的保密说明。文案放在后端，保证 API 与界面口径一致。"""
        out = [
            "密钥默认只存在于服务进程内存里，进程退出即消失，不写磁盘。",
            f"只有勾选「记住到本机」才写入 {self.path}，并尽力收紧为仅属主可读写。",
            "接口对外只返回掩码；完整值不进日志、不进浏览器存储，也不会随页面刷新留在本地。",
        ]
        if os.name == "nt":
            out.append("Windows 上 chmod 只能改只读位，实际保护来自用户目录 ACL；公共电脑上建议不要勾选「记住到本机」。")
        return out


def _restrict(path: Path) -> None:
    """把凭据文件收紧到仅属主可读写。

    POSIX 上是实打实的 0600。Windows 上 chmod 只能切换只读位，
    真正的保护来自 `%USERPROFILE%` 目录默认的 ACL（仅本人与管理员可进）。
    这里不假装 Windows 也被保护住了——`storage_note()` 会把实情告诉用户。
    """
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("凭据文件权限收紧失败（不影响功能，但请注意保护该文件）：%s", exc)


# 进程级单例（≈ Spring 里那个 @Bean 出来的 CredentialsService）
store = CredentialsStore()
