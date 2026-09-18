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
`OfflineEvaluator` 在阶段 6 收尾时**从跑起来的系统里到不了**——
"三次同类错误可生成 Proposal"这条验收条件只在测试里成立、
在生产上**不可操作**。能力证据矩阵把它记为 E1（仅测试）。

⚠️ **准确的说法是"那个调用者自己不可达"，不是"零调用者"。**
`LearningService` 从阶段 6 起就在 `src/` 里调用它们；问题在于
`LearningService` 本身没有任何入口。两者都不报错，只是永远不跑。

CLI 与 HTTP 路由（``POST /api/v1/learning/runs``）是它的两个入口。
⚠️ **它们共享的是「组合根」（`build_container`），不是同一个实例**——
CLI 与服务是两个进程，各自建各自的容器。两条路径都存在是因为
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
            "注意：它不能批准任何东西（不变量 11）"
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
    # 🔴 **先把标准流的编码错误策略改成"替换"。**
    #
    # 阶段 6.5 §八 评审 A 实测：`python -m ai_psi.main --help` 在
    # GBK 控制台下**确定性崩溃**——`UnicodeEncodeError: 'gbk' codec
    # can't encode character '\U0001f534'`，退出码 1。字符串是
    # `learn` 子命令 help 里的那个红点，而**只有**父解析器打印子命令
    # 列表时才会输出它。
    #
    # ⚠️ 只改 `errors`，**不改 `encoding`**：控制台真是 GBK 的话，
    # 强制输出 UTF-8 会让中文全变乱码——那是拿一个崩溃换一个更难查的
    # 显示问题。`errors="replace"` 让不可编码的字符退化成 `?`，
    # 中文照常。
    #
    # 如果你的终端是 UTF-8 的（Windows Terminal、VSCode 终端），
    # 中文反而会乱码——那是 Python 按 OEM 代码页选了 GBK。
    # 那种情况下用 `PYTHONUTF8=1` 启动（与变异测试子进程的处理一致），
    # 而不是在这里替所有人做决定。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no cover - 真实终端才有
            reconfigure(errors="replace")

    arguments = _build_parser().parse_args(argv)
    # 🔴 两条路径都要先装策略：CLI 同样会连 PostgreSQL。
    install_selector_loop_policy()

    if arguments.command == "learn":
        return _run_learn(arguments)
    return _serve()


def _run_learn(arguments: argparse.Namespace) -> int:
    """跑学习链路，并把**可动作的**失败信息放在第一行。

    🔴 阶段 6.5 §八 评审 A 实测：数据库连不上时，``asyncio.run``
    直接把 SQLAlchemy 的异常抛出去，屏幕上是一条约 30 层深的
    traceback（末行 ``ConnectionTimeout``，耗时约两分钟），
    而**没有任何一个字**告诉读它的人"去改哪个环境变量"。

    运维读 CLI 输出时看的是头几行。这里把结论提到最前面，
    traceback 仍然照打——**不为了好看把调试信息丢掉**。
    """
    from sqlalchemy.exc import SQLAlchemyError

    try:
        return asyncio.run(_learn(arguments))
    except SQLAlchemyError as error:
        print(
            f"数据库操作失败：{type(error).__name__}: {error}\n"
            "  请先确认：① AI_PSI_DATABASE_URL 指向的实例在跑；"
            "② 迁移已到 head（make migrate）。\n"
            "  下面是完整的 traceback。",
            file=sys.stderr,
        )
        raise


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
