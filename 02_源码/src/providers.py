"""预设模型供应商清单加载器。

预设清单 ``keys/providers.preset.json`` 随源码提交（只装配置，不装密钥）。
本模块负责把它读进来，供「添加密钥」界面下拉、以及后端构造模型客户端使用。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger("opc.providers")

# src/ 的上一级是 02_源码/，再进 keys/
_PRESET_PATH = Path(__file__).resolve().parent.parent / "keys" / "providers.preset.json"


@lru_cache(maxsize=1)
def load_providers() -> dict:
    """读取预设清单，返回 {provider_id: {...元数据...}}。"""
    try:
        data = json.loads(_PRESET_PATH.read_text(encoding="utf-8"))
        return {p["id"]: p for p in data.get("providers", [])}
    except Exception:
        # fail-open（空清单兜底，不阻断启动），但结构性损坏必须留痕——
        # 静默吞掉会让「前端厂商下拉为空」变成无从排查的悬案。
        logger.warning(
            "预设清单读取失败：%s 损坏或缺失，厂商下拉将为空，请检查该文件。",
            _PRESET_PATH,
            exc_info=True,
        )
        return {}


def get_provider(provider_id: str) -> Optional[dict]:
    return load_providers().get(provider_id)


def provider_list() -> list[dict]:
    """给前端用的精简列表（不含任何密钥）。"""
    out = []
    for pid, p in load_providers().items():
        out.append(
            {
                "id": pid,
                "vendor": p.get("vendor", pid),
                "base_url": p.get("base_url", ""),
                "models": p.get("models", []),
                "default_model": p.get("default_model", ""),
                "homepage": p.get("homepage", ""),
                "note": p.get("note", ""),
            }
        )
    return out
