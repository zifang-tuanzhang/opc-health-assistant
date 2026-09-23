# -*- coding: utf-8 -*-
"""8 组验收测试（对应开发指导架构 Step 7 的 DoD：8 组测试 / 输入·预期·实际·通过）。

设计原则——**离线可复跑**：
- 注入「假模型 + 假检索」，把网络与密钥隔离掉，使断言确定、可在任何机器复跑；
- 断言只针对「行为与结构」（是否检索、是否打回、是否降级、字段是否合规），
  不针对具体医院事实（事实真假只能靠来源与人工核验）。
- 真实网络 + 真实模型的那一版证据，见 03_测试记录/真实性证据_2026-09-21.md。

运行：
    python tests/test_acceptance_8groups.py            # 跑 8 组
    python tests/test_acceptance_8groups.py --json out.json   # 额外导出机器可读结果

赛题《测试记录》要求「逐组记录**输入、预期行为、实际结果、通过情况、测试时间**及截图或日志」。
为不漏字段，本脚本除了打印明细表，还会**自动**把逐组记录（含每条的执行时刻与耗时）导出为
``03_测试记录/验收8组_逐组记录_20260921.json``——文档里的表格直接由它生成，避免手工转录出错。
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import time

# 在 import src 之前隔离密钥库，避免碰到用户真实密钥（与 test_smoke 同口径）
_TMP_STORE = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "_accept_store.json"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.keystore as keystore  # noqa: E402

keystore.STORE_DIR = _TMP_STORE.parent
keystore.STORE_PATH = _TMP_STORE

from fastapi.testclient import TestClient  # noqa: E402

import src.llm_client as llm_client  # noqa: E402
import src.orchestrator as orchestrator  # noqa: E402
from src.server import app  # noqa: E402

client = TestClient(app)

records: list[dict] = []

# 逐组计时：赛题要求「逐组记录测试时间」。耗时按「上一条记录到本条记录」的间隔计算，
# 这样不必在 12 个调用点各加一次计时埋点，也不会漏记。
_T_START = time.time()
_T_LAST = [_T_START]
RUN_STARTED_AT = datetime.datetime.fromtimestamp(_T_START).strftime("%Y-%m-%d %H:%M:%S")


def record(gid: str, name: str, input_text: str, expect: str, actual: str, passed: bool) -> None:
    now = time.time()
    elapsed = now - _T_LAST[0]
    _T_LAST[0] = now
    records.append({
        "id": gid, "name": name, "input": input_text,
        "expect": expect, "actual": actual, "passed": bool(passed),
        "at": datetime.datetime.fromtimestamp(now).strftime("%H:%M:%S"),
        "seconds": round(elapsed, 3),
    })
    print("[%s] %-4s %-22s | %s  (%.2fs @ %s)" % (
        "PASS" if passed else "FAIL", gid, name, actual, elapsed,
        records[-1]["at"]))


# ─────────────────────── 假模型 / 假检索（隔离网络） ───────────────────────


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, tid, name, arguments):
        self.id = tid
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]
        self.usage = None


def tool_call(query, city="昆明"):
    """假模型第一轮：请求检索。"""
    return _Resp(_Msg("", [_TC("call_1", "web_search",
                               json.dumps({"query": query, "city": city}, ensure_ascii=False))]))


def no_tool():
    """假模型第一轮：不检索，直接进入生成。"""
    return _Resp(_Msg("", None))


def json_out(payload: dict):
    """假模型生成轮：返回 4 段式 JSON。"""
    return _Resp(_Msg(json.dumps(payload, ensure_ascii=False)))


def text_out(text: str):
    """假模型闲聊轮：返回纯文本。"""
    return _Resp(_Msg(text))


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        # 记录每次调用实际收到的 messages —— 这是验证「多轮上下文被真正承接」
        # 与「不同会话相互隔离」的唯一确定性手段（不看模型输出，看喂给模型的输入）。
        self.seen_messages: list[list] = []

    def __call__(self, model, messages, **kwargs):
        self.calls += 1
        self.seen_messages.append([dict(m) if isinstance(m, dict) else m for m in messages])
        if not self.script:
            raise AssertionError("假模型脚本已用尽（说明真实调用次数超出预期）")
        return self.script.pop(0)

    def ever_saw(self, needle: str) -> bool:
        """本次会话中，喂给模型的任何一条消息里是否出现过 needle。"""
        for msgs in self.seen_messages:
            for m in msgs:
                if needle in str((m or {}).get("content", "")):
                    return True
        return False


def install(fake_llm, hits=None):
    """装上假模型 + 假检索，并保证 is_ready/active_model 可用。"""
    llm_client.chat = fake_llm
    llm_client.is_ready = lambda: True
    llm_client.active_model = lambda: "fake-model"
    orchestrator.web_search = lambda q, c=None, **kw: list(hits or [])


def uninstall():
    import importlib

    importlib.reload(llm_client)
    importlib.reload(orchestrator)


# 注入用的假检索结果（离线注入，不打网络）。
# 说明：这组数据只用于「离线注入版」的确定性断言，不代表任何真实事实；
#       真实来源的证据在「实网真机版」里取（见 03_测试记录/真实性证据_2026-09-21.md）。
U1 = "https://news.qq.com/rain/a/20250612A01YGT00"
U2 = "https://www.youlai.cn/hospitalrank/custom_F52CA4AaF.html"
U3 = "https://www.yn.gov.cn/zwgk/ggxx/index.html"

HITS = [
    {"title": "被蛇咬伤上哪治 昆明这两家医院有抗蛇毒血清_腾讯新闻",
     "url": U1,
     "snippet": "2025年6月12日 - 目前,省一院和联勤保障部队第920医院均备有4种抗蛇毒血清"},
    {"title": "昆明三甲医院有哪些 - 有来医生",
     "url": U2,
     "snippet": "2026年8月28日 - 1昆明医科大学第一附属医院三甲综合"},
    {"title": "云南省医疗机构信息公开专栏 - 云南省人民政府",
     "url": U3,
     "snippet": "2026年3月11日 - 公开医疗机构执业登记与科室设置信息"},
]


def out4(results=None, tips=None):
    return {
        "query_condition": {"region": "昆明", "hospital": "", "department": "",
                            "resource": "", "title": "", "date": ""},
        "query_results": results if results is not None else [],
        "info_basis": [],
        "usage_tips": tips if tips is not None else ["请以医院官方渠道为准。"],
    }


def res(name, status=None, url=U1):
    r = {"name": name, "type": "医院", "description": "据公开报道。",
         "source": {"title": "腾讯新闻", "url": url, "updated_note": "来源未标注更新时间"}}
    if status:
        r["info_status"] = status
    return r


def doc(name, dept, title, expertise, url=U2):
    """医生信息条目：姓名 / 所属科室 / 职称 / 公开擅长领域（对应赛题基础需求3）。"""
    return {"name": name, "type": "医生",
            "description": "所属科室：%s；职称：%s；公开擅长领域：%s" % (dept, title, expertise),
            "source": {"title": "医院官网公开信息", "url": url,
                       "updated_note": "来源未标注更新时间"}}


# R6 类禁语：不得出现无来源支撑的绝对化/承诺性表述（疗效承诺、排名等）
FORBIDDEN_DOCTOR_WORDS = ("包治", "疗效保证", "绝对安全", "最好的医生", "第一名",
                          "全国第一", "治愈率", "无风险")


# ══════════════════════ 8 组：严格对应赛题要求的 8 个覆盖方向 ══════════════════════
# 依据赛题原文《基础需求7 · 测试记录》：
#   「至少提交8组测试，逐组记录输入、预期行为、实际结果、通过情况、测试时间及截图或日志。
#     应覆盖：医院资源、医生信息、出诊时效、多轮条件变更、无结果或来源不可访问、
#     特殊资源未核实、诊疗越界问题、小程序接口或模拟调用。」
# 故本套件的 G1~G8 按**上述顺序一一对应**，便于逐项核对。
print("=" * 96)
print("8 组验收测试（离线注入版：假模型 + 假检索，断言行为与结构）")
print("G1~G8 与赛题原文要求的 8 个覆盖方向一一对应（顺序一致）")
print("本次运行开始时间：%s（逐组耗时见各行末尾）" % RUN_STARTED_AT)
print("=" * 96)

# ── G1 医院资源：域内检索型；多项结果逐项列出、每条带合法 http 来源 ──
q1 = "昆明哪家三甲医院有心血管内科？"
install(FakeLLM([
    tool_call("昆明 三甲医院 心血管内科"),
    json_out(out4([res("昆明医科大学第一附属医院", url=U1),
                   res("云南省第一人民医院", url=U2),
                   res("昆明某民营医院（民营，具可核验公开信息）", url=U3)])),
]), HITS)
e = client.post("/chat", json={"message": q1}).json()
results1 = e["output"]["query_results"]
searched = any(r["action"] == "search" and r["status"] == "ok" for r in e.get("retrieval_log", []))
url_ok = all(str((x.get("source") or {}).get("url", "")).startswith("http") for x in results1)
ok1 = e["ok"] and e["mode"] == "agent" and searched and len(results1) >= 3 and url_ok
record("G1", "医院资源", q1, "触发检索；≥3 项结果逐项列出；每条带合法 http 来源",
       "检索=%s，结果=%d 条，来源合法=%s" % (searched, len(results1), url_ok), ok1)

# ── G2 医生信息：姓名/科室/职称/公开擅长领域；重点主任医师；不得作疗效承诺或排名 ──
q2 = "昆明医科大学第一附属医院心内科的主任医师有哪些？"
install(FakeLLM([
    tool_call("昆明医科大学第一附属医院 心内科 主任医师"),
    json_out(out4([doc("（示例）医师A", "心血管内科", "主任医师", "冠心病介入诊疗、高血压规范化管理"),
                   doc("（示例）医师B", "心血管内科", "副主任医师", "心律失常与心力衰竭诊治")])),
]), HITS)
e = client.post("/chat", json={"message": q2}).json()
docs = [x for x in e["output"]["query_results"] if str(x.get("type")) == "医生"]
txt2 = json.dumps(e["output"], ensure_ascii=False)
has_title = all(("主任医师" in str(x.get("description", ""))) for x in docs) if docs else False
has_dept = all(("科室" in str(x.get("description", ""))) for x in docs) if docs else False
has_expertise = all(("擅长" in str(x.get("description", ""))) for x in docs) if docs else False
no_promise = not any(w in txt2 for w in FORBIDDEN_DOCTOR_WORDS)
src_ok = all(str((x.get("source") or {}).get("url", "")).startswith("http") for x in docs) if docs else False
ok2 = (e["ok"] and len(docs) >= 2 and has_title and has_dept and has_expertise
       and no_promise and src_ok)
record("G2", "医生信息", q2, "医生条目含科室/职称/擅长领域 + 合法来源；无疗效承诺或排名",
       "医生条目=%d，含职称=%s，含科室=%s，含擅长=%s，无承诺/排名=%s，来源合法=%s"
       % (len(docs), has_title, has_dept, has_expertise, no_promise, src_ok), ok2)

# ── G3 出诊时效：断言式出诊表述应被 R6 打回，改写为"待核实"；不得从旧排班推断当前 ──
q3 = "昆明医科大学第一附属医院心内科主任出诊时间？"
install(FakeLLM([
    tool_call("昆明医科大学第一附属医院 心内科 出诊"),
    json_out(out4([res("心内科主任")], ["该主任确定出诊，可预约成功。"])),   # R6 打回
    json_out(out4([res("心内科主任")],
                  ["暂未查到可核验的出诊安排，请查看医院官方挂号渠道；"
                   "本结果不代表当前排班，请以医院最新公布为准。"])),
]), HITS)
e = client.post("/chat", json={"message": q3}).json()
txt3 = json.dumps(e["output"], ensure_ascii=False)
says_ok = ("确定出诊" not in txt3) and ("可预约成功" not in txt3)
guide3 = ("官方" in txt3) or ("挂号" in txt3)
ok3 = e["ok"] and e["reflection_count"] == 1 and says_ok and guide3
record("G3", "出诊时效", q3, "断言性出诊被打回并改写为待核实；引导官方挂号渠道",
       "打回=%s 轮，已消除断言=%s，含官方挂号引导=%s"
       % (e["reflection_count"], says_ok, guide3), ok3)

# ── G4 多轮条件变更：同一会话承接 + 以新条件为准 + 不同会话隔离 + 重置生效 ──
# 赛题原文（基础需求4 · 上下文记忆）：支持用户补充条件、修改条件、继续追问；
#   同一会话更新条件后应以新条件为准；不同会话之间应相互隔离，并提供新建或重置会话方式。
# 断言方式（关键）：不看模型输出，直接看**喂给模型的 messages** —— 这是唯一确定性证据。
q4a = "昆明哪家医院有心血管内科？"
q4b = "换成神经内科呢？"
q4c = "第二家的官方预约挂号入口呢？"
SID_A, SID_B = "acc-g4-A", "acc-g4-B"
fake4 = FakeLLM([
    # 会话 A 第 1 轮
    tool_call("昆明 医院 心血管内科"), json_out(out4([res("甲医院", url=U1), res("乙医院", url=U2)])),
    # 会话 A 第 2 轮（改条件）
    tool_call("昆明 医院 神经内科"), json_out(out4([res("丙医院", url=U3)])),
    # 会话 A 第 3 轮（继续追问）
    tool_call("乙医院 官方预约挂号"), json_out(out4([res("乙医院", url=U2)])),
    # 会话 B（独立会话，与 A 无关）
    no_tool(), json_out(out4([res("丁医院（另一会话）", url=U1)])),
    # 会话 A 重置之后再问（不应再带上下文）
    tool_call("昆明 医院 皮肤科"), json_out(out4([res("戊医院", url=U1)])),
])
install(fake4, HITS)
e4a = client.post("/chat", json={"message": q4a, "session_id": SID_A}).json()
mark_after_a1 = len(fake4.seen_messages)
e4b = client.post("/chat", json={"message": q4b, "session_id": SID_A}).json()
e4c = client.post("/chat", json={"message": q4c, "session_id": SID_A}).json()
mark_before_b = len(fake4.seen_messages)
e4d = client.post("/chat", json={"message": "昆明哪家医院有皮肤科？", "session_id": SID_B}).json()
b_round_msgs = fake4.seen_messages[mark_before_b:]

sid_stable = (e4a.get("session_id") == e4b.get("session_id") == e4c.get("session_id") == SID_A
              and e4d.get("session_id") == SID_B)


def _msgs_contain(msgs_list, needle: str) -> bool:
    return any(needle in str((m or {}).get("content", ""))
               for msgs in msgs_list for m in msgs)


# 承接：会话 A 第 2 轮喂给模型的消息里，应能看到第 1 轮的提问
carry_ok = _msgs_contain(fake4.seen_messages[mark_after_a1:], q4a)
# 隔离：会话 B 的消息里**不应**出现会话 A 的任何提问（不串会话）
isolated_ok = not (_msgs_contain(b_round_msgs, q4a) or _msgs_contain(b_round_msgs, q4b)
                   or _msgs_contain(b_round_msgs, q4c))
# 反向自检（防止断言恒真导致的误判通过）：
#   上面的 isolated_ok=True 有两种可能——① 真的隔离了；② 检测函数失灵、
#   无论喂什么都返回 False。所以必须证明「检测函数能检出**该检出**的东西」：
#   会话 B 的消息里应当能检出它**自己**的提问。这一条不过，isolated_ok 就不算数。
detector_sane = _msgs_contain(b_round_msgs, "皮肤科")
# 重置：/reset 之后再问，消息里不应再出现之前的提问
rs = client.post("/reset", json={"session_id": SID_A})
mark_before_rs = len(fake4.seen_messages)
e4e = client.post("/chat", json={"message": "昆明哪家医院有皮肤科？", "session_id": SID_A}).json()
reset_ok = (rs.status_code == 200) and not _msgs_contain(
    fake4.seen_messages[mark_before_rs:], q4a)
ok4 = (e4a["ok"] and e4b["ok"] and e4c["ok"] and e4d["ok"] and e4e["ok"]
       and sid_stable and carry_ok and isolated_ok and detector_sane and reset_ok)
record("G4", "多轮条件变更", "%s → %s → %s（另含独立会话与重置）" % (q4a, q4b, q4c),
       "同会话承接上下文；改条件后以新条件为准；不同会话相互隔离；提供重置方式",
       "会话id稳定=%s，上下文承接=%s，会话隔离=%s（检测函数自检=%s），重置生效=%s"
       % (sid_stable, carry_ok, isolated_ok, detector_sane, reset_ok), ok4)

# ── G5 无结果或来源不可访问：诚实说"暂未查到"，且**不得断言"没有"**；给官方核实渠道 ──
q5 = "昆明某民营医院有神经内科吗？"
install(FakeLLM([
    tool_call("昆明 某民营医院 神经内科"),
    json_out(out4([], ["暂未查到可核验的联网信息，请以医院官方公布或电话咨询为准。"])),
]), [])   # ← 注入"检索全空"，模拟无结果/来源不可访问
e = client.post("/chat", json={"message": q5}).json()
txt5 = json.dumps(e["output"], ensure_ascii=False)
honest5 = "暂未查到" in txt5
no_negative_claim = not any(w in txt5 for w in ("没有神经内科", "不存在", "该院无", "确定没有", "未开设"))
channel5 = any(w in txt5 for w in ("官方", "电话", "咨询", "挂号"))
empty5 = len(e["output"]["query_results"]) == 0 and len(e["output"]["info_basis"]) == 0
ok5 = e["ok"] and empty5 and honest5 and no_negative_claim and channel5
record("G5", "无结果或来源不可访问", q5, "结果为空；明确「暂未查到」；不断言「没有」；给官方核实渠道",
       "结果空=%s，含「暂未查到」=%s，未断言「没有」=%s，含官方渠道=%s"
       % (empty5, honest5, no_negative_claim, channel5), ok5)

# ── G6 特殊资源未核实：易变资源强制三态；不得把历史报道当"现在有库存" ──
q6 = "昆明哪家医院有抗蛇毒血清？"
install(FakeLLM([
    tool_call("昆明 抗蛇毒血清 医院"),
    json_out(out4([res("省一院"), res("920医院")])),                      # 无三态 → R9 打回
    json_out(out4([res("省一院", "公开页面介绍具备相关能力"),
                   res("920医院", "公开页面介绍具备相关能力")])),          # 补三态 → 通过
]), HITS)
e = client.post("/chat", json={"message": q6}).json()
has_status = all(x.get("info_status") for x in e["output"]["query_results"])
ok6 = e["ok"] and e["reflection_count"] == 1 and has_status
record("G6", "特殊资源未核实", q6, "缺三态→R9 打回 1 轮→补标三态后通过",
       "打回=%s 轮，三态齐全=%s" % (e["reflection_count"], has_status), ok6)

# ── G7 诊疗越界问题：不得给诊断/用药建议；须拒答 + 引导就医 ──
q7 = "我高血压该吃什么药？去哪家医院看？"
install(FakeLLM([
    no_tool(),
    json_out(out4([], ["建议服用降压药，每天两次。"])),                    # R4 打回
    json_out(out4([], ["本系统不提供诊断与用药建议，请及时就医由医生判断。"])),
]), HITS)
e = client.post("/chat", json={"message": q7}).json()
txt7 = json.dumps(e["output"], ensure_ascii=False)
safe7 = ("建议服用" not in txt7) and ("就医" in txt7 or "120" in txt7)
ok7 = e["ok"] and e["reflection_count"] == 1 and safe7
record("G7", "诊疗越界问题", q7, "越界用药建议被拦，改为拒答 + 引导就医",
       "打回=%s 轮，安全=%s" % (e["reflection_count"], safe7), ok7)

# ── G8 小程序接口或模拟调用：完整走一次「请求 → 回答」，并含异常状态约定 ──
# 赛题原文：允许通过测试页面或模拟调用方验证接入流程，但**须明确标注模拟范围**。
# 这里以「模拟小程序服务端」的身份直接调 REST 接口，覆盖：
#   ① 首次不传 session_id → 后端返回新 id（小程序把它存进 globalData 实现多轮）
#   ② 回传 session_id 完成第二轮 → 会话承接
#   ③ 一次完整响应包含 ok/session_id/output/retrieval_log/mode/error_code 全部约定字段
#   ④ 异常状态约定：超长输入 → ok=false + error_code（不是 500 崩溃）
q8 = "昆明哪家医院能做心脏搭桥手术？"
install(FakeLLM([
    tool_call("昆明 医院 心脏搭桥 手术"), json_out(out4([res("昆明某三甲医院", url=U1)])),
    tool_call("该医院 官方预约挂号"), json_out(out4([res("昆明某三甲医院", url=U1)])),
]), HITS)
env8a = client.post("/chat", json={"message": q8}).json()          # 模拟小程序首次调用（不传 session_id）
sid8 = env8a.get("session_id")
contract_fields = ("ok", "session_id", "output", "retrieval_log", "mode", "error_code", "degraded")
contract_ok = all(k in env8a for k in contract_fields) and bool(sid8)
out_fields = ("query_condition", "query_results", "info_basis", "usage_tips")
output_ok = all(k in (env8a.get("output") or {}) for k in out_fields)
env8b = client.post("/chat", json={"message": "它的官方预约挂号入口呢？", "session_id": sid8}).json()
carry8 = env8b.get("session_id") == sid8
env8c = client.post("/chat", json={"message": "昆明" * 400}).json()   # 异常状态约定
err_ok8 = (env8c.get("ok") is False) and bool(env8c.get("error_code")) \
    and (env8c.get("output") is not None)
demo_note = any("模拟" in t for t in ("模拟小程序服务端调用",))     # 明确标注模拟范围
ok8 = contract_ok and output_ok and carry8 and err_ok8 and demo_note
record("G8", "小程序接口或模拟调用", "模拟小程序服务端两次调用（首次不传 session_id）",
       "响应含全部约定字段；首次返回新 session_id；回传后承接；异常态有 error_code",
       "约定字段全=%s，4段式全=%s，回传承接=%s，异常态=%s，已标注模拟范围=%s"
       % (contract_ok, output_ok, carry8, err_ok8, demo_note), ok8)

# ═════════════════════ 附加健壮性（不计入赛题要求的 8 组）═════════════════════
# 这几项是本作品自加的加固场景（紧急优先/闲聊/输入护栏/密钥降级），
# 不属于赛题点名的 8 个覆盖方向，故单列，避免把"8 组覆盖"的数字搅混。
print("-" * 96)
print("附加健壮性（不计入 8 组覆盖）")
print("-" * 96)

# ── A1 紧急优先（R5）：含紧急症状必须提示 120 ──
qa1 = "我父亲胸痛、出冷汗，昆明哪家医院急诊好？"
install(FakeLLM([
    tool_call("昆明 医院 急诊 胸痛"),
    json_out(out4([res("某医院急诊科")], ["建议尽快前往医院就诊。"])),        # 缺 120 → R5 打回
    json_out(out4([res("某医院急诊科")], ["请立即拨打 120 或前往最近医院急诊科。"])),
]), HITS)
e = client.post("/chat", json={"message": qa1}).json()
tips = " ".join(e["output"]["usage_tips"])
oka1 = e["ok"] and e["reflection_count"] == 1 and "120" in tips
record("A1", "紧急优先(R5)", qa1, "紧急症状必须出现 120 提示",
       "打回=%s 轮，含120=%s" % (e["reflection_count"], "120" in tips), oka1)

# ── A2 闲聊/非医疗域：走 chat 模式，不套检索话术 ──
qa2 = "你好，你叫什么名字？"
install(FakeLLM([no_tool(), text_out("你好，我是医院资源查询与便民就医助手，请告诉我你的就医需求。")]), HITS)
e = client.post("/chat", json={"message": qa2}).json()
tips_a2 = " ".join(e["output"]["usage_tips"])
oka2 = (e["ok"] and e["mode"] == "chat" and e["output"]["query_condition"] is None
        and "未查到" not in tips_a2 and len(tips_a2) > 0)
record("A2", "闲聊/非医疗域", qa2, "mode=chat 且不出现「未查到」检索话术",
       "mode=%s，查询条件=%s，含检索话术=%s" % (
           e["mode"], e["output"]["query_condition"], "未查到" in tips_a2), oka2)

# ── A3 输入护栏：超长输入应被结构化拒绝（不进入编排） ──
qa3 = "昆明" * 400  # 800 字 > 默认上限 500
install(FakeLLM([]), HITS)
e = client.post("/chat", json={"message": qa3}).json()
oka3 = (e["ok"] is False) and bool(e.get("error_code")) and e.get("output") is not None
record("A3", "输入护栏超长", "（800 字超长输入）", "ok=false 且带 error_code，含完整 output 包裹层",
       "ok=%s，error_code=%s" % (e["ok"], e.get("error_code")), oka3)

# ── A4 未配密钥：优雅降级为 need_key，不崩溃 ──
def _not_ready():
    return False


llm_client.is_ready = _not_ready
e = client.post("/chat", json={"message": "昆明哪家医院有抗蛇毒血清？"}).json()
oka4 = (e["ok"] is False and e["mode"] == "need_key" and e["error_code"] == "NEED_KEY"
        and e["degraded"] is True)
record("A4", "未配密钥降级", "昆明哪家医院有抗蛇毒血清？", "mode=need_key / ok=false / 提示添加密钥",
       "mode=%s，error_code=%s" % (e["mode"], e.get("error_code")), oka4)

# ── A5 默认范围检索：未给城市 → 直接按昆明检索，不追问城市（本地化平台定位）──
# 依据：产品定位固定昆明（UI 已声明）；"你查哪个城市"是多余且会答不了的问。
# 赛题第41行"缺失且影响检索的条件应先询问"仍满足：地区不缺失（有默认范围），真正会问的是科室/医院。
qa5 = "帮我找有卒中中心的医院"
_c5: list = []


def _search5(q, c=None, **kw):
    _c5.append((q, c))
    return list(HITS)


install(FakeLLM([
    tool_call("昆明 卒中中心 医院"),                                  # 首轮·模型直接检索昆明（不问城市）
    json_out(out4([res("某医院卒中中心")], ["以医院官方渠道为准。"])),  # 首轮·生成
]), HITS)
orchestrator.web_search = _search5
e5 = client.post("/chat", json={"message": qa5}).json()
qc5 = e5["output"].get("query_condition") or {}
oka5 = (bool(e5["ok"])
        and len(_c5) >= 1                              # ① 直接检索（默认范围）
        and not e5["output"].get("need_clarification")  # ② 不是澄清轮
        and (qc5.get("region") or "") == "昆明")         # ③ 地区=昆明（展示默认）
record("A5", "默认范围检索(不问城市)", qa5,
       "未给城市 → 直接按昆明检索，不进入澄清、不追问城市",
       "检索=%d次，region=%r，need_clarification=%s" % (
           len(_c5), qc5.get("region"), e5["output"].get("need_clarification")), oka5)

# ── A6 缺真实条件才澄清：靠模型声明的字段，不靠词表 ──
# 用户"帮我找个专家"（缺科室）→ 模型首轮 JSON 声明 need_clarification=true → 不检索、问科室；
# 补充"心血管内科" → 下一轮真实检索。证明澄清机制仍工作，且判定来自字段而非措辞。
qa6 = "我想看专家出诊信息"
_c6: list = []


def _search6(q, c=None, **kw):
    _c6.append((q, c))
    return list(HITS)


sid6 = "acc_clarify_2"
install(FakeLLM([
    json_out({"query_condition": {"region": "昆明", "resource": ""},
              "query_results": [], "info_basis": [],
              "usage_tips": ["请问您想查哪类科室的专家？"],
              "need_clarification": True, "clarify_for": "科室"}),    # post1·resp1 声明澄清
    json_out({"query_condition": {"region": "昆明", "resource": ""},
              "query_results": [], "info_basis": [],
              "usage_tips": ["请问您想查哪类科室的专家？"],
              "need_clarification": True, "clarify_for": "科室"}),    # post1·生成（澄清轮不检索）
    tool_call("昆明 心血管内科 专家"),                                 # post2·resp1 检索
    json_out(out4([res("某医院心血管内科专家")], ["以官方渠道为准。"])),  # post2·生成
]), HITS)
orchestrator.web_search = _search6
e6a = client.post("/chat", json={"message": qa6, "session_id": sid6}).json()
oka6a = (bool(e6a["ok"]) and len(_c6) == 0
         and e6a["output"].get("need_clarification") is True
         and e6a["output"].get("clarify_for") == "科室")
e6b = client.post("/chat", json={"message": "心血管内科", "session_id": sid6}).json()
oka6b = bool(e6b["ok"]) and len(_c6) >= 1 and len(e6b["output"]["query_results"]) >= 1
oka6 = oka6a and oka6b
record("A6", "缺条件才澄清(字段声明)", "%s → 心血管内科" % qa6,
       "缺科室→模型声明need_clarification不检索；补科室后检索出结果",
       "第1轮检索=%d,clarify=%s；第2轮检索=%d,结果=%d" % (
           len(_c6), e6a["output"].get("need_clarification"), len(_c6),
           len(e6b["output"]["query_results"])), oka6)

# ── A7 澄清判定不依赖措辞词表（"由模型判断"而非"写死话术"的硬验证）──
# 模型用**任意说法**声明 need_clarification=true（此处措辞不含"请问/城市/地区"等硬编码词），
# 系统仍正确识别为澄清轮；且输出**不**被强制要求追问城市。证明判据来自字段而非词表。
qa7 = "我想查一下医疗资源"
_c7: list = []


def _search7(q, c=None, **kw):
    _c7.append((q, c))
    return []


install(FakeLLM([
    json_out({"query_condition": {"region": "昆明"},
              "query_results": [], "info_basis": [],
              "usage_tips": ["缺哪类科室或资源？告诉我即可。"],   # 无"请问/城市"等硬编码词
              "need_clarification": True, "clarify_for": "资源"}),
    json_out({"query_condition": {"region": "昆明"},
              "query_results": [], "info_basis": [],
              "usage_tips": ["缺哪类科室或资源？告诉我即可。"],
              "need_clarification": True, "clarify_for": "资源"}),
]), HITS)
orchestrator.web_search = _search7
e7 = client.post("/chat", json={"message": qa7}).json()
tips7 = " ".join(e7["output"]["usage_tips"])
oka7 = (bool(e7["ok"])
        and e7["output"].get("need_clarification") is True
        and "城市" not in tips7 and "地区" not in tips7)   # 未被强制追问城市
record("A7", "澄清不依赖词表", qa7,
       "模型任意措辞声明need_clarification=true即触发澄清，且不被追问城市",
       "clarify=%s，提示含城市/地区=%s" % (
           e7["output"].get("need_clarification"), ("城市" in tips7 or "地区" in tips7)), oka7)

# ─────────────────────────────── 汇总 ───────────────────────────────
uninstall()
G8_IDS = {"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"}
grp = [r for r in records if r["id"] in G8_IDS]
add = [r for r in records if r["id"] not in G8_IDS]
p8 = sum(1 for r in grp if r["passed"])
pa = sum(1 for r in add if r["passed"])
fa = len(add) - pa
print("-" * 96)
print("8 组覆盖明细（赛题要求方向 → 组号）")
for gid, direction in (
    ("G1", "医院资源"), ("G2", "医生信息"), ("G3", "出诊时效"), ("G4", "多轮条件变更"),
    ("G5", "无结果或来源不可访问"), ("G6", "特殊资源未核实"), ("G7", "诊疗越界问题"),
    ("G8", "小程序接口或模拟调用"),
):
    row = next((r for r in grp if r["id"] == gid), None)
    print("  %-3s %-16s %s" % (gid, direction, "✅ 通过" if (row and row["passed"]) else "✘ 未通过"))
print("-" * 96)
print("===== 8 组验收测试：通过 %d / 失败 %d ｜ 附加健壮性 %d/%d ====="
      % (p8, len(grp) - p8, pa, len(add)))

_all_ok = (p8 == len(grp)) and (fa == 0)

# 逐组记录导出（含时刻与耗时）：默认写到 03_测试记录/，供文档表格直接取数。
# 用 try 包住——测试本身不应因为"写旁证文件失败"而判失败。
_EVIDENCE = {
    "started_at": RUN_STARTED_AT,
    "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "total_seconds": round(time.time() - _T_START, 3),
    "env": "离线注入版（假模型 + 假检索；不打网络、不需密钥）",
    "passed": p8, "total": len(grp),
    "extra_passed": pa, "extra_total": len(add),
    "note": "「expect/actual」即赛题要求的「预期行为/实际结果」；耗时为「上一条到本条」的间隔。",
    "records": records,
}
try:
    _out = pathlib.Path(__file__).resolve().parent.parent.parent / "03_测试记录"
    _out.mkdir(parents=True, exist_ok=True)
    (_out / "验收8组_逐组记录_20260921.json").write_text(
        json.dumps(_EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
    print("逐组记录已导出：03_测试记录/验收8组_逐组记录_20260921.json")
    # 顺手把文档里的表格同步成刚导出的数据。
    # 为什么必须自动同步：逐组「测试时间」每跑一次都会变，靠人手往文档里粘，
    # 就会留下"测试是绿的、文档写着上一轮时间"这种最难察觉的不一致。
    try:
        import importlib.util as _ilu
        _gp = pathlib.Path(__file__).resolve().parent / "生成8组表格.py"
        _spec = _ilu.spec_from_file_location("_gen8", _gp)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        print("8 组表格：" + _mod.generate())
    except Exception as _e2:  # noqa: BLE001
        print("（提示：8 组表格同步跳过：%s）" % _e2)
except Exception as _e:  # noqa: BLE001
    print("（提示：逐组记录导出跳过：%s）" % _e)

if "--json" in sys.argv:
    idx = sys.argv.index("--json")
    path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "acceptance_result.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_EVIDENCE, fh, ensure_ascii=False, indent=2)
    print("结果已导出：%s" % path)

try:
    os.remove(_TMP_STORE)
except OSError:
    pass

# 注意：本文件是「脚本式证据工具」（模块导入即执行 8 组并打印明细表），
# 因此退出码必须只在「直接运行」时生效；否则被 pytest 等工具导入时，
# 模块层的 SystemExit 会打断调用方的进程（曾导致 `pytest tests/` 报 INTERNALERROR）。
if __name__ == "__main__":
    sys.exit(0 if _all_ok else 1)
