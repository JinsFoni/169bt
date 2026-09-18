"""验证码求解。

**实测事实**（ARCHITECTURE.md §5.1.3 / 附录 A.5）：

| 事实 | 数值 |
|---|---|
| 单模型准确率（无偏，n=40） | 默认 50.0% / beta 55.0% |
| 两候选取或 | 67.5% |
| 单图可校验次数 | **约 3 次**，超出后该图作废 |
| 每次重新取图 | 轮换为新码 |
| 大小写敏感 | **不敏感** |
| 生产策略 | 每图 ≤2 候选、≤6 张图 → **40/40 求解成功**，平均 1.48 张 |

**不做任何图像预处理**：实测二值化把准确率从 100% 打到 13–30%（ADR-5）。
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

__all__ = [
    "Solver",
    "PythonSolver",
    "StubSolver",
    "CaptchaSolver",
    "SolveResult",
    "parse_seccode_check",
    "MAX_IMAGES",
    "MAX_CANDIDATES_PER_IMAGE",
]

#: 最多换多少张图（实测 40/40 在 6 张内成功）。
MAX_IMAGES = 6

#: 每张图最多消耗几次 check（实测上限约 3 次，留 1 次余量）。
MAX_CANDIDATES_PER_IMAGE = 2

#: seccode check 响应里的 CDATA 内容。
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


@runtime_checkable
class Solver(Protocol):
    """验证码识别器接口。

    实测 Go 与 Python 实现输出等价（附录 A.12），因此这层抽象让
    「换 OCR 后端」不需要改任何业务代码。
    """

    def classify(self, image: bytes) -> str:
        """返回识别结果（大小写不保证）。"""
        ...


class PythonSolver:
    """基于 ``ddddocr`` 的求解器。

    懒加载模型：``ddddocr`` 导入 + 建模型约 1 秒、占几十 MB 内存，
    只在真正需要登录时才付这个代价。
    """

    def __init__(self, *, beta: bool = False) -> None:
        self._beta = beta
        self._ocr = None

    def _ensure(self):  # type: ignore[no-untyped-def]
        if self._ocr is None:
            import ddddocr  # 延迟导入：启动时不加载 onnxruntime

            self._ocr = ddddocr.DdddOcr(beta=self._beta, show_ad=False)
        return self._ocr

    def classify(self, image: bytes) -> str:
        return str(self._ensure().classification(image))


class StubSolver:
    """固定返回值的求解器（测试用）。"""

    def __init__(self, value: str = "abcd") -> None:
        self.value = value
        self.calls = 0

    def classify(self, image: bytes) -> str:
        self.calls += 1
        return self.value


def parse_seccode_check(body: str) -> bool:
    """解析 ``misc.php?mod=seccode&action=check`` 的响应。

    实测响应形如::

        <?xml version="1.0" encoding="utf-8"?>
        <root><![CDATA[succeed]]></root>

    ⚠️ **必须剥掉 CDATA**。早期实现直接 ``body.startswith("succeed")``
    导致结果**永远为假**，把「100% 失败」误当成测量结论。

    容错：无 CDATA 时再试「剥掉 XML 标签后的纯文本」，
    这样 ``<root>succeed</root>`` 与裸 ``succeed`` 都能识别。
    """
    m = _CDATA_RE.search(body)
    if m:
        content = m.group(1)
    else:
        # 去掉 XML 声明与标签，留下纯文本
        content = re.sub(r"<[^>]*>", "", body)
    return content.strip().lower().startswith("succeed")


class SolveResult:
    """求解结果。"""

    __slots__ = ("code", "images_used", "checks_used")

    def __init__(self, code: str, images_used: int, checks_used: int) -> None:
        self.code = code
        self.images_used = images_used
        self.checks_used = checks_used

    def __repr__(self) -> str:
        return (
            f"SolveResult(code={self.code!r}, images_used={self.images_used},"
            f" checks_used={self.checks_used})"
        )


class CaptchaSolver:
    """编排「取图 → OCR → 预校验」的循环。

    依赖注入两个回调，因此**可以完全离线测试**：

    - ``fetch_image()``：取一张新验证码图（每次调用轮换新码）
    - ``check(code)``：用服务端 check 端点预校验（不消耗登录额度）
    """

    def __init__(
        self,
        *,
        solvers: list[Solver],
        fetch_image,
        check,
        max_images: int = MAX_IMAGES,
        max_candidates: int = MAX_CANDIDATES_PER_IMAGE,
    ) -> None:
        if not solvers:
            raise ValueError("至少需要一个 solver")
        self._solvers = solvers
        self._fetch_image = fetch_image
        self._check = check
        self._max_images = max_images
        self._max_candidates = max_candidates

    def solve(self) -> SolveResult | None:
        """求解一个服务端确认正确的验证码。

        Returns:
            :class:`SolveResult`；超过换图上限仍未成功则返回 ``None``。
        """
        checks = 0
        for image_no in range(1, self._max_images + 1):
            image = self._fetch_image()

            # 两个模型都跑，结果去重后按顺序试
            candidates: list[str] = []
            for solver in self._solvers:
                try:
                    raw = solver.classify(image)
                except Exception:
                    continue          # 单个 solver 崩掉不该终止整个流程
                code = (raw or "").strip()
                if code and code not in candidates:
                    candidates.append(code)

            for code in candidates[: self._max_candidates]:
                checks += 1
                if self._check(code):
                    return SolveResult(code, image_no, checks)

        return None
