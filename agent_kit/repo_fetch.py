"""仓库获取：把一个 URL 或本地路径变成「一个可以评审的本地目录」。

为什么需要这一层
----------------
`main.py review --path` 只能评审已经在本机的目录，但真实的评审对象往往是
一个 GitHub 链接。这一层负责把它变成目录，并且：

    · **先 git 后 tarball**：git 能拿到历史（提交频率、贡献者、变更热点），
      是评审的重要输入；但 git 走 `github.com` 主站，网络不稳时常失败，
      所以失败自动降级到 codeload 的 tarball（只拿工作区，也够用）。
    · **默认拒绝覆盖**：目标目录已存在且非空时直接报错，不静默覆盖别人的东西。
    · **URL 白名单**：只允许 http/https，且强制走 ``_normalize_root`` 的
      路径校验，杜绝 `../../` 之类把写操作引到仓库之外。

这个模块只做"拿到代码"，不理解代码——那是 modules.py / review.py 的事。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from agent_kit.logging_conf import get_logger

log = get_logger("repo_fetch")

#: 允许的下载域名。刻意用白名单而不是黑名单：
#: tarball 路径会**把响应体解包到磁盘上**，等于允许任意主机往本机写文件。
TARBALL_HOSTS = frozenset({
    "codeload.github.com",
    "github.com",
    "gitlab.com",
})

#: 单个仓库超过这个大小就放弃（tarball 是压缩包，解开后可能数倍）
MAX_TARBALL_BYTES = 512 * 1024 * 1024
MAX_TAR_MEMBERS = 200_000

_DEFAULT_TIMEOUT = 60

#: git 克隆的墙钟上限。网络不通时 git 会长时间挂住（实测卡满 10 分钟），
#: 所以先做可达性探测，探不通直接走 tarball，别让用户干等。
GIT_TIMEOUT_S = 90
REACHABILITY_TIMEOUT_S = 4


class FetchError(Exception):
    """拿不到仓库。调用方转成 4xx/5xx。"""


@dataclass(frozen=True)
class Fetched:
    path: Path
    source: str          # 'local' | 'git' | 'tarball'
    origin: str          # 原始输入（URL 或路径）
    default_branch: str = ""
    degraded_reason: str = ""

    @property
    def has_git_history(self) -> bool:
        return (self.path / ".git").is_dir()


# ---------------------------------------------------------------- 入口

def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://", "git@"))


def fetch(target: str, *, dest_root: Path, prefer_git: bool = True) -> Fetched:
    """把 target 变成一个本地目录。

    Args:
        target: 本地路径、https URL 或 git@ 远程地址
        dest_root: 克隆/解压的目标根目录
        prefer_git: 是否优先走 git（拿得到历史）

    Raises:
        FetchError: 目标不存在、URL 不在白名单、下载失败等
    """
    dest_root = Path(dest_root)
    if is_url(target):
        name = _repo_name_from_url(target)
        dest = dest_root / name
        if dest.exists() and _looks_complete(dest):
            # 复用已有副本：重复评审同一个仓库时不该反复下载
            src = "git" if (dest / ".git").is_dir() else "tarball"
            log.info("复用已存在的仓库副本：%s（%s）", dest, src)
            return Fetched(path=dest, source=src, origin=target)
        if dest.exists():
            # 半成品：上次 git clone 被中断/超时留下的目录，只有 .git 没有工作区。
            # 直接复用会评审到一个空仓库——比报��更难查。
            log.warning("发现不完整的仓库副本，已清理后重新获取：%s", dest)
            _cleanup(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if prefer_git and _reachable(_probe_url(target)):
            try:
                _git_clone(target, dest)
                return Fetched(path=dest, source="git", origin=target,
                               default_branch=_current_branch(dest))
            except Exception as exc:  # noqa: BLE001 —— 降级是这里的本意
                reason = f"git 克隆失败（{type(exc).__name__}），已降级为 tarball 下载"
                log.warning("%s：%s", reason, exc)
                _cleanup(dest)
        else:
            reason = "git 路径不可用" if prefer_git else ""
        branch, base = _tarball_url(target)
        # 拿不到默认分支时逐个候选试：不同仓库的主分支命名习惯差异很大
        # （deepseek-harness 是 master，别的多是 main），全试一遍最省事。
        candidates = [branch] if branch else [*FALLBACK_BRANCHES, ""]
        last_exc: Exception | None = None
        for cand in candidates:
            ref = f"refs/heads/{cand}" if cand else "HEAD"
            try:
                _download_tarball(f"{base}/tar.gz/{ref}", dest)
                branch = cand
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise FetchError(f"仓库获取失败：HTTP {exc.code}") from exc
                last_exc = exc  # 404 说明这个分支名不对，换下一个试
            except FetchError:
                raise
            except Exception as exc:
                raise FetchError(f"仓库获取失败：{exc}") from exc
        else:
            raise FetchError(
                f"下载失败：已尝试分支 {candidates}，均取不到该仓库"
                f"（仓库可能是私有的，或名称有误）"
            ) from last_exc
        return Fetched(path=dest, source="tarball", origin=target,
                       default_branch=branch, degraded_reason=reason)
    path = Path(target).expanduser()
    if not path.is_dir():
        raise FetchError(f"目录不存在：{target}")
    return Fetched(path=path.resolve(), source="local", origin=target)


def _looks_complete(path: Path) -> bool:
    """判断已有的目录是不是一次**成功**获取的产物。

    为什么必须查：git clone 被超时/中断时会留下"只有 .git、没有工作区"的目录，
    看起来目录存在且非空，直接复用会让评审对着一个空仓库跑——
    报告仍然出得来，只是结论全是"未发现"，极难定位。
    """
    if not path.is_dir():
        return False
    has_worktree = any(
        p.is_file() and not p.name.startswith(".")
        for p in path.rglob("*")
        if "node_modules" not in p.parts and ".git" not in p.parts
    )
    if not has_worktree:
        return False
    if (path / ".git").is_dir():
        # 有 .git 就得更严格：确认 HEAD 真的解引用得开（中断的克隆会失败）
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return True


# ---------------------------------------------------------------- 可达性


def _reachable(url: str, timeout: int = REACHABILITY_TIMEOUT_S) -> bool:
    """HEAD 探一下主机通不通。

    存在的理由：git clone 在网络黑洞里是**静默挂起**的（连不上也不报错，
    一直等到 TCP 超时）。与其等它超时，不如 4 秒内自己判断并降级。
    """
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Atlas-Review/1.0"})
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True   # 有响应就是通的（404 之类说明主机可达）
    except Exception:  # noqa: BLE001 —— 探测失败一律当"不通"
        return False


# ---------------------------------------------------------------- git

def _git_clone(url: str, dest: Path) -> None:
    """浅克隆。只要默认分支的工作区，不拿完整历史。

    `--depth 1` 是刻意的：完整克隆一个 260MB 的仓库要几分钟，而评审需要的是
    「最近有没有在动」，不是全部提交记录。真需要完整历史时再单独跑 git。
    """
    cmd = [
        "git", "clone", "--depth", "1", "--single-branch", "--no-tags",
        "--config", "core.longpaths=true",   # Windows 上深层路径容易超 260 字符
        url, str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:300])


def _current_branch(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- 默认分支

#: 查不到默认分支时依次试这些。`HEAD` **不是**合法的分支名，
#: 直接拼 /tar.gz/refs/heads/HEAD 会 404（deepseek-harness 就是 master 而非 main）。
FALLBACK_BRANCHES = ("main", "master", "develop", "trunk")


def _github_default_branch(owner: str, repo: str) -> str:
    """问 GitHub API 要默认分支名。api.github.com 一般比主站稳（实测可达）。"""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Atlas-Review/1.0",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        branch = str(data.get("default_branch") or "").strip()
        if branch and "/" not in branch:
            return branch
    except Exception as exc:  # noqa: BLE001 —— 拿不到就靠回退分支名逐个试
        log.debug("查 %s/%s 默认分支失败：%s", owner, repo, exc)
    return ""


# ---------------------------------------------------------------- tarball

def _tarball_url(target: str) -> tuple[str, str]:
    """由仓库地址推导出 (分支, tarball URL)。

    分支拿不到就试几个常见默认值——直接用 HEAD 之外的分支名去请求会 404
    （deepseek-harness 的默认分支就是 master 而非 main）。
    """
    if target.startswith("git@"):
        # git@github.com:owner/repo.git —— 与 https 形式统一走 codeload，
        # 否则会拼出 https://github.com/owner/repo 这种**网页地址**（下载到的是 HTML）
        m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", target)
        if not m:
            raise FetchError(f"无法解析 git 地址：{target}")
        host, path = m.group(1), m.group(2)
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            raise FetchError(f"地址里看不出 owner/repo：{target}")
        if host.lower() not in TARBALL_HOSTS:
            raise FetchError(
                f"暂不支持从 {host} 做 tarball 下载，请先 git clone 到本地再传路径"
            )
        owner, repo = parts[0], parts[1].removesuffix(".git")
        return _github_default_branch(owner, repo), f"https://codeload.github.com/{owner}/{repo}"

    parsed = urlparse(target)
    host = (parsed.netloc or "").lower()
    if host not in TARBALL_HOSTS:
        raise FetchError(
            f"暂不支持从 {host or '该主机'} 下载。"
            f"当前白名单：{', '.join(sorted(TARBALL_HOSTS))}；"
            f"其他平台请先 git clone 到本地，再传本地路径。"
        )
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise FetchError(f"URL 里看不出 owner/repo：{target}")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return _github_default_branch(owner, repo), f"https://codeload.github.com/{owner}/{repo}"


def _download_tarball(url: str, dest: Path) -> None:
    """下载并解包 tarball。

    解包时**逐条校验成员路径**：tarfile 的 member 名字可以带 `../`，
    直接 extractall 会把文件写到目标目录之外（tar 路径穿越）。

    解包目标是 `dest 同级/.<name>.partial`，成功后再 `os.rename` 落地。
    为什么不直接解到 dest：中断（Ctrl-C、进程被杀、磁盘满）会留下一个
    **看起来完整其实缺文件**的仓库目录——实测踩过：1633 个文件躺在临时目录里，
    目标目录压根没生成，报告却"看起来跑完了"。先解到 .partial 再原子改名，
    中断只会留下一个明显残缺的标记目录，下次进来会被 _looks_complete 识别并清理。
    """
    log.info("下载 tarball：%s", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.parent / f".{dest.name}.partial"
    if partial.exists():
        shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    archive = dest.parent / f".{dest.name}.tar.gz"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Atlas-Review/1.0"})
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp, \
                archive.open("wb") as fh:
            total = 0
            while chunk := resp.read(256 * 1024):
                total += len(chunk)
                if total > MAX_TARBALL_BYTES:
                    raise FetchError(f"仓库包超过 {MAX_TARBALL_BYTES // 1024 // 1024} MB，已中止")
                fh.write(chunk)

        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            if len(members) > MAX_TAR_MEMBERS:
                raise FetchError(f"仓库包内条目过多（{len(members)}），疑似异常，已中止")
            for m in members:
                _assert_safe_member(m, partial)
            tf.extractall(partial)  # 上面已逐条校验过每个成员，不存在越界

        # GitHub 的 tarball 解出来是单层目录（repo-master/），把它提上来
        entries = [e for e in partial.iterdir()]
        staged = entries[0] if len(entries) == 1 and entries[0].is_dir() else partial
        if dest.exists():
            _cleanup(dest)
        os.rename(staged, dest)  # 同盘 rename 是原子的：要么全到位，要么什么都没有
        log.info("解包完成：%s（%d 个顶层条目）", dest, len(entries))
    finally:
        archive.unlink(missing_ok=True)
        if partial.exists():
            shutil.rmtree(partial, ignore_errors=True)


def _assert_safe_member(member: tarfile.TarInfo, root: Path) -> None:
    """拒绝会写到 root 之外的 tar 成员（路径穿越 / 绝对路径 / 设备文件）。

    符号链接**不能一律拒绝**——GitHub 打包时会把仓库内的软链原样带上
    （deepseek-harness 的 .agents/notes/**/CLAUDE.md 就是指向别处的软链）。
    正确做法是校验链接目标：相对路径解析后必须仍在仓库内。
    """
    root_resolved = root.resolve()
    if member.issym() or member.islnk():
        # 只拒绝**绝对路径**链接；含 ".." 不等于越界——
        # 仓库里很常见 `.agents/skills/x/SKILL.md -> ../../../packages/...` 这种
        # 上溯到根再进别处目录的写法，判据只能是"解析后是否还在仓库内"。
        if member.linkname.startswith("/") or (len(member.linkname) > 1 and member.linkname[1] == ":"):
            raise FetchError(f"tarball 含绝对路径链接，已拒绝：{member.name} -> {member.linkname}")
        target = (root_resolved / member.name).parent / member.linkname
        try:
            resolved = target.resolve()
        except OSError:
            raise FetchError(f"tarball 链接无法解析，已拒绝：{member.name}") from None
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise FetchError(f"tarball 链接指向仓库外，已拒绝：{member.name} -> {member.linkname}")
        return
    if not (member.isfile() or member.isdir()):
        raise FetchError(f"tarball 含非普通文件，已拒绝：{member.name}")
    target = (root_resolved / member.name).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise FetchError(f"tarball 条目越界，已拒绝：{member.name}")


# ---------------------------------------------------------------- 工具

def _probe_url(target: str) -> str:
    """拿一个轻量地址做可达性探测（git 仓库的网页地址即可）。"""
    if target.startswith("git@"):
        m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", target)
        return f"https://{m.group(1)}" if m else target
    p = urlparse(target)
    return f"{p.scheme or 'https'}://{p.netloc}"


def _repo_name_from_url(target: str) -> str:
    """从 URL 推出一个安全的目录名（不允许路径分隔符与奇怪字符）。"""
    if target.startswith("git@"):
        m = re.search(r":(.+?)(?:\.git)?$", target)
        raw = m.group(1) if m else "repo"
    else:
        parts = [p for p in urlparse(target).path.split("/") if p]
        raw = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "repo")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.") or "repo"
    # 同一账号下多个仓库时补上 owner，避免 a/x 和 b/x 撞目录
    return name.replace("--", "-") + "-" + _short_hash(target)[:6]


def _short_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cleanup(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
