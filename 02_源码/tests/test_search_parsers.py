# -*- coding: utf-8 -*-
"""检索层解析器 + 风控页识别 —— 离线确定性验证（不打网络）。

为什么要有这个测试（两段动因，都是实测事故）：
  ① 风控页事故：本机高频检索后被引擎风控，引擎返回 HTTP 200 但正文是
     「百度安全验证」「访问异常页面」。若把它当搜索结果解析，会产出 **编造来源**
     ——这正是赛题红线。故新增 ``_is_block_page``，用真实风控页样本离线验证。
  ② 措辞事故：360 对「昆明 抗蛇毒血清 医院」（分词串）命中 0，对
     「昆明哪家医院有抗蛇毒血清」（自然语句）命中 5。故解析器要能被确定性地测。

做法：把实测抓到的 **真实页面** 存成样本（tests/fixtures/），让解析器与风控页
识别在【离线、可复跑、不依赖网络与风控状态】的条件下被验证。这样无论交付后
引擎是否再次风控，本测试都能稳定给出结论——避免"测试时通、交付后不通"。

样本来源（2026-09-21 实测抓取）：
  - fixtures/sogou_sample.html        搜狗正常结果页（532KB，9 条）
  - fixtures/m_so360_sample.html      360移动版正常结果页（351KB，10 条，含原文直链）
  - fixtures/baidu_block_sample.html  百度风控页「百度安全验证」（1.4KB）
  - fixtures/so360_block_sample.html  360 风控页「访问异常页面」（9.8KB）
  - fixtures/sm_昆明_抗蛇毒血清_医院.html    神马正常结果页（抗蛇毒血清，约 380KB）
  - fixtures/sm_昆明_三甲医院_心血管内科.html  神马正常结果页（三甲心血管内科，约 500KB）

运行：激活 venv 后 python tests/test_search_parsers.py
"""
import ast
import importlib.util
import inspect
import os
import socket
import sys
import urllib.request
from pathlib import Path

BASE = str(Path(__file__).resolve().parent.parent)   # tests/ 的上一级 = 02_源码/
sys.path.insert(0, BASE)

from src import search  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
SOGOU_OK = FIX / "sogou_sample.html"
MSO360_OK = FIX / "m_so360_sample.html"
BAIDU_BLOCK = FIX / "baidu_block_sample.html"
SO360_BLOCK = FIX / "so360_block_sample.html"
SM_OK_1 = FIX / "sm_昆明_抗蛇毒血清_医院.html"
SM_OK_2 = FIX / "sm_昆明_三甲医院_心血管内科.html"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


class _BlockedTransport:
    """替身传输层：不发任何真实请求（离线测试用）。"""

    def handle_request(self, request):  # noqa: D102 - 覆盖基类接口
        raise RuntimeError("blocked-transport：离线测试不发真实请求")

    def close(self) -> None:  # noqa: D102 - 覆盖基类接口
        pass


def _record_httpx_clients(call) -> list[dict]:
    """执行 ``call()``，记录期间**每一次** ``httpx.Client(...)`` 构造的实参。

    三个设计决定，都对应一类**实测确认的假绿 / 假红**：

    ① **拦截点选 `httpx.Client.__init__`，不选 `httpx.get`。**
       只拦 `httpx.get` 会与"具体写法"耦合——把 `httpx.get` 存成模块级别名、或改成
       直接 `httpx.Client(...)` 发请求，都是**正确且更好的**重构，却会被误判失败（假红）。
       `httpx.get` / `httpx.stream` 等便捷函数内部**必然**构造一个 `Client`，
       所以 `Client.__init__` 是所有外呼路径的**收敛点**：写法怎么变都绕不开。

    ② **返回全部记录，不返回第一条。**
       只在首条上做断言时，函数里先发一发"带正确参数的诱饵请求"就能骗过断言
       （实测：诱饵带 `trust_env=False` 占住位置 0，真实调用没有该参数，断言仍全绿）。

    ③ **给每个客户端塞入"不发请求"的替身传输层**，保证离线、不打网络。
    """
    import httpx as _hx

    recs: list[dict] = []
    real_init = _hx.Client.__init__

    def _spy_init(self, *a, **kw):
        recs.append(dict(kw))
        if "transport" not in kw:
            kw["transport"] = _BlockedTransport()
        return real_init(self, *a, **kw)

    _hx.Client.__init__ = _spy_init
    try:
        call()
    except Exception:  # noqa: BLE001 - 被测函数会把外呼异常吞成空串
        pass
    finally:
        _hx.Client.__init__ = real_init
    return recs


def _outbound_is_direct(rec: dict) -> bool:
    """一次外呼构造是否【真直连】——**静态三相**（辅助判据）。

    ① `trust_env=False`：不读进程环境里的 HTTP(S)_PROXY。
    ② `proxy` 为假：没有**显式**指定代理。只核 ① 是不够的——显式写
       `proxy="http://127.0.0.1:1"`（一个必然连不上的代理）时 `trust_env` 仍是 False，
       断言照绿而外呼全废（实测确认）。
    ③ `mounts` 为假：没有按域名挂自定义传输/代理路由表。

    注：②③ 用**假值判断**（`not rec.get(...)`）而不是 `"proxy" not in rec`——
    `httpx.get(...)` 内部会把 `proxy=None` **显式**传给 `Client`，用 `not in` 会假红。

    **三相并不完备——实测确认的第四相：`transport=`。**
    传一个自己带代理的传输层时，三相**全绿**而请求仍然走代理。**枚举不完**（httpx 还
    可能有别的暗门），所以静态检查只当辅助；**主判据是下面的连通性活体探针**——它不枚举
    任何参数，凡是让请求绕道的写法，都会在"到底连不连得上"上暴露。
    """
    return (
        rec.get("trust_env") is False
        and not rec.get("proxy")
        and not rec.get("mounts")
    )


