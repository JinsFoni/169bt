"""设置读写。按分区提交，密钥字段脱敏。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bt169.api.deps import get_settings
from bt169.config import (
    SETTINGS_SECTIONS,
    ConfigError,
    parse_rss_url,
    section_key,
)
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
        if value == "":
            to_delete.append(storage)
        else:
            to_write[storage] = value

    skipped = sr.put_many(to_write)
    for storage in to_delete:
        sr.delete(storage)

    def _bare(k: str) -> str:
        return k.partition(".")[2]

    return {
        "section": patch.section,
        # 返回值去掉前缀，前端看到的是分区内字段名
        "saved": sorted(_bare(k) for k in set(to_write) - set(skipped)),
        "skipped": sorted(_bare(k) for k in skipped),
        "deleted": sorted(_bare(k) for k in to_delete),
    }
