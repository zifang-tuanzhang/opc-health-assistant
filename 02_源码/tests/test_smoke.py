# -*- coding: utf-8 -*-
"""端到端冒烟测试（Step 2 骨架 + 密钥架构验证，Task #15）。

用 fastapi TestClient（in-process，不走网络、不受代理影响）直接驱动真实 ASGI app，
覆盖两条主链路：
  A. 无密钥时的「护栏 + need_key 降级」链路（不依赖任何外部密钥）
  B. 密钥架构链路（预设清单 / 自动扫描状态 / 添加并校验连接），
     全程用临时密钥库隔离，绝不污染用户真实的 %USERPROFILE%\\.opc_health\\keys.json。

运行：激活 venv 后 python tests/test_smoke.py
"""
import sys
import json
import tempfile
from pathlib import Path

# 项目内的 src 包位置：由本文件位置推导，绝不写死绝对路径
# （交付包会被评审解压到任意目录，写死路径会导致测试直接崩，或更糟——静默测到别的目录的源码）
BASE = str(Path(__file__).resolve().parent.parent)   # tests/ 的上一级 = 02_源码/
sys.path.insert(0, BASE)

# 在 import src 之前，先把 keystore 的存储位置重定向到临时目录，保证测试隔离。
import src.keystore as keystore  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="opc_test_"))
keystore.STORE_DIR = _TMP
keystore.STORE_PATH = _TMP / "keys.json"

from fastapi.testclient import TestClient  # noqa: E402
import src.server as s  # noqa: E402
import src.providers as providers  # noqa: E402
import src.llm_client as llm_client  # noqa: E402

client = TestClient(s.app)
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


# ───────────────────────── A. 无密钥链路（护栏 + need_key 降级） ─────────────────────────
# T0 /health
h = client.get("/health")
hj = h.json()
check("T0 health", h.status_code == 200 and hj.get("status") == "ok",
      f"demo_city={hj.get('demo_city')} key_configured={hj.get('key_configured')}")

# T1 正常提问，但未配置密钥 → 必须走 need_key 降级（ok=False, mode=need_key, 不崩溃）
r = client.post("/chat", json={"message": "昆明哪家三甲医院有心血管内科？"})
j = r.json()
check("T1 need_key-mode", r.status_code == 200 and j.get("ok") is False and j.get("mode") == "need_key",
      f"mode={j.get('mode')}")
check("T1 need_key-usable", isinstance(j.get("output"), dict)
      and isinstance(j["output"].get("usage_tips"), list) and len(j["output"]["usage_tips"]) >= 1,
      "降级信封仍带 usage_tips，界面可引导用户添加密钥")
check("T1 no-fabrication", j["output"].get("query_results") == [] and j["output"].get("info_basis") == [],
      "无密钥不编造任何事实")

# T2 空输入护栏
r2 = client.post("/chat", json={"message": "   "})
j2 = r2.json()
check("T2 guard-empty", j2.get("ok") is False and j2.get("error_code") == "EMPTY", j2.get("error_code"))

# T3 超长护栏（> MAX_MESSAGE_LEN=500）
r3 = client.post("/chat", json={"message": "测" * 600})
j3 = r3.json()
check("T3 guard-long", j3.get("ok") is False and j3.get("error_code") == "TOO_LONG", j3.get("error_code"))

# T4 多轮会话隔离（同 session_id 不串）
sid = "smoke_s1"
a = client.post("/chat", json={"message": "昆明", "session_id": sid})
b = client.post("/chat", json={"message": "再看延安医院", "session_id": sid})
check("T4 session-isolation", a.json().get("session_id") == b.json().get("session_id") == sid, sid)

# T9 前端界面可服务
idx = client.get("/")
check("T9 index-served", idx.status_code == 200 and "text/html" in idx.headers.get("content-type", ""),
      f"bytes={len(idx.content)}")

# T11 会话重置接口（前端「重置会话」会调用；应清掉服务端上下文）
sid_r = "smoke_reset"
client.post("/chat", json={"message": "昆明", "session_id": sid_r})
rs = client.post("/reset", json={"session_id": sid_r})
check("T11 reset-ok", rs.status_code == 200 and rs.json().get("ok") is True and rs.json().get("reset") is True,
      str(rs.json()))
rs2 = client.post("/reset", json={"session_id": sid_r})
check("T11 reset-idempotent", rs2.json().get("reset") is False, "重复重置不报错")