def _resolved_proxy_markers(client) -> list[str]:
    """读【已解析的传输层】里有没有代理痕迹——**环境无关判据**。

    为什么需要它：下面的连通性活体探针依赖"本环境对回环地址没有隐式放行"。如果有人
    设了 `NO_PROXY=127.0.0.1`，那么**连默认会走代理的客户端也能连上本机服务**，活体探针
    就恒真、失去鉴别力（实测确认）。此时改读传输层解析结果，就与环境设置无关了。

    实测取证（本机 httpx 0.28.1，直接构造四种客户端后读）：
      - `Client(trust_env=False)`（真直连）      → `_transport._pool` 是 ConnectionPool，
        无 `_proxy_url` 属性 → **无痕迹** ；
      - `Client()`（读环境代理）                → `_mounts` 里出现带死代理 URL 的条目
        （`get_environment_proxies()` 会把 http/https 前缀整段挂到代理上）→ **有痕迹** ；
      - `Client(transport=HTTPTransport(proxy=…))`（第四维）→ 该 transport 的 `_pool`
        是 HTTPProxy，**带 `_proxy_url`** → **有痕迹** ；
      - `Client(proxy=…)`（显式代理）            → `_mounts` 带代理 URL → **有痕迹** 。
    即：**四条路由维度里有代理痕迹的，都会在这里现形**——这正是它当兜底的资格。

    读的是 httpx 私有结构（`_transport._pool._proxy_url` 与各 `_mounts[...]` 同项）。
    **读不到就抛异常**，由调用方判 INCONCLUSIVE——**绝不静默当成"干净"**（那正是假绿）。
    """
    pool_seen = False
    markers: list[str] = []
    seen_ids: set[int] = set()
    cands = [getattr(client, "_transport", None)]
    cands += list((getattr(client, "_mounts", None) or {}).values())
    for obj in cands:
        if obj is None or id(obj) in seen_ids:
            continue
        seen_ids.add(id(obj))
        pool = getattr(obj, "_pool", None)
        if pool is None:
            continue
        pool_seen = True
        url = getattr(pool, "_proxy_url", None)
        if url is not None:
            markers.append(str(url))
    if not pool_seen:
        raise RuntimeError("读不到已解析传输层（httpx 内部结构可能变了）→ 无法判直连")
    return markers


def _live_verdict(direct_ok: bool, ctrl_env_ok: bool, ctrl_exp_ok: bool,
                  markers_fn) -> dict:
    """把「活体 + 对照 + 环境无关兜底」三件事合成为一个判定。

    参数
      direct_ok   被测对象能否在死代理环境下连上本机服务
      ctrl_env_ok **默认会读环境代理**的客户端能否连上（对照臂 1）
      ctrl_exp_ok **显式指向死代理**的客户端能否连上（对照臂 2）
      markers_fn  无参回调，返回"已解析传输层的代理痕迹"列表；读不到应抛异常

    判定规则（对照臂是**被断言的**，不是只打印）：

      - 两条对照臂**都连不上** → 本环境的"环境代理道"与"显式代理道"都会断 → 任何
        形式用上了代理的对象都必然连不上 → 活体判据**全维度有效** → 以 `direct_ok`
        定 PASS / FAIL。

      - 只要**任一条对照臂能连上** → 该道在本环境被放行（典型：`NO_PROXY=127.0.0.1`
        或 Windows 注册表 `ProxyOverride` 含 `<local>`）→ 活体判据在那道上**恒真、
        无鉴别力**（实测：读环境代理的坏客户端照样能连上回环）→ **不判绿**，改用
        环境无关的 `markers_fn`：无痕迹= PASS，有痕迹= FAIL，**读不到= INCONCLUSIVE**。

      ⚠️ 为什么必须"两条都断"才算有效，而不是"任一条断"：只有"环境代理道"被放行而
      "显式代理道"正常断掉时，一个**没写 `trust_env=False`** 的坏实现照样能连上本机
      服务——若此时按 `direct_ok` 判，就会给出**假绿**（这正是实测确认的退化场景）。
    """
    live_conclusive = (not ctrl_env_ok) and (not ctrl_exp_ok)
    base = (f"直连={direct_ok}｜对照(默认读环境代理)={ctrl_env_ok}｜"
            f"对照(显式死代理)={ctrl_exp_ok}")
    if live_conclusive:
        return {
            "verdict": "PASS" if direct_ok else "FAIL",
            "live_conclusive": True,
            "note": f"{base} → 两条对照臂都被代理断掉，活体判据全维度有效，以连通性定论",
        }
    try:
        markers = markers_fn()
    except Exception as e:  # noqa: BLE001
        return {
            "verdict": "INCONCLUSIVE",
            "live_conclusive": False,
            "note": (f"{base} → 有对照臂能连上（本环境对该道代理放行），活体判据无鉴别力；"
                     f"且读不到传输层代理痕迹（{type(e).__name__}: {e}）→ 本环境无法取证"),
        }
    return {
        "verdict": "PASS" if not markers else "FAIL",
        "live_conclusive": False,
        "note": (f"{base} → 活体判据无鉴别力（有对照臂能连上）；"
                 f"改读已解析传输层的代理痕迹={markers}"),
    }


def _verdict_ok(v: dict) -> bool:
    """只有 PASS 算通过。INCONCLUSIVE 明确**不算绿**。"""
    return v["verdict"] == "PASS"


def _under_dead_proxy(handler_factory):
    """上下文：把进程环境代理指向**必然连不上的死地址**，并起一个本机 HTTP 服务。

    用法：``with _under_dead_proxy(lambda: MyHandler) as base_url:``
    在块内，任何**真直连**的客户端都能访问 `base_url`；任何**读了环境代理/显式代理**
    的客户端必然连不上——于是"能不能连上"成为"是否真直连"的活体判据，
    比读任何属性都硬（属性可以被设成想要的值，连通性不能）。
    """
    import contextlib
    import http.server
    import threading

    @contextlib.contextmanager
    def _cm():
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory())
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        keys = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
        saved = {k: os.environ.get(k) for k in keys}
        for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            os.environ[k] = "http://127.0.0.1:1"  # 必然连不上的死代理
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        try:
            yield base_url
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            srv.shutdown()
            srv.server_close()

    return _cm()


def _health_handler():
    """返回一个只应答固定 JSON 的 HTTP handler 类（供活体探针当靶子）。"""
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - 基类接口名
            body = b'{"api_version":"1.0","search_backends":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: D102 - 静音
            pass

    return _Handler


