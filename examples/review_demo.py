"""工程健康度评审的离线演示：零 API Key 跑完整条链路。

演示一个具体业务：**接手一个陌生仓库，先做一次工程体检**。

四段：
  1. 取证      —— 确定性工具跑出事实（不调模型，这一步永远不会编）
  2. 判断      —— 用脚本化回执模拟模型，看报告长什么样
  3. 防幻觉    —— 模型编了一条「没有证据的阻断结论」，被强制改判为未覆盖
  4. 降级      —— 没有模型时只出证据清单，一句结论都不下

为什么第 2 段用脚本化回执而不是真模型：
  这条链路要验证的是**编排**（取证怎么分发、结论怎么汇总、没证据怎么处置），
  不是模型的判断力。用确定性回执才能断言，也才能在 CI 里跑。
  真机评估走 `python main.py eval --real`（需要 Key）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# 允许 `python examples/review_demo.py` 直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_kit.multi_agent.review import (
    DIMENSION_NAMES,
    DIMENSIONS,
    EXPERT_GROUPS,
    collect_evidence,
    parse_findings,
    render_markdown,
    run_review,
)

# ---------------------------------------------------------------------------
# 一个「故意有问题」的迷你仓库，用来让取证真的命中问题
# ---------------------------------------------------------------------------
# 假密钥**分段拼接**的原因：完整串写在源码里，会被本项目自己的 scan_secrets 扫到，
# 评自己时凭空多一条告警——那是一条假警报。拼起来只在运行时存在于临时仓库里。
_FAKE_TOKEN = "sk-" + "Qw9xT2mN" + "5pRk7vLb3Zc"
_FAKE_PASSWORD = "Tr0ub4dor" + "&3xKmN9pQ"

_BAD_REPO = {
    "app.py": (
        f'API_KEY = "{_FAKE_TOKEN}"\n'
        f'DB_PASSWORD = "{_FAKE_PASSWORD}"\n\n'
        "def run():\n"
        "    try:\n"
        "        return 1 / 0\n"
        "    except Exception:\n"
        "        pass\n"
        "    # TODO: 补上日志\n"
    ),
    "requirements.txt": "requests\nflask\n",
    "main.py": "from app import run\nrun()\n",
}


def _make_bad_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="atlas-review-"))
    for name, text in _BAD_REPO.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# 脚本化判断：按组名返回预设结论，证据引用真实取证工具的输出
# ---------------------------------------------------------------------------
_SCRIPTED = {
    "结构与配置": [
        {"dimension": "config", "verdict": "阻断",
         "fact": "源码里硬编码了 API_KEY 与 DB_PASSWORD",
         "evidence": "scan_secrets: count=2（app.py:1 通用 sk- 令牌、app.py:2 硬编码口令）",
         "suggestion": "密钥改为从环境变量读取，并提供 .env.example；已泄露的 Key 立即轮换"},
        {"dimension": "layering", "verdict": "建议",
         "fact": "全部逻辑堆在两个文件里，没有分层",
         "evidence": "project_code_stats: files=3, code_lines=8",
         "suggestion": "规模尚小可暂不分层，但要先定目录约定，别等大了再拆"},
    ],
    "质量与测试": [
        {"dimension": "tests", "verdict": "阻断",
         "fact": "没有任何测试，也没有 tests 目录",
         "evidence": "check_tests: has_tests_dir=false, test_cases=0",
         "suggestion": "至少补一条冒烟用例，钉住「能跑起来」这条底线"},
        {"dimension": "errors", "verdict": "阻断",
         "fact": "存在 except Exception: pass，异常被吞掉",
         "evidence": "scan_debt_markers + 取证可见 app.py:7",
         "suggestion": "至少打日志再放行；吞掉的异常几周后会以更难排查的方式冒出来"},
        {"dimension": "logging", "verdict": "未覆盖",
         "fact": "取证工具覆盖不到日志实现，需人工看代码",
         "evidence": "",
         "suggestion": ""},
    ],
    "交付与文档": [
        {"dimension": "docs", "verdict": "阻断",
         "fact": "没有 README，接手者无法知道怎么跑起来",
         "evidence": "readme_outline: 未找到 README",
         "suggestion": "补 README，至少写清「怎么装依赖、怎么启动」"},
        {"dimension": "ci", "verdict": "阻断",
         "fact": "没有 CI 配置",
         "evidence": "project_checklist: 未发现 .github/workflows",
         "suggestion": "先加一条 lint + test 的流水线，跑不起来就别谈质量"},
    ],
    "依赖与安全": [
        {"dimension": "deps", "verdict": "阻断",
         "fact": "依赖全部未钉版本，CI 的「可复现」不成立",
         "evidence": "dependency_audit: unpinned=['requests','flask']",
         "suggestion": "钉到具体版本，或用 lock 文件"},
    ],
}


def _scripted_judge(prompt: str) -> str:
    """按 prompt 里的组名返回脚本化回执。

    刻意**带代码围栏**返回——真实模型几乎必然加，正好验证 `parse_findings` 兜得住。
    """
    for name, findings in _SCRIPTED.items():
        if f"「{name}」" in prompt:
            return "```json\n" + json.dumps({"findings": findings}, ensure_ascii=False) + "\n```"
    return "{}"


def main() -> None:
    repo = _make_bad_repo()
    try:
        _banner("1 · 取证 —— 确定性工具跑出事实，这一步永远不会编")
        evidence = collect_evidence(repo)
        ok = [k for k, v in evidence.items() if v["ok"]]
        print(f"  跑了 {len(evidence)} 个取证工具，成功 {len(ok)} 个")
        for name in ("scan_secrets", "check_tests", "dependency_audit", "project_checklist"):
            item = evidence.get(name, {})
            if item.get("ok"):
                payload = json.dumps(item["data"], ensure_ascii=False, default=str)
                print(f"  {name:20s} {payload[:150]}")
            else:
                print(f"  {name:20s} 取证失败：{item.get('error')}")

        _banner("2 · 判断 —— 四个专家并行，每人只拿自己那部分证据")
        report = run_review(repo, judge=_scripted_judge)
        print(f"  总评：{report.headline}")
        print(f"  结论 {len(report.findings)} 条：阻断 {len(report.blockers())} / "
              f"建议 {len(report.suggestions())} / 良好 {len(report.goods())}")
        print(f"  未覆盖 {len(report.uncovered)} 项：{report.uncovered or '无'}")
        print("\n  分组与证据隔离（每个专家只看到自己那一组工具的输出）：")
        for group in EXPERT_GROUPS:
            dims = "、".join(DIMENSION_NAMES[d] for d in group["dimensions"])
            print(f"    {group['name']:8s} 维度[{dims}]  工具[{', '.join(group['tools'])}]")

        _banner("3 · 防幻觉 —— 模型编了一条没证据的阻断结论")
        fake = parse_findings(json.dumps({"findings": [{
            "dimension": "tests", "verdict": "阻断项",
            "fact": "测试覆盖率不足 30%，质量堪忧", "evidence": "", "suggestion": "补测试",
        }]}, ensure_ascii=False))
        print(f"  模型返回：verdict={fake[0].verdict}  fact={fake[0].fact}")
        from agent_kit.multi_agent.review import _enforce_evidence

        fixed = _enforce_evidence(fake)
        print(f"  强制改判后：verdict={fixed[0].verdict}")
        print("\n  为什么必须这么硬：评审报告里最危险的一行是「未发现问题」，")
        print("  它读起来像查过了。没取到证就落「未覆盖」，读者才知道要自己去看。")

        _banner("4 · 降级 —— 没有模型时，只说取证能直接判定的事")
        degraded = run_review(repo, judge=None)
        print(f"  degraded={degraded.degraded}  结论条数={len(degraded.findings)}  "
              f"未覆盖={len(degraded.uncovered)}/{len(DIMENSIONS)}")
        print(f"  总评：{degraded.headline}")
        print("\n  注意**不是零结论**：没有测试、依赖没钉版本、疑似硬编码密钥，")
        print("  这三类取证数据本身就能判定，不需要模型；")
        print("  需要读代码才能说的维度（分层、日志、错误处理）才是真的未覆盖。")

        _banner("报告样例（脚本化回执生成，非真实模型判断）")
        print(render_markdown(report))

        print("=" * 78)
        print("  演示结束。业务链路：取证（确定性） → 四专家并行判断 → 汇总冲突 → 报告。")
        print("  真机评审：`python main.py review --path <仓库路径>`（需要配模型 Key）")
        print("=" * 78)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    main()
