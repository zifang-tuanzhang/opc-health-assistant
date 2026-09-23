"""OPC 接单吧·AI 医院资源查询与便民就医助手 —— 后端源码包。

架构原则（来自开发指导架构）：
- 编排层 = 模型当控制器；代码只给「原则 / 工具 / 输出契约 / 护栏」，不给事实。
- 本包内任何 .py 都不得写入医院名 / 科室名 / 医生名 / 排班 / 资源结论等事实
  （事实一律由模型从真实检索来源产出）。任何代码写入事实均属违规。
"""
from __future__ import annotations

__all__ = ["config", "guard", "session", "orchestrator", "server", "schema"]
