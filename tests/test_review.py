"""工程健康度评审的测试。

评审这类功能，**「跑通」不是验收标准，「没编」才是**。
所以这里的用例重心不在报告长什么样，而在三件事：
  1. 取证能不能打到别的仓库（换根是否真的生效、会不会污染默认值）
  2. 模型不老实的时候（加围栏、返回垃圾、编造没证据的结论）能不能兜住
  3. 没有模型时是不是真的**一句结论都不下**
"""

from __future__ import annotations

import json

import pytest

from agent_kit.mcp_servers._common import project_root, reset_root
from agent_kit.multi_agent.review import (
    BLOCKER,
    DIMENSIONS,
    EXPERT_GROUPS,
    GOOD,
    SUGGESTION,
    UNCOVERED,
    Finding,
    _dimension_id,
    _enforce_evidence,
    _normalize_verdict,
    collect_evidence,
    evidence_digest,
    parse_findings,
    render_markdown,
    repo_digest,
    run_review,
)


@pytest.fixture
def tiny_repo(tmp_path):
    """一个最小的「有问题的仓库」，够取证命中即可。"""
    (tmp_path / "app.py").write_text('TOKEN = "sk-Qw9xT2mN5pRk7vLb3Zc"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_root():
    """每个用例后都要把取证根目录恢复默认——它是进程级全局状态。"""
    yield
    reset_root()


# ---------------------------------------------------------------------------
# 取证
# ---------------------------------------------------------------------------
def test_collect_evidence_targets_another_repo(tiny_repo):
    """换根必须真的生效，否则「评审任意仓库」这个业务不成立。"""
    evidence = collect_evidence(tiny_repo)
    assert evidence["dependency_audit"]["ok"] is True
    assert "requests" in json.dumps(evidence["dependency_audit"]["data"], ensure_ascii=False)


def test_collect_evidence_restores_default_root(tiny_repo):
    """取证完必须切回默认根，否则会污染后续调用。"""
    before = project_root()
    collect_evidence(tiny_repo)
    assert project_root() == before


def test_collect_evidence_survives_tool_failure(tiny_repo):
    """单个工具失败不能带崩整轮取证，失败要记进结果。"""
    evidence = collect_evidence(tiny_repo, ("dependency_audit", "not_a_tool"))
    assert evidence["dependency_audit"]["ok"] is True
    assert evidence["not_a_tool"]["ok"] is False
    assert "未知工具" in evidence["not_a_tool"]["error"]


def test_evidence_digest_truncates_long_payload():
    """证据太长模型反而不看，截断是准确性手段不是省钱手段。"""
    big = {"scan_secrets": {"ok": True, "data": {"blob": "x" * 5000}}}
    text = evidence_digest(big, max_chars=100)
    assert "截断" in text
    assert len(text) < 400


def test_repo_digest_falls_back_when_stats_missing():
    assert "未知" in repo_digest({})


# ---------------------------------------------------------------------------
# 解析：模型不老实时的兜底
# ---------------------------------------------------------------------------
def test_parse_findings_strips_code_fence():
    """真实模型几乎必然加 ```json 围栏，prompt 约束不住。"""
    payload = {"findings": [{"dimension": "tests", "verdict": "阻断", "fact": "没测试",
                             "evidence": "check_tests: 0", "suggestion": "补"}]}
    text = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    findings = parse_findings(text)
    assert len(findings) == 1 and findings[0].dimension == "tests"


def test_parse_findings_rejects_garbage():
    assert parse_findings("我觉得这个项目挺好的") == []


def test_parse_findings_rejects_broken_json():
    assert parse_findings('{"findings": [{"dimension": "]') == []


def test_parse_findings_empty_input():
    assert parse_findings("") == []
    assert parse_findings(None) == []  # type: ignore[arg-type]


def test_parse_findings_skips_single_bad_item():
    """一条不合规不该拖垮整组。"""
    text = json.dumps({"findings": [
        {"dimension": "tests", "verdict": "阻断", "fact": "ok", "evidence": "e"},
        {"dimension": "ci", "verdict": 12345},
    ]}, ensure_ascii=False)
    assert len(parse_findings(text)) == 1


# ---------------------------------------------------------------------------
# 防幻觉
# ---------------------------------------------------------------------------
def test_verdict_variants_are_normalized():
    """模型爱写「阻断项」「建议改进」这类变体。"""
    assert _normalize_verdict("阻断项") == BLOCKER
    assert _normalize_verdict("建议改进") == SUGGESTION
    assert _normalize_verdict("做得好的地方") == GOOD
    assert _normalize_verdict("不知道") == UNCOVERED


def test_finding_without_evidence_is_downgraded():
    """核心用例：没有证据的结论必须改判「未覆盖」。

    评审报告里最危险的一行是「未发现问题」——它读起来像查过了。
    """
    finding = Finding(dimension="tests", verdict="阻断", fact="测试覆盖率不足 30%", evidence="")
    fixed = _enforce_evidence([finding])
    assert fixed[0].verdict == UNCOVERED
    assert fixed[0].suggestion


def test_finding_with_evidence_keeps_verdict():
    finding = Finding(dimension="tests", verdict="阻断", fact="无测试", evidence="check_tests: 0")
    assert _enforce_evidence([finding])[0].verdict == BLOCKER


def test_uncovered_stays_uncovered():
    finding = Finding(dimension="logging", verdict="未覆盖", fact="看不出来", evidence="")
    assert _enforce_evidence([finding])[0].verdict == UNCOVERED


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def test_every_dimension_is_owned_by_an_expert():
    """不变量：八项不能有遗漏，也不能有个专家领了不存在的维度。"""
    owned = [d for g in EXPERT_GROUPS for d in g["dimensions"]]
    assert set(owned) == {d for d, _name in DIMENSIONS}
    assert len(owned) == len(set(owned)), "同一个维度被两个专家重复认领，会出重复结论"


def test_every_group_tool_is_callable():
    for group in EXPERT_GROUPS:
        for tool in group["tools"]:
            assert tool in collect_evidence.__globals__["TOOL_REGISTRY"]


def test_degraded_run_only_claims_what_evidence_proves(tiny_repo):
    """没有模型时只出「取证规则能直接判定」的结论，其余一律未覆盖。

    注意不是「零结论」：`has_tests_dir=false` 就是没有测试，这是确定性事实，
    不需要模型来判断，也不该因为没有模型就不说。
    """
    report = run_review(tiny_repo, judge=None)
    assert report.degraded is True
    assert report.findings, "规则能判定的结论不该因为没模型就丢掉"
    assert all(f.evidence for f in report.findings), "降级报告的每条结论都必须带证据"
    assert "依赖与安全" not in report.uncovered      # 依赖未钉版本是可判定的
    assert "测试" not in report.uncovered
    assert "分层与依赖方向" in report.uncovered      # 需要读代码，不给结论
    assert "降级" not in report.headline or "模型" in report.headline


def test_scripted_judge_produces_findings(tiny_repo):
    def judge(prompt: str) -> str:
        if "依赖与安全" in prompt:
            return json.dumps({"findings": [{
                "dimension": "deps", "verdict": "阻断", "fact": "依赖未钉版本",
                "evidence": "dependency_audit: unpinned=['requests']", "suggestion": "钉版本",
            }]}, ensure_ascii=False)
        return json.dumps({"findings": [{
            "dimension": "logging", "verdict": "未覆盖", "fact": "取证覆盖不到", "evidence": "",
        }]}, ensure_ascii=False)

    report = run_review(tiny_repo, judge=judge)
    assert report.degraded is False
    assert any(f.verdict == BLOCKER for f in report.findings)
    assert "日志" in report.uncovered


def test_broken_judge_does_not_kill_the_round(tiny_repo):
    """某个专家内部炸了，其他三组仍要交回执。"""

    def judge(prompt: str) -> str:
        if "依赖与安全" in prompt:
            raise RuntimeError("专家内部出错")
        return json.dumps({"findings": [{
            "dimension": "deps", "verdict": "良好", "fact": "依赖都钉了版本",
            "evidence": "dependency_audit: unpinned=[]",
        }]}, ensure_ascii=False)

    report = run_review(tiny_repo, judge=judge)
    assert report.degraded is False
    assert len(report.findings) >= 3, "炸掉一组不能让整轮空手而归"
    # 规则结论会补位：tiny_repo 没有 tests 目录，这条不依赖模型也说得出来
    assert any(f.dimension == "tests" for f in report.findings)
    assert "日志" in report.uncovered     # 没人给出结论的维度仍是未覆盖


def test_dimension_id_accepts_both_forms():
    assert _dimension_id("tests") == "tests"
    assert _dimension_id("测试") == "tests"
    assert _dimension_id("不存在的维度") == "不存在的维度"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def test_render_markdown_has_all_sections(tiny_repo):
    report = run_review(tiny_repo, judge=lambda _p: json.dumps({"findings": [
        {"dimension": "tests", "verdict": "阻断", "fact": "无测试", "evidence": "check_tests: 0", "suggestion": "补冒烟"},
        {"dimension": "docs", "verdict": "良好", "fact": "README 齐全", "evidence": "readme_outline: ok"},
    ]}, ensure_ascii=False))
    text = render_markdown(report)
    assert "## 阻断项（必须改）" in text
    assert "## 做得好的地方" in text
    assert "## 未覆盖" in text
    assert "check_tests: 0" in text


def test_render_markdown_warns_when_degraded(tiny_repo):
    text = render_markdown(run_review(tiny_repo, judge=None))
    assert "降级报告" in text
    assert "未经模型判断" in text


def test_render_markdown_can_attach_raw_evidence(tiny_repo):
    report = run_review(tiny_repo, judge=None)
    text = render_markdown(report, evidence=collect_evidence(tiny_repo))
    assert "取证原始输出" in text
    assert "dependency_audit" in text
