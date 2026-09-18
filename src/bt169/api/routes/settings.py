"""设置读写。按分区提交，密钥字段脱敏。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bt169.api.auth import GATE_KEY, revoke_all_sessions
from bt169.api.deps import get_db, get_settings
from bt169.config import (
    PLACEHOLDER,
    SETTINGS_SECTIONS,
    ConfigError,
    parse_rss_url,
    section_key,
)
from bt169.crypto import hash_password
from bt169.db import Database
from bt169.repo.settings import SettingsRepo

router = APIRouter(tags=["settings"])


class SettingsPatch(BaseModel):
    """一次提交只针对一个分区（ADR-18）。"""

    section: str = Field(..., description="分区名")
    values: dict[str, str] = Field(default_factory=dict)


@router.get("/api/settings")
def read_settings(sr: SettingsRepo = Depends(get_settings)) -> dict[str, Any]:
    """返回全部分区的设置。

    密钥字段一律替换为固定长度占位符——**不回传明文，也不回传长度或末位**。
    前端把占位符原样回传即表示「未修改」。

    存储键带分区前缀（``site.password``），这里拆回嵌套形状：
    ``{"site": {"password": "••••••••"}}``。
    """
    out: dict[str, dict[str, str]] = {s: {} for s in SETTINGS_SECTIONS}
    for storage_key, value in sr.get_all(masked=True).items():
        section, _, key = storage_key.partition(".")
        if section in out:
            out[section][key] = value
    return out


@router.put("/api/settings")
def write_settings(
    patch: SettingsPatch,
    sr: SettingsRepo = Depends(get_settings),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """保存一个分区的设置。

    约定：

    - 值等于 :data:`PLACEHOLDER` 的密钥字段 → 视为未修改，跳过（计入 ``skipped``）
    - 值为**空字符串** → 删除该键
    - ``rss_url`` → 解析并清洗（剥掉 ``auth`` 等无关参数）
    """
    if patch.section not in SETTINGS_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_section",
                "message": f"未知分区：{patch.section!r}"
                           f"（合法值：{list(SETTINGS_SECTIONS)}）",
            },
        )

    to_write: dict[str, str] = {}
    to_delete: list[str] = []

    for key, value in patch.values.items():
        # ★ 加分区前缀，避免「站点」与「代理」的 password 互相覆盖
        storage = section_key(patch.section, key)
        if key == "rss_url" and value:
            try:
                _, value = parse_rss_url(value)
            except ConfigError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_rss_url", "message": str(exc)},
                ) from exc
        # ★ 访问密码必须哈希后落库（S-6 / ADR-17）。
        #   漏掉这一步，门禁就退化成明文比对——而且明文会躺在库里。
        #
        # ★★ 但**占位符必须在哈希之前排掉**：前端每次保存都会把密钥
        #    字段回传成 ``••••••••``。若先哈希，占位符就变成一个合法
        #    的哈希写进库，用户只是点了一下保存、什么都没改，访问密码
        #    就被换掉了——再也进不来。哈希是**不可逆**的，这个错误
        #    没有任何补救余地。
        if storage == GATE_KEY and value and value != PLACEHOLDER:
            value = hash_password(value)
        if value == "":
            to_delete.append(storage)
        else:
            to_write[storage] = value

    skipped = sr.put_many(to_write)
    for storage in to_delete:
        sr.delete(storage)

    # ★ 改了访问密码就踢掉所有设备：否则「改密码」在安全上是空的，
    #   任何拿过 cookie 的人照样能用到 30 天后。
    #
    # ★★ 必须排除「只回传了占位符」的情况——那种情况下什么都没改，
    #    把人踢下线纯属误伤（用户会看到自己莫名被登出）。
    gate_changed = (GATE_KEY in to_delete) or (
        GATE_KEY in to_write and GATE_KEY not in skipped
    )
    if gate_changed:
        revoke_all_sessions(db)

    def _bare(k: str) -> str:
        return k.partition(".")[2]

    return {
        "section": patch.section,
        # 返回值去掉前缀，前端看到的是分区内字段名
        "saved": sorted(_bare(k) for k in set(to_write) - set(skipped)),
        "skipped": sorted(_bare(k) for k in skipped),
        "deleted": sorted(_bare(k) for k in to_delete),
    }
