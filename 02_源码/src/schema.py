# -*- coding: utf-8 -*-
"""输出契约 schema（Step 1，已与编排循环 / 前端对齐为扁平 4 段式）。

红线（见编排层第3节）：本文件只定义"结构形状"与"枚举值"，
绝不写死任何医院名/科室名/医生名/排班事实/预设对话。
事实全部由模型从真实检索来源产出。
"""

from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

# ---- 枚举值（结构契约，非事实）----
SourceType = Literal["权威", "补充"]
InfoStatus = Literal[
    "历史上有相关报道",
    "公开页面介绍具备相关能力",
    "目前是否可提供尚待核实",
]


class Source(BaseModel):
    """来源（每条事实的凭证）。"""

    title: str = Field(..., description="来源标题")
    url: str = Field("", description="来源链接（合法 http(s) 优先；无则留空并标注）")
    source_type: SourceType = Field("补充", description="来源性质：权威|补充")
    updated_note: Optional[str] = Field(
        None, description="来源更新时间说明（如'来源未标注更新时间'）；无则模型填说明"
    )


class QueryCondition(BaseModel):
    """查询条件（4 段式第①段）。"""

    region: Optional[str] = Field(None, description="地区")
    hospital: Optional[str] = Field(None, description="医院名称")
    campus: Optional[str] = Field(None, description="院区（用户指明时填写）")
    department: Optional[str] = Field(None, description="科室")
    resource: Optional[str] = Field(None, description="资源（如抗蛇毒血清）")
    title: Optional[str] = Field(None, description="标题/主题")
    date: Optional[str] = Field(None, description="查询日期")


class QueryResult(BaseModel):
    """单条查询结果（4 段式第②段的一项）。"""

    name: str = Field(..., description="名称（医院/科室/医生/资源）")
    type: str = Field("其他", description="类型：医院/科室/医生/资源/其他")
    campus: Optional[str] = Field(
        None,
        description="适用院区。医院有多个院区时必须标明本条属哪个院区"
        "（不同院区的地址/科室/医生/出诊排班不得混用）；"
        "来源未指明院区时写「来源未标明院区」，不得自行推定",
    )
    description: str = Field("", description="说明（忠实于来源，不编造）")
    info_status: Optional[InfoStatus] = Field(
        None, description="信息状态三态（特殊易变资源强制标注）"
    )
    source: Source = Field(..., description="来源（必填）")


class InfoBasis(BaseModel):
    """信息依据（4 段式第③段的一项）。"""

    title: str = Field(..., description="依据标题")
    url: str = Field("", description="依据链接")
    note: Optional[str] = Field(None, description="说明")
    source_type: SourceType = Field("补充", description="来源性质：权威|补充")


class OutputContract(BaseModel):
    """4 段式输出契约（代码定结构，模型填内容）。"""

    query_condition: Optional[QueryCondition] = Field(None, description="①查询条件")
    query_results: List[QueryResult] = Field(default_factory=list, description="②查询结果")
    info_basis: List[InfoBasis] = Field(default_factory=list, description="③信息依据")
    usage_tips: List[str] = Field(default_factory=list, description="④使用提示")
    # 澄清声明：模型自决「是否需要向用户澄清缺条件」。代码读此字段，不猜措辞。
    need_clarification: bool = Field(
        False,
        description="是否需要向用户澄清（缺关键条件）。模型自决；默认 false。",
    )
    clarify_for: Optional[str] = Field(
        None,
        description="need_clarification=true 时，写明缺失的是哪一类条件（如『科室』『医院』）；"
        "用于前端展示与护栏判断。注意：地区/城市**不是**缺条件（演示范围固定，见 SYSTEM_PROMPT）。",
    )


class RetrievalLogEntry(BaseModel):
    """检索过程留痕（独立展示，不混入答案；评审核查点）。"""

    action: str = Field(..., description="动作：search（联网检索）/ guardrail（护栏判定）")
    query: str = Field("", description="检索词")
    city: Optional[str] = Field(None, description="城市")
    status: str = Field(..., description="状态：search 用 calling/ok/empty；guardrail 用 degraded/reject")
    hit_count: Optional[int] = Field(None, description="命中条数")
    urls: List[str] = Field(default_factory=list, description="本轮该次检索命中并返回的真实来源 URL 列表（供 R13 来源可追溯校验；未记录则跳过校验）")
    timestamp: str = Field("", description="时间戳")


class ResponseEnvelope(BaseModel):
    """统一响应包装（接入层出口结构）。"""

    ok: bool = Field(True, description="是否成功（失败仍 HTTP 200，前端看此字段）")
    session_id: str = Field(..., description="会话标识（会话隔离维度）")
    error_code: Optional[str] = Field(None, description="失败码（ok=False 时有值）")
    output: OutputContract = Field(..., description="4 段式输出契约")
    retrieval_log: List[RetrievalLogEntry] = Field(default_factory=list, description="检索过程留痕")
    reflection_count: int = Field(0, description="反射式打回轮数（编排层实际写入）")
    degraded: bool = Field(False, description="是否降级输出（护栏超限/检索失败）")
    mode: str = Field("live", description="运行模式：skeleton(骨架) / agent(真模型+搜索) / need_key(未配密钥)")
    note: Optional[str] = Field(None, description="附加说明（如降级原因）")


def make_error_envelope(
    session_id: str,
    error_code: str,
    hint: str,
    mode: str = "skeleton",
    degraded: bool = True,
) -> ResponseEnvelope:
    """构造「失败信封」的唯一口径。

    为什么要有它：失败响应也必须带完整的 4 段式 ``output`` 包裹层——
    前端与小程序统一读 ``output.*``；若失败时返回扁平 dict（无 output），
    前端取 `env.output` 会得到 undefined 而渲染空白，属真实缺陷。
    因此所有失败路径（护栏拒答 / 限流 / 异常兜底 / 未配密钥）都走这里。
    """
    return ResponseEnvelope(
        ok=False,
        session_id=session_id,
        error_code=error_code,
        output=OutputContract(
            query_condition=None,
            query_results=[],
            info_basis=[],
            usage_tips=[hint],
        ),
        retrieval_log=[],
        reflection_count=0,
        degraded=degraded,
        mode=mode,
        note=error_code,
    )
