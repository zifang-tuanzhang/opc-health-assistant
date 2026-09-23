"""真联网检索工具（免密钥，多后端瀑布回退）。

赛题要求「真实联网检索」，而模型 API 由评审自己添加。为降低评审摩擦，
搜索默认走 **免密钥** 的公开检索入口，评审只要有一个模型 Key 就能完整跑通
「检索 → 提取 → 生成」。

后端与顺序（见 config.SEARCH_BACKENDS，默认 so360 → mso360 → sogou → sm）：
- ``so360`` ：360搜索 **桌面版**（结果块内含 ``data-mdurl``，可拿到 **文章级** 真实来源 URL）；
- ``mso360``：360搜索 **移动版**（``data-pcurl`` 直出 **文章级** 真实来源 URL + 发布日期）。
  与桌面版同引擎、**不同入口（不同限流额度）**：实测桌面版返回风控页时，移动版在同一分钟内
  仍能正常出结果，故作为第二后端——既是同质量的文章级来源，又抬高一层抗限流上限。
  ⚠️ 如实说明：**这是"提高上限"，不是"免疫"**。本机持续高频自测时，两个入口先后都被风控过。
  它的价值在于：中等请求量下多一次回退机会（评测官正常使用即属中等量级）。
- ``sogou``：搜狗搜索（结果 ``<cite>`` 内为 **域名级** 真实来源 URL，另附发布日期）；
- ``sm``   ：神马搜索（``m.sm.cn``，阿里巴巴/UC 独立运营，与 360/搜狗/百度**不同限流桶**）。
  本机实测返回 **相关、查询区分** 的结果，作为第 4 默认后端，治理 so360/mso360/sogou
  三者同族、IP 限流会一起挂的「伪冗余」。⚠️ 结果多为神马 **聚合中转页**
  （``page.sm.cn/blm/midpage`` 或 ``vt.quark.cn`` 医疗库），**非官网直链**；作为兜底源
  足够（标题/摘要可喂给模型做依据），引用时以「神马聚合」标注、不冒充官方来源。
- ``bing`` ：必应国内版（cn.bing.com，Microsoft 独立运营，不同限流桶）。**默认不启用**——
  本机实测对脚本化请求返回空/降级页（机器人识别），不能作为稳定独立源；在 bing 可达的
  环境用 ``OPC_SEARCH_BACKENDS`` 显式加回即可（解析器已就绪、注册保留）。
- ``ddg``  ：DuckDuckGo lite（海外网络可达，国内常不可用）。**默认不启用**——
  国内不可达时每次外呼要白等满超时，实测仅此一项就让端到端响应从 ~9 秒涨到 62.9 秒
  （详见 config.SEARCH_BACKENDS 与 SEARCH_BUDGET_SECONDS 的注释）。

行为约定（赛题红线相关）：
- 首个返回非空的后端胜出；全部失败 → 返回空列表（不抛异常）。
- 只返回 **带真实 http(s) 来源 URL** 的结果，保证「来源可核验」（对应护栏 R2）。
  拿不到真实 URL 的条目一律丢弃，宁少不假。
- 🔴 **风控页识别**：搜索引擎在判定访问异常时会返回 HTTP 200 的「安全验证 / 访问
  异常」页面。若把这种页面当搜索结果解析，会产出 **编造来源**（触碰红线）。故
  所有外呼统一过 ``_is_block_page``，命中即视为该后端失败、切下一个后端。
- 编排层在检索为空时会 **显式告知模型「未检索到」**，模型须诚实回复
  「暂未查到可核验的联网信息」，**绝不得把训练记忆当检索结果、也不得编造
  来源链接**（赛题严禁冒充动态查询）。

本模块只取「标题/链接/摘要」，不判断事实真伪——真伪由编排层护栏把关。

设计说明（可测性）：外呼(``_get``) 与解析(``_parse_*``) 分离。解析器是纯函数，
可用 ``tests/fixtures/`` 下的真实页面样本做 **离线确定性** 验证，不必打网络。
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import config

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_ENTITY = (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
           ("&lt;", "<"), ("&gt;", ">"))

# 引擎自身 / 跳转页域名（不是有效来源，需剔除）
_ENGINE_HOSTS = (
    "so.com", "hao.360.com", "360kan.com", "sogou.com", "baidu.com",
    "bing.com", "duckduckgo.com", "microsoft.com", "msn.com",
)
# 无关结果标题（引擎的模块标题，不是真实条目）
_NOISE_TITLES = ("其他人还搜了", "相关搜索", "大家还在搜", "热搜", "为您推荐",
                 "搜狗问医生", "直接问真实医生")

# ── 风控页识别 ────────────────────────────────────────────────────────
# 依据实测（tests/fixtures/*_block_sample.html）：被风控时引擎返回 HTTP 200，
# 但 <title> 是「百度安全验证」「访问异常页面」这类字样。只查 <title> 而不扫
# 正文，是为了避免把正文里恰好出现「安全验证」的正常新闻页误判为风控页。
_BLOCK_TITLE_MARKERS = ("安全验证", "访问异常", "验证码", "人机验证",
                        "请稍后再试", "拒绝访问", "unusual traffic",
                        "captcha", "forbidden")

_SO360_URL = "https://www.so.com/s"
_MSO360_URL = "https://m.so.com/s"
_SOGOU_URL = "https://www.sogou.com/web"
_DDG_URL = "https://lite.duckduckgo.com/lite/"
_SM_URL = "https://m.sm.cn/s"
# 神马(m.sm.cn) 是移动版，需用移动 UA 才能拿到正常结果页（桌面 UA 会拿到降级/空页）
_SM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def _clean(text: str) -> str:
    """去标签 + 解实体 + 压缩空白。"""
    s = _TAG_RE.sub("", text or "")
    for a, b in _ENTITY:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _is_engine_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return True
    return any(e in host for e in _ENGINE_HOSTS)


def _title_of(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    return _clean(m.group(1)) if m else ""


def _is_block_page(html: str) -> bool:
    """判断响应是否为搜索引擎的风控/验证页（命中则不能当结果用）。"""
    t = _title_of(html).lower()
    if not t:
        return False
    return any(mk in t for mk in _BLOCK_TITLE_MARKERS)


# ── 统一外呼层（含节流 + 风控页识别）──────────────────────────────────
_LAST_OUTBOUND: list[float] = [0.0]
_MIN_INTERVAL: float = 1.0       # 出站最小间隔（秒）


def _throttle() -> None:
    """出站节流：两次外呼之间至少间隔 _MIN_INTERVAL 秒（fail-open）。"""
    try:
        wait = _MIN_INTERVAL - (time.time() - _LAST_OUTBOUND[0])
        if 0 < wait < 5:
            time.sleep(wait)
        _LAST_OUTBOUND[0] = time.time()
    except Exception:
        pass


def _get(url: str, params: dict, timeout: float, headers: Optional[dict] = None) -> str:
    """统一外呼：节流 → 取页面 → 风控页/异常一律返回空串（视为该后端失败）。

    ``headers`` 可覆盖默认桌面 UA（移动版后端如神马需移动 UA 才能拿到正常结果页）。
    """
    _throttle()
    try:
        resp = httpx.get(
            url,
            params=params,
            headers=headers or _HEADERS,
            timeout=timeout,
            follow_redirects=True,
            # 🔴 直连纪律：忽略进程环境里的 HTTP(S)_PROXY。
            # 检索走的是【免密钥公开引擎】，直连即可；若沿用环境代理（本机/评审机
            # 可能残留代理或 VPN 出口），请求会被引擎当成机器/境外流量而触发风控，
            # 表现为「HTTP 200 却是验证页」或干脆「检索不到」。直连可彻底规避这一类问题。
            trust_env=False,
        )
    except Exception:
        return ""
    html = resp.text or ""
    if _is_block_page(html):
        return ""
    return html


# ─────────────────────────── 后端 1：360 搜索 ───────────────────────────


def _parse_so360(html: str, max_results: int) -> list[dict]:
    """解析 360 搜索结果页：``res-list`` 块内取 标题 + 真实 URL + 摘要。

    真实来源 URL 来自块内的 ``data-mdurl`` / ``url`` 属性（实测可得原文直链），
    这是「来源可核验」的关键；无真实 URL 的条目（引擎模块卡）直接丢弃。
    """
    out: list[dict] = []
    for block in (html or "").split('class="res-list"')[1:]:
        tm = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        if not tm:
            continue
        title = _clean(tm.group(1))
        if not title or any(n in title for n in _NOISE_TITLES):
            continue
        um = re.search(r'(?:data-mdurl|url)="(https?://[^"]+)"', block)
        url = um.group(1).replace("&amp;", "&") if um else ""
        if not url or _is_engine_url(url):
            continue
        dm = re.search(r'class="res-desc"[^>]*>(.*?)</p>', block, re.S)
        cm = re.search(r"<cite[^>]*>(.*?)</cite>", block, re.S)
        snippet = _clean(dm.group(1)) if dm else (_clean(cm.group(1)) if cm else "")
        out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= max_results:
            break
    return out


def _search_so360(q: str, max_results: int, timeout: float) -> list[dict]:
    return _parse_so360(_get(_SO360_URL, {"q": q}, timeout), max_results)


# ──────────────────────── 后端 2：360 搜索（移动版）────────────────────────
# 为什么单独加一条"同引擎的移动端"：
#   实测（2026-09-21）桌面版 www.so.com/s 返回 <title>访问异常页面</title>（HTTP 200
#   风控页）时，同一分钟内 m.so.com/s 仍返回正常结果页——两者是 **不同入口/不同限流桶**。
#   移动版的结果块里 ``data-pcurl`` 直接给出 **文章级原文直链**（无需再解跳转），
#   来源粒度与桌面版同级，故作为第二后端：同质量 + 多一道抗限流冗余。


def _parse_mso360(html: str, max_results: int) -> list[dict]:
    """解析 360 移动版结果页：``g-card res-list`` 块内取 标题 + 真实 URL + 摘要 + 日期。

    真实来源 URL 优先取 ``data-pcurl``（实测为原文直链，如 news.qq.com/rain/a/...）；
    无 ``data-pcurl`` 时回退解 ``jump?u=<URL编码>`` 里的真实地址。
    两条路都拿不到真实 URL 的条目一律丢弃（宁少不假）；引擎自身的模块卡
    （如标题以「_相关医院」结尾、链接指回 m.so.com）由 ``_is_engine_url`` 剔除。
    """
    out: list[dict] = []
    for block in (html or "").split('class="g-card res-list')[1:]:
        tm = re.search(r'class="res-title"[^>]*>(.*?)</h3>', block, re.S)
        if not tm:
            continue
        title = _clean(tm.group(1))
        if not title or any(n in title for n in _NOISE_TITLES):
            continue
        um = re.search(r'data-pcurl="(https?://[^"]+)"', block)
        url = um.group(1).replace("&amp;", "&") if um else ""
        if not url:
            jm = re.search(r"jump\?u=([^\"&]+)", block)
            if jm:
                url = unquote(jm.group(1))
        if not url or _is_engine_url(url):
            continue
        sm = re.search(r'class="g-main summary"[^>]*>(.*?)</p>', block, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        dm = re.search(r"<time[^>]*>(.*?)</time>", block, re.S)
        date = _clean(dm.group(1)) if dm else ""
        item = {"title": title, "url": url, "snippet": snippet}
        if date:
            item["date"] = date
        out.append(item)
        if len(out) >= max_results:
            break
    return out


def _search_mso360(q: str, max_results: int, timeout: float) -> list[dict]:
    return _parse_mso360(_get(_MSO360_URL, {"q": q}, timeout), max_results)


# ─────────────────────────── 后端 3：搜狗搜索 ───────────────────────────


def _parse_sogou(html: str, max_results: int) -> list[dict]:
    """解析搜狗搜索结果页：``vrwrap`` 块内取 标题 + 真实域名 URL + 摘要 + 日期。

    URL 取 ``<cite>`` 里的真实站点地址（域名级，可点击核验）。
    注：``<h3>`` 内的 ``/link?url=`` 是搜狗跳转，实测该地址返回 HTTP 200 的
    HTML 跳转页（非 302），解析它需为每条结果多发一次请求，性价比低，故不用；
    拿不到 ``<cite>`` 真实地址的条目不收录（宁少不假）。
    """
    out: list[dict] = []
    for block in re.split(r'<div class="vrwrap"', html or "")[1:]:
        tm = re.search(r'class="vr-title"[^>]*>(.*?)</h3>', block, re.S)
        if not tm:
            continue
        title = _clean(tm.group(1))
        if not title or any(n in title for n in _NOISE_TITLES):
            continue
        cm = re.search(r'class="citeLinkClass".*?</a>', block, re.S)
        url = ""
        date = ""
        if cm:
            spans = re.findall(r"<span[^>]*>(.*?)</span>", cm.group(0), re.S)
            for s in spans:
                s = _clean(s)
                if not url and s.startswith("http"):
                    url = s
                elif url and not date and re.match(r"^\d{4}-\d{2}-\d{2}$", s):
                    date = s
        if not url or _is_engine_url(url):
            continue
        sm = re.search(r'id="cacheresult_summary_\d+"[^>]*>(.*?)</div>', block, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        item = {"title": title, "url": url, "snippet": snippet}
        if date:
            item["date"] = date
        out.append(item)
        if len(out) >= max_results:
            break
    return out


def _search_sogou(q: str, max_results: int, timeout: float) -> list[dict]:
    return _parse_sogou(_get(_SOGOU_URL, {"query": q}, timeout), max_results)


# ─────────────────────── 后端 4：DuckDuckGo lite ───────────────────────


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    """解析 DuckDuckGo lite：``result-link`` 与 ``result-snippet``。"""
    results: list[dict] = []
    link_re = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    for m in link_re.finditer(html or ""):
        url = m.group(1)
        # DDG 跳转链形如 //duckduckgo.com/l/?uddg=<真实URL编码>
        if "uddg=" in url:
            try:
                real = parse_qs(urlparse(url).query).get("uddg", [""])[0]
                url = real or url
            except Exception:
                pass
        if url.startswith("//"):
            url = "https:" + url
        title = _clean(m.group(2))
        if not url or not title or _is_engine_url(url):
            continue
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= max_results:
            break
    snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html or "", re.S)
    for i, sn in enumerate(snippets):
        if i < len(results):
            results[i]["snippet"] = _clean(sn)
    return results


def _search_ddg(q: str, max_results: int, timeout: float) -> list[dict]:
    return _parse_ddg(_get(_DDG_URL, {"q": q}, timeout), max_results)


# ─────────────────────── 后端 5：神马搜索（m.sm.cn） ───────────────────────
# 为什么用它做默认「独立」源（稳定性关键，对应 B3 / 治伪冗余）：
#   so360/mso360/sogou 全是 360 / 搜狗同族，IP 一旦被限流三个会一起挂（伪冗余）；
#   神马由阿里巴巴/UC 独立运营，与 360/搜狗/百度是**不同限流桶**——这正是「独立」的含义。
#   本机实测（2026-09-22）：360/搜狗被风控时，m.sm.cn 仍返回相关、且**随查询变化**的结果
#   （「抗蛇毒血清」vs「三甲心血管内科」给出完全不同的医院/新闻条目），证明确是真独立源。
#   解析特征：结果即 ``<a href="https://...">`` 链接（移动版 DOM 混淆、无固定结果容器）。
#   保留 page.sm.cn/blm/midpage（神马聚合中转页）与 vt.quark.cn（夸克医疗库）为有效结果 URL；
#   剔除广告（m.sm.cn/adclick）与搜索页导航（m.sm.cn 主页）。标题取链接文本（医院卡含
#   院名+等级+地址，信息密度高）。⚠️ 多为中转页、非官网直链——兜底源足够，引用以「神马聚合」标注。

_SM_SEARCH_HOST = "m.sm.cn"


def _parse_sm(html: str, max_results: int) -> list[dict]:
    """解析神马搜索结果页（移动版，DOM 混淆、无固定结果容器）。

    策略：扫描所有 ``<a href="https://...">`` 链接，剔除广告与搜索页导航，
    保留神马中转页(page.sm.cn/blm/midpage)/夸克医疗库(vt.quark.cn)/第三方真实来源。
    标题取链接文本（医院卡文本含院名+等级+地址，信息密度高）；神马摘要多在
    异步加载/混淆容器，离线样本难稳定提取，故 snippet 留空（兜底源足够，标题已含依据）。
    去重按 URL；同一条结果可能有两个 <a>（图包空文本 + 链接富文本），优先保留非空文本。
    健壮性：异常输入一律返回空，绝不抛异常、绝不编造。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]+href="(https://[^"]+)"[^>]*>(.*?)</a>', html or "", re.S):
        url = m.group(1).replace("&amp;", "&")
        if "m.sm.cn/adclick" in url:          # 广告
            continue
        host = (urlparse(url).netloc or "").lower()
        if host == _SM_SEARCH_HOST:           # 搜索页导航/模块，非结果
            continue
        if _is_engine_url(url):               # 其他引擎自身域
            continue
        title = _clean(m.group(2))
        if not title or len(title) < 3:       # 图包等空文本 <a> 跳过（留给富文本 <a>）
            continue
        if any(n in title for n in _NOISE_TITLES):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= max_results:
            break
    return out


