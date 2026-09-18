"""进程入口（HTTP 服务 + 学习链路的命令行入口）。

🔴 **Windows 上必须先装 selector 事件循环策略，再启动 uvicorn。**

psycopg 的异步驱动不支持 Windows 默认的 ``ProactorEventLoop``
（见 :mod:`ai_psi.infrastructure.asyncio_compat`）。uvicorn 的
``--loop asyncio`` 在 Windows 上会新建默认循环，因此策略必须在
``uvicorn.run`` **之前**装好——晚一步就来不及了。

用法::

    uv run python -m ai_psi.main                  # 启动 HTTP 服务
    uv run python -m ai_psi.main serve            # 同上（显式）
    uv run python -m ai_psi.main learn            # 跑一次学习链路

## 为什么学习链路需要一个 CLI 入口（阶段 6.5 §四/§七）

`PatternDetector` / `PromotionPolicy` / `ProposalGenerator` /
`OfflineEvaluator` 在阶段 6 收尾时在 `src/` 里**零调用者**——
"三次同类错误可生成 Proposal"这条验收条件只在测试里成立、
在跑起来的系统里**不可操作**。能力证据矩阵把它记为 E1（仅测试）。

CLI 与 HTTP 路由（``POST /api/v1/learning/runs``）是它的两个入口，
**走的是同一个 `LearningService` 实例**。两条路径都存在是因为
它们服务两种不同的用法：运维想"直接跑一次看结果"，
而黑盒验收要"走真实 HTTP + 真实 PostgreSQL"。

🔴 **两条入口都不能批准任何东西。** 它们只生成 `DRAFT`，
评估与批准仍然要人来做（不变量 11）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from ai_psi.infrastructure.asyncio_compat import install_selector_loop_policy

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="ai_psi",
        description="AI-PSI 认知运行时：HTTP 服务与学习链路的命令行入口",
    )
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser("serve", help="启动 HTTP 服务（默认）")

    learn = subcommands.add_parser(
        "learn",
        help=(
            "跑一次学习链路：读经验 → 模式发现 → 门禁复核 → 生成 DRAFT 草案。"
            "🔴 它不能批准任何东西（不变量 11）"
        ),
    )
    learn.add_argument(
        "--fix-direction",
        default=None,
        help="严重错误的明确修复方向（可选）。只影响理由与条件二，不影响次数门槛",
    )
    learn.add_argument(
        "--baseline",
        nargs="*",
        type=UUID,
        default=None,
        metavar="回合ID",
        help=(
            "离线评测的基线回合。不提供时只算全部回合的基线快照——"
            "那仍然是一次真实评测，只是**没有对照**"
        ),
    )
    learn.add_argument(
        "--candidate",
        nargs="*",
        type=UUID,
        default=None,
        metavar="回合ID",
        help="对照用的候选回合。不提供表示没有候选数据，**不等于**「没有退化」",
    )
    learn.add_argument("--actor", default="learning_cli", help="产生者标识（写进事件）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """进程入口。

    Args:
        argv: 命令行参数；``None`` 时读 ``sys.argv``。

    Returns:
        退出码。``learn`` 在没有任何提案生成时仍然返回 0——
        **"这次没有可生成的"是一个正常结果，不是失败**。
    """
    arguments = _build_parser().parse_args(argv)
    # 🔴 两条路径都要先装策略：CLI 同样会连 PostgreSQL。
    install_selector_loop_policy()

    if arguments.command == "learn":
        return asyncio.run(_learn(arguments))
    return _serve()


def _serve() -> int:
    """启动 HTTP 服务。"""
    import uvicorn

    from ai_psi.api.app import create_app
    from ai_psi.config import get_settings

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=8000,
        log_config=None,  # 日志统一走 structlog，不让 uvicorn 覆盖配置
    )
    return 0


async def _learn(arguments: argparse.Namespace) -> int:
    """跑一次学习链路并打印结果。"""
    from ai_psi.application.learning_service import EvaluationWindow
    from ai_psi.config import get_settings
    from ai_psi.container import build_container

    container = build_container(get_settings())
    try:
        window = (
            None
            if arguments.baseline is None
            else EvaluationWindow(
                baseline=tuple(arguments.baseline),
                candidate=None if arguments.candidate is None else tuple(arguments.candidate),
            )
        )
        run = await container.learning_service.review(
            fix_direction=arguments.fix_direction,
            evaluation_window=window,
            actor_id=arguments.actor,
        )
    finally:
        await container.aclose()

    # 🔴 **把"为什么没有"也打出来。** 一份只说"生成了 0 条"的报告
    # 无法回答那个问题，而它正是下一次运行时最需要知道的。
    print(run.summary())
    for entry in run.created:
        print(
            f"  [DRAFT] {entry.proposal.id} "
            f"（加权计数 {entry.weighted_count}/{entry.threshold}；{entry.data_quality}）"
        )
    for pattern, reasons in run.suppressed:
        # ⚠️ 第一项在"未达模式门槛"那一支里是 None（那时连模式都没形成），
        # 因此这里不能假定它有 error_type。
        label = "（未达模式门槛）" if pattern is None else f"{pattern.error_type.value}"
        print(f"  未通过 {label}：", file=sys.stderr)
        for reason in reasons:
            print(f"    - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
