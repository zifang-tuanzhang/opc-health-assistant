# -*- coding: utf-8 -*-
"""运行保障与网关边界测试（进阶3 收口 + 交付前独立审计 P1#5/#6 补洞）。

为什么有它（来自交付前全模块闭环静态审计）：
  · cost_gate 整个模块（缓存去重 / 每分钟限速 / 每日预算）此前零测试——
    而「运行保障」是赛题进阶3的宣称项，宣称了就必须有断言锚定；
  · /chat/stream 是前端主路径（index.html 首选 SSE，/chat 只是回落），此前零测试；
  · guard.py 的 BLOCKED_SCHEME 分支 / 安全响应头、llm_client 的环境变量回落分支、
    providers/keystore 的「损坏告警」行为，均无断言。

全部离线注入（TestClient in-process + 模块属性打桩 + 临时密钥库隔离），
不联网、不需密钥、确定性可复跑。与 test_smoke.py 同一套路式风格。

运行：激活 venv 后 python tests/test_ops_guarantees.py
"""
import json
import logging
import sys
import tempfile
from pathlib import Path

# 项目内的 src 包位置：由本文件位置推导，绝不写死绝对路径（与 test_smoke.py 同理由）
BASE = str(Path(__file__).resolve().parent.parent)   # tests/ 的上一级 = 02_源码/
sys.path.insert(0, BASE)

# 在 import src 之前，把 keystore 存储位置重定向到临时目录（测试隔离，不碰真实密钥库）
import src.keystore as keystore  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="opc_ops_test_"))
keystore.STORE_DIR = _TMP
keystore.STORE_PATH = _TMP / "keys.json"

from fastapi.testclient import TestClient  # noqa: E402

import src.config as config  # noqa: E402
import src.cost_gate as cost_gate  # noqa: E402
import src.guard as guard  # noqa: E402
import src.llm_client as llm_client  # noqa: E402
import src.providers as providers  # noqa: E402
import src.server as s  # noqa: E402

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


def _sse_events(resp) -> list[dict]:
    """把 SSE 响应体解析成事件 dict 列表（data: 行 → json）。"""
    events = []
    for line in resp.iter_lines():
        line = (line or "").strip()
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


# ───────────────────────── A. cost_gate：进阶3 运行保障三态 ─────────────────────────
cost_gate.reset()

# A1 缓存去重：写入后同 key 命中，异 key 不命中
key1 = cost_gate.cache_key("m1", [{"role": "user", "content": "问"}], None)
key2 = cost_gate.cache_key("m1", [{"role": "user", "content": "另一问"}], None)
cost_gate.set_cached(key1, "缓存答案")
check("A1 cache-hit", cost_gate.get_cached(key1) == "缓存答案", "同指纹命中")
check("A1 cache-miss", cost_gate.get_cached(key2) is None, "异指纹不命中")
check("A1 cache-hit-stats", cost_gate.stats()["cache_hits"] >= 1,
      f"hits={cost_gate.stats()['cache_hits']}")

# A2 TTL 过期：TTL=0 时缓存立即视为过期（边界行为）
_ttl = config.LLM_CACHE_TTL_SECONDS
try:
    config.LLM_CACHE_TTL_SECONDS = 0
    check("A2 cache-ttl-expire", cost_gate.get_cached(key1) is None, "TTL=0 即过期")
finally:
    config.LLM_CACHE_TTL_SECONDS = _ttl

# A3 每分钟限速：上限 2 → 第 3 次调用被 GateDenied(LLM_RATE_LIMIT)
_lim = config.LLM_RATE_LIMIT_PER_MIN
cost_gate.reset()
try:
    config.LLM_RATE_LIMIT_PER_MIN = 2
    cost_gate.check()
    cost_gate.check()
    denied = None
    try:
        cost_gate.check()
    except cost_gate.GateDenied as e:
        denied = e
    check("A3 rate-limit", denied is not None and denied.code == "LLM_RATE_LIMIT",
          f"code={getattr(denied, 'code', None)}")