def _search_sm(q: str, max_results: int, timeout: float) -> list[dict]:
    return _parse_sm(_get(_SM_URL, {"q": q}, timeout, _SM_HEADERS), max_results)


# ─────────────── 后端 6（可选）：必应国内版（cn.bing.com） ───────────────
# 说明（诚实修正，2026-09-22）：必应解析器代码正确、已注册保留，但本机实测
#   cn.bing.com 对脚本化请求返回 **空/降级页（机器人识别）**，并不能稳定作为独立源。
#   故默认不启用；在 bing 可达的环境用 OPC_SEARCH_BACKENDS 显式加回即可。
#   默认独立源改用「后端 5：神马搜索」（见上），它在本机实测返回相关、查询区分的结果。
# 解析特征（留作可选后端）：结果块 <li class="b_algo">；<h2><a href> 多为**真实直链**，
#   仅少数仍是 /ck/a?u= 跳转，需解 u 参数；摘要在 <div class="b_caption"><p>。


def _parse_bing(html: str, max_results: int) -> list[dict]:
    """解析必应国内版结果页：``b_algo`` 块内取 标题 + 真实 URL + 摘要 +（可选）日期。

    真实来源 URL 优先取 ``<h2><a href>`` 直链；少数 ``bing.com/ck/a?u=`` 跳转链
    解 ``u`` 参数还原真实地址。拿不到真实 URL 的条目一律丢弃（宁少不假）。
    """
    out: list[dict] = []
    for block in (html or "").split('class="b_algo"')[1:]:
        hm = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not hm:
            continue
        url = hm.group(1)
        if "bing.com/ck" in url:  # 少数跳转链：解 u 参数还原真实地址
            um = re.search(r"[?&]u=([^&]+)", url)
            if um:
                try:
                    url = unquote(um.group(1))
                except Exception:
                    url = ""
        if not url or not url.startswith("http") or _is_engine_url(url):
            continue
        title = _clean(hm.group(2))
        if not title or any(n in title for n in _NOISE_TITLES):
            continue
        pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _clean(pm.group(1)) if pm else ""
        item: dict = {"title": title, "url": url, "snippet": snippet}
        # 日期：块内 tptt 像「YYYY-MM-DD / MM-DD」才收，避免把域名当日期
        dm = re.search(r'class="tptt"[^>]*>([^<]+)</div>', block)
        if dm:
            txt = dm.group(1).strip()
            if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", txt) or re.match(r"^\d{1,2}[-/]\d{1,2}", txt):
                item["date"] = txt
        out.append(item)
        if len(out) >= max_results:
            break
    return out


