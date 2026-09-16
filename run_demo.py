"""演示入口。

用法：
    python run_demo.py                         # 全部场景，fake 模型
    python run_demo.py --scenario stream       # 单个场景
    python run_demo.py --list                  # 查看场景清单
    python run_demo.py --provider openai       # 切换真实模型
"""

from __future__ import annotations

import argparse
import sys
import traceback
import warnings
from pathlib import Path

# langchain 1.4.0 + langgraph 1.2.x 在序列化 runtime context 时会刷 Pydantic 的
# PydanticSerializationUnexpectedValue 警告（库内部 schema 与 None 默认值不匹配导致）。
# 这是已知的上游噪音，与业务代码无关，这里显式忽略以保持演示输出干净。
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_kit.config import AgentSettings, require_api_key
from examples.scenarios import SCENARIO_HELP, SCENARIOS


def _isolate_memory() -> None:
    """把记忆后端切到**进程内内存**，与生产 Redis / PostgreSQL 完全隔离。

    只改进程内环境变量，不动用户系统配置。

    为什么不用 sqlite：sqlite 分支需要额外安装
    `langgraph-checkpoint-sqlite` / `langgraph-store-sqlite`，
    而 demo 的定位是「零依赖可跑」，不该为此多装包，也不该留落盘文件。
    演示脚本每个场景都是独立跑完的，内存足够。
    """
    import os

    os.environ["SHORT_TERM_BACKEND"] = "memory"
    os.environ["LONG_TERM_BACKEND"] = "memory"


def main() -> int:
    parser = argparse.ArgumentParser(description="LangChain 1.x Agent 全套能力演示")
    parser.add_argument("--scenario", "-s", help="只跑指定场景；不填则跑全部", default=None)
    parser.add_argument("--list", "-l", action="store_true", help="列出所有场景")
    parser.add_argument(
        "--provider",
        "-p",
        default="",
        help="fake | openai | deepseek | dashscope | anthropic；留空则按环境变量自动探测",
    )
    parser.add_argument("--model", "-m", default="", help="模型名，覆盖默认值")
    parser.add_argument(
        "--real-memory",
        action="store_true",
        help="记忆层仍用真实的 Redis / PostgreSQL（默认不接，见下方说明）",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 隔离：演示默认**不碰**生产 Redis / PostgreSQL
    #
    # 否则每跑一次 demo 都会往生产 Redis 里塞 s1~s8 之类的一次性会话，
    # 污染 Web 端的会话列表（历史上一度出现过 14 个垃圾会话）。
    # 演示脚本本就追求「零依赖可跑」，用 sqlite/内存落盘足矣；
    # 想验证真实记忆层请用 --real-memory 或 examples/memory_e2e.py。
    # ------------------------------------------------------------------
    if not args.real_memory:
        _isolate_memory()
    else:
        print("⚠️  已启用 --real-memory：本次演示会读写真实的 Redis / PostgreSQL。\n")

    if args.list:
        print("可用场景：")
        for name, desc in SCENARIO_HELP.items():
            print(f"  {name:16} {desc}")
        return 0

    if args.scenario and args.scenario not in SCENARIOS:
        print(f"未知场景：{args.scenario}。可用：{list(SCENARIOS)}")
        return 1

    # 注意：脚本化场景（fake）依赖预设脚本，切到真实模型时逻辑由模型自行决定，
    # 演示脚本里写死的 tool_call 顺序不再生效，因此真实模式下提示用户注意。
    # provider 未显式指定时，AgentSettings 会读取 LLM_PROVIDER 或按已配置的 Key 自动探测
    settings = AgentSettings(provider=args.provider or None, model_name=args.model)
    if settings.provider != "fake":
        try:
            require_api_key(settings)
        except RuntimeError as exc:
            print(f"[配置错误] {exc}")
            return 2
        print(f"⚠️  provider={settings.provider}（{settings.model_name}）：工具调用顺序由真实模型自行决定，")
        print("    场景脚本里写死的步骤仅对 fake 模式生效。\n")

    targets = {args.scenario: SCENARIOS[args.scenario]} if args.scenario else SCENARIOS

    print("=" * 78)
    print(f"  LangChain Agent 演示  |  provider={settings.provider}  model={settings.model_name}")
    print("=" * 78)

    failed: list[str] = []
    for name, func in targets.items():
        try:
            func()
        except Exception:  # noqa: BLE001 —— 演示要的是「跑完所有场景」，单个失败不中断
            failed.append(name)
            print(f"\n[场景失败] {name}")
            traceback.print_exc()

    print("\n" + "=" * 78)
    if failed:
        print(f"  完成，但有 {len(failed)} 个场景失败：{', '.join(failed)}")
    else:
        print(f"  全部 {len(targets)} 个场景执行完毕 ✅")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