finally:
    config.LLM_RATE_LIMIT_PER_MIN = _lim

# A4 每日预算：预算 100 → 记录 100 后再调用被 GateDenied(LLM_BUDGET_EXCEEDED)
_bud = config.LLM_DAILY_BUDGET_TOKENS
cost_gate.reset()
try:
    config.LLM_DAILY_BUDGET_TOKENS = 100
    cost_gate.check()          # 先正常占用一个名额
    cost_gate.record_usage(100)
    denied = None
    try:
        cost_gate.check()
    except cost_gate.GateDenied as e:
        denied = e
    check("A4 budget-exceeded", denied is not None and denied.code == "LLM_BUDGET_EXCEEDED",
          f"code={getattr(denied, 'code', None)}")
    check("A4 stats-reflect", cost_gate.stats()["tokens_today"] == 100,
          f"tokens_today={cost_gate.stats()['tokens_today']}")
finally:
    config.LLM_DAILY_BUDGET_TOKENS = _bud

# A5 非整数量用法按 0 计（上游不给 token 数时不虚报）
cost_gate.reset()
cost_gate.record_usage(None)
check("A5 usage-none-safe", cost_gate.stats()["tokens_today"] == 0, "None → 0")

# A6 reset 清零（本套件同时是 reset() 的调用方，消除「零调用」悬置）
cost_gate.reset()
st = cost_gate.stats()
check("A6 reset-clears", st["tokens_today"] == 0 and st["cache_entries"] == 0
      and st["llm_calls_total"] == 0, str({k: st[k] for k in ("tokens_today", "cache_entries", "llm_calls_total")}))

# ───────────────────────── B. /chat/stream：前端主路径 SSE ─────────────────────────
# B1 无密钥主链路：session 首事件 → … → final 收尾，envelope 为 need_key 降级（确定性）
with client.stream("POST", "/chat/stream", json={"message": "昆明哪家医院有抗蛇毒血清？"}) as r:
    check("B1 sse-content-type", r.status_code == 200
          and "text/event-stream" in r.headers.get("content-type", ""),
          r.headers.get("content-type"))
    evs = _sse_events(r)
check("B1 first-is-session", evs and evs[0].get("type") == "session"
      and bool(evs[0].get("session_id")), str(evs[:1]))
check("B1 last-is-final", evs and evs[-1].get("type") == "final", f"共 {len(evs)} 事件")
_env = evs[-1].get("envelope", {}) if evs else {}
check("B1 need_key-degrade", _env.get("ok") is False and _env.get("mode") == "need_key",
      f"mode={_env.get('mode')} error_code={_env.get('error_code')}")
check("B1 no-fabrication", isinstance(_env.get("output"), dict)
      and _env["output"].get("query_results") == [] and _env["output"].get("info_basis") == [],
      "无密钥不编造任何事实")

# B2 危险协议前缀 → 网关在 SSE 通道里结构化拒绝（BLOCKED_SCHEME）
with client.stream("POST", "/chat/stream", json={"message": "file:///etc/passwd"}) as r:
    evs2 = _sse_events(r)
_fin2 = next((e for e in evs2 if e.get("type") == "final"), {})
check("B2 blocked-scheme", _fin2.get("envelope", {}).get("error_code") == "BLOCKED_SCHEME",
      _fin2.get("envelope", {}).get("error_code"))

# B3 空输入 → EMPTY
with client.stream("POST", "/chat/stream", json={"message": "   "}) as r:
    evs3 = _sse_events(r)
_fin3 = next((e for e in evs3 if e.get("type") == "final"), {})
check("B3 empty-input", _fin3.get("envelope", {}).get("error_code") == "EMPTY",
      _fin3.get("envelope", {}).get("error_code"))

# ───────────────────────── C. guard.py：网关边界 ─────────────────────────
ok4, code4, hint4 = guard.validate_message("file://C:/Windows/system32")
check("C1 scheme-blocked", ok4 is False and code4 == "BLOCKED_SCHEME" and bool(hint4), code4)
ok4b, code4b, _ = guard.validate_message("javascript:alert(document.cookie)")
check("C1b scheme-blocked-js", ok4b is False and code4b == "BLOCKED_SCHEME", code4b)
check("C2 sanitize-controls", guard.sanitize_input("a\x00b\x1fc") == "abc",
      "C0 控制符被剥离")