def _search_bing(q: str, max_results: int, timeout: float) -> list[dict]:
    return _parse_bing(_get("https://cn.bing.com/search", {"q": q}, timeout), max_results)


_BACKENDS: dict[str, Callable[[str, int, float], list[dict]]] = {
    "so360": _search_so360,
    "mso360": _search_mso360,
    "sogou": _search_sogou,
    "sm": _search_sm,
    "bing": _search_bing,
    "ddg": _search_ddg,
}


def _query_variants(q: str) -> list[str]:
    """查询变形：同一检索意图换措辞再试，提高命中率。

    动因（实测，本环境 so360）：
        空格分词串「昆明 抗蛇毒血清 医院」        → 命中 0 条
        自然语言句「昆明哪家医院有抗蛇毒血清」    → 命中 5 条
        空格分词串「昆明 心血管内科 医院」        → 命中 4 条（有时又行）
    可见引擎对措辞敏感，且方向不固定。故原样失败时，用"去空格连写"再试一次。
    （连写方向同时兼顾了"模型可能拟出分词 query" 这一真实情况。）
    """
    q = (q or "").strip()
    if not q:
        return []
    variants = [q]
    if " " in q:
        squeezed = re.sub(r"\s+", "", q)
        if squeezed and squeezed != q:
            variants.append(squeezed)
    return variants


