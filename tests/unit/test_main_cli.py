"""命令行入口（阶段 6.5 §八 评审 A 的发现）。

## 这里为什么要有测试

`src/ai_psi/main.py` 是**两个正式入口之一**（学习的 CLI 路径），
而它在阶段 6.5 §八 之前**一条测试都没有**。评审 A 跑了一下就发现
`--help` 在 GBK 控制台下**确定性崩溃**——
`UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f534'`，
退出码 1。那个字符是 `learn` 子命令 help 里的红点，
而**只有**父解析器打印子命令列表时才会输出它。

教训很直白：**正式入口没有被任何测试跑到过。**
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_psi.main import _build_parser

pytestmark = pytest.mark.unit

#: 仓库根。子进程要用它当 cwd，否则 `python -m ai_psi.main` 找不到包。
_REPO = Path(__file__).resolve().parents[2]


class TestTheHelpTextCanBePrinted:
    """🔴 崩溃复现：**别把不可编码的字符放进面向控制台的文本里**。"""

    def test_the_help_text_is_representable_in_gbk(self) -> None:
        """help 文本必须能在 GBK 控制台下**逐字符**输出。

        ⚠️ 这里是 ``encode("gbk")`` **不加** ``errors=``——故意的。
        测试要断言的是"它编得出来"，而不是"编不出来时别炸"。
        后者由 `main()` 里给标准流装 ``errors="replace"` 兜底，
        两者挡的不是同一件事：
        这一个拦"文本里出现了控制台根本不认识的字符"，
        那一个拦"将来某个 print 又带进来一个"。
        """
        # 🔴 `format_help()` 打印的是**子命令列表**——每一项都带着
        # 各自 `add_parser(help=...)` 的那段文本，正是原崩溃的触发路径。
        _build_parser().format_help().encode("gbk")

    def test_the_command_runs_on_a_gbk_console(self) -> None:
        """🔴 **逐字复现评审 A 的现场**，而不是在进程内模拟。

        pytest 会把 ``sys.stdout`` 换成自己的捕获对象，那个对象的编码
        与真实控制台无关——在进程里测，这条用例**永远绿**。
        要让 `PYTHONIOENCODING` 真的生效，只能开一个子进程。
        """
        completed = subprocess.run(
            [sys.executable, "-m", "ai_psi.main", "--help"],
            capture_output=True,
            cwd=_REPO,
            env={**os.environ, "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"},
            check=False,
        )
        stderr = completed.stderr.decode("gbk", errors="replace")
        assert completed.returncode == 0, stderr
        assert "UnicodeEncodeError" not in stderr, stderr
        # 前提：这一跑真的走到了打印帮助那一步，不是被别的原因挡下的
        assert b"usage:" in completed.stdout


class TestTheDatabaseFailureIsActionable:
    """🔴 连不上库时，第一行必须告诉运维**去改什么**。"""

    def test_a_bad_database_url_points_at_the_variable(self) -> None:
        """评审 A 实测：连不上库时屏幕上是约 30 层深的 traceback，
        末行 ``ConnectionTimeout``，**没有一个字**提到
        ``AI_PSI_DATABASE_URL``。

        ⚠️ 这里断言的是"提示出现在 traceback **之前**"——
        运维读 CLI 输出时看的是头几行，把提示放在末尾等于没放。
        """
        completed = subprocess.run(
            [sys.executable, "-m", "ai_psi.main", "learn"],
            capture_output=True,
            cwd=_REPO,
            env={
                **os.environ,
                # ⚠️ `connect_timeout=1` **不是为了好看，是为了这条用例
                # 跑得完**：不加它，libpq 要等到默认超时（实测 2 分钟）
                # 才放弃，而这条用例要走的是"连接失败被兜住"这条路径，
                # 不是"等它超时"。区别只在快慢，不在覆盖。
                "AI_PSI_DATABASE_URL": (
                    "postgresql+psycopg://ai_psi:ai_psi@127.0.0.1:1/ai_psi?connect_timeout=1"
                ),
                **({"PYTHONUTF8": "0", "PYTHONIOENCODING": "gbk"}),
            },
            check=False,
        )
        stderr = completed.stderr.decode("gbk", errors="replace")
        assert completed.returncode != 0, stderr
        assert "AI_PSI_DATABASE_URL" in stderr, stderr
        # 提示必须在 traceback **之前**
        assert stderr.index("AI_PSI_DATABASE_URL") < stderr.index("Traceback"), stderr
