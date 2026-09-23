# -*- coding: utf-8 -*-
"""pytest 配置：让 `pytest tests/` 与项目的「脚本式证据工具」共存。

背景：
    本项目的测试刻意写成「脚本式证据工具」（模块导入即执行、打印明细表、
    以退出码表达结果），因为它们要同时承担两个角色——
      · 作为测试（断言行为与结构）；
      · 作为证据（把中间过程打印出来，供人工核验并写入测试记录文档）。
    这类文件若被 pytest 直接收集，会在「收集阶段」就执行全部逻辑，
    还会触发模块重载等副作用，甚至因模块层的 sys.exit 打断 pytest 进程。

🔴 为什么这个忽略清单必须与「脚本式套件」严格同步（一次真实踩坑）：
    `test_search_parsers.py` 的 §7 会**替换** `llm_client.chat` 与
    `orchestrator.web_search`（注入假模型/假检索）来做降级路径断言。
    脚本式执行时进程随即结束，无副作用；但**若被 pytest 导入**，这些替换会留在
    同一进程里，**污染后续所有测试**——表现为"别的测试莫名其妙失败"，
    而根因在收集阶段。**新增脚本式套件时，务必同步加进本清单。**

做法：
    1) 用 collect_ignore 让 pytest 跳过所有脚本式套件；
    2) 由 tests/test_all_suites.py 作为唯一入口，以子进程方式串行调用它们，
       再按退出码断言——既保留脚本式工具的全部价值，又让 pytest 结果干净可读。

想跑单套明细，请直接执行脚本本身，例如：
    python tests/test_acceptance_8groups.py
"""

collect_ignore = [
    "test_smoke.py",
    "test_guardrails.py",
    "test_acceptance_8groups.py",
    "test_authenticity.py",
    "test_adversarial.py",
    "test_adversarial2.py",
    "test_search_parsers.py",
    "test_clarify_regression.py",
    "test_ops_guarantees.py",
]