# ── 检索结果缓存 ──────────────────────────────────────────────────────
# 动因（实测事故）：本机在短时间内发出大量检索请求后，360/百度先后返回
# 「访问异常页面」「安全验证」（HTTP 200 的风控页），检索层整体失效。
# 真实使用者不会这样高频检索，但缓存仍有两层价值：
#   ① 同查询短期复用，减少外呼（降低触发风控的概率，也更快）；
#   ② 演示场景里连续追问同一话题时命中率高、响应更稳。
# 只缓存 **非空** 结果（空结果要允许恢复，不能被缓存钉死）。全部 fail-open。
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL: float = 600.0        # 10 分钟


def _cache_get(key: str) -> Optional[list[dict]]:
    try:
        item = _CACHE.get(key)
        if not item:
            return None
        ts, hits = item
        if (time.time() - ts) > _CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        return [dict(h) for h in hits]
    except Exception:
        return None


def _cache_put(key: str, hits: list[dict]) -> None:
    try:
        if hits:                                  # 只缓存有结果的（空结果要允许恢复）
            _CACHE[key] = (time.time(), [dict(h) for h in hits])
    except Exception:
        pass


def cache_stats() -> dict:
    return {"entries": len(_CACHE), "ttl_seconds": _CACHE_TTL, "min_interval": _MIN_INTERVAL}


