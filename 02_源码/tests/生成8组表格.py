# -*- coding: utf-8 -*-
"""把「逐组记录 JSON」回填进「验收 8 组」文档的表格标记区（幂等）。

存在意义
--------
赛题要求逐组记录「输入 / 预期行为 / 实际结果 / 通过情况 / 测试时间」。
这些值每跑一次测试都会变（尤其**测试时间**）。若靠人手往文档里粘，
就会出现一种最难察觉的缺陷：**测试是绿的，但文档里写的是上一轮的时间**。

本脚本把这条链路改成机器闭环：

    跑测试  →  导出 03_测试记录/验收8组_逐组记录_20260921.json
            →  本脚本按标记区回填 03_测试记录/验收测试_8组_2026-09-21.md

标记区之间的内容每次都会被**整体重写**，所以：
- 幂等：值没变则输出与输入逐字节相同；
- 安全：标记区之外的一个字都不动；
- 可复查：运行后会打印"本次写了哪几行"。

用法
----
    python 02_源码/tests/生成8组表格.py            # 回填
    python 02_源码/tests/生成8组表格.py --check     # 只检查是否已同步（不同步则退出码 1）

被 `tests/test_acceptance_8groups.py` 在导出 JSON 后自动调用（失败不影响测试结论）。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
JSON_PATH = ROOT / "03_测试记录" / "验收8组_逐组记录_20260921.json"
DOC_PATH = ROOT / "03_测试记录" / "验收测试_8组_2026-09-21.md"

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _cell(text: object) -> str:
    """把任意值洗成能安全放进 Markdown 表格单元格的单行文本。"""
    s = "" if text is None else str(text)
    s = s.replace("\r", " ").replace("\n", " ").replace("|", "｜")
    return s.strip()


def _mk(tag: str) -> re.Pattern[str]:
    return re.compile(
        r"(<!-- AUTO:%s:BEGIN -->\n)(.*?)(\n<!-- AUTO:%s:END -->)" % (tag, tag),
        re.S,
    )


def _rows_8groups(records: list[dict]) -> str:
    out = []
    for i, r in enumerate(records):
        mark = CIRCLED[i] if i < len(CIRCLED) else "%d." % (i + 1)
        ok = "✅" if r.get("passed") else "✘"
        out.append(
            "| %s%s | **%s** | %s | %s | %s | %s（%.2fs） | %s |"
            % (mark, _cell(r.get("name")), _cell(r.get("id")), _cell(r.get("input")),
               _cell(r.get("expect")), _cell(r.get("actual")),
               _cell(r.get("at")), float(r.get("seconds") or 0.0), ok)
        )
    return "\n".join(out)


def _rows_extra(records: list[dict]) -> str:
    out = []
    for r in records:
        ok = "✅" if r.get("passed") else "✘"
        out.append(
            "| %s | %s | %s | %s | %s | %s（%.2fs） | %s |"
            % (_cell(r.get("id")), _cell(r.get("name")), _cell(r.get("input")),
               _cell(r.get("expect")), _cell(r.get("actual")),
               _cell(r.get("at")), float(r.get("seconds") or 0.0), ok)
        )
    return "\n".join(out)


def _runtime_line(ev: dict) -> str:
    return (
        "> 本次运行：**%s 起，全程 %.2f 秒**。\n"
        "> 测试环境：%s。"
        % (_cell(ev.get("started_at")), float(ev.get("total_seconds") or 0.0),
           _cell(ev.get("env")).strip("。"))
    )


def render(ev: dict) -> dict[str, str]:
    """按标记区算出四块应该是什么内容。"""
    recs = ev.get("records") or []
    grp = [r for r in recs if re.fullmatch(r"G\d+", str(r.get("id", "")))]
    add = [r for r in recs if not re.fullmatch(r"G\d+", str(r.get("id", "")))]
    return {
        "RUNTIME": _runtime_line(ev),
        "8GROUPS": _rows_8groups(grp),
        "EXTRA": _rows_extra(add),
    }


def generate(check_only: bool = False) -> str:
    if not JSON_PATH.exists():
        raise FileNotFoundError("缺少逐组记录：%s（请先跑一次验收测试）" % JSON_PATH)
    if not DOC_PATH.exists():
        raise FileNotFoundError("缺少文档：%s" % DOC_PATH)

    ev = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    want = render(ev)
    src = DOC_PATH.read_text(encoding="utf-8")
    out = src
    written = []

    for tag, body in want.items():
        pat = _mk(tag)
        m = pat.search(out)
        if not m:
            raise ValueError("文档缺少标记区 AUTO:%s（请在文档中补上 BEGIN/END 注释）" % tag)
        new_block = m.group(1) + body + m.group(3)
        if m.group(0) != new_block:
            written.append(tag)
        out = pat.sub(lambda _m, nb=new_block: nb, out, count=1)

    if out == src:
        return "已是最新（3 个标记区均与 JSON 一致）"

    if check_only:
        raise SystemExit("需要同步的标记区：%s（当前为 --check，不写入）" % "、".join(written))

    DOC_PATH.write_text(out, encoding="utf-8", newline="")
    return "已回填标记区：%s（数据源 started_at=%s）" % ("、".join(written), ev.get("started_at"))


if __name__ == "__main__":
    try:
        print(generate(check_only="--check" in sys.argv))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print("同步失败：%s" % exc)
        sys.exit(1)
