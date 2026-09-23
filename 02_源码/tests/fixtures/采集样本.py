# -*- coding: utf-8 -*-
"""检索样本采集脚本 —— 重建 tests/fixtures/ 下的离线测试样本。

为什么需要这个脚本（证据链）：
  tests/test_search_parsers.py 的结论必须能站住，靠的是「样本来自真实页面」。
  本脚本把样本的 **来源 URL、采集参数、采集时间、字节数、HTTP 状态** 全部
  记录下来，任何人可以在自己的网络环境下重跑，验证样本并非人工编造。

产出（写入本目录）：
  sogou_sample.html        搜狗正常结果页      （解析器的正样本）
  baidu_block_sample.html  百度风控页「安全验证」（风控页识别的正样本）
  so360_block_sample.html  360 风控页「访问异常」（风控页识别的正样本）
  _采集记录.json           每个样本的来源与校验信息

注意：风控页样本只有在 **被风控时** 才采得到；引擎正常时采到的是结果页。
      所以本脚本对风控类样本记录实际内容标题，不强行断言一定是风控页。

运行：激活 venv 后 python tests/fixtures/采集样本.py
"""
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
QUERY = "昆明哪家医院有抗蛇毒血清"

TARGETS = [
    # (文件名, 引擎名, URL, 查询参数名)
    ("sogou_sample.html", "sogou", "https://www.sogou.com/web", "query"),
    ("baidu_block_sample.html", "baidu", "https://www.baidu.com/s", "wd"),
    ("so360_block_sample.html", "so360", "https://www.so.com/s", "q"),
]


def grab(name: str, engine: str, url: str, param: str) -> dict:
    rec = {
        "file": name, "engine": engine, "source_url": url,
        "query": QUERY, "params": {param: QUERY},
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        # 直连纪律（与 src/search.py 同源）：本脚本虽不在测试链上（离线测试只读已落盘的
        # 样本），但**重采样本时**若沿用进程环境里的 HTTP(S)_PROXY，会被带到非预期出口而
        # 拿到风控页（HTTP 200 却是验证页）→ 采到的"正常结果页"样本其实是假的。
        r = httpx.get(url, params={param: QUERY}, headers=HEADERS,
                      timeout=20.0, follow_redirects=True, trust_env=False)
        body = r.content
        (HERE / name).write_bytes(body)
        text = body.decode("utf-8", errors="replace")
        title = (re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I) or [None, ""])[1]
        rec.update({
            "http_status": r.status_code,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "page_title": title.strip()[:80],
            "h3_count": len(re.findall(r"<h3", text)),
            "looks_like_block_page": any(
                m in title for m in ("安全验证", "访问异常", "验证码", "人机验证")
            ),
        })
        print(f"[OK]   {name:26s} {r.status_code} {len(body):>7d}B  title={title.strip()[:34]!r}")
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"[ERR]  {name:26s} {type(e).__name__}: {e}")
    return rec


def main() -> int:
    print(f"采集时间：{datetime.now().isoformat(timespec='seconds')}  查询词：{QUERY}")
    print("-" * 78)
    records = []
    for name, engine, url, param in TARGETS:
        records.append(grab(name, engine, url, param))
        time.sleep(1.5)          # 尊重对方服务器，顺序采集不并发
    (HERE / "_采集记录.json").write_text(
        json.dumps({"captured_query": QUERY, "records": records},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("-" * 78)
    print(f"采集记录已写入 {HERE / '_采集记录.json'}")
    print("提示：采集完请重跑 python tests/test_search_parsers.py 确认解析器仍匹配。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