# T12 B1 历史会话清单（进阶3）：/history 返回已存在会话的轻量元信息。
# 前置：T4/T11 已通过 /chat 创建会话，内存会话库非空。
hh = client.get("/history")
hj = hh.json()
check("T12 history-200", hh.status_code == 200 and isinstance(hj, list), f"type={type(hj).__name__}")
check("T12 history-nonempty", len(hj) >= 1, f"count={len(hj)}")
check("T12 history-shape", all(
    isinstance(x, dict) and "session_id" in x and "first_query" in x
    and "turns" in x and "updated_at" in x for x in hj),
    "每条含 session_id/first_query/turns/updated_at")
check("T12 history-no-content-leak", all("content" not in x and "messages" not in x for x in hj),
      "不泄露完整对话内容（只返回轻量元信息）")

# ───────────────────────── B. 密钥架构链路（隔离密钥库） ─────────────────────────
# T5 预设厂商清单（随源码发货，不含任何密钥值）
p = client.get("/api/keys/providers")
pj = p.json()
prov_list = pj.get("providers", [])
check("T5 providers-200", p.status_code == 200 and len(prov_list) >= 10, f"count={len(prov_list)}")
leaked = [x for x in prov_list if any(k in x for k in ("api_key", "secret", "token"))]
check("T5 providers-no-secret", len(leaked) == 0, "预设清单不含密钥字段")
check("T5 providers-has-baseurl", all(x.get("base_url") for x in prov_list), "每个厂商带 base_url（OpenAI 兼容网关）")

# T6 自动扫描状态（未配置）
st = client.get("/api/keys/status").json()
check("T6 status-unconfigured", st.get("configured") is False and st.get("store_path"),
      f"store={st.get('store_path')}")

# T7 写入一个「假密钥」后，自动扫描应识别为已配置（验证外部库读写闭环）
keystore.upsert_provider("deepseek", "sk-test-fake-0000", "deepmock-chat")
st2 = client.get("/api/keys/status").json()
check("T7 status-configured", st2.get("configured") is True and st2.get("provider_id") == "deepseek",
      f"provider={st2.get('provider_id')}")
check("T7 real-store-untouched",
      not (Path.home() / ".opc_health" / "keys.json").exists()
      or "sk-test-fake" not in (Path.home() / ".opc_health" / "keys.json").read_text(encoding="utf-8", errors="ignore"),
      "真实用户密钥库未被测试污染")

# T8 添加密钥路由的两条分支（用 mock 隔离真实网络，保证确定性 + 快速）
real_test = llm_client.test_connection


def _mock_ok(*a, **k):
    return True, "mock 连接成功"


def _mock_fail(*a, **k):
    return False, "mock 连接失败：AuthenticationError: invalid"


# T8a 连接成功 → 200，密钥写入外部库
llm_client.test_connection = _mock_ok
add_ok = client.post("/api/keys/add", json={"provider_id": "qwen", "api_key": "sk-mock-qwen", "model": "qwen-max"})
check("T8a add-success", add_ok.status_code == 200 and add_ok.json().get("ok") is True,
      f"vendor={add_ok.json().get('vendor')}")
check("T8a persisted", keystore.get_active() is not None and keystore.get_active()["id"] == "qwen",
      "密钥已落到外部库并设为激活")

# T8b 连接失败 → 400，且不写入任何密钥（不污染）
llm_client.test_connection = _mock_fail
before = keystore.load_store()
add_bad = client.post("/api/keys/add", json={"provider_id": "kimi", "api_key": "sk-mock-bad", "model": "moonshot-v1"})
check("T8b add-fail-400", add_bad.status_code == 400 and "连接失败" in add_bad.json().get("detail", ""),
      add_bad.json().get("detail"))
after = keystore.load_store()
check("T8b not-persisted", "kimi" not in after.get("providers", {}), "失败密钥未写入外部库")
llm_client.test_connection = real_test  # 还原

# T10 未知厂商 → 400
unk = client.post("/api/keys/add", json={"provider_id": "no_such", "api_key": "x"})
check("T10 unknown-provider", unk.status_code == 400, unk.json().get("detail"))

print(f"\n===== 冒烟+密钥架构测试：通过 {passed} / 失败 {failed} =====")
if failed:
    raise SystemExit(1)
print("\n[need_key 降级样例响应]")
print(json.dumps(j, ensure_ascii=False, indent=2))
