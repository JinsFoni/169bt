"""``BT169_DATA_DIR`` 隔离开关。

★ 存在的理由是一个真实事故：E2E 测试脚本通过 HTTP 写**生产数据库**，
把用户的真实论坛账号密码覆盖成了 ``e2e-user``（不可恢复）。
测试必须能跑在临时目录里。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def run_with_env(env_value: str | None) -> dict[str, str]:
    """在子进程里 import config 并打印路径（env 必须在 import 前设好）。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from bt169 import config\n"
        "print(config.DATA_DIR)\n"
        "print(config.DB_PATH)\n"
        "print(config.IMAGE_DIR)\n"
        "print(config.UI_DIR)\n" % str(SRC)
    )
    import os

    env = dict(os.environ)
    env.pop("BT169_DATA_DIR", None)
    if env_value is not None:
        env["BT169_DATA_DIR"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        check=True,
    ).stdout.strip().splitlines()
    return dict(zip(["data", "db", "images", "ui"], out))


def test_env_overrides_data_dir(tmp_path):
    got = run_with_env(str(tmp_path))
    assert got["data"] == str(tmp_path)
    assert got["db"] == str(tmp_path / "169bt.db")
    assert got["images"] == str(tmp_path / "images")


def test_env_does_not_affect_ui_dir(tmp_path):
    """★ UI 目录跟着仓库走，不该被数据目录开关带走。

    UI 是**代码**不是数据；跟过去会让测试服务找不到前端资源。
    """
    got = run_with_env(str(tmp_path))
    assert got["ui"].endswith("/ui")
    assert str(tmp_path) not in got["ui"]


def test_default_is_project_data_dir():
    got = run_with_env(None)
    assert got["data"].endswith("/data")
    assert "/.venv/" not in got["data"]


def test_env_is_read_at_import_time(tmp_path):
    """★ 必须是 import 时读——路径常量在模块顶层算一次，之后改 env 无效。

    这条钉住「派生常量」的语义：想换目录必须重启进程（或设好 env 再 import）。
    """
    got = run_with_env(str(tmp_path / "a"))
    assert got["db"] == str(tmp_path / "a" / "169bt.db")
