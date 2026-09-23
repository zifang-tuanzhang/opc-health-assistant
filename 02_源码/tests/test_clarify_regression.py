# -*- coding: utf-8 -*-
"""U2 澄清轮稳健性 · 聚焦回归测试（离线可复跑）。

目的（守「判断优先于指令 / 不硬编码台词」铁律）：
  U2 澄清机制已字段化（need_clarification + clarify_for），G6/G7 已覆盖基础路径。
  本文件在基础上额外锁定两类易回归点，且不新增任何脆弱兜底：
    U2a  厂商歧义澄清：用户问法隐含多个厂商/供应商可能时，模型可声明
         need_clarification=true、clarify_for="厂商"，系统正确识别为澄清轮（不检索）。
    U2b  任意 clarify_for 值透传：clarify_for 是开放 Optional[str]，**不绑定任何固定词表**；
         模型可写明任意缺失条件类别（如"就诊时间"），schema 不报错且原样透传——
         这正是"给判断不给台词"的硬验证（若某天有人把它改回枚举/词表，本测试立刻挂）。
    U2c  前端零硬编码澄清台词：index.html 不出现任何具体澄清问句（如"请问您要查哪个厂商"），
         澄清完全由模型输出的 usage_tips 承载（字段驱动渲染）。

运行：激活 venv 后 python tests/test_clarify_regression.py
"""
import os
import sys
import json
import pathlib

# 与 test_acceptance_8groups.py / test_smoke.py 同口径：import src 前隔离密钥库
_TMP_STORE = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "_clarify_store.json"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.keystore as keystore  # noqa: E402

keystore.STORE_DIR = _TMP_STORE.parent
keystore.STORE_PATH = _TMP_STORE

from fastapi.testclient import TestClient  # noqa: E402
import src.llm_client as llm_client  # noqa: E402
import src.orchestrator as orchestrator  # noqa: E402
from src.server import app  # noqa: E402

client = TestClient(app)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name} {detail}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")


# ─────────────────────── 最小假模型 / 假检索（复用范式） ───────────────────────
class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _TC:
    def __init__(self, name, arguments):
        self.id = "call_1"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _Resp:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]
        self.usage = None


def json_out(payload: dict):
    return _Resp(_Msg(json.dumps(payload, ensure_ascii=False)))


def tool_call(query, city="昆明"):
    return _Resp(_Msg("", [_TC("web_search", json.dumps({"query": query, "city": city}, ensure_ascii=False))]))


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)

    def __call__(self, model, messages, **kwargs):
        if not self.script:
            raise AssertionError("假模型脚本已用尽")
        return self.script.pop(0)


def install(fake_llm, hits=None):
    llm_client.chat = fake_llm
    llm_client.is_ready = lambda: True
    llm_client.active_model = lambda: "fake-model"
    orchestrator.web_search = lambda q, c=None, **kw: list(hits or [])


def uninstall():
    import importlib
    importlib.reload(llm_client)
    importlib.reload(orchestrator)


# ─────────────────────── U2a 厂商歧义澄清 ───────────────────────
_calls = []
def _search(q, c=None, **kw):
    _calls.append((q, c))
    return []


install(FakeLLM([
    json_out({"query_condition": {"region": "昆明"}, "query_results": [], "info_basis": [],
              "usage_tips": ["您说的这款药涉及多个厂商，请问您指的是哪个厂商/规格？"],
              "need_clarification": True, "clarify_for": "厂商"}),
    json_out({"query_condition": {"region": "昆明"}, "query_results": [], "info_basis": [],
              "usage_tips": ["您说的这款药涉及多个厂商，请问您指的是哪个厂商/规格？"],
              "need_clarification": True, "clarify_for": "厂商"}),
]), hits=[])
r_a = client.post("/chat", json={"message": "帮我查XX药在昆明哪家医院能开到", "session_id": "clarify_vendor"}).json()
oka = (bool(r_a["ok"]) and len(_calls) == 0
       and r_a["output"].get("need_clarification") is True
       and r_a["output"].get("clarify_for") == "厂商")
check("U2a 厂商歧义→澄清轮不检索", oka,
      f"检索={len(_calls)} clarify={r_a['output'].get('need_clarification')} for={r_a['output'].get('clarify_for')}")
uninstall()

# ─────────────────────── U2b 任意 clarify_for 值透传（开放字段，不绑词表） ───────────────────────
install(FakeLLM([
    json_out({"query_condition": {"region": "昆明"}, "query_results": [], "info_basis": [],
              "usage_tips": ["请问您希望什么就诊时间？"],
              "need_clarification": True, "clarify_for": "就诊时间"}),   # 任意不在任何词表里的类别
    json_out({"query_condition": {"region": "昆明"}, "query_results": [], "info_basis": [],
              "usage_tips": ["请问您希望什么就诊时间？"],
              "need_clarification": True, "clarify_for": "就诊时间"}),
]), hits=[])
r_b = client.post("/chat", json={"message": "我想挂号", "session_id": "clarify_open"}).json()
okb = (bool(r_b["ok"])
       and r_b["output"].get("need_clarification") is True
       and r_b["output"].get("clarify_for") == "就诊时间")   # 原样透传，schema 未报错
check("U2b 任意clarify_for值透传(开放字段)", okb,
      f"clarify_for={r_b['output'].get('clarify_for')}（若被改回枚举则此处校验失败）")
uninstall()

# ─────────────────────── U2c 前端零硬编码澄清台词 ───────────────────────
js = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
_hardcoded = ["请问您要查哪个厂商", "请问您要查哪个城市", "厂商是", "请问您需要补充"]
okc = not any(h in js for h in _hardcoded)
check("U2c 前端零硬编码澄清台词", okc,
      "index.html 不含具体澄清问句（澄清完全由 usage_tips 字段驱动渲染）")

# ─────────────────────── 汇总 ───────────────────────
print(f"\n===== U2 澄清轮回归：通过 {passed} / 失败 {failed} =====")
if failed:
    raise SystemExit(1)
