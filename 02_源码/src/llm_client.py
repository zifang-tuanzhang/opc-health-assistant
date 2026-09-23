"""模型客户端：解析连接信息 → 构造 OpenAI 兼容客户端 → 带运行保障的调用。

所有国内厂商都是 OpenAI 兼容网关，因此后端只用一套 openai SDK，按 provider 的
base_url 调。

连接信息解析顺序（:func:`_resolve_connection`）：
  1. **外部密钥库**（主路径）：使用者在网页「添加密钥」写入
     ``%USERPROFILE%\\.opc_health\\keys.json``，密钥不进仓库；
  2. **环境变量回落**：容器 / CI / 无用户目录场景用 ``OPC_LLM_API_KEY`` 等；
     二者同时存在时密钥库优先。

调用入口 :func:`chat` 统一挂上运行保障（缓存去重 / 限速 / 每日预算，见 cost_gate），
使编排层不必关心用量节制。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx
from openai import OpenAI

from . import config, cost_gate, keystore, providers

logger = logging.getLogger("opc.llm_client")

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _direct_http_client(timeout: float) -> httpx.Client:
    """构造【忽略环境代理】的 HTTP 客户端（直连纪律）。

    与 src/search.py 的 ``_get()`` 同源问题：httpx 与 openai SDK 默认读取进程环境里的
    ``HTTP(S)_PROXY``。本机或使用者机器上若残留代理或 VPN 出口（例如调试用代理未关），
    模型调用会被带到非预期出口，表现为连接失败、超时，甚至被误读成「没配密钥」的假故障；
    而国内厂商网关（如 DashScope）直连本就通畅。故此处一律直连，不读环境代理。
    """
    return httpx.Client(timeout=timeout, trust_env=False)


class LLMError(Exception):
    """模型调用失败（含未配置密钥 / 未指定模型 / 上游错误），``code`` 供响应使用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─────────────────────────── 连接信息解析 ───────────────────────────


def _resolve_connection() -> Optional[dict]:
    """返回 {base_url, api_key, model, source}；未配置返回 None。密钥库优先。"""
    active = keystore.get_active()
    if active:
        preset = providers.get_provider(active["id"]) or {}
        return {
            "base_url": preset.get("base_url") or _DEFAULT_BASE_URL,
            "api_key": active["api_key"],
            "model": active.get("model") or preset.get("default_model") or "",
            "source": "keystore",
        }
    if config.LLM_API_KEY:
        return {
            "base_url": config.LLM_BASE_URL or _DEFAULT_BASE_URL,
            "api_key": config.LLM_API_KEY,
            "model": config.LLM_MODEL or "",
            "source": "env",
        }
    return None


def build_client() -> Optional[OpenAI]:
    """用当前连接信息构造客户端；未配置返回 None。"""
    conn = _resolve_connection()
    if not conn:
        return None
    config.register_secret(conn["api_key"])  # 密钥进内存即登记，日志永不落明文
    try:
        return OpenAI(
            base_url=conn["base_url"],
            api_key=conn["api_key"],
            timeout=30.0,
            http_client=_direct_http_client(30.0),  # 直连，不读环境代理
        )
    except Exception:
        # fail-open（返回 None 走 NEED_KEY 降级），但 SDK 构造失败必须留痕——
        # 否则结构性故障被伪装成「未配密钥」，误导排查（交付前独立审计 P2#16）。
        logger.warning("模型客户端构造失败（连接信息存在但 SDK 初始化异常）。", exc_info=True)
        return None


def is_ready() -> bool:
    """是否已有可用连接（不校验密钥真伪，只校验存在性）。"""
    return _resolve_connection() is not None


def active_model() -> str:
    """当前生效的模型名（密钥库优先，缺失回落预设 default_model）。"""
    conn = _resolve_connection()
    return (conn or {}).get("model", "")


def connection_info() -> dict:
    """连接概况（不含密钥值，供 /health 与界面展示）。"""
    conn = _resolve_connection()
    if not conn:
        return {"configured": False}
    return {
        "configured": True,
        "source": conn["source"],
        "model": conn["model"],
        "base_url": conn["base_url"],
    }


# ─────────────────────────── 带运行保障的调用 ───────────────────────────


class _CachedMessage:
    __slots__ = ("content", "tool_calls")

    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _CachedChoice:
    __slots__ = ("message",)

    def __init__(self, content: str) -> None:
        self.message = _CachedMessage(content)


class _CachedResponse:
    """命中缓存时的轻量响应替身（只提供编排层需要的接口）。"""

    __slots__ = ("choices", "usage")

    def __init__(self, content: str) -> None:
        self.choices = [_CachedChoice(content)]
        self.usage = None


def chat(model: str, messages: list, **kwargs: Any):
    """统一模型调用入口：缓存 → 限速/预算闸门 → 调用 → 记用量 → 回填缓存。

    抛出 :class:`LLMError`（未配置 / 未指定模型 / 上游错误）或
    :class:`cost_gate.GateDenied`（限速 / 超额），由编排层转成结构化响应。
    """
    if not model:
        raise LLMError("NO_MODEL", "未指定模型名，请在「添加密钥」时选择模型。")

    key = cost_gate.cache_key(model, messages, kwargs)
    cached = cost_gate.get_cached(key)
    if cached is not None:
        return _CachedResponse(cached)

    client = build_client()
    if client is None:
        raise LLMError("NEED_KEY", "尚未配置模型密钥，请点「添加密钥」后重试。")

    cost_gate.check()  # 超限抛 GateDenied（不消耗缓存、不发起调用）

    try:
        resp = client.chat.completions.create(model=model, messages=messages, **kwargs)
    except Exception as e:  # noqa: BLE001 - 上游异常统一转成结构化错误
        raise LLMError("UPSTREAM_ERROR", f"{type(e).__name__}: {e}") from e

    usage = getattr(resp, "usage", None)
    cost_gate.record_usage(getattr(usage, "total_tokens", 0) if usage else 0)

    try:
        content = resp.choices[0].message.content or ""
    except Exception:
        content = ""
    # 只缓存「最终生成」这类可复现请求（带 response_format），不缓存工具调用轮
    if kwargs.get("response_format"):
        cost_gate.set_cached(key, content)
    return resp


# ─────────────────────────────── 密钥校验 ───────────────────────────────


def test_connection(api_key: str, base_url: str, model: str) -> Tuple[bool, str]:
    """校验一组密钥能否真正连通（用于「添加密钥」时即时反馈）。

    只用一次极小调用（max_tokens=1），不消费多少额度。
    返回 (是否成功, 信息)。
    """
    config.register_secret(api_key)
    try:
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=20.0,
            http_client=_direct_http_client(20.0),  # 直连，不读环境代理
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
        ok = bool(resp and resp.choices)
        msg = "连接成功" if ok else "返回为空，请检查模型名"
        return ok, msg
    except Exception as e:  # noqa: BLE001 - 校验本就要捕获一切
        return False, f"连接失败：{type(e).__name__}: {e}"