def _client_reaches_localhost_under_dead_proxy(make_client) -> dict:
    """活体判据（**收敛式，主判据**）：死代理下，``make_client()`` 造的客户端能否连上本机服务。

    为什么它能覆盖静态三相覆盖不了的第四相：它**不枚举任何参数**。只要最终请求真的
    经过了任何代理（环境变量来的、显式指定的、或藏在自定义 `transport=` 里的），请求就会
    被带向死地址而失败。属性可以被设成想要的值，**连通性不能**。

    带**两条被断言的对照臂**（见 `_live_verdict`）：两条都连不上才算活体判据全维度有效；
    只要**任一条能连上**，说明本环境对该道代理放行、活体判据恒真无鉴别力，此时**不判绿**，
    回落读已解析传输层的代理痕迹；痕迹也读不到则判 INCONCLUSIVE。
    """
    import httpx as _hx

    def _try(c):
        try:
            with c() as cli:
                r = cli.get(base_url + "/health", timeout=3.0)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    with _under_dead_proxy(_health_handler) as base_url:
        direct_ok = _try(make_client)
        ctrl_env_ok = _try(lambda: _hx.Client(timeout=3.0))
        ctrl_exp_ok = _try(lambda: _hx.Client(timeout=3.0, proxy="http://127.0.0.1:1"))

        def _markers():
            """兜底判据：读被测工厂真实造出的客户端里已解析的代理痕迹。

            ⚠️ 在**同一个死代理环境块内**读（故此处内联定义、就地调用）——否则环境已被
            还原成"本机真实代理"，读到的痕迹随机器而变、不可复现。
            """
            with make_client() as c:
                return _resolved_proxy_markers(c)

        return _live_verdict(direct_ok, ctrl_env_ok, ctrl_exp_ok, _markers)


def _search_reaches_localhost_under_dead_proxy(search_mod) -> dict:
    """对**检索层真实外呼路径**做同样的连通性取证（主判据）。

    为什么不能只靠"记录 `Client` 构造参数"那套静态检查：`transport=` 这类第四相是
    枚举不出来的（实测假绿）。这里直接让 `search._get(...)` 去打本机服务——
    它真直连就能拿回正文；走了任何代理就拿不回（`_get` 会把外呼异常吞成空串）。
    **不枚举参数，也就无法作假。**
    """
    import httpx as _hx

    def _try_raw(fn, url):
        try:
            return fn(url).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    with _under_dead_proxy(_health_handler) as base_url:
        direct_ok = bool(search_mod._get(base_url + "/health", {}, 3.0))
        ctrl_env_ok = _try_raw(lambda u: _hx.get(u, timeout=3.0), base_url + "/health")
        ctrl_exp_ok = _try_raw(
            lambda u: _hx.get(u, timeout=3.0, proxy="http://127.0.0.1:1"), base_url + "/health")

    def _markers():
        """回落判据：读**检索层自己构造的**客户端里已解析的代理痕迹。

        两个要点，都是实测踩出来的：

        ① 不能新造一个干净客户端来读（那样必然为空、等于恒真）。这里重跑一次检索外呼，
           拦截 `Client.__init__`，在**构造完成的当下**（池子已解析、尚未 close）读痕迹。

        ② ⚠️ **不能给它塞替身传输层**。`Client(transport=...)` 会**整个跳过**环境代理
           解析（`_mounts` 直接为空）→ 痕迹被抹掉 → 兜底判据恒真。而这条兜底恰恰是在
           "环境代理道被放行"时启用的，此时要看的正是"它有没有把环境代理解析进 `_mounts`"。
           不塞替身也不会打外网：真直连时打的是本机靶子服务，用了代理时打的是死代理。
        """
        marks: list[str] = []
        read_err: list[Exception] = []
        real_init = _hx.Client.__init__

        def _spy(self, *a, **kw):
            real_init(self, *a, **kw)
            try:
                marks.extend(_resolved_proxy_markers(self))
            except Exception as e:  # noqa: BLE001
                read_err.append(e)

        _hx.Client.__init__ = _spy
        try:
            with _under_dead_proxy(_health_handler) as b2:
                search_mod._get(b2 + "/health", {}, 3.0)
        finally:
            _hx.Client.__init__ = real_init
        if read_err and not marks:
            raise read_err[0]          # 读不到 → 让上层判 INCONCLUSIVE
        return marks

    return _live_verdict(direct_ok, ctrl_env_ok, ctrl_exp_ok, _markers)


