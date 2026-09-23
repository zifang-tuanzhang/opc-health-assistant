# -*- coding: utf-8 -*-
"""前端静态页结构冒烟测试（零依赖，纯标准库）。

为什么有它：A5 模拟页、index.html、小程序接口测试.html 都是「纯静态展示」，
不接框架、不跑构建；但 A4/B2/U3 等会改 index.html 的渲染逻辑，
前端一旦写错（标签未闭合、关键函数被误删）肉眼难发现。
故用 html.parser 做「结构健康」确定性校验：解析不抛异常 + 关键元素存在。

不判断「长得对不对」（那是视觉/评审的事），只判断「结构没坏、关键契约还在」。
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


class _StructureChecker(HTMLParser):
    """解析并统计关键 class，解析异常会自然抛出（视为失败）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.classes: dict[str, int] = {}
        self.found_script_blocks: int = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "script":
            self.found_script_blocks += 1
        for k, v in attrs:
            if k == "class" and v:
                for c in v.split():
                    self.classes[c] = self.classes.get(c, 0) + 1

    def has(self, cls: str) -> bool:
        return self.classes.get(cls, 0) > 0

    def count(self, cls: str) -> int:
        return self.classes.get(cls, 0)


def _check(path: Path) -> _StructureChecker:
    if not path.exists():
        raise AssertionError(f"静态页不存在：{path}")
    text = path.read_text(encoding="utf-8")
    if not text.lstrip().lower().startswith("<!doctype html>"):
        raise AssertionError(f"{path.name} 缺少 <!DOCTYPE html> 声明")
    chk = _StructureChecker()
    chk.feed(text)  # 解析异常即视为结构损坏
    return chk


def check_a5():
    """A5 模拟页：手机外壳 + 至少 3 个用户气泡 / 3 个 AI 气泡（3 场景演示）。"""
    chk = _check(STATIC / "便民就医助手_界面模拟.html")
    assert chk.has("phone"), "A5 缺少手机外壳(.phone)"
    # class="msg user" 会被解析器拆成 msg / user 两个 token，故分别计数
    assert chk.count("msg") >= 6, f"A5 对话气泡不足 6（实际 {chk.count('msg')}）"
    assert chk.count("user") >= 3, f"A5 用户气泡不足 3（实际 {chk.count('user')}）"
    assert chk.count("bot") >= 3, f"A5 AI 气泡不足 3（实际 {chk.count('bot')}）"
    print("[PASS] A5 模拟页结构健康：手机外壳 + 3 场景对话演示")