_h = client.get("/health")
check("C3 security-headers", _h.headers.get("X-Content-Type-Options") == "nosniff"
      and _h.headers.get("X-Frame-Options") == "DENY"
      and _h.headers.get("Referrer-Policy") == "no-referrer",
      "三个安全响应头齐全")
body = guard.error_body("TEST_CODE", "提示语", session_id="s_x")
check("C4 error-body-shape", body["ok"] is False and body["error_code"] == "TEST_CODE"
      and set(body["output"].keys()) == {"query_condition", "query_results", "info_basis", "usage_tips"}
      and body["output"]["usage_tips"] == ["提示语"],
      "与 ResponseEnvelope 同形状（前端统一读 output.*）")

# C5 错误信封的 mode 必须落在接口文档声明的枚举内。
# 回归背景：error_body 曾写死 "skeleton"——一个接口文档从未声明的占位值，
# 评审按赛题要求测「超长输入」时会直接读到它，属契约不一致。
_MODE_ENUM = {"agent", "chat", "need_key"}
check("C5 err-mode-in-enum", body["mode"] in _MODE_ENUM, f"mode={body['mode']!r}")

# ───────────────────────── D. llm_client：环境变量回落分支 ─────────────────────────
_ak = config.LLM_API_KEY
_mdl = config.LLM_MODEL
try:
    config.LLM_API_KEY = "sk-env-fallback-test"
    config.LLM_MODEL = "env-model-x"
    conn = llm_client.connection_info()
    check("D1 env-fallback", conn.get("configured") is True and conn.get("source") == "env"
          and conn.get("model") == "env-model-x", str(conn))
finally:
    config.LLM_API_KEY = _ak
    config.LLM_MODEL = _mdl
conn2 = llm_client.connection_info()
check("D2 env-restored", conn2.get("source") != "env", f"还原后 source={conn2.get('source')}")

# ───────────────────────── E. providers / keystore：损坏告警（fail-open 不静默） ─────────────────────────
_preset = providers._PRESET_PATH
_bad = _TMP / "providers.preset.corrupt.json"
_bad.write_text("{ 这不是合法 JSON", encoding="utf-8")
_records: list[logging.LogRecord] = []


