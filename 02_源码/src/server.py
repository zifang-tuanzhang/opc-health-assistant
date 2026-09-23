"""接入层入口（server）：FastAPI 网关。

职责（来自开发指导架构·M1 接入层）：
- /chat        ：用户提问入口（小程序/curl 用的稳定 JSON 契约）。
- /chat/stream ：同上的 SSE 流式版，按阶段推进度事件（前端「检索中」可见）。
- /reset       ：会话重置（清空服务端上下文）。
- /health      ：状态探测（含密钥/连接/搜索后端/运行保障用量）。
- /api/keys/*  ：预设清单 / 自动扫描状态 / 添加密钥（免翻文件）。
- /            ：前端交互界面（含「添加密钥」弹窗）。
- /static/小程序接口测试.html ：小程序接口模拟测试页。

密钥存在项目【外部】（%USERPROFILE%\\.opc_health\\keys.json），后端自动扫描连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, cost_gate, guard, keystore, keys_routes, llm_client, orchestrator, session
from .schema import ResponseEnvelope, make_error_envelope

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("opc.server")

API_VERSION = "1.0"

app = FastAPI(title="OPC 接单吧·AI 医院资源查询与便民就医助手", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(guard.SecurityHeadersMiddleware)

# 密钥路由
app.include_router(keys_routes.router)

# 前端静态目录（index.html 即交互界面，含「添加密钥」弹窗）
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ResetRequest(BaseModel):
    session_id: Optional[str] = None


@app.exception_handler(Exception)
async def _exc_handler(request: Request, exc: Exception):
    return await guard.exception_handler(request, exc)


# ─────────────────────────── 小程序 / curl 稳定契约 ───────────────────────────


def _live_mode() -> str:
    """错误信封的运行模式：如实反映当前密钥配置状态（与 /health 的 key_configured 同口径）。

    为什么不让 schema 写死默认值：错误信封的 ``mode`` 会被小程序与使用者直接读到，
    必须落在接口文档声明的枚举内（``agent`` / ``chat`` / ``need_key``），
    不得出现文档未定义的占位值。
    """
    return "agent" if keystore.is_configured() else "need_key"


@app.post("/chat", response_model=ResponseEnvelope)
def chat(req: ChatRequest, request: Request) -> ResponseEnvelope:
    """一次请求 → 一个 4 段式信封（小程序 wx.request 直接可用）。

    ⚠️ 此处必须是**同步 def**，不能改成 async def。
    ``orchestrator.run_turn`` 是同步阻塞实现（内部要联网检索 + 调用模型），
    若在 async def 里直接调用，会**占住整个事件循环**：实测表现为
    「一旦有人提问，同一进程内的 /health、静态页、其他人的请求全部无响应」，
    直到本轮回答产出为止（联网慢时可达数十秒）。
    写成同步 def 后，FastAPI 会自动把它放入线程池执行，事件循环始终可用。
    这与 /chat/stream 的做法一致——后者也用 run_in_executor 把 run_turn 移出事件循环。
    """
    # 1) 输入护栏（左侧可硬编码）：清洗 + 空/超长/危险协议
    text = guard.sanitize_input(req.message)
    ok, code, hint = guard.validate_message(text)
    if not ok:
        return make_error_envelope(
            req.session_id or "(未生成)", code or "INVALID_INPUT", hint or "输入不合法。",
            mode=_live_mode(),
        )

    # 2) 网关级限流
    key = guard.client_key(request, req.session_id)
    if not guard.rate_limiter.allow(key):
        return make_error_envelope(
            req.session_id or "(未生成)", "RATE_LIMIT", "请求过于频繁，请稍后再试。",
            mode=_live_mode(),
        )

    # 3) 编排（真实循环：模型当控制器 + 真联网检索 + 4 段式 + 护栏打回）
    sid = req.session_id or session.session_store.new_id()
    # 会话登记：请求一到就建立上下文容器（即便未配密钥/失败也能被 /reset 清理）
    session.session_store.get_or_create(sid)
    resp = orchestrator.run_turn(text, sid)
    resp.session_id = sid
    return resp


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_once(envelope: ResponseEnvelope) -> StreamingResponse:
    """把（输入护栏 / 限流的）失败信封包成同样的 SSE 流，前端不必特判。"""

    async def gen():
        yield _sse({"type": "session", "session_id": envelope.session_id})
        yield _sse({"type": "final", "envelope": envelope.model_dump()})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """流式进度版 /chat（SSE）：先推阶段事件，最后推完整信封。

    事件序列示例：session → analyze → search(calling) → search(ok/empty,hit_count)
    → generate → guardrail → done/final。前端据此展示「正在联网检索…命中 N 条…」，
    让使用者**看得见**过程（DoD：检索中状态可见）。小程序等不便用 SSE 的端仍用 /chat。
    """
    text = guard.sanitize_input(req.message)
    ok, code, hint = guard.validate_message(text)
    if not ok:
        return _sse_once(
            make_error_envelope(
                req.session_id or "(未生成)", code or "INVALID_INPUT", hint or "输入不合法。",
                mode=_live_mode(),
            )
        )

    key = guard.client_key(request, req.session_id)
    if not guard.rate_limiter.allow(key):
        return _sse_once(
            make_error_envelope(
                req.session_id or "(未生成)", "RATE_LIMIT", "请求过于频繁，请稍后再试。",
                mode=_live_mode(),
            )
        )

    sid = req.session_id or session.session_store.new_id()
    session.session_store.get_or_create(sid)

    queue: "asyncio.Queue[dict]" = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(payload: dict) -> None:
        """编排线程 → 事件循环的安全投递。"""
        loop.call_soon_threadsafe(queue.put_nowait, dict(payload))

    def worker() -> None:
        try:
            env = orchestrator.run_turn(text, sid, emit)
            loop.call_soon_threadsafe(queue.put_nowait, {"__final__": env.model_dump()})
        except Exception as e:  # noqa: BLE001 - 已在上层兜底，这里只保证流能收尾
            loop.call_soon_threadsafe(
                queue.put_nowait, {"__error__": f"{type(e).__name__}: {e}"}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, {"__end__": True})

    loop.run_in_executor(None, worker)

    async def gen():
        yield _sse({"type": "session", "session_id": sid})
        while True:
            ev = await queue.get()
            if ev.get("__end__"):
                break
            if "__final__" in ev:
                yield _sse({"type": "final", "envelope": ev["__final__"]})
                continue
            if "__error__" in ev:
                yield _sse({"type": "error", "message": ev["__error__"]})
                continue
            yield _sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/reset")
async def reset(req: ResetRequest, request: Request) -> dict:
    """重置会话（清空该 session 的服务端上下文）。

    与会话隔离配套：前端「重置会话」会换新 session_id 并调用本接口，
    避免旧上下文在服务端无限累积（内存版会话的清理入口）。
    敏感端点：OPC_API_TOKEN 设置后需带令牌（纵深防御）。
    """
    if not guard.api_token_ok(request):
        raise HTTPException(status_code=403, detail="缺少有效的 API 令牌，操作被拒绝。")
    existed = session.session_store.reset(req.session_id) if req.session_id else False
    return {"ok": True, "reset": existed}


@app.get("/history")
async def history(request: Request) -> list:
    """进阶3：历史会话清单（前端「历史记录」入口）。

    返回 list_sessions() 的轻量元信息（不泄露完整对话内容）；
    空存储时返回 []，前端据此展示「暂无历史会话」。
    敏感端点：OPC_API_TOKEN 设置后需带令牌（纵深防御）。
    """
    if not guard.api_token_ok(request):
        raise HTTPException(status_code=403, detail="缺少有效的 API 令牌，操作被拒绝。")
    return session.list_sessions()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "api_version": API_VERSION,
        "demo_city": config.DEMO_CITY,
        "key_configured": keystore.is_configured(),
        "llm": llm_client.connection_info(),
        "search_backends": config.SEARCH_BACKENDS,
        "active_sessions": session.session_store.count(),
        "rate_limit_per_min": config.RATE_LIMIT_PER_MIN,
        "max_message_len": config.MAX_MESSAGE_LEN,
        "guardrail_max_reflect": config.GUARDRAIL_MAX_REFLECT,
        "key_store": keystore.store_path(),
        "usage": cost_gate.stats(),
    }


@app.get("/")
async def index():
    """交互界面入口。"""
    html = _STATIC_DIR / "index.html"
    if html.exists():
        return FileResponse(str(html), media_type="text/html")
    return {"message": "前端未构建，请访问 /health 与 /chat。"}


# 挂载静态资源（js/css 等，若有）
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.server:app", host=config.HOST, port=config.PORT, reload=False)
