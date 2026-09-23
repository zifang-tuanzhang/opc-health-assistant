# -*- coding: utf-8 -*-
"""补采一条 fixtures 样本：360 移动版（m.so.com）正常结果页。

与 采集样本.py 同规矩：抓真实页面 → 存盘 → 记录 来源URL/参数/时间/字节/SHA256/标题。
单独成文是因为本次加固（新增 mso360 后端）需要它做离线确定性证物。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))          # 指向 02_源码/

import httpx                                        # noqa: E402
from src import search                              # noqa: E402

QUERY = "昆明 抗蛇毒血清 医院"
URL = "https://m.so.com/s"
PARAMS = {"q": QUERY}


def main() -> int:
    # 直连纪律（与 src/search.py 同源）：本脚本虽不在测试链上（离线测试只读已落盘的
    # 样本），但**重采样本时**若沿用进程环境里的 HTTP(S)_PROXY，会被带到非预期出口而
    # 拿到风控页（HTTP 200 却是验证页）→ 采到的"正常结果页"样本其实是假的。
    r = httpx.get(URL, params=PARAMS, headers=search._HEADERS,
                  timeout=15.0, follow_redirects=True, trust_env=False)
    html = r.text
    name = "m_so360_sample.html"
    (HERE / name).write_text(html, encoding="utf-8", newline="")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    rec = {
        "file": name,
        "engine": "mso360",
        "source_url": URL,
        "query": QUERY,
        "params": PARAMS,
        "captured_at": "2026-09-21 (本机实测抓取，采集方法见 采集样本.py / _采集mso360.py)",
        "http_status": r.status_code,
        "bytes": len(html.encode("utf-8")),
        "sha256": digest,
        "page_title": search._title_of(html),
        "h3_count": html.count('class="res-title"'),
        "looks_like_block_page": search._is_block_page(html),
        "parsed_results": len(search._parse_mso360(html, 10)),
    }
    meta = HERE / "_采集记录.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["records"] = [x for x in data["records"] if x.get("file") != name] + [rec]
    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
                    newline="")
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    ok = (not rec["looks_like_block_page"]) and rec["parsed_results"] >= 1
    print("\n样本可用：%s" % ("是 ✅" if ok else "否 ✘（页面可能被风控，稍后重采）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