class _Cap(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _records.append(record)


_hnd = _Cap()
logging.getLogger("opc.providers").addHandler(_hnd)
logging.getLogger("opc.providers").setLevel(logging.WARNING)
try:
    providers._PRESET_PATH = _bad
    providers.load_providers.cache_clear()
    check("E1 corrupt-preset-failopen", providers.load_providers() == {},
          "损坏仍返回空清单（不阻断启动）")
    check("E2 corrupt-preset-logged", any(r.levelno >= logging.WARNING for r in _records),
          f"告警 {len(_records)} 条（留痕可排查）")
finally:
    providers._PRESET_PATH = _preset
    providers.load_providers.cache_clear()
    logging.getLogger("opc.providers").removeHandler(_hnd)

# E3 密钥库损坏 → 空壳兜底 + 告警留痕（不抛异常、不崩溃）
keystore.STORE_PATH.write_text("{{{{坏 JSON", encoding="utf-8")
_records2: list[logging.LogRecord] = []


class _Cap2(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _records2.append(record)


_hnd2 = _Cap2()
logging.getLogger("opc.keystore").addHandler(_hnd2)
logging.getLogger("opc.keystore").setLevel(logging.WARNING)
try:
    st = keystore.load_store()
    check("E3 corrupt-keystore-failopen", st == {"providers": {}, "active_provider": None},
          "损坏按空库处理")
    check("E3 corrupt-keystore-logged", any(r.levelno >= logging.WARNING for r in _records2),
          "告警留痕（「密钥凭空消失」可排查）")
finally:
    logging.getLogger("opc.keystore").removeHandler(_hnd2)

# 收尾：把临时坏文件恢复成干净空库（本进程后续无依赖，防意外）
keystore.save_store({"providers": {}, "active_provider": None})

# ───────────────────────── F. 令牌闸（纵深防御）enforce 模式 ─────────────────────────
# config.API_TOKEN 取 import-time 环境变量，故 enforce 必须用「先设 OPC_API_TOKEN 再 import」的
# 子进程验证；本进程以默认演示模式运行（闸门放行，已在 C3/health 等用例隐式覆盖）。
# 三个敏感端点（/reset、/history、/api/keys/add）缺令牌必须 403；带正确令牌进入业务（/reset、
# /history 200，/api/keys/add 因未知供应商走业务校验 400，不联网）。钉死「拒绝分支返回真 403」，
# 防回归成「被异常处理器吞成 200/INTERNAL」的坏端点。
import subprocess as _subprocess  # noqa: E402
import os as _os  # noqa: E402

_enforce_script = _TMP / "enforce_gate_check.py"
_enforce_lines = [
    "import sys, json",
    "from pathlib import Path",
    "sys.path.insert(0, %s)" % json.dumps(BASE),
    "from fastapi.testclient import TestClient",
    "import src.server as S",
    "c = TestClient(S.app)",
    "r1 = c.post('/reset', json={'session_id':'x'}).status_code",
    "r2 = c.get('/history', params={'session_id':'x'}).status_code",
    "r3 = c.post('/reset', json={'session_id':'x'}, headers={'X-OPC-Token':'secret123'}).status_code",
    "r4 = c.get('/history', params={'session_id':'x'}, headers={'X-OPC-Token':'secret123'}).status_code",
    "r5 = c.post('/api/keys/add', json={'provider_id':'does_not_exist','api_key':'x'}).status_code",
    "r6 = c.post('/api/keys/add', json={'provider_id':'does_not_exist','api_key':'x'}, headers={'X-OPC-Token':'secret123'}).status_code",
    "print(json.dumps({'reset_no':r1,'history_no':r2,'reset_ok':r3,'history_ok':r4,'keys_no':r5,'keys_ok':r6}))",
]
_enforce_script.write_text("\n".join(_enforce_lines), encoding="utf-8")
_env = dict(_os.environ)
_env["OPC_API_TOKEN"] = "secret123"
_try = _subprocess.run([sys.executable, str(_enforce_script)], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(BASE), env=_env, timeout=120)
_enf = None
if _try.returncode == 0:
    try:
        _enf = json.loads(_try.stdout.strip().splitlines()[-1])
    except Exception:
        _enf = None
check("F1 reset-gate-403", bool(_enf) and _enf["reset_no"] == 403, str(_enf))
check("F2 history-gate-403", bool(_enf) and _enf["history_no"] == 403, str(_enf))
check("F3 reset-gate-pass-200", bool(_enf) and _enf["reset_ok"] == 200, str(_enf))
check("F4 history-gate-pass-200", bool(_enf) and _enf["history_ok"] == 200, str(_enf))
check("F5 keys-gate-403", bool(_enf) and _enf["keys_no"] == 403, str(_enf))
check("F6 keys-gate-business-400", bool(_enf) and _enf["keys_ok"] == 400, str(_enf))

# F7 端到端：输入非法（超长）时的错误信封，mode 同样必须落在契约枚举内
_j7 = client.post("/chat", json={"message": "阿" * (config.MAX_MESSAGE_LEN + 50)}).json()
check("F7 err-mode-enum-e2e",
      _j7.get("mode") in _MODE_ENUM and _j7.get("error_code") == "TOO_LONG" and _j7.get("ok") is False,
      f"mode={_j7.get('mode')!r} code={_j7.get('error_code')!r}")

print(f"\n===== 运行保障与网关边界测试：通过 {passed} / 失败 {failed} =====")
if failed:
    sys.exit(1)
print("\n运行保障（进阶3）三态 + SSE 主路径 + 网关边界 + 损坏告警 全部锚定 ✅")
