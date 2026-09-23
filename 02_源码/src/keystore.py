"""外部密钥库（keystore）。

设计约束：
- 密钥【绝不】存放在项目目录内，而是存在用户主目录下的
  ``%USERPROFILE%\\.opc_health\\keys.json``（Windows 例：
  ``C:\\Users\\<用户名>\\.opc_health\\keys.json``）。
- 这样即便把整个项目打成 ZIP 发出去，密钥也不会跟着走；
  git 也根本碰不到它（它不在仓库里）。
- 本项目只「读」这个外部文件来连接模型，绝不把密钥写回仓库任何位置。

使用者在页面点击「添加密钥」后，密钥被写入这里；后端每次启动/每次请求都
自动扫描这个固定位置——有有效密钥就直接连接（你说的「自动扫描」）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("opc.keystore")

# ── 密钥固定存放位置：项目外部（用户主目录） ────────────────────────────
STORE_DIR = Path(os.path.expanduser("~")) / ".opc_health"
STORE_PATH = STORE_DIR / "keys.json"

# 当前激活供应商在库里的键名
_ACTIVE_KEY = "active_provider"


def _ensure_store() -> None:
    """确保密钥目录与文件存在（文件缺失则建空壳）。"""
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        if not STORE_PATH.exists():
            STORE_PATH.write_text(
                json.dumps({"providers": {}, _ACTIVE_KEY: None}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        # 极端情况下（如主目录不可写）不让程序崩，交还给调用方判断
        pass


def load_store() -> dict:
    """读取密钥库；损坏或缺失返回空壳。绝不抛异常。"""
    _ensure_store()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"providers": {}, _ACTIVE_KEY: None}
        data.setdefault("providers", {})
        data.setdefault(_ACTIVE_KEY, None)
        return data
    except Exception:
        # fail-open（空壳兜底），但密钥库损坏必须留痕——否则「已配密钥凭空消失」
        # 会被误判成 need_key，无从排查（交付前独立审计 P2#15）。
        logger.warning(
            "密钥库 %s 读取失败（文件可能损坏），按空库处理。", STORE_PATH, exc_info=True
        )
        return {"providers": {}, _ACTIVE_KEY: None}


def save_store(data: dict) -> None:
    """写回密钥库（仅写到外部位置）。"""
    _ensure_store()
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_configured() -> bool:
    """是否已有「非空密钥」的激活供应商（不校验真伪，只校验存在性）。"""
    data = load_store()
    active = data.get(_ACTIVE_KEY)
    if not active:
        return False
    entry = data.get("providers", {}).get(active)
    if not entry:
        return False
    return bool((entry.get("api_key") or "").strip())


def get_active() -> Optional[dict]:
    """返回当前激活供应商的连接信息：{id, api_key, model, base_url?}。

    base_url 由预设清单提供，这里只存 id / api_key / model。
    """
    data = load_store()
    active = data.get(_ACTIVE_KEY)
    if not active:
        return None
    entry = data.get("providers", {}).get(active)
    if not entry or not (entry.get("api_key") or "").strip():
        return None
    return {
        "id": active,
        "api_key": entry.get("api_key", "").strip(),
        "model": entry.get("model") or "",
    }


def upsert_provider(provider_id: str, api_key: str, model: str = "") -> None:
    """新增/覆盖某个供应商的密钥，并设为激活。"""
    data = load_store()
    data.setdefault("providers", {})
    data["providers"][provider_id] = {
        "api_key": api_key.strip(),
        "model": (model or "").strip(),
    }
    data[_ACTIVE_KEY] = provider_id
    save_store(data)


def set_active(provider_id: str) -> bool:
    """切换激活供应商；不存在返回 False。"""
    data = load_store()
    if provider_id not in data.get("providers", {}):
        return False
    data[_ACTIVE_KEY] = provider_id
    save_store(data)
    return True


def list_configured() -> list[dict]:
    """已配置供应商清单（不含密钥值，只给界面展示用）。"""
    data = load_store()
    out = []
    for pid, entry in data.get("providers", {}).items():
        out.append(
            {
                "id": pid,
                "model": entry.get("model", ""),
                "has_key": bool((entry.get("api_key") or "").strip()),
            }
        )
    return out


def store_path() -> str:
    """返回密钥库路径（界面与日志中可告知使用者密钥存放位置）。"""
    return str(STORE_PATH)
