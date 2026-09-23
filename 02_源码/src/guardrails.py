"""护栏层（M4）：Validator / Reflector 的「左侧」实现。

设计依据：`01_需求与方案/编排层_2026-09-21.md` §2.4（反射式校验打回循环）。

🔴 本模块的硬边界（绝不能越）：
    只校验 **结构 / 契约 / 边界触发词**，**绝不判断事实真假**
    （如"昆医附一是否真有心血管内科"）——事实真假只能靠来源链接 + 人工/评委核验。
    越过这条 = 代码越权判断事实，会架空模型的判断空间（严禁）。

职责分工：
- `validate_output()`（Validator）：输入模型产出 + 检索留痕 + 用户原话，
  输出 `ValidationResult{passed, violations, hints}`。纯函数、无副作用、不联网（R3 除外且默认关）。
- `build_reflection_prompt()`（Reflector 的"话术生成"部分）：把违规点编码成结构化
  反思指令，由编排层回灌给模型。**代码只说"哪里违规"，改由模型自己改**——
  这正是本模块的设计要点：代码只指出违规点，由模型自行修正答案。

规则集（全部「左侧」，代码可写）：
    R1 结构完备   R2 来源必填   R3 来源可达(可选)   R4 边界拒答
    R5 紧急优先   R6 禁断言     R7 空字段诚实       R8 检索真实性
    R9 易变资源三态标注（防"把历史报道当现在确定可提供"）
    R10 院区纪律（防"不同院区的地址/科室/医生/排班混用"）
    R11 同名医院/院区消歧一致性（防"多后端返回同名多家却漏标/混用院区"）
    R12 多来源冲突主动提示（防"同一事实不同来源说法不一却静默合并"）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from . import config
from .schema import OutputContract, RetrievalLogEntry

# ─────────────────────────── 词表（边界触发词，非事实） ───────────────────────────
# 说明：这些是「边界触发词」——用来判断"该不该触发某条边界规则"，
# 不含任何医院/科室/医生/资源结论（符合编排层 §3「左侧可写」表）。

# R5 紧急词：命中则产出必须含 120 提示（以急救指引优先于继续检索）
EMERGENCY_WORDS: tuple[str, ...] = (
    "胸痛", "胸闷剧痛", "心梗", "心肌梗死", "昏迷", "休克", "大出血", "大量出血",
    "吐血", "咯血", "便血", "呼吸困难", "窒息", "抽搐", "无意识", "意识不清",
    "中毒", "服毒", "农药", "煤气", "一氧化碳", "自杀", "轻生", "不想活", "活不下去",
    "猝死", "溺水", "触电", "坠落", "脑梗", "脑出血", "中风", "偏瘫", "呼吸停止",
    "心脏骤停", "昏倒", "晕厥", "过敏休克", "大咯血",
)

# R4 诊断/用药意图：命中则产出必须体现"不诊断/不处方 + 引导就医"
DIAGNOSIS_INTENT_WORDS: tuple[str, ...] = (
    "吃什么药", "吃啥药", "用什么药", "开点药", "开药", "配药", "怎么治", "如何治",
    "治疗方案", "能治好吗", "能不能治好", "是不是得了", "我得了", "我这是什么病",
    "什么病", "诊断一下", "帮我诊断", "处方", "用药建议", "剂量", "吃多少", "该吃",
    "要不要吃", "能不能吃",
)

# R4 拒答标志：诊断/用药意图下，产出须至少含其一（证明"拒答+引导"而非"给建议"）
# 拆成两组：
#   BOUNDARY_REFUSAL_MARKERS —— 明确声明"不诊断/不用药"（R7 的豁免依据之一）
#   GUIDANCE_MARKERS         —— 引导线下就医/急救
BOUNDARY_REFUSAL_MARKERS: tuple[str, ...] = (
    "不提供诊断", "不能诊断", "不做诊断", "无法诊断", "不提供用药", "不能提供用药",
    "不提供处方", "无法提供用药建议", "不提供具体用药", "不给予用药建议",
    "不提供治疗方案", "不作为诊断依据",
)
GUIDANCE_MARKERS: tuple[str, ...] = (
    "请及时就医", "请尽快就医", "请立即就医", "请到医院", "请线下就诊", "请前往医院",
    "请咨询医生", "请咨询专业医生", "由医生判断", "遵医嘱", "就医", "120",
)
REFUSAL_MARKERS: tuple[str, ...] = BOUNDARY_REFUSAL_MARKERS + GUIDANCE_MARKERS

# R4 越界用药话术：出现即判违规（代码只识别"话术模式"，不判断药名是否对症）
MEDICATION_ADVICE_RE = re.compile(
    r"(建议|可以|应当|应该|推荐|不妨|最好)\s*(服用|口服|吃|使用|注射|输液|买)"
)
DOSAGE_RE = re.compile(r"\d+\s*(mg|毫克|ml|毫升|片|粒|袋|次/日|mg/日|μg|微克)")

# R6 禁断言：无检索支撑的绝对化表述（注意：与三态枚举「目前是否可提供尚待核实」不冲突，
# 因为该枚举串不含子串「目前可提供」）
FORBIDDEN_ASSERTIONS: tuple[str, ...] = (
    "目前可提供", "现有库存", "库存充足", "保证有", "一定有号", "随时可预约",
    "可预约成功", "确定出诊", "名额充足", "包治", "疗效保证", "绝对安全",
    "百分百", "百分之百", "最权威", "排名第一", "全市最好", "一定能治好",
    "可立即使用", "当天就能做",
)

# R7 诚实标志：无结果/无来源时，提示里须出现其一（证明"如实告知"而非"留空/编造"）
HONESTY_MARKERS: tuple[str, ...] = (
    "未查到", "暂未查到", "未能查到", "待核实", "尚待核实", "无法核验", "未能核验",
    "以官方渠道为准", "请以官方", "无法确认", "建议直接联系",
)

# R12 冲突提示标志：同一事实多来源说法不一，产出须体现其一（证明"已主动标注冲突，而非静默合并"）
# ⚠️ 关键收窄（生产级修正）：**不含**「以官方渠道为准 / 以官方为准」这类 R7 诚实通用收尾语——
# 模型几乎每条医疗答案都带它们作收尾，若纳入则 R12 被轻松"蒙混"通过、A3 形同虚设。
# 只保留**真正表示冲突**的词，使 R12 仅在模型确实写出冲突陈述时才算"已标注"。
CONFLICT_MARKERS: tuple[str, ...] = (
    "冲突", "不一致", "存在差异", "说法不一", "各有不同", "来源间",
    "请以来源", "相互矛盾",
)

# R8 检索真实性：模型声称"已检索"的字样（用于与 retrieval_log 对账）
CLAIM_SEARCH_RE = re.compile(r"(已检索|已联网检索|联网检索到|检索到|搜索到|已查询)")

# 医疗资源域关键词：判断「这句话是否该触发检索」。
# 用途：闲聊/寒暄不该被强制成"暂未查到"（否则对"你好"回一句"未检索到"很怪）；
# 而域内提问一旦检索为空，就必须走诚实降级。属「左侧」域判定，不含任何事实。
DOMAIN_KEYWORDS: tuple[str, ...] = (
    "医院", "院区", "分院", "门诊", "急诊", "住院", "病房", "床位", "病床",
    "科室", "医生", "医师", "专家", "主任", "副主任", "挂号", "出诊", "排班",
    "就诊", "就医", "看病", "转诊", "会诊", "手术", "体检", "疫苗", "透析",
    "检验", "化验", "影像", "超声", "核磁", "磁共振", "CT", "X光", "病理",
    "资源", "血清", "抗蛇毒", "血库", "备血", "血液", "库存", "药品", "药房",
    "三甲", "二甲", "三级", "专科", "卫健委", "卫健", "医保", "预约", "急救",
    "120", "救护车", "ICU", "重症",
)

# R9 易变资源词：命中则要求每条结果标注「信息状态三态」。
# 理由：这类资源随时间变化，公开信息未必即时准确；不标三态会让人把
# 「历史上相关报道」误当「现在确定可提供」——正是本题最容易踩的真实性坑。
VOLATILE_RESOURCE_WORDS: tuple[str, ...] = (
    "血清", "抗蛇毒", "抗毒蛇", "血库", "备血", "血浆", "血小板",
    "库存", "存货", "号源", "床位", "病床", "呼吸机", "疫苗", "透析", "ICU",
)

# R10 院区词：命中则要求每条结果都标明「适用院区」。
# 依据：赛题《基础需求2》原文「医院结果至少包含：医院全称、所在地区及院区…；
#   **不同院区的地址、科室和医生安排不得混用**」，《基础需求3》两处要求标注院区
#   （科室信息含「医院及院区」、出诊信息标「适用院区」）。二者同属"基础验收内容"。
# ⚠️ 本词表**只放通用词**，不放任何具体院区名（如"呈贡""西昌路"）——
#   具体院区名属于"医院事实"，代码一律不得硬编码（本项目铁律）。
#   通用词命中只用来说明"用户确实在问院区层面的事"，据此触发结构校验，不判断事实。
CAMPUS_WORDS: tuple[str, ...] = (
    "院区", "分院", "本部", "总院", "东院", "西院", "南院", "北院",
    "东区", "西区", "南区", "北区",
)

_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

# 规则的"人话"提醒（回灌给模型的修正要求）
RULE_HINTS: dict[str, str] = {
    "R1": "4 段式结构必须完整：query_condition 不可为 null，usage_tips 不可为空。",
    "R2": "每条 query_results 必须附 source，且 source.url 必须是合法 http(s) 链接；"
          "若确实拿不到可核验链接，请把该条从结果中移除，并在 usage_tips 说明「暂未查到可核验的来源」。",
    "R3": "来源链接不可访问，请换用可访问的官方来源，或标注「该来源当前无法访问，待核实」。",
    "R4": "用户涉及诊断/用药，不得给出诊断结论或用药建议；须明确说明不提供诊断与用药指导，并引导线下就医。",
    "R5": "用户描述可能属紧急情况，必须在 usage_tips 中明确提示「请立即拨打 120 或前往急诊」，"
          "不得以继续检索替代急救指引。",
    "R6": "禁止出现无检索支撑的绝对化断言（如「目前可提供」「可预约成功」「确定出诊」）。"
          "改为「暂未查到可核验的…，请以官方渠道为准」，或标注信息状态为「目前是否可提供尚待核实」。",
    "R7": "没有可核验结果时，query_results / info_basis 应为空数组，并在 usage_tips 中写明"
          "「暂未查到可核验的联网信息，请以官方渠道为准」；不得留空、不得编造。",
    "R8": "你声称有检索结果，但本轮检索留痕为空或未命中——严禁用训练记忆冒充联网检索结果。"
          "若本轮确实未检索到，请返回空结果并如实说明。",
    "R9": "本题涉及随时间变化的易变资源（如抗蛇毒血清/血库/床位/号源）。每条 query_results 必须标注 "
          "info_status 三态之一，并区分清楚「历史上有相关报道」「公开页面介绍具备相关能力」"
          "「目前是否可提供尚待核实」；不得让读者误以为当前确定可提供。",
    "R10": "本题涉及医院院区。每条 query_results 必须标明**适用院区**"
           "（campus 字段，或在条目名称/说明中写清是哪个院区）；"
           "不同院区的地址、科室、医生与出诊排班**不得混用、不得合并成一条**；"
           "来源未指明院区时，请写「来源未标明院区」，不得自行推定到某个院区。",
    "R11": "检测到多条结果医院名称相同（可能为同一医院的不同院区、或不同主体的同名医院，"
           "多后端各返回同一医院不同院区是常见真实场景）。同一同名组内的结果必须**逐条标注适用院区**"
           "（campus 字段，或写「来源未标明院区」），不得漏标、不得把不同院区的地址/科室混用或合并成一条；"
           "若同组已有结果标注了院区、本条却未标，即为漏标/混用风险。",
    "R12": "同一事实在不同来源说法不一致（如某资源是否可提供、更新时间、出诊安排相互矛盾）。"
           "当同名结果对同一事实给出了不同的信息状态时，须主动标注「来源间存在冲突，以官方渠道为准」"
           "（或类似冲突提示语），不得静默合并两边、也不得只取其中之一当作确定结论。",
}


@dataclass
class Violation:
    """单条违规（rule 如 "R2"，where 如 "query_results[1].source.url"）。"""

    rule: str
    where: str
    detail: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "where": self.where, "detail": self.detail}


@dataclass
class ValidationResult:
    """校验结果。``passed`` 为 True 时 violations/hints 为空。"""

    passed: bool
    violations: list[Violation] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def rules(self) -> list[str]:
        """违规规则清单（去重保序），供留痕。"""
        seen: list[str] = []
        for v in self.violations:
            if v.rule not in seen:
                seen.append(v.rule)
        return seen

    def summary(self) -> str:
        """形如 ``R2×2,R6``，写入 retrieval_log 的一行摘要。"""
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.rule] = counts.get(v.rule, 0) + 1
        return ",".join(f"{r}×{c}" if c > 1 else r for r, c in counts.items())


# 闲聊/寒暄标志词：用于「明确非域内」的**反向判定（黑名单）**。
# 见下方 needs_retrieval 的判据说明。
CHITCHAT_KEYWORDS: tuple[str, ...] = (
    "你好", "您好", "哈喽", "hello", "hi ", "在吗", "谢谢", "感谢", "多谢",
    "再见", "拜拜", "辛苦了", "早上好", "中午好", "晚上好", "晚安",
    "你是谁", "你叫什么", "你的名字", "介绍一下你", "自我介绍", "简单介绍",
    "你能做什么", "你会什么", "你有什么功能", "怎么用你",
)

# 「定位型提问」信号：用户在问"哪里/哪家/有没有/多少钱"之类可核验的事实。
# 用途：区分「该真检索的问题」与「只是在聊天的陈述句」，避免对泛泛提问强制检索。
LOCATING_SIGNALS: tuple[str, ...] = (
    "哪家", "哪个", "哪里", "哪儿", "哪边", "哪些", "在哪", "有哪些",
    "有没有", "有吗", "是否有", "能不能", "可不可以", "怎么走", "怎么去",
    "多少钱", "多少", "几家", "推荐", "挂号", "预约", "排队", "几点",
)


def needs_retrieval(message: str) -> bool:
    """这句提问是否该走「医疗资源域」路径（提供检索工具 + 域内专用指令）。

    ★ 判据设计（重要，经历过一次真实翻修）：
      旧做法 = **白名单命中**：必须命中 DOMAIN_KEYWORDS 才算域内。
        实测被绕过（对抗测试第二轮 B2/B3）：用口语「打那个解药」或错别字
        「医园/抗蛇读/血请」发言时，关键词一个都不命中 → 被判为「闲聊」→
        **连带不提供检索工具** → 模型可能凭记忆作答（红线风险）。
      新做法 = **黑名单排除**：只有「明确是闲聊寒暄」才判为非域内，其余一律倾向检索。
        理由：漏检的代价（该查却没查 → 可能编造）远大于多查一次的代价。

    属「左侧」域判定：只看提问的形态，不含任何事实、不判断用户对错。
    """
    m = (message or "").strip()
    if not m:
        return False
    if any(w in m for w in DOMAIN_KEYWORDS):
        return True                      # 明确域内
    if len(m) <= 20 and any(w in m for w in CHITCHAT_KEYWORDS):
        return False                     # 明确闲聊（短句 + 寒暄标志）
    return True                          # 其余倾向检索（宁可多查，不可漏查）


def must_retrieve(message: str) -> bool:
    """代码是否必须为这句提问兜底做一次真实检索（即使模型没主动调工具）。

    与 needs_retrieval 的分工：
      needs_retrieval = 要不要给它检索工具（宽松，默认给）；
      must_retrieve   = 代码要不要**强制**补一次检索（较严，要有"可核验提问"的证据）。
    这样才能既堵住"绕开关键词就不检索"，又不会对"帮我写首诗"这类
    泛泛提问强塞一次无关检索。
    """
    m = (message or "").strip()
    if not needs_retrieval(m):
        return False
    if any(w in m for w in DOMAIN_KEYWORDS):
        return True                      # 命中域关键词
    if any(w in m for w in LOCATING_SIGNALS):
        return True                      # 是"在哪/哪家/有没有"型可核验提问
    return len(m) >= 12                  # 较长且非闲聊的提问，倾向兜底检索


def _joined_text(output: OutputContract) -> str:
    """把 4 段式里所有「给人看的文字」拼起来（用于边界词检查，不判断事实真假）。"""
    parts: list[str] = []
    qc = output.query_condition
    if qc is not None:
        parts += [x for x in (qc.region, qc.hospital, qc.department, qc.resource, qc.title, qc.date) if x]
    for r in output.query_results:
        parts += [r.name or "", r.description or "", r.type or ""]
        parts += [r.source.title or "", r.source.url or ""]
        if r.info_status:
            parts.append(r.info_status)
    for b in output.info_basis:
        parts += [b.title or "", b.note or "", b.url or ""]
    parts += list(output.usage_tips or [])
    return "\n".join(parts)


def _probe_url(url: str, timeout: float = 5.0) -> bool:
    """R3：轻量探测 url 可达性。任何异常一律视为"未知"，返回 True（不误伤）。"""
    try:
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as c:
            r = c.head(url)
            if r.status_code >= 400:  # 有些站点不支持 HEAD，退回 GET
                r = c.get(url)
            return r.status_code < 400
    except Exception:  # noqa: BLE001 - 探测失败不构成违规证据
        return True


def validate_output(
    output: OutputContract,
    retrieval_log: Sequence[RetrievalLogEntry],
    user_message: str,
    clarify: bool = False,
) -> ValidationResult:
    """Validator：纯左侧校验。只判「结构/契约/边界词」，绝不判事实真假。

    ``clarify``：本轮是否为**条件澄清轮**（用户提问缺关键条件、系统正在询问用户，
    例：「帮我找有卒中中心的医院」→ 先问「请问您要查哪个城市或地区？」）。
    澄清轮本就没有结果可查、条件本身就是缺的，故 **R1（须给条件）与 R7（空结果须标注
    "暂未查到"）在此轮豁免**——否则会把一句正常的询问连环打回并降级（实测复现：
    R7 打回 → R1 打回 → 超限降级，最终用户看到的是"输出未满足结构性约束"，
    而真正该说的"请问您要查哪个城市"反而没说出来）。

    ⚠️ 豁免的**只是**「允许没有条件 / 没有结果」，**不豁免任何编造**：
    澄清轮若塞了 query_results，仍会被 R8（检索真实性对账）打回。
    """
    violations: list[Violation] = []
    text = _joined_text(output)
    msg = user_message or ""

    # ── R1 结构完备 ──
    # 注：只有「确实需要检索的提问」（must_retrieve）才要求 query_condition；
    # 闲聊/寒暄、以及泛泛提问本就没有查询条件，强制要求会逼模型硬填
    # （实测会出现"你好"被填成地区的怪象）。澄清轮同样豁免：条件本身还没齐，
    # 此时要求"必须给出地区"会与"缺失条件应先询问"直接冲突。
    if output.query_condition is None and must_retrieve(user_message) and not clarify:
        violations.append(Violation("R1", "query_condition", "查询条件缺失（null），须给出地区/主题等条件"))
    if not output.usage_tips:
        violations.append(Violation("R1", "usage_tips", "使用提示为空，须至少给出一条说明或安全提示"))
    if output.query_condition is None and not output.query_results and not output.info_basis and not output.usage_tips:
        violations.append(Violation("R1", "output", "输出为空壳，4 段式无任何内容"))

    # ── R2 来源必填（逐条） ──
    for i, r in enumerate(output.query_results):
        url = (r.source.url or "").strip() if r.source else ""
        title = (r.source.title or "").strip() if r.source else ""
        if not url or not _HTTP_RE.match(url):
            violations.append(
                Violation("R2", f"query_results[{i}].source.url", f"第{i + 1}条结果缺少合法 http(s) 来源链接")
            )
        elif not title:
            violations.append(
                Violation("R2", f"query_results[{i}].source.title", f"第{i + 1}条结果来源缺少标题")
            )

    # ── R3 来源可达（可选，默认关；联网探测，避免误判） ──
    if config.GUARDRAIL_VERIFY_URLS:
        for i, r in enumerate(output.query_results):
            url = (r.source.url or "").strip() if r.source else ""
            if url and _HTTP_RE.match(url) and not _probe_url(url):
                violations.append(
                    Violation("R3", f"query_results[{i}].source.url", f"第{i + 1}条结果来源链接不可访问：{url}")
                )

    # ── R4 边界拒答（用户问诊断/用药） ──
    if any(w in msg for w in DIAGNOSIS_INTENT_WORDS):
        has_refusal = any(m in text for m in REFUSAL_MARKERS)
        if not has_refusal:
            violations.append(
                Violation("R4", "usage_tips", "用户涉及诊断/用药，但产出未体现「不提供诊断/用药指导 + 引导就医」")
            )
        if MEDICATION_ADVICE_RE.search(text) or DOSAGE_RE.search(text):
            violations.append(
                Violation("R4", "query_results/usage_tips", "产出出现用药建议或剂量表述，越界（不诊断、不用药）")
            )

    # ── R5 紧急优先（用户描述紧急症状） ──
    hit_emergency = next((w for w in EMERGENCY_WORDS if w in msg), None)
    if hit_emergency and "120" not in text:
        violations.append(
            Violation("R5", "usage_tips", f"检测到紧急词「{hit_emergency}」，产出未提示拨打 120")
        )

    # ── R6 禁断言（无检索支撑的绝对化表述） ──
    for phrase in FORBIDDEN_ASSERTIONS:
        if phrase in text:
            violations.append(
                Violation("R6", "output", f"出现无检索支撑的断言「{phrase}」，须改为待核实表述")
            )

    # ── R7 空字段诚实（逐条 + 空结果兜底） ──
    for i, r in enumerate(output.query_results):
        if not (r.name or "").strip():
            violations.append(Violation("R7", f"query_results[{i}].name", f"第{i + 1}条结果名称为空（须补全或移除）"))
        if not (r.description or "").strip():
            violations.append(
                Violation("R7", f"query_results[{i}].description", f"第{i + 1}条结果说明为空（须补全或标注待核实）")
            )
    if not output.query_results and not output.info_basis:
        # 豁免三类"本来就没有『查没查到』可言"的回复：
        #   ① 边界拒答（明确不诊断/不用药）；
        #   ② 紧急急救指引（已让打 120）；
        #   ③ **条件澄清轮**（正在问用户"要查哪个城市"，此时要求它写「暂未查到」
        #      既答非所问、又会让澄清被连环打回降级——实测踩过）。
        boundary_reply = any(m in text for m in BOUNDARY_REFUSAL_MARKERS) or ("120" in text)
        if must_retrieve(user_message) and not boundary_reply and not clarify:
            if not any(m in text for m in HONESTY_MARKERS):
                violations.append(
                    Violation("R7", "usage_tips", "无可核验结果，但提示中未如实标注「暂未查到/待核实」")
                )

    # ── R8 检索真实性（声称有结果 vs 检索留痕对账） ──
    claims = bool(output.query_results or output.info_basis)
    real_hits = any(e.action == "search" and (e.hit_count or 0) > 0 for e in retrieval_log)
    if claims and not real_hits:
        violations.append(
            Violation("R8", "retrieval_log", "产出含检索结果/来源，但本轮检索留痕为空或零命中（涉嫌以记忆冒充联网检索）")
        )
    elif CLAIM_SEARCH_RE.search(text) and not real_hits:
        violations.append(
            Violation("R8", "usage_tips", "产出自称「已检索」，但检索留痕无命中，两者不一致")
        )

    # ── R9 易变资源三态标注（防"把历史报道当现在可提供"） ──
    # 只在用户确实问到易变资源时触发；要求每条结果都标 info_status 三态。
    if any(w in msg for w in VOLATILE_RESOURCE_WORDS):
        for i, r in enumerate(output.query_results):
            if not r.info_status:
                violations.append(
                    Violation(
                        "R9",
                        f"query_results[{i}].info_status",
                        f"第{i + 1}条涉及易变资源却未标注信息状态三态"
                        "（历史上有相关报道 / 公开页面介绍具备相关能力 / 目前是否可提供尚待核实）",
                    )
                )

    # ── R10 院区纪律（用户提到院区时，要求逐条标明"适用院区"） ──
    # 依据赛题基础需求2「不同院区的地址、科室和医生安排不得混用」与基础需求3「标明适用院区」。
    # 与 R9 同构：只在用户确实在问院区层面的事时才触发，避免对不涉及院区的提问误报。
    if any(w in msg for w in CAMPUS_WORDS):
        for i, r in enumerate(output.query_results):
            if not _mentions_campus(r):
                violations.append(
                    Violation(
                        "R10",
                        f"query_results[{i}].campus",
                        f"第{i + 1}条未标明适用院区（用户问到院区；"
                        "不同院区的地址/科室/医生/出诊排班不得混用）",
                    )
                )

    # ── R11 同名医院/院区消歧一致性（进阶需求1：多后端返回同名多家，自动区分院区） ──
    # 判据（结构性、不判事实真假）：把 results 按「归一化 name（去空格+小写）」分组，
    #   同名组 size ≥ 2 时，若组内"有的标了院区、有的没标" → 漏标/混用风险，对未标那条报 R11。
    # 这样设计只为抓「组内不一致」，不误伤：
    #     · 单条结果（无同名组）不触发；
    #     · 同名组全标院区（含诚实写法「来源未标明院区」）→ 一致，不触发；
    #     · 同名组全空（模型一致地未掌握院区）→ 不触发，避免把"单院区多科室"误伤降级。
    _name_groups: dict[str, list[int]] = {}
    for _i, _r in enumerate(output.query_results):
        _n = (getattr(_r, "name", "") or "").strip().lower()
        if _n:
            _name_groups.setdefault(_n, []).append(_i)
    for _n, _idxs in _name_groups.items():
        if len(_idxs) < 2:
            continue
        _filled = [i for i in _idxs if (output.query_results[i].campus or "").strip()]
        if _filled and len(_filled) < len(_idxs):
            for _j in _idxs:
                if _j in _filled:
                    continue
                violations.append(
                    Violation(
                        "R11",
                        f"query_results[{_j}].campus",
                        f"同名医院「{output.query_results[_j].name}」出现 {len(_idxs)} 次，"
                        f"同组其他结果已标注院区、本条（第{_j + 1}条）却未标——"
                        "须逐条标注适用院区、不得漏标或混用",
                    )
                )

    # ── R12 多来源冲突主动提示（进阶需求1 联动 A3：同一事实不同来源说法不一） ──
    # 判据（结构性、不判事实真假）：同名组内若存在 ≥2 条带「不同的非空 info_status」，
    #   即"同一事实（如某资源是否可提供）在不同来源说法不一致"的结构性信号 →
    #   要求产出文本含冲突提示语之一（CONFLICT_MARKERS），否则报 R12。
    # 设计取舍（防误伤，生产级稳定）：
    #     · 仅在「同名 + info_status 实际发散」时触发——这是可结构化的真实冲突信号
    #       （易变资源如抗蛇毒血清，不同来源常对"是否可提供"说法不一）；
    #     · 同名组 info_status 一致 / 不同名 / 无 info_status → 不触发，避免对普通答案误报；
    #     · 只检查"是否主动标注冲突"，不比较来源正文的事实真假（事实真假不归代码判）。
    for _n, _idxs in _name_groups.items():
        if len(_idxs) < 2:
            continue
        _statuses = {
            (output.query_results[i].info_status or "").strip()
            for i in _idxs
        }
        _statuses.discard("")
        if len(_statuses) >= 2:
            if not any(m in text for m in CONFLICT_MARKERS):
                violations.append(
                    Violation(
                        "R12",
                        f"query_results[{_idxs[0]}].info_status",
                        f"同名医院「{output.query_results[_idxs[0]].name}」的 {len(_idxs)} 条结果"
                        f"对同一事实给出了不同信息状态（如是否可提供/更新时间不一致），"
                        "须主动标注「来源间存在冲突，以官方渠道为准」，不得静默合并",
                    )
                )

    hints = [RULE_HINTS[v.rule] for v in violations if v.rule in RULE_HINTS]
    # 去重保序
    seen: list[str] = []
    for h in hints:
        if h not in seen:
            seen.append(h)
    return ValidationResult(passed=not violations, violations=violations, hints=seen)


def _mentions_campus(r) -> bool:
    """该条结果是否体现了「适用院区」。

    判据（结构性，不判断事实对错）：
      ① 显式填了 campus 字段（含诚实写法「来源未标明院区」）；
      ② 或在名称/说明里出现了通用院区词（说明院区信息已写在正文里）。

    ⚠️ 它只回答"有没有把院区讲清楚"，**不回答"讲的那个院区对不对"**——
    后者涉及医院事实，属模型与来源的职责，代码不越界。
    """
    if (getattr(r, "campus", None) or "").strip():
        return True
    text = f"{getattr(r, 'name', '') or ''} {getattr(r, 'description', '') or ''}"
    return any(w in text for w in CAMPUS_WORDS)


def build_reflection_prompt(result: ValidationResult, round_no: int) -> str:
    """Reflector 话术：把违规点编码成结构化反思指令，回灌给模型。

    ⚠️ 只描述"哪里不合规、要怎么改"，**不提供任何事实、不预设答案**——
    改由模型自己重新组织语言（模型始终是控制器）。
    """
    lines: list[str] = [
        f"【护栏拦截·第 {round_no} 轮】你的上一条输出未通过结构性校验，问题如下：",
    ]
    for i, v in enumerate(result.violations, 1):
        lines.append(f"{i}. {v.rule}（{v.where}）：{v.detail}")
    lines.append("")
    lines.append("修正要求：")
    for h in result.hints:
        lines.append(f"- {h}")
    lines.append("")
    lines.append(
        "请仅在上述问题范围内修正后，再次输出同一套 4 段式 JSON："
        "不要新增本轮检索结果中未出现的信息，不要编造任何来源链接。"
    )
    return "\n".join(lines)