def check_index():
    """index.html：核心渲染函数 renderOutput 仍在 + 关键容器齐全。"""
    chk = _check(STATIC / "index.html")
    # 注：index.html 的气泡/留痕均由 JS 动态生成（className='msg user'/'msg bot'/class="retro"），
    # 静态 HTML 里无这些 class，故此处校验「动态标记 + 渲染函数」仍在。
    js = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function renderOutput" in js, "index.html 的 renderOutput 渲染函数被误删"
    assert "function renderEnvelope" in js, "index.html 的 renderEnvelope 被误删"
    assert "'msg user'" in js, "index.html 动态气泡(.msg user)标记缺失"
    assert "'msg bot'" in js, "index.html 动态气泡(.msg bot)标记缺失"
    assert "retro" in js, "index.html 检索过程留痕(.retro)标记缺失"
    # A4 横向比较视图：对比入口 + 动态表格渲染函数 + 容器 class 均须在
    assert "对比视图" in js, "index.html 缺少「对比视图」入口（A4 回归）"
    assert "toggleCompare" in js, "index.html 缺少 toggleCompare（A4 对比开关）"
    assert "buildCompareTable" in js, "index.html 缺少 buildCompareTable（A4 动态表格）"
    assert "cmp-table" in js, "index.html 缺少 .cmp-table 样式锚点（A4）"
    # B1 历史记录：入口按钮 + 面板容器 + 拉取/切换函数均须在
    assert "histBtn" in js, "index.html 缺少「历史记录」入口按钮（B1 回归）"
    assert "historyPanel" in js, "index.html 缺少历史记录面板容器（B1 回归）"
    assert "loadHistory" in js, "index.html 缺少 loadHistory（B1 历史拉取）"
    assert "switchSession" in js, "index.html 缺少 switchSession（B1 切换会话）"
    assert "/history" in js, "index.html 未调用 /history 接口（B1 回归）"
    # B2 条件筛选控件：筛选栏 + 类型下拉 + 文本搜索 + 过滤函数 + 每条结果带 data-type/data-q
    assert "filterbar" in js, "index.html 缺少筛选栏 .filterbar（B2 回归）"
    assert "applyFilter" in js, "index.html 缺少 applyFilter（B2 过滤逻辑）"
    assert "f-type" in js, "index.html 缺少类型下拉 .f-type（B2 回归）"
    assert "data-type" in js, "index.html 结果条未带 data-type（B2 筛选数据缺失）"
    assert "data-q" in js, "index.html 结果条未带 data-q（B2 搜索数据缺失）"
    # U3 输出增强：来源已在新标签打开（target=_blank）+ 冲突提示视觉高亮（与 A3 联动）
    assert "target=\"_blank\"" in js, "index.html 来源链接未在新标签打开（U3 回归）"
    assert "class=\"warn\"" in js, "index.html 缺少冲突提示高亮 class（U3 回归）"
    assert "_cfx" in js, "index.html 缺少冲突信号词检测（U3 与 A3 联动）"
    # 赛题《基础需求5》③：信息依据须含「来源更新时间（如有）」**与「本次查询时间」**——两者是不同概念。
    # 「来源更新时间」由 Source.updated_note 承载；「本次查询时间」由本次检索留痕时间戳渲染，故两处都要在。
    assert "本次查询时间" in js, "index.html 缺少「本次查询时间」（赛题 基础需求5 ③ 明确要求）"
    assert "renderOutput(o, env)" in js, "index.html 的 renderOutput 未接收 env（本次查询时间取不到检索留痕）"
    assert "updated_note" in js or "b.note" in js, "index.html 未渲染来源更新时间说明（赛题 基础需求5 ③ 要求）"
    # 布局契约：应用级操作（历史/密钥/重置）在顶栏工具区，底栏只做输入
    assert "class=\"tools\"" in js, "index.html 缺少顶栏工具区 .tools（布局契约）"
    assert "class=\"composer\"" in js, "index.html 缺少独立输入区 .composer（布局契约）"
    assert "id=\"histList\"" in js, "index.html 缺少历史抽屉列表容器 #histList"
    assert "id=\"scrim\"" in js, "index.html 缺少抽屉遮罩 #scrim"
    # 过程时间线收拢：回答产出后必须把过程留痕收起，否则整块留痕会长期占据版面
    assert "function settleProgress" in js, "index.html 缺少 settleProgress（过程时间线不会收拢）"
    # 注意：必须匹配到「带分号的调用」而不是函数定义行（function settleProgress(prog){），
    # 否则调用点被删掉、定义还在，断言会假通过（已用反向验证确认）。
    assert js.count("settleProgress(prog);") >= 2, \
        f"index.html 未在回答产出/失败分支调用 settleProgress（实际 {js.count('settleProgress(prog);')} 处）"
    assert "prog.done" in js and "prog-sum" in js, "index.html 缺少过程时间线收拢态样式"
    # 右上角密钥入口：常驻胶囊（已配置/未配置两态）+ 首次进入主动引导
    assert "class=\"keypill\"" in js, "index.html 缺少右上角密钥入口胶囊（.keypill）"
    assert "id=\"keyLabel\"" in js and "id=\"keyAct\"" in js, "index.html 密钥入口缺少动态文案节点"
    assert "function maybeIntro" in js, "index.html 缺少首次进入引导（maybeIntro）"
    assert "opc_intro_seen_v1" in js, "index.html 缺少首次引导的本地标记（会反复弹出打扰用户）"
    # 接线闭环：JS 中 $('xxx') 引用的每个 id 必须真实存在于本页 HTML。
    # 这条防的是「改了 id 却漏改 JS」——那种错浏览器不报错，只是在运行时静默失效。
    body = js[js.index("<body>"): js.index("<script>")]
    html_ids = set(re.findall(r'\bid="([^"]+)"', body))
    js_ids = set(re.findall(r"""\$\(\s*['"]([^'"]+)['"]\s*\)""", js))
    dangling = sorted(js_ids - html_ids)
    assert not dangling, f"index.html 中 JS 引用了不存在的 id（接线断裂）：{dangling}"
    print("[PASS] index.html 结构健康：容器齐全 + 渲染函数完好 + A4 对比视图 + B1 历史记录 + B2 筛选 + U3 来源新标签/冲突高亮均在位")


def check_miniapp_mock():
    """小程序接口测试.html（已有）：存在且可解析即可。"""
    chk = _check(STATIC / "小程序接口测试.html")
    print("[PASS] 小程序接口测试.html 结构健康")


if __name__ == "__main__":
    fails = 0
    for fn in (check_a5, check_index, check_miniapp_mock):
        try:
            fn()
        except AssertionError as e:
            print(f"[FAIL] {fn.__name__}: {e}")
            fails += 1
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} 异常：{type(e).__name__}: {e}")
            fails += 1
    if fails:
        print(f"\n前端冒烟测试失败 {fails} 项")
        sys.exit(1)
    print("\n前端冒烟测试全部通过 ✅")
