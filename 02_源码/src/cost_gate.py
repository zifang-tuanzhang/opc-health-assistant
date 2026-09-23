"""LLM 运行保障（cost gate）：缓存去重 + 每分钟限速 + 每日 token 预算。

对应赛题「进阶3·运行保障」。三件事都在这一处收口，避免散落在编排里：

1. **缓存去重**：相同 (模型, 消息, 参数) 的请求在 TTL 内直接复用结果，
   不重复烧 token（演示现场反复问同一句时尤其有用）。
2. **每分钟限速**：LLM 调用次数上限（``config.LLM_RATE_LIMIT_PER_MIN``），
   防止死循环或误刷把额度打空。
3. **每日预算**：累计 token 上限（``config.LLM_DAILY_BUDGET_TOKENS``），
   超限直接拒绝新的调用，返回明确降级码。

设计取舍：
- 全部为**进程内内存**实现（演示与单机交付足够），不落盘、不依赖外部服务；
- 线程安全（编排在线程池里跑）；
- **fail-open**：本模块任何内部异常都不阻断主流程（运行保障不该成为新故障点）；
- 只做「用量与节制」，**不碰内容**——不判断事实真假，不修改模型输出。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional

from . import config


class GateDenied(Exception):
    """被运行保障拒绝（限速 / 超额）。``code`` 直接作为响应的 error_code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}
_tokens_today: int = 0
_day: str = ""
_calls_this_minute: list[float] = []
_calls_total: int = 0
_cache_hits: int = 0


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _rollover_locked() -> None:
    """跨天清零当日计数（调用方须已持锁）。"""
    global _tokens_today, _day
    d = _today()
    if d != _day:
        _day = d
        _tokens_today = 0


# ─────────────────────────────── 缓存 ───────────────────────────────


def cache_key(model: str, messages: Any, params: Any) -> str:
    """请求指纹（模型 + 完整消息 + 关键参数）。"""
    raw = json.dumps(
        {"model": model, "messages": messages, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached(key: str) -> Optional[str]:
    """命中且未过期则返回缓存内容，否则 None。"""
    global _cache_hits
    try:
        with _lock:
            item = _cache.get(key)
            if not item:
                return None
            ts, value = item
            if time.time() - ts > config.LLM_CACHE_TTL_SECONDS:
                _cache.pop(key, None)
                return None
            _cache_hits += 1
            return value
    except Exception:
        return None


def set_cached(key: str, value: str) -> None:
    try:
        with _lock:
            _cache[key] = (time.time(), value)
    except Exception:
        pass


# ─────────────────────────── 限速与预算 ───────────────────────────


def check() -> None:
    """调用前闸门：超限抛 :class:`GateDenied`；通过则占用一个调用名额。

    注意：命中缓存时不应调用本函数（没有真实调用发生，不该占名额）。
    """
    now = time.time()
    with _lock:
        _rollover_locked()
        _calls_this_minute[:] = [t for t in _calls_this_minute if now - t < 60.0]
        limit = config.LLM_RATE_LIMIT_PER_MIN
        if limit > 0 and len(_calls_this_minute) >= limit:
            raise GateDenied(
                "LLM_RATE_LIMIT",
                f"模型调用过于频繁（每分钟上限 {limit} 次），请稍后重试。",
            )
        budget = config.LLM_DAILY_BUDGET_TOKENS
        if budget > 0 and _tokens_today >= budget:
            raise GateDenied(
                "LLM_BUDGET_EXCEEDED",
                f"今日模型额度已用尽（已用 {_tokens_today} / 上限 {budget} tokens），"
                "请明日再试或调整预算配置。",
            )
        _calls_this_minute.append(now)


def record_usage(total_tokens: Optional[int]) -> None:
    """记录一次真实调用的用量（token 数由上游响应提供，缺失按 0 计）。"""
    global _tokens_today, _calls_total
    try:
        with _lock:
            _rollover_locked()
            _calls_total += 1
            if isinstance(total_tokens, int) and total_tokens > 0:
                _tokens_today += total_tokens
    except Exception:
        pass


def stats() -> dict:
    """运行保障实时用量（供 /health 展示）。"""
    with _lock:
        return {
            "tokens_today": _tokens_today,
            "daily_budget_tokens": config.LLM_DAILY_BUDGET_TOKENS,
            "llm_calls_total": _calls_total,
            "cache_entries": len(_cache),
            "cache_hits": _cache_hits,
            "cache_ttl_seconds": config.LLM_CACHE_TTL_SECONDS,
            "rate_limit_per_min": config.LLM_RATE_LIMIT_PER_MIN,
        }


def reset() -> None:
    """清空全部计数与缓存（测试用）。"""
    global _cache, _tokens_today, _calls_this_minute, _calls_total, _cache_hits, _day
    with _lock:
        _cache = {}
        _tokens_today = 0
        _calls_this_minute = []
        _calls_total = 0
        _cache_hits = 0
        _day = _today()
