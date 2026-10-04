"""模块职责分析的测试。

这些用例盯的是**诚实性**，不是"能不能跑通"：
陌生仓库的摘要经常不足以判断职责，此时必须说"信息不足"，
而不是替模型编一个听起来合理的职责。
"""

from __future__ import annotations

import json

import pytest

from agent_kit import modules
from agent_kit.module_analysis import analyze_modules


def _w(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pkg(path, name, *, entry=None, main=None, readme=None):
    path.mkdir(parents=True, exist_ok=True)
    fields = {"name": name}
    if entry:
        fields["bin"] = {name: entry}
    if main:
        fields["main"] = main
    _w(path / "package.json", json.dumps(fields))
    if readme:
        _w(path / "README.md", readme)
    sub = path / "lib"
    _w(sub / (entry or main or "index.js"), "export function run() {}\n" * 30)
    return path


@pytest.fixture
def scan_repo(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    _pkg(tmp_path / "apps" / "cli", "cli", entry="bin.js",
         readme="# cli\n\n命令行入口，负责子命令分发。\n")
    _pkg(tmp_path / "apps" / "web", "web", entry="main.ts",
         readme="# web\n\nWeb 渲染层。\n")
    _pkg(tmp_path / "packages" / "core", "core-lib", main="index.js",
         readme="# core\n\n核心能力库。\n")
    return tmp_path, modules.scan_modules(tmp_path)


def _ok(judge, *, name="cli", purpose="命令行入口"):
    def _j(prompt: str) -> str:
        return json.dumps({"modules": [
            {"name": name, "purpose": purpose, "concerns": ["无测试"], "evidence": "bin + README"},
        ]}, ensure_ascii=False)
    return _j


# ------------------------------------------------------------ 正常路径


def test_modules_get_purposes(scan_repo):
    root, scan = scan_repo
    result = analyze_modules(root, scan, _ok(judge=None), top_n=3)
    judged = {m.name: m for m in result.modules if m.judged}
    assert judged, "至少应有模块拿到结论"
    assert all(m.evidence for m in judged.values()), "每条结论必须带依据"
    assert "无测试" in next(iter(judged.values())).concerns


def test_batches_cover_every_module(scan_repo):
    """分批不能丢模块——批数 × 批大小要覆盖住目标集。"""
    root, scan = scan_repo
    seen: list[str] = []

    def judge(prompt: str) -> str:
        for name in ("cli", "web", "core-lib"):
            if name in prompt:
                seen.append(name)
        return '{"modules": []}'

    result = analyze_modules(root, scan, judge, top_n=3, batch_size=1)
    assert result.batches == 3
    assert sorted(seen) == ["cli", "core-lib", "web"], f"每个模块都该被送进 prompt：{seen}"


def test_order_follows_importance(scan_repo):
    """按重要性取 Top N，顺序要跟着评分走（带入口的在前）。"""
    root, scan = scan_repo
    result = analyze_modules(root, scan, _ok(judge=None), top_n=2)
    assert len(result.modules) == 2
    scores = [m.name for m in result.modules]
    # cli / web 有 bin（可执行入口），core-lib 只有 main（库导出）→ 前者应在前
    assert "core-lib" not in scores[:1], f"库不该排在带入口的应用之前：{scores}"


# ------------------------------------------------------------ 降级


def test_no_judge_degrades_without_inventing_purposes(scan_repo):
    """没有判断模型时只给画像，**绝不能**编一个职责出来。"""
    root, scan = scan_repo
    result = analyze_modules(root, scan, None, top_n=3)
    assert result.degraded is True
    assert all(not m.judged for m in result.modules)
    assert all("未接入判断模型" in m.purpose for m in result.modules)
    assert all("行" in m.purpose for m in result.modules), "降级时至少应给出结构画像"


# ------------------------------------------------------------ 容错


def test_missing_module_in_reply_is_marked_not_invented(scan_repo):
    """模型漏掉某个模块时，要标成未给出，不能替它编。"""
    root, scan = scan_repo
    result = analyze_modules(root, scan, _ok(judge=None, name="cli"), top_n=3, batch_size=3)
    by = {m.name: m for m in result.modules}
    assert by["cli"].judged is True
    assert by["web"].judged is False
    assert "未返回" in by["web"].purpose or "未取得" in by["web"].purpose


def test_all_batches_failing_marks_degraded(scan_repo):
    root, scan = scan_repo

    def dead(prompt: str) -> str:
        raise RuntimeError("Error code: 401 - Incorrect API key provided")

    result = analyze_modules(root, scan, dead, top_n=3, batch_size=3)
    assert result.degraded is True
    assert result.failures
    assert all("鉴权" in f for f in result.failures)
    assert all(not m.judged for m in result.modules)


def test_json_variants_are_accepted(scan_repo):
    """模型常把 JSON 包在代码围栏里，或换 keys —— 都要能解。"""
    root, scan = scan_repo

    fenced = "```json\n" + json.dumps({"modules": [
        {"module": "cli", "description": "命令行入口", "basis": "README"}]}) + "\n```"
    r1 = analyze_modules(root, scan, lambda p: fenced, top_n=1, batch_size=1)
    assert r1.modules[0].judged and "命令行" in r1.modules[0].purpose

    flat = json.dumps([{"name": "cli", "purpose": "命令行入口"}], ensure_ascii=False)
    r2 = analyze_modules(root, scan, lambda p: flat, top_n=1, batch_size=1)
    assert r2.modules[0].judged


def test_garbage_reply_does_not_crash(scan_repo):
    root, scan = scan_repo
    for bad in ("", "not json at all", "{broken", "[1,2,3]", '{"modules": "oops"}'):
        result = analyze_modules(root, scan, lambda p, b=bad: b, top_n=2, batch_size=2)
        assert len(result.modules) == 2
        assert all(not m.judged for m in result.modules), f"垃圾输出不该被当成结论：{bad!r}"


def test_module_not_in_scope_is_ignored(scan_repo):
    """模型报出不在目标里的模块名时不能凭空多出模块。"""
    root, scan = scan_repo
    result = analyze_modules(root, scan, lambda p: json.dumps({"modules": [
        {"name": "cli", "purpose": "命令行入口"},
        {"name": "并不存在的模块", "purpose": "我编的"},
    ]}), top_n=3, batch_size=3)
    names = {m.name for m in result.modules}
    assert "并不存在的模块" not in names
    assert len(result.modules) == 3


def test_name_matching_tolerates_model_formatting(scan_repo):
    """模型常把 name 写成 `模块名（路径）` 或短名——都不能算"没返回"。

    端到端实测踩过：模型返回 `@deepseek-ai/dsh（apps/cli）`，
    精确匹配失配 → 三个模块的结论被整体丢弃，明明有高质量输出。
    """
    from agent_kit.module_analysis import _match_returned

    p = scan_repo[1].modules[0]
    assert p.name  # 确认拿到了目标模块
    for key in (p.name, f"{p.name}（{p.path}）", f"{p.name}({p.path})", p.path,
                p.name.rsplit("/", 1)[-1]):
        assert _match_returned({key: {"purpose": "x"}}, p) is not None, f"应能匹配：{key}"

def test_name_matching_does_not_cross_modules(scan_repo):
    """短名不能模糊命中另一个模块——张冠李戴比匹配失败更糟。

    `dsh` 是 `dsh-session` 的前缀；一旦允许模糊包含，
    A 模块的职责就会被安到 B 模块头上，而且报告里看不出来。
    """
    from agent_kit.module_analysis import _match_returned

    by_name = {m.name: m for m in scan_repo[1].modules}
    target = by_name["web"]
    for other in ("cli", "core-lib"):
        assert _match_returned({other: {"purpose": "x"}}, target) is None,             f"「{other}」不该匹配到 web 模块"