def _rank_by_authority(hits: list[dict]) -> list[dict]:
    """对检索结果做权威域加权排序（赛题：优先官网与卫健委等权威渠道），并标注 authority。

    不删除任何结果（"宁少不假"是指丢弃无 URL 的条目，这里是"排序"），只把官方/医院官网来源
    排到前面，让模型与前端优先引用；authority 字段同时回传给编排层上下文，供模型识别【官方】结果。
    判定纯模式（.gov.cn / hospital / yiyuan / .edu.cn），不硬编码任何具体医院名（符合铁律）。
    """
    def _score(h: dict) -> int:
        host = (urlparse(h.get("url", "")).netloc or "").lower()
        if host.endswith(".gov.cn"):
            return 3                      # 卫健委 / 政府官方
        if "hospital" in host or "yiyuan" in host or host.endswith(".edu.cn"):
            return 2                      # 医院官网 / 医学院附属医院信号
        return 1

    for h in hits:
        s = _score(h)
        h["authority"] = "官方" if s == 3 else ("权威" if s == 2 else "补充")
    return sorted(hits, key=_score, reverse=True)


def web_search(
    query: str,
    city: Optional[str] = None,
    max_results: int = 5,
    timeout: float = 12.0,
) -> list[dict]:
    """多后端瀑布检索，返回 [{title, url, snippet}, ...]。失败返回空列表。

    三层保障：
      ① 缓存层：同一查询 10 分钟内直接复用（减少外呼，规避风控）；
      ② 后端层：按 config.SEARCH_BACKENDS 顺序换后端；
      ③ 措辞层：同一后端原样查 0 条时，换措辞再查（见 _query_variants）。
      ④ 预算层：外呼总耗时不超过 config.SEARCH_BUDGET_SECONDS（见下）。

    编排层会在检索为空时走「诚实降级」：明确告诉模型未检索到，禁止编造。

    ⚠️ 为什么需要第 ④ 层（实测动因，2026-09-21）：
        本函数是「后端 × 查询变体」双层循环，总耗时 = 所有外呼超时之和，**没有上界**。
        仅一个国内不可达的后端（ddg），就能让单次检索多耗 ~24 秒，端到端实测达 62.9 秒——
        评审会误判成"系统卡死"。故加预算：第一个后端保证至少跑一次（不乱降级），
        此后一旦超出预算就停止尝试后续后端，直接返回已有结果。
    """
    q = f"{query} {city}".strip() if city and city not in query else query.strip()
    key = "%s|%s|%d" % (q, config.SEARCH_BACKENDS, max_results)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    budget = float(getattr(config, "SEARCH_BUDGET_SECONDS", 18.0) or 0.0)
    deadline = (time.monotonic() + budget) if budget > 0 else float("inf")
    tried_any = False

    for name in config.SEARCH_BACKENDS:
        fn = _BACKENDS.get(name)
        if fn is None:
            continue
        if tried_any and time.monotonic() >= deadline:
            break  # 预算已耗尽：不再尝试后续后端
        tried_any = True
        for v in _query_variants(q):
            left = deadline - time.monotonic()
            if left <= 1.0 and not (budget <= 0):
                break  # 剩余时间不足以完成一次外呼
            per_call = timeout if budget <= 0 else max(1.0, min(timeout, left))
            try:
                hits = fn(v, max_results, per_call)
            except Exception:
                hits = []
            if hits:
                # 先排序再缓存：缓存命中路径（见上方 _cache_get）直接回放缓存内容，
                # 若缓存的是未排序结果，则「同一查询第二次起」会丢掉官方域优先排序与
                # authority 标注（首次与缓存结果不一致）。故此处缓存**已排序**的列表。
                ranked = _rank_by_authority(hits)
                _cache_put(key, ranked)
                return ranked
    return []


if __name__ == "__main__":
    import json

    print(json.dumps(web_search("昆明 抗蛇毒血清 医院", "昆明"), ensure_ascii=False, indent=2))
