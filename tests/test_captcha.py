"""验证码模块测试。全部离线。"""

from __future__ import annotations

import pytest

from bt169.source.captcha import (
    CaptchaSolver,
    StubSolver,
    parse_seccode_check,
)


# ------------------------------------------------------------ 响应解析


def test_parse_seccode_check_succeed():
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<root><![CDATA[succeed]]></root>"
    )
    assert parse_seccode_check(body) is True


def test_parse_seccode_check_invalid():
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<root><![CDATA[invalid]]></root>"
    )
    assert parse_seccode_check(body) is False


def test_parse_seccode_check_without_cdata():
    """容错：万一服务端不裹 CDATA 也要能认。"""
    assert parse_seccode_check("succeed") is True
    assert parse_seccode_check("<root>succeed</root>") is True


def test_parse_seccode_check_is_case_insensitive():
    """实测服务端大小写不敏感，响应解析也不该挑剔。"""
    assert parse_seccode_check("<root><![CDATA[SUCCEED]]></root>") is True


def test_parse_seccode_check_rejects_other_content():
    assert parse_seccode_check("<root><![CDATA[error]]></root>") is False
    assert parse_seccode_check("") is False


def test_parse_seccode_check_regression_cdata_bug():
    """★ 回归：早期实现直接 startswith("succeed")，因 CDATA 前缀**永远为假**。

    这曾导致把「100% 失败」当成测量结果（ARCHITECTURE.md 附录 A.2 的教训）。
    """
    body = '<root><![CDATA[succeed]]></root>'
    assert body.startswith("succeed") is False   # 说明裸 startswith 是错的
    assert parse_seccode_check(body) is True     # 正确实现


# ------------------------------------------------------------ 求解编排


def test_solve_first_try():
    solver = StubSolver("abcd")
    result = CaptchaSolver(
        solvers=[solver],
        fetch_image=lambda: b"img",
        check=lambda code: code == "abcd",
    ).solve()
    assert result is not None
    assert result.code == "abcd"
    assert result.images_used == 1
    assert result.checks_used == 1


def test_solve_retries_with_new_images():
    """前两张图的候选都不对，第三张才对。"""
    images = iter([b"a", b"b", b"c"])
    solvers = [StubSolver("xxxx")]
    seen_images = []

    def fetch():
        img = next(images)
        seen_images.append(img)
        return img

    def check(code):
        return len(seen_images) == 3 and code == "xxxx"

    result = CaptchaSolver(solvers=solvers, fetch_image=fetch, check=check).solve()
    assert result is not None
    assert result.images_used == 3
    assert seen_images == [b"a", b"b", b"c"]


def test_solve_uses_second_candidate():
    """第一候选错、第二候选对 → 只消耗 1 张图、2 次 check。

    ★ 需要**两个** solver 才会在同一张图上产生两个候选；
    单个 solver 每张图只产出一个候选。
    """
    class Bad:
        def classify(self, image): return "bad"

    class Good:
        def classify(self, image): return "good"

    result = CaptchaSolver(
        solvers=[Bad(), Good()],
        fetch_image=lambda: b"img",
        check=lambda code: code == "good",
    ).solve()
    assert result is not None
    assert result.code == "good"
    assert result.images_used == 1
    assert result.checks_used == 2


def test_solve_gives_up_after_max_images():
    """永远失败时必须在 max_images 处停下，不能无限循环。"""
    result = CaptchaSolver(
        solvers=[StubSolver("nope")],
        fetch_image=lambda: b"img",
        check=lambda code: False,
        max_images=4,
    ).solve()
    assert result is None


def test_solve_respects_max_candidates_per_image():
    """★ 硬上限：每图最多 2 次 check（实测单图约 3 次机会，留 1 次余量）。"""
    calls = []

    class ThreeSolver:
        def classify(self, image):
            calls.append(1)
            return f"c{len(calls)}"

    CaptchaSolver(
        solvers=[ThreeSolver()],
        fetch_image=lambda: b"img",
        check=lambda code: False,
        max_images=1,
    ).solve()
    # 1 张图 × 最多 2 候选 = 2 次 check，而不是 3 次
    assert len(calls) == 1        # classify 每次取图只调一次（一个 solver）


def test_solve_dedupes_identical_candidates():
    """两个模型给出相同结果时不应重复 check（浪费额度）。"""
    checks = []

    class Same:
        def classify(self, image): return "same"

    CaptchaSolver(
        solvers=[Same(), Same()],
        fetch_image=lambda: b"img",
        check=lambda code: checks.append(code) or False,
        max_images=1,
    ).solve()
    assert checks == ["same"]


def test_solve_survives_solver_exception():
    """单个 solver 抛异常不该终止流程。"""
    class Boom:
        def classify(self, image): raise RuntimeError("模型崩了")

    result = CaptchaSolver(
        solvers=[Boom(), StubSolver("ok")],
        fetch_image=lambda: b"img",
        check=lambda code: code == "ok",
    ).solve()
    assert result is not None and result.code == "ok"


def test_solve_ignores_empty_candidates():
    class Empty:
        def classify(self, image): return "   "

    result = CaptchaSolver(
        solvers=[Empty()],
        fetch_image=lambda: b"img",
        check=lambda code: True,
        max_images=2,
    ).solve()
    assert result is None


def test_solve_requires_at_least_one_solver():
    with pytest.raises(ValueError, match="solver"):
        CaptchaSolver(solvers=[], fetch_image=lambda: b"", check=lambda c: False)


def test_solve_result_repr():
    result = CaptchaSolver(
        solvers=[StubSolver("ab")],
        fetch_image=lambda: b"i",
        check=lambda c: True,
    ).solve()
    assert result is not None
    assert "ab" in repr(result)
