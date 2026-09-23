"""密钥相关路由：预设列表、自动扫描状态、添加并校验连接。

这即是使用者「无需翻找文件即可添加密钥」的入口——
- GET  /api/providers   预设厂商下拉清单
- GET  /api/keys/status  后端自动扫描外部密钥库的结果（连没连上）
- POST /api/keys/add     选厂商 + 粘贴密钥 → 后端真连一次验证 → 写入外部库
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import guard, keystore, llm_client, providers

router = APIRouter(prefix="/api/keys", tags=["keys"])


class AddKeyRequest(BaseModel):
    provider_id: str
    api_key: str
    model: str = ""


@router.get("/providers")
def api_providers():
    """给「添加密钥」界面的下拉清单（不含任何密钥值）。"""
    return {"providers": providers.provider_list()}


@router.get("/status")
def api_status():
    """后端自动扫描外部密钥库的结果。"""
    active = keystore.get_active()
    if active:
        preset = providers.get_provider(active["id"])
        return {
            "configured": True,
            "provider_id": active["id"],
            "vendor": (preset or {}).get("vendor", active["id"]),
            "model": active["model"] or (preset or {}).get("default_model", ""),
            "store_path": keystore.store_path(),
            # 措辞纪律（交付前运行时审计）：此处只做「存在性校验」，不代表厂商侧
            # 当前仍有效——密钥可能在添加后被厂商作废/欠费（实测发生过）。
            # 失效时对话链路会以结构化错误如实上报，不在这里虚报「已连接」。
            "message": "已检测到密钥（存在性校验通过；添加时验证过连接，之后不保证厂商侧仍有效，失效时对话会明确报错）。",
        }
    return {
        "configured": False,
        "provider_id": None,
        "vendor": None,
        "model": "",
        "store_path": keystore.store_path(),
        "message": "未扫描到密钥。请点「添加密钥」选择厂商并粘贴。",
    }


@router.post("/add")
def api_add(req: AddKeyRequest, request: Request):
    """选厂商 + 粘贴密钥 → 真连一次验证 → 写入外部密钥库。

    敏感端点（写入外部密钥库）：OPC_API_TOKEN 设置后需带令牌（纵深防御）。
    """
    if not guard.api_token_ok(request):
        raise HTTPException(status_code=403, detail="缺少有效的 API 令牌，操作被拒绝。")
    preset = providers.get_provider(req.provider_id)
    if not preset:
        raise HTTPException(status_code=400, detail=f"未知供应商：{req.provider_id}")
    api_key = (req.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="密钥不能为空。")
    model = (req.model or "").strip() or preset.get("default_model", "")
    base_url = preset.get("base_url", "https://api.openai.com/v1")

    ok, msg = llm_client.test_connection(api_key, base_url, model)
    if not ok:
        # 连接失败：不写入，明确告知使用者原因
        raise HTTPException(status_code=400, detail=msg)

    keystore.upsert_provider(req.provider_id, api_key, model)
    return {
        "ok": True,
        "provider_id": req.provider_id,
        "vendor": preset.get("vendor", req.provider_id),
        "model": model,
        "message": f"已连接并保存：{preset.get('vendor', req.provider_id)} / {model}",
    }
