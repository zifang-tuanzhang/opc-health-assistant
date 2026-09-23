"""网关守卫（guard）：接入层基础设施，业务无关。

采用输入清洗 / 安全头 / 会话限流 / 异常兜底 / 日志脱敏的网关守卫范式，
剥离全部业务，纯做「网关边界」。
本模块绝不判断任何医疗事实——它只管「输入是否合法、请求是否超限、异常如何兜底」。

职责边界（来自开发指导架构·输入护栏）：
- 属于「左侧可硬编码」：空输入 / 超长 / 控制符注入 / 会话限速 / 安全头。
- 不属于本模块：任何医院/科室/医生/排班/边界拒答的判定（那是编排层+护栏层的事）。

与护栏层（guardrails）的分工：本模块守「输入侧网关边界」（每条请求、与业务无关），
guardrails 守「产出侧结构性校验」（4 段式契约 / 边界词 / 检索真实性）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import config
from . import keystore  # 仅用于错误信封如实标注当前运行模式，不涉业务逻辑

logger = logging.getLogger("opc.guard")

# Unicode C0 控制字符（含 \x00-\x1f，不含换行\t 之外的可打印区）；
# 用于剥离日志注入/协议走私字符，防止恶意输入污染日志或越权。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 危险协议前缀（防 SSRF 类提示注入进入检索词）
_DANGEROUS_SCHEME = re.compile(r"^\s*(file|gopher|ftp|dict|javascript|vbscript|data):(//)?", re.IGNORECASE)


class RateLimiter:
    """进程内滑动窗口限流（按 session 维度，缺失则按客户端 IP）。

    网关层保护：防止单会话刷接口。
    故障即放行（非致命），绝不因限流自身异常阻断主流程。
    """

    def __init__(self, limit_per_min: int) -> None:
        self.limit = max(0, int(limit_per_min))
        self._hits: dict[str, list[float]] = {}
        self._window = 60.0

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        window = self._hits.get(key, [])
        window = [t for t in window if now - t < self._window]
        if len(window) >= self.limit:
            self._hits[key] = window
            return False
        window.append(now)
        self._hits[key] = window
        return True


# 模块级单例（全进程共享限流状态）。
rate_limiter = RateLimiter(config.RATE_LIMIT_PER_MIN)


def sanitize_input(text: str) -> str:
    """清洗用户输入：剥离 C0 控制符（防日志注入）。不改动业务语义。"""
    if not text:
        return ""
    cleaned = _CONTROL_CHARS.sub("", text)
    return cleaned


def validate_message(text: str) -> tuple[bool, Optional[str], Optional[str]]:
    """输入护栏（左侧可硬编码）：空 / 超长 / 危险协议前缀。

    返回 (ok, error_code, hint)。hint 为面向用户的提示文案，不含任何业务事实。
    """
    if text is None:
        return False, "EMPTY", "请输入您想查询的内容（例如：昆明哪家三甲医院有心血管内科？）"
    stripped = text.strip()
    if not stripped:
        return False, "EMPTY", "输入为空，请描述您想查询的医院、科室、医生或便民信息。"
    if len(stripped) > config.MAX_MESSAGE_LEN:
        return (
            False,
            "TOO_LONG",
            f"输入过长（上限 {config.MAX_MESSAGE_LEN} 字），请精简提问后重试。",
        )
    if _DANGEROUS_SCHEME.match(stripped):
        return False, "BLOCKED_SCHEME", "输入含不支持的协议前缀，已被忽略。请正常描述查询需求。"
    return True, None, None


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """统一安全响应头（防 MIME 嗅探 / 点击劫持等）。"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response


def client_key(request: Request, session_id: Optional[str]) -> str:
    """限流维度：优先 session，缺失则用客户端 IP。"""
    if session_id:
        return f"s:{session_id}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def api_token_ok(request: Request) -> bool:
    """可选 API 令牌闸（纵深防御）。

    OPC_API_TOKEN 未设置时默认通过（本地演示，仅 127.0.0.1 可达，暴露面极小）；
    设置后，敏感端点（/api/keys/add、/history、/reset）必须携带
    ``X-OPC-Token`` 请求头或 ``?token=`` 查询参数且与之匹配，否则拒绝。
    失败即拒绝（这是安全闸，与限流"故障即放行"的取舍相反——安全优先）。
    """
    tok = getattr(config, "API_TOKEN", "")
    if not tok:
        return True
    return request.headers.get("X-OPC-Token") == tok or (request.query_params.get("token") or "") == tok


def error_body(code: str, hint: str, session_id: Optional[str] = None) -> dict:
    """统一结构化错误体（不泄漏堆栈/内部细节）。

    形状必须与 ``ResponseEnvelope`` 一致（含 ``output`` 4 段式包裹层）：
    前端/小程序统一读 ``output.*``，若此处返回扁平结构会渲染空白。
    本模块不 import schema（保持「网关边界、业务无关」），因此按同形状手工构造。
    """
    return {
        "ok": False,
        "session_id": session_id or "(未生成)",
        "error_code": code,
        "output": {
            "query_condition": None,
            "query_results": [],
            "info_basis": [],
            "usage_tips": [hint],
        },
        "retrieval_log": [],
        "reflection_count": 0,
        "degraded": True,
        # 如实反映当前运行模式（与 /health 同口径）：未配密钥=need_key，已配=agent。
        # 不用占位值——错误信封的 mode 会被小程序/评审直接读到，必须落在契约枚举内。
        "mode": "agent" if keystore.is_configured() else "need_key",
        "note": code,
    }


async def exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理器：任何未捕获异常返回结构化错误，绝不泄漏内部细节。"""
    logger.exception("未捕获异常（已兜底）: %s", config.secret_redaction(str(exc)))
    return JSONResponse(
        status_code=200,  # 业务层用 ok=False 表达失败，HTTP 始终 200 便于前端解析
        content=error_body("INTERNAL", "服务暂不可用，请稍后重试。"),
    )
