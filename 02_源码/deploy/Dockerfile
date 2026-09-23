# OPC 接单吧·AI 医院资源查询与便民就医助手 后端镜像
# 零配置可跑（待配置模式：不发起检索、不调模型，页面给出配置引导），配 Key 即启用「真联网检索 + 模型生成」。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPC_ENV=prod \
    OPC_HOST=0.0.0.0 \
    OPC_PORT=8137

WORKDIR /app

# 依赖先装（利用层缓存）
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端源码（src/ 即包）
COPY src/ ./src/

# 复制前端静态资源（index.html / 模拟页 / 小程序接口测试页）。
# 缺了它容器内 / 返回 JSON 兜底、/static 不挂载，网页界面 404。
COPY static/ ./static/

EXPOSE 8137

# 启动入口：src.server:app（接入层骨架 + 后续真实编排循环）
CMD ["sh", "-c", "python -m uvicorn src.server:app --host 0.0.0.0 --port 8137"]