def _localhost_reach_under_dead_proxy(launcher_mod) -> dict:
    """启动器直连 opener 的活体取证（含**两条被断言的对照臂**）。

    为什么必须活体探针，而不是查 handler **类名**：类名天然可绕——实测用一个自定义
    `ProxyHandler` **子类**（类名不含 "ProxyHandler"），`"ProxyHandler" not in handlers`
    仍为真、断言照绿，但它的 `proxies` 非空、请求真的被带去死代理、`_health_probe`
    返回 None（旧故障完整复现）。活体探针用"能不能连通"说话，无法作假。

    **对照臂（关键）**：光有"默认 opener 应当失败"这一个观察点不足——它只打印不判定，
    在"本机对回环地址有隐式放行"的环境里，活体判据会恒真、失去鉴别力（实测确认）。
    故这里加两条对照臂，且**由 `_live_verdict` 统一判定**：两条对照臂**都连不上**，才说明
    本环境的代理道全部有效、活体判据覆盖全维度；只要**任一条能连上**就说明本环境对该道
    放行 → 不判绿，回落读直连 opener 的代理表（环境无关判据）。
    """
    def _try(opener):
        try:
            with opener.open(target, timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _markers():
        """环境无关兜底：直连 opener 里**任何**带非空代理表的 handler 都算痕迹。"""
        marks = [f"{h.__class__.__name__}={getattr(h, 'proxies', None)}"
                 for h in launcher_mod._direct_opener().handlers
                 if getattr(h, "proxies", None)]
        if not launcher_mod._direct_opener().handlers:
            raise RuntimeError("直连 opener 没有任何 handler → 无法判直连")
        return marks

    with _under_dead_proxy(_health_handler) as base_url:
        target = base_url + "/health"
        direct_ok = _try(launcher_mod._direct_opener())
        ctrl_env_ok = _try(urllib.request.build_opener())          # 默认会读环境代理
        ctrl_exp_ok = _try(urllib.request.build_opener(           # 显式指向死代理
            urllib.request.ProxyHandler({"http": "http://127.0.0.1:1"})))

    return _live_verdict(direct_ok, ctrl_env_ok, ctrl_exp_ok, _markers)


def _load_launcher():
    """加载项目根的启动器（文件名是中文，故按路径加载）；不存在返回 None。

    说明：`启动项目.py` 只在 `__main__` 下才会真正起服务，import 它没有副作用。
    """
    path = Path(__file__).resolve().parents[2] / "启动项目.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("opc_launcher_under_test", str(path))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=" * 72)
    print("检索层解析器 + 风控页识别（离线确定性）")
    print("=" * 72)

    # 0) 样本齐备性
    print("\n§0 样本齐备性")
    for p in (SOGOU_OK, MSO360_OK, BAIDU_BLOCK, SO360_BLOCK, SM_OK_1, SM_OK_2):
        check(f"样本存在 {p.name}", p.is_file(), f"大小 {p.stat().st_size if p.is_file() else 0}")
        if not p.is_file():
            print("\n样本缺失，无法继续（请重跑采集脚本或检查 fixtures/ 是否被排除）")
            return 1

    sogou_html = _read(SOGOU_OK)
    mso360_html = _read(MSO360_OK)
    baidu_html = _read(BAIDU_BLOCK)
    so360_html = _read(SO360_BLOCK)

    # 1) 风控页识别（红线相关）
    print("\n§1 风控页识别（防「把验证页当来源」）")
    check("百度风控页 判定为风控页", search._is_block_page(baidu_html) is True,
          f"title={search._title_of(baidu_html)!r}")
    check("360风控页 判定为风控页", search._is_block_page(so360_html) is True,
          f"title={search._title_of(so360_html)!r}")
    check("搜狗正常页 不误判为风控页", search._is_block_page(sogou_html) is False,
          f"title={search._title_of(sogou_html)!r}")
    check("360移动版正常页 不误判为风控页", search._is_block_page(mso360_html) is False,
          f"title={search._title_of(mso360_html)!r}")
    check("空串 不判为风控页", search._is_block_page("") is False)
    # 关键设计性质：只查 <title>，不扫正文 —— 正文里出现风控字样不影响判定
    # （正常新闻正文出现「安全验证」是常见的，扫正文会大面积误杀正常结果）
    fake_news = (
        "<html><head><title>昆明哪家医院有抗蛇毒血清 - 本地资讯</title></head>"
        "<body>本文说明：预约挂号需通过安全验证，若出现验证码或人机验证请重试。</body></html>"
    )
    check("正文含风控字样、标题正常 -> 不误判", search._is_block_page(fake_news) is False,
          f"title={search._title_of(fake_news)!r}")
    # 有意为之的取舍：标题命中风控字样即判风控页。
    # 代价不对称：漏判 → 把验证页当搜索结果 → 产出编造来源（违反约束，不可接受）；
    #             误判 → 只是多切一个后端/多一次降级（无害）。
    check("标题命中风控字样即判风控（宁严勿漏，代价不对称）",
          search._is_block_page("<html><head><title>某医院安全验证通道</title></head></html>") is True)

    # 2) 搜狗解析器（真实页面）
    print("\n§2 搜狗解析器（真实结果页）")
    hits = search._parse_sogou(sogou_html, max_results=8)
    check(f"解析出结果数 >= 3（实际 {len(hits)}）", len(hits) >= 3)
    if hits:
        first = hits[0]
        check("结果含 title/url/snippet 三字段",
              all(k in first for k in ("title", "url", "snippet")),
              f"keys={sorted(first)}")
        check("全部结果 url 都是 http(s) 绝对地址",
              all(h["url"].startswith("http") for h in hits),
              str([h["url"] for h in hits][:3]))
        check("全部结果 url 都不是引擎自身域名（来源可核验）",
              all(not search._is_engine_url(h["url"]) for h in hits),
              str([h["url"] for h in hits][:3]))
        check("全部结果标题非空且非引擎模块噪音",
              all(h["title"] and not any(n in h["title"] for n in search._NOISE_TITLES) for h in hits))
        check("至少一条带摘要", any(h["snippet"] for h in hits))
        check("标题未残留 HTML 标签",
              all("<" not in h["title"] for h in hits))
        check("同一条结果 url 与 title 不串位（串位会误报来源）",
              all(not h["title"].startswith("http") for h in hits))
        print("      样本解析结果预览：")
        for h in hits[:3]:
            print(f"        - {h['title'][:34]} | {h['url'][:52]}")
    check("max_results 生效（限 2 条）", len(search._parse_sogou(sogou_html, 2)) <= 2)

    # 2b) 360 移动版解析器（真实页面）——它给的是【文章级】原文直链，粒度与桌面版同级
    print("\n§2b 360移动版解析器（真实结果页，文章级来源）")
    mh = search._parse_mso360(mso360_html, max_results=8)
    check(f"解析出结果数 >= 3（实际 {len(mh)}）", len(mh) >= 3)
    if mh:
        first = mh[0]
        check("结果含 title/url/snippet 三字段",
              all(k in first for k in ("title", "url", "snippet")), f"keys={sorted(first)}")
        check("全部结果 url 都是 http(s) 绝对地址",
              all(h["url"].startswith("http") for h in mh),
              str([h["url"] for h in mh][:3]))
        check("全部结果 url 都不是引擎自身域名（来源可核验）",
              all(not search._is_engine_url(h["url"]) for h in mh),
              str([h["url"] for h in mh][:3]))
        # 关键：移动版必须给出 **文章级** 直链（不是 m.so.com/jump 跳转链、不是模块卡）
        check("URL 不含跳转链特征（m.so.com/jump 会被误当来源）",
              all("so.com/jump" not in h["url"] for h in mh),
              str([h["url"] for h in mh][:3]))
        check("至少一条为文章级深链接（含路径段，非裸域名）",
              any(len([p for p in h["url"].split("/")[3:] if p]) >= 1 for h in mh),
              str([h["url"] for h in mh][:3]))
        check("全部结果标题非空且非引擎模块噪音",
              all(h["title"] and not any(n in h["title"] for n in search._NOISE_TITLES) for h in mh))
        check("标题未残留 HTML 标签", all("<" not in h["title"] for h in mh))
        check("至少一条带发布日期", any(h.get("date") for h in mh))
        print("      样本解析结果预览：")
        for h in mh[:3]:
            print(f"        - {h['title'][:34]} | {h['url'][:56]} | {h.get('date','')}")
    check("max_results 生效（限 2 条）", len(search._parse_mso360(mso360_html, 2)) <= 2)

    # 2c) 神马解析器（真实页面）—— 默认第 4 后端，治理同族伪冗余
    print("\n§2c 神马解析器（真实结果页，独立源）")
    sm_html_1 = _read(SM_OK_1)
    sm_html_2 = _read(SM_OK_2)
    sm1 = search._parse_sm(sm_html_1, max_results=8)
    sm2 = search._parse_sm(sm_html_2, max_results=8)
    check(f"解析出结果数 >= 3（抗蛇毒血清，实际 {len(sm1)}）", len(sm1) >= 3)
    check(f"解析出结果数 >= 3（三甲心血管内科，实际 {len(sm2)}）", len(sm2) >= 3)
    for tag, sm in (("抗蛇毒血清", sm1), ("三甲心血管内科", sm2)):
        if sm:
            first = sm[0]
            check(f"[{tag}] 结果含 title/url/snippet 三字段",
                  all(k in first for k in ("title", "url", "snippet")), f"keys={sorted(first)}")
            check(f"[{tag}] 全部结果 url 都是 http(s) 绝对地址",
                  all(h["url"].startswith("http") for h in sm), str([h["url"] for h in sm][:3]))
            check(f"[{tag}] 全部结果 url 都不是引擎自身域名（来源可核验）",
                  all(not search._is_engine_url(h["url"]) for h in sm), str([h["url"] for h in sm][:3]))
            check(f"[{tag}] 全部结果标题非空且非引擎模块噪音",
                  all(h["title"] and not any(n in h["title"] for n in search._NOISE_TITLES) for h in sm))
            check(f"[{tag}] 标题未残留 HTML 标签", all("<" not in h["title"] for h in sm))
            check(f"[{tag}] 同一条结果 url 与 title 不串位",
                  all(not h["title"].startswith("http") for h in sm))
            print("      样本解析结果预览：")
            for h in sm[:3]:
                print(f"        - {h['title'][:34]} | {h['url'][:52]}")
    # 关键独立性证据：两个**不同**查询解析出**不同**的标题集合（证明不是返回同一页）
    t1 = {h["title"] for h in sm1}
    t2 = {h["title"] for h in sm2}
    check("两查询返回的结果**随查询变化**（独立性证据，非固定页）",
          len(t1 & t2) < min(len(t1), len(t2)), f"交集 {len(t1 & t2)} 条")
    check("max_results 生效（限 2 条）", len(search._parse_sm(sm_html_1, 2)) <= 2)

    # 3) 交叉不串页（某引擎页面喂给另一个引擎的解析器应为空）
    print("\n§3 交叉不串页（页面类型与解析器必须匹配）")
    check("360 解析器吃搜狗页 -> 0 条", search._parse_so360(sogou_html, 8) == [])
    check("ddg 解析器吃搜狗页 -> 0 条", search._parse_ddg(sogou_html, 8) == [])
    check("搜狗解析器吃 360 风控页 -> 0 条", search._parse_sogou(so360_html, 8) == [])
    check("移动版解析器吃搜狗页 -> 0 条", search._parse_mso360(sogou_html, 8) == [])
    check("桌面版解析器吃移动版页 -> 0 条", search._parse_so360(mso360_html, 8) == [])
    check("搜狗解析器吃移动版页 -> 0 条", search._parse_sogou(mso360_html, 8) == [])
    check("移动版解析器吃 360 风控页 -> 0 条", search._parse_mso360(so360_html, 8) == [])
    check("神马解析器吃百度风控页 -> 0 条（非神马页不误产结果）",
          search._parse_sm(baidu_html, 8) == [])

    # 4) 健壮性（异常输入不抛异常）
    print("\n§4 健壮性（异常输入不抛异常、返回空）")
    for bad in ("", "not html at all", "<html></html>", "<h3></h3>"):
        try:
            r1 = search._parse_sogou(bad, 5)
            r2 = search._parse_so360(bad, 5)
            r3 = search._parse_ddg(bad, 5)
            r4 = search._parse_mso360(bad, 5)
            r5 = search._parse_sm(bad, 5)
            check(f"异常输入不崩 {bad[:18]!r}",
                  r1 == [] and r2 == [] and r3 == [] and r4 == [] and r5 == [],
                  f"sogou={len(r1)} so360={len(r2)} ddg={len(r3)} mso360={len(r4)} sm={len(r5)}")
        except Exception as e:  # noqa: BLE001
            check(f"异常输入不崩 {bad[:18]!r}", False, f"{type(e).__name__}: {e}")

    # 5) 后端注册与默认顺序
    print("\n§5 后端注册与默认顺序")
    check("搜狗已注册进 _BACKENDS", "sogou" in search._BACKENDS)
    check("so360/ddg 仍在", "so360" in search._BACKENDS and "ddg" in search._BACKENDS)
    check("360移动版(mso360) 已注册进 _BACKENDS", "mso360" in search._BACKENDS)
    from src import config
    check(f"默认后端含 sogou（当前 {config.SEARCH_BACKENDS}）", "sogou" in config.SEARCH_BACKENDS)
    # ── 直连纪律（三处同源）：外呼必须忽略进程环境里的 HTTP(S)_PROXY ──
    # 动因：本机残留代理或 VPN 出口时，请求被带到非预期出口 —— 检索会被引擎当
    # 机器流量风控（「HTTP 200 却是验证页」）；模型调用会超时/连接失败；启动器的就绪探测
    # 会误判「后端启动超时」，导致【双击后浏览器根本打不开】。
    # ⚠️ 断言一律**行为级**（真实调用 / 真实构造后读属性 / 活体探针）。源码文本或语法树
    #    级检查已被实测证明存在多类「假绿」：注释里留字样、能通过的死代码分支、
    #    同名遮蔽、ProxyHandler 子类、getattr/别名间接调用 —— 都会让断言绿而功能坏。
    # 辅助判据：静态三相。**明确标注不完备**——实测确认漏第四相 `transport=`，
    # 故它只用于"外呼确实发生了、且没在显式维度上写歪"，主判据是下面的连通性活体探针。
    _search_recs = _record_httpx_clients(
        lambda: search._get("http://127.0.0.1:1/", {"q": "x"}, 0.5))
    _bad_recs = [r for r in _search_recs if not _outbound_is_direct(r)]
    check("检索层外呼在【静态三相】上合规（辅助判据，不完备：漏 transport=；"
          "真实调用 _get、拦截 Client 构造点、核全部外呼的 trust_env=False 且无 proxy/mounts）",
          bool(_search_recs) and not _bad_recs,
          f"外呼次数={len(_search_recs)}，不合规记录={_bad_recs}")
    # 主判据（收敛式活体探针）：不枚举任何参数，直接看检索层真实外呼**能不能连上本机服务**。
    # 这一条覆盖静态三相覆盖不了的第四相 `transport=`：实测 `trust_env=False` +
    # `transport=HTTPTransport(proxy=死代理)` 时三相全绿而请求走死代理 —— 只有连通性能拆穿。
    _sv = _search_reaches_localhost_under_dead_proxy(search)
    check("检索层外呼真直连（收敛式活体探针：死代理下 _get 仍能取回本机服务正文；"
          "不枚举参数，故 transport= 等暗门一并覆盖）",
          _verdict_ok(_sv), _sv["note"])
    from src import llm_client
    _hc = llm_client._direct_http_client(1.0)
    try:
        check("直连工厂造出的客户端已忽略环境代理（真实构造后读 trust_env）",
              _hc.trust_env is False, f"trust_env={_hc.trust_env!r}")
        _hc_mounts = getattr(_hc, "_mounts", None)
        _hc_proxy = getattr(_hc, "_transport_proxies", None)
        check("直连工厂造出的客户端无自定义路由表 / 无显式代理取值",
              not _hc_mounts and not _hc_proxy,
              f"_mounts={_hc_mounts!r}｜_transport_proxies={_hc_proxy!r}")
    finally:
        _hc.close()
    # 活体探针（最直接判据）：死代理环境下，直连工厂造的客户端必须仍能连上本机服务。
    # 这一条同时覆盖 trust_env 与**显式 proxy** 两个维度——只核 trust_env 时，
    # 显式写 proxy="http://127.0.0.1:1" 仍会让断言变绿而外呼全废（实测确认的假绿）；
    # 而"能不能连通"骗不了：只要真用了任何代理，请求必被带向死地址而失败。
    # ⚠️ 探针自带**两条被断言的对照臂**：本机若对回环地址整体放行（`NO_PROXY=127.0.0.1`
    #    或 Windows 注册表 `ProxyOverride` 含 `<local>`），活体判据会恒真、失去鉴别力。
    #    此时**不判绿**，回落读已解析传输层的代理痕迹；连痕迹都读不到就判 INCONCLUSIVE。
    _llm_v = _client_reaches_localhost_under_dead_proxy(
        lambda: llm_client._direct_http_client(3.0))
    check("死代理下直连工厂的客户端仍能连上本机服务（活体探针＋对照臂三态判定；"
          "覆盖显式 proxy 与 transport= 等所有会让请求绕道的写法）",
          _verdict_ok(_llm_v), _llm_v["note"])
    # build_client：**真实构造**并读它底层 httpx 客户端的 trust_env。
    # 只查「有没有 http_client= 这个关键字」不够——传一个会读代理的客户端、或在函数内
    # 局部重定义同名工厂，都能骗过静态检查（均已实测为假绿）。故喂一个假连接、真造一次。
    _orig_resolve = llm_client._resolve_connection
    llm_client._resolve_connection = lambda: {
        "base_url": "https://example.invalid/v1",
        "api_key": "DUMMY-PLACEHOLDER-KEY-OFFLINE-TESTS-ONLY",
        "model": "offline-test-model",
        "source": "test",
    }
    try:
        _bc = llm_client.build_client()
    finally:
        llm_client._resolve_connection = _orig_resolve
    try:
        _bc_trust = getattr(getattr(_bc, "_client", None), "trust_env", "<无底层 _client>")
        check("build_client 造出的客户端已忽略环境代理（真造一次后读底层 httpx 的 trust_env）",
              _bc is not None and _bc_trust is False,
              f"build_client()={_bc!r}｜底层 trust_env={_bc_trust!r}")
    finally:
        if _bc is not None:
            try:
                _bc.close()
            except Exception:  # noqa: BLE001
                pass
    # test_connection：把 OpenAI 换成只记录构造参数的替身，再读它收到的 http_client。
    _captured: dict = {}

    class _StubOpenAI:  # noqa: D401 - 只用于抓构造参数
        def __init__(self, **kwargs):
            _captured.clear()
            _captured.update(kwargs)
            self.chat = None  # 后续访问会抛异常，被 test_connection 自身捕获

    _orig_cls = llm_client.OpenAI
    llm_client.OpenAI = _StubOpenAI
    try:
        llm_client.test_connection(
            "DUMMY-PLACEHOLDER-KEY-OFFLINE-TESTS-ONLY",
            "https://example.invalid/v1", "offline-test-model",
        )
    finally:
        llm_client.OpenAI = _orig_cls
    _hc2 = _captured.get("http_client")
    try:
        check("密钥校验调用注入的 http 客户端已忽略环境代理（读真实构造参数的 trust_env）",
              _hc2 is not None and getattr(_hc2, "trust_env", None) is False,
              f"http_client={_hc2!r}")
    finally:
        if _hc2 is not None:
            try:
                _hc2.close()
            except Exception:  # noqa: BLE001
                pass
    # ── 启动器（交付根 启动项目.py）：就绪探测直连 + 端口自适应 —— 全部行为级取证 ──
    _lm = _load_launcher()
    if _lm is None:
        check("启动项目.py 存在（交付根启动器）", False, "未找到 启动项目.py")
    else:
        # ① 活体探针：把环境代理指向死地址，直连 opener 仍须能连上本机服务。
        #    （此前查 handler 类名，被一个自定义 ProxyHandler 子类绕过且就绪探测真失效。）
        #    ⚠️ 对照臂**由 `_live_verdict` 统一判定**，不再"只 print 不判"：两条对照臂都
        #       连得上时活体判据无鉴别力 → 不判绿，回落读直连 opener 的代理表。
        _lm_v = _localhost_reach_under_dead_proxy(_lm)
        check("死代理下启动器直连 opener 仍能连上本机服务（活体探针＋对照臂三态判定）",
              _verdict_ok(_lm_v), _lm_v["note"])
        # ①bis 显式声明：本环境若对回环地址放行（有对照臂能连上），上面的活体判据会退化；
        #      此时判定已自动切到"读代理痕迹"这条环境无关判据，这里如实打印便于复核。
        print(f"      本机活体判据鉴别力："
              f"{'全维度有效' if _lm_v['live_conclusive'] else '无（有对照臂能连上）'}"
              f"；判定={_lm_v['verdict']}")
        # ② 静态补防：任何带代理配置的 handler，其代理表必须为空（含自定义子类）
        _bad_proxies = [(h.__class__.__name__, getattr(h, "proxies", None))
                        for h in _lm._direct_opener().handlers
                        if getattr(h, "proxies", None)]
        check("直连 opener 不含任何非空代理配置（含自定义 handler 子类）",
              not _bad_proxies, repr(_bad_proxies))
        # ③ 端口自适应：占用方**也设 SO_REUSEADDR**——这是唯一能抓出「本函数自己设了 REUSE
        #    就会把被占端口误判为空闲」的场景（占用方不设时抓不到，已实测确认）。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
            _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 关键：占用方也设
            _sock.bind((_lm.HOST, 0))
            _occupied = _sock.getsockname()[1]
            _sock.listen(1)
            check(f"在监听中的端口 {_occupied}（占用方亦设 REUSE）被判为「占用」",
                  _lm._port_free(_occupied) is False,
                  "_port_free 返回了 True —— 多半是它自己设了 REUSE 类选项（Windows 上会失真）")
            _saved_default = _lm.DEFAULT_PORT
            _lm.DEFAULT_PORT = _occupied
            try:
                _shifted, _reused = _lm.resolve_port()
            finally:
                _lm.DEFAULT_PORT = _saved_default
            check(f"默认端口被别的程序占用时自动顺延（{_occupied} -> {_shifted}）",
                  _shifted is not None and _shifted > _occupied and _reused is False,
                  f"got ({_shifted!r}, {_reused!r})")
        # ④ 第二道静态防线：_port_free 不得直接调用 setsockopt（间接/别名调用由 ③ 行为断言兜）
        _pf_nodes = [n for n in ast.walk(ast.parse(inspect.getsource(_lm._port_free)))
                     if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "setsockopt"]
        check("_port_free 未调用 setsockopt（即未设 REUSE 类选项，避免 Windows 判断失真）",
              not _pf_nodes, f"发现 {len(_pf_nodes)} 处 setsockopt 调用")
        # ⑤ 报错里承诺的 OPC_PORT 必须真的被启动器读取（否则提示是死路）
        _env_backup = os.environ.get("OPC_PORT")
        os.environ["OPC_PORT"] = "19000"
        try:
            _env_port = _lm._env_port()
        finally:
            if _env_backup is None:
                os.environ.pop("OPC_PORT", None)
            else:
                os.environ["OPC_PORT"] = _env_backup
        check("启动器真的读取 OPC_PORT（否则报错里「set OPC_PORT=9000」是死路）",
              _env_port == 19000, f"_env_port()={_env_port!r}（设了 OPC_PORT=19000）")
        # ⑤ 收紧：原断言只要求"是合法 int 或 None"，等于什么都没约束（任何端口号都过）。
        #    真正要守的是**不变量**：返回的端口要么是"复用本项目已有实例"（already=True），
        #    要么必须**真的空闲**（能绑定）。一个既非复用、又绑不上的端口 = 旧版"静默回落
        #    到被占端口"的缺陷原形，必须判失败。
        _now_port, _now_reused = _lm.resolve_port()
        check("resolve_port 不返回「既非复用、又绑不上」的端口（不变量：复用 ∨ 真空闲）",
              _now_port is None or _now_reused or _lm._port_free(_now_port),
              f"got ({_now_port!r}, reused={_now_reused!r})")
    check("默认第一后端是 so360（文章级 URL 最优）",
          config.SEARCH_BACKENDS[0] == "so360", str(config.SEARCH_BACKENDS))
    # 抗限流冗余：so360 与 mso360 必须靠前挨着，才能在桌面版被风控时立刻切到移动版；
    # 两者都在 sogou（域名级来源）之前——保证优先拿到粒度更好的文章级来源。
    check("mso360 紧跟 so360 之后（抗限流冗余）",
          config.SEARCH_BACKENDS[:2] == ["so360", "mso360"], str(config.SEARCH_BACKENDS))
    check("文章级后端(so360/mso360) 均排在域名级(sogou) 之前",
          max(config.SEARCH_BACKENDS.index(b) for b in ("so360", "mso360")
              if b in config.SEARCH_BACKENDS)
          < (config.SEARCH_BACKENDS.index("sogou") if "sogou" in config.SEARCH_BACKENDS else 99),
          str(config.SEARCH_BACKENDS))
    # ddg 默认不启用——实测动因（2026-09-21）：它在国内网络不可达，每次外呼白等满超时；
    #   而检索是「后端 × 变体」双层循环，仅一个 ddg 就让单次查询多耗 ~24 秒，
    #   端到端实测被拖到 62.9 秒（用户会误判成"卡死"）。故移出默认列表，仅保留注册。
    check("默认后端已剔除 ddg（国内不可达，避免白等超时）",
          "ddg" not in config.SEARCH_BACKENDS, str(config.SEARCH_BACKENDS))
    check("ddg 仍保留在 _BACKENDS 中（可按需显式加回）", "ddg" in search._BACKENDS)
    # 神马(sm)：独立源，治理 so360/mso360/sogou 同族伪冗余，作为第 4 默认后端。
    check("神马(sm) 已注册进 _BACKENDS（独立源，治理伪冗余）", "sm" in search._BACKENDS)
    check("神马(sm) 在默认后端中（第 4 后端）",
          "sm" in config.SEARCH_BACKENDS, str(config.SEARCH_BACKENDS))
    # 必应国内版：解析器就绪但本机实测不稳定（机器人识别返回空/降级页），故默认不启用；
    # 仅保留注册，供 bing 可达环境用 OPC_SEARCH_BACKENDS 显式加回。
    check("必应(bing) 已从默认后端剔除（本机实测不稳定）",
          "bing" not in config.SEARCH_BACKENDS, str(config.SEARCH_BACKENDS))
    check("必应(bing) 仍保留在 _BACKENDS 中（可达环境可加回）", "bing" in search._BACKENDS)
    check("检索总耗时预算已配置且为正",
          float(getattr(config, "SEARCH_BUDGET_SECONDS", 0)) > 0,
          str(getattr(config, "SEARCH_BUDGET_SECONDS", None)))

    # 6) 缓存/节流配置可见
    print("\n§6 缓存与节流（降低触发风控概率）")
    st = search.cache_stats()
    check("缓存 TTL 为正", st.get("ttl_seconds", 0) > 0, str(st))
    check("出站最小间隔为正", st.get("min_interval", 0) > 0, str(st))

    # 6b) 检索总耗时预算：**真跑一次**（注入两个"慢后端"），验证预算确实截断外呼
    #     ——不是只断言配置存在，而是断言行为：慢后端不会把响应拖到无上界。
    print("\n§6b 检索总耗时预算（防止慢后端把响应拖到分钟级）")
    import time as _time

    _calls: list = []

    def _slow_backend(_q, _n, _t):
        _calls.append(round(float(_t), 2))
        _time.sleep(0.8)
        return []

    _bak = list(config.SEARCH_BACKENDS)
    _bak_budget = config.SEARCH_BUDGET_SECONDS
    search._BACKENDS["__slow_a__"] = _slow_backend
    search._BACKENDS["__slow_b__"] = _slow_backend
    try:
        config.SEARCH_BACKENDS = ["__slow_a__", "__slow_b__"]
        config.SEARCH_BUDGET_SECONDS = 1.2
        search._CACHE.clear()
        _t0 = _time.monotonic()
        _out = search.web_search("预算测试查询", None, 5, 12.0)
        _spent = _time.monotonic() - _t0
        check("预算生效：慢后端被截断（只外呼 1 次、耗时 < 1.6s）",
              _spent < 1.6 and len(_calls) == 1,
              f"耗时 {_spent:.2f}s，外呼 {len(_calls)} 次，各次超时 {_calls}")
        check("预算截断后返回空列表（不乱编造）", _out == [], str(_out))
    finally:
        config.SEARCH_BACKENDS = _bak
        config.SEARCH_BUDGET_SECONDS = _bak_budget
        search._BACKENDS.pop("__slow_a__", None)
        search._BACKENDS.pop("__slow_b__", None)
        search._CACHE.clear()

    # 6c) 官方域优先排序：**首次与缓存命中必须一致**（防"第二次丢排序/丢标注"回归）
    #     动因：排序若只在 return 前做、缓存里存的是未排序结果，则同一查询第二次起会
    #     丢掉官方域优先顺序与 authority 标注，首次与缓存结果不一致（连续提问两遍即可看出）。
    print("\n§6c 官方域优先排序（首次与缓存命中一致）")
    _rank_source = [
        {"title": "补充来源", "url": "https://news.example.com/a", "snippet": "x"},
        {"title": "官方来源", "url": "https://wjw.km.gov.cn/b", "snippet": "x"},
        {"title": "权威来源", "url": "https://www.kmhospital.com/c", "snippet": "x"},
    ]

    def _rank_backend(_q, _n, _t):
        return [dict(h) for h in _rank_source]

    search._BACKENDS["__rank__"] = _rank_backend
    _bak_r = list(config.SEARCH_BACKENDS)
    try:
        config.SEARCH_BACKENDS = ["__rank__"]
        search._CACHE.clear()
        first = search.web_search("排序测试查询", None, 5, 5.0)
        second = search.web_search("排序测试查询", None, 5, 5.0)  # 命中缓存
        check("首次：官方域(.gov.cn)排最前",
              bool(first) and ".gov.cn" in first[0].get("url", ""),
              str([h.get("url") for h in first]))
        check("首次：带 authority 标注（官方/权威/补充）",
              bool(first) and first[0].get("authority") == "官方"
              and first[-1].get("authority") == "补充",
              str([h.get("authority") for h in first]))
        check("缓存命中：排序与标注不丢失（与首次一致）",
              bool(second) and [h.get("url") for h in second] == [h.get("url") for h in first]
              and all(h.get("authority") for h in second),
              str([(h.get("url"), h.get("authority")) for h in second]))
    finally:
        config.SEARCH_BACKENDS = _bak_r
        search._BACKENDS.pop("__rank__", None)
        search._CACHE.clear()

    # 7) 产品化降级：检索全空时必须给「手动检索入口」
    # 动因（实测）：三家免密钥引擎会同时限流，检索层返回空，产品只说一句
    #   "暂未查到"——用户会误以为是功能故障。故由代码兜底补一条可点击入口，
    #   让用户看到的是"产品降级"而不是"功能坏了"。
    # 本项用假模型 + 假检索注入，断言确定、不依赖网络与密钥。
    print("\n§7 产品化降级（检索全空 → 手动检索入口，代码兜底）")
    try:
        import json as _json

        from fastapi.testclient import TestClient

        import src.llm_client as llm_client
        import src.orchestrator as orchestrator
        import src.server as server

        class _Msg:
            def __init__(self, content="", tool_calls=None):
                self.content, self.tool_calls = content, tool_calls

        class _Resp:
            def __init__(self, m):
                self.choices = [type("C", (), {"message": m})()]
                self.usage = None

        script = [
            # 第 1 轮：模型不发起检索 —— 触发代码层检索兜底
            _Resp(_Msg("", None)),
            # 第 2 轮：模型如实降级（结果为空，不得编造）
            _Resp(_Msg(_json.dumps({
                "query_condition": {"region": "昆明", "hospital": "", "department": "",
                                    "resource": "抗蛇毒血清", "title": "", "date": ""},
                "query_results": [],
                "info_basis": [],
                "usage_tips": ["暂未查到可核验的联网信息，请以医院官方渠道为准。"],
            }, ensure_ascii=False))),
        ]
        llm_client.chat = lambda model, messages, **kw: script.pop(0)
        llm_client.is_ready = lambda: True
        llm_client.active_model = lambda: "fake-model"
        orchestrator.web_search = lambda q, c=None, **kw: []   # 模拟「所有后端都被限流」

        cli = TestClient(server.app)
        e = cli.post("/chat", json={"message": "昆明哪家医院有抗蛇毒血清？",
                                    "session_id": "deg-1"}).json()
        tips = " ｜ ".join(e.get("output", {}).get("usage_tips") or [])
        check("降级时 usage_tips 含「手动检索入口」",
              ("手动检索" in tips) or ("自行检索" in tips), tips[:110])
        check("入口含 360 可点击地址", "https://www.so.com/s?q=" in tips, tips[:170])
        check("入口含 搜狗 可点击地址", "https://www.sogou.com/web?query=" in tips, tips[:170])
        check("降级时结果为空（不编造）",
              (e.get("output", {}).get("query_results") or []) == [])
        check("检索日志记录 status=empty",
              any(r.get("status") == "empty" for r in e.get("retrieval_log", [])),
              str(e.get("retrieval_log")))
        # 手动入口必须只是"检索地址"，不能冒充事实来源
        check("入口未被误当作 query_results 的来源",
              all("so.com/s?q=" not in str(x) for x in
                  (e.get("output", {}).get("query_results") or [])))
    except Exception as ex:  # noqa: BLE001
        check("§7 降级路径可跑通", False, f"{type(ex).__name__}: {ex}")

    print("\n" + "=" * 72)
    total = _passed + _failed
    print(f"结论：{'通过' if _failed == 0 else '失败'}  {_passed}/{total} 项通过")
    print("=" * 72)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
