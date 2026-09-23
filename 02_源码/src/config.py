"""集中配置：.env 加载 + 环境变量解析 + 密钥纪律。

采用零依赖 .env 加载器 + 密钥服务端管理 + 数值 fail-fast 的配置范式，
并针对本项目需求做三处增强：
  1) 搜索走【免密钥】公开检索，可用 SEARCH_BACKENDS 指定后端回退顺序；
  2) 模型密钥主路径是【外部密钥库】，环境变量 LLM_* 为容器 / CI 回落位；
  3) 新增演示城市 DEMO_CITY、反射打回上限 GUARDRAIL_MAX_REFLECT、
     运行保障（cost_gate：每日预算 / 缓存 / 限速）等编排参数。

配置纪律（强制）：
- 敏感配置只从环境变量 / 外部密钥库读取，代码内零明文；
- 所有配置项均有安全默认值，零配置即可以「骨架模式」本地运行（不接模型）；
- 数值型环境变量解析失败时 fail-fast，给出清晰报错而非隐性跑偏；
- 日志输出前须经 secret_redaction 脱敏；密钥库里的密钥在装载时登记，同样脱敏。

红线提醒：本文件只放「配置与参数」，不得写入任何医院/科室/医生/排班事实。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("opc.config")


class ConfigError(Exception):
    """配置错误：环境变量误配时 fail-fast，给出可读报错。"""


def _load_dotenv(path: str | None = None) -> bool:
    """零依赖 .env 加载器。

    把 .env 中的 ``KEY=VALUE`` 注入 os.environ（仅当该键尚未存在于环境中，
    即真实环境变量优先于 .env 文件）。搜索顺序：显式 path → 后端 src 目录 .env →
    项目 deploy/.env → 当前工作目录 .env。.env 不入版本库，仓库只留 .env.example。
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    src_dir = Path(__file__).resolve().parent  # src/
    candidates.append(src_dir / ".env")
    candidates.append(src_dir.parent / "deploy" / ".env")  # 项目根/deploy/.env
    candidates.append(Path.cwd() / ".env")
    for cand in candidates:
        if cand.is_file():
            try:
                with open(cand, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            os.environ[key] = value
                return True
            except OSError:
                return False
    return False


# import 时先加载 .env（若存在），再读取下方配置项。
_load_dotenv()


def _as_int(env_name: str, default: str) -> int:
    """读取整型环境变量，解析失败 fail-fast。"""
    raw = os.environ.get(env_name, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"环境变量 {env_name} 必须为整数，当前值无法解析: {raw!r}"
        ) from e


def _as_bool(env_name: str, default: str = "0") -> bool:
    """读取布尔环境变量（1/true/yes/on/y 视为真），其余为假。"""
    return os.environ.get(env_name, default).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_float(env_name: str, default: str) -> float:
    """读取浮点环境变量，解析失败 fail-fast。"""
    raw = os.environ.get(env_name, default)
    try:
        return float(raw)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"环境变量 {env_name} 必须为数字，当前值无法解析: {raw!r}"
        ) from e


# ── 运行环境 ──
ENV: str = os.environ.get("OPC_ENV", "dev").lower()  # dev / prod
# 默认只监听本机回环（127.0.0.1）：本作品是「本地单用户演示」，无需暴露到局域网/公网；
# 监听 0.0.0.0 会在机器并入网络时被同网段任意主机访问（/api/keys、/history、/reset 无鉴权时的暴露面）。
# 若确需跨机演示，用 OPC_HOST=0.0.0.0 显式开启，并务必同步设置 OPC_API_TOKEN。
HOST: str = os.environ.get("OPC_HOST", "127.0.0.1")
PORT: int = _as_int("OPC_PORT", "8137")

# 可选 API 令牌（敏感端点闸）：未设置时默认开放（仅 127.0.0.1 可达，本地演示够用）；
# 设置后，/api/keys/add、/history、/reset 必须带 X-OPC-Token 头或 ?token= 参数，否则 403。
# 纵深防御：与 HOST=127.0.0.1 共同收敛「误暴露」风险。
API_TOKEN: str = os.environ.get("OPC_API_TOKEN", "")

# ── 演示城市（编排参数：默认检索范围；模型仍可接受其他城市）──
# 注意：这是「检索默认城市」配置，不是硬编码答案；演示范围已实搜验证（昆明）。
DEMO_CITY: str = os.environ.get("OPC_DEMO_CITY", "昆明")

# ── 免密钥搜索后端回退顺序（本届核心：真联网检索，严禁硬编码答案冒充查询）──
# 说明：搜索全部走【免密钥】公开检索（见 src/search.py），按此顺序回退，
#       首个有结果的后端胜出；全部失败则返回空，由编排层「诚实护栏」兜底。
# 默认 "so360,mso360,sogou,sm"：
#   so360  = 360搜索【桌面版】（国内可达，返回【文章级】真实来源 URL，实测对医院类查询命中率高）；
#   mso360 = 360搜索【移动版】（同引擎、不同入口，返回【文章级】真实来源 URL + 发布日期。
#            桌面版被风控时移动版仍可用，故作为额外回退。⚠️ 是"提高上限"不是"免疫"：
#            持续高频下两个入口会先后被风控）；
#   sogou  = 搜狗搜索（国内可达，返回【域名级】真实来源 URL + 发布日期，作为第三后端）；
#   sm     = 神马搜索（m.sm.cn，阿里巴巴/UC 独立运营，与 360/搜狗/百度**不同限流桶**）。
#            本机实测 360/搜狗被风控时 m.sm.cn 仍返回【相关、查询区分】的结果，是真正的
#            「独立源」，用以治理 so360/mso360/sogou 三者同族、IP 限流会一起挂的「伪冗余」。
#            ⚠️ 仍是免费公开检索，不引入任何密钥；结果多为神马聚合中转页(page.sm.cn/blm/midpage
#            或 vt.quark.cn 医疗库)，非官网直链——作兜底源足够，引用以「神马聚合」标注。
#   bing   = 必应国内版（cn.bing.com，Microsoft 独立运营，不同限流桶）。**默认不启用**：
#            本机实测对脚本化请求返回空/降级页（机器人识别），不能稳定作为独立源；
#            在 bing 可达的环境用 OPC_SEARCH_BACKENDS 显式加回即可（解析器已就绪）。
#
# ddg（DuckDuckGo lite）**默认不再启用**——实测动因（2026-09-21）：
#   它在国内网络不可达，每次外呼要**白等满 12 秒超时**；而检索是「后端 × 查询变体」
#   双层循环，仅一个 ddg 就会让每次查询多耗 ~24 秒。一次端到端实测因此达到 **62.9 秒**，
#   用户会误判成"系统卡死"。需要时可显式加回：OPC_SEARCH_BACKENDS="so360,mso360,sogou,sm,ddg"。
# 可用 OPC_SEARCH_BACKENDS 覆盖，如 "sogou,so360"。
SEARCH_BACKENDS: list[str] = [
    b.strip().lower()
    for b in os.environ.get("OPC_SEARCH_BACKENDS", "so360,mso360,sogou,sm").split(",")
    if b.strip()
]

# ── 检索总耗时预算（秒）──
# 上限护栏：无论后端列表配成什么，单次 web_search 的**外呼总耗时**不超过此值
# （第一个后端保证至少跑一次；此后预算耗尽即停止尝试后续后端，直接返回当前结果）。
# 动因：检索耗时必须有上界，否则"某后端不可达/被限流"会把整体响应拖到分钟级。
# 设 0 或负数表示不限制（不推荐）。
SEARCH_BUDGET_SECONDS: float = _as_float("OPC_SEARCH_BUDGET_SECONDS", "18")

# ── 模型 API 回落（主路径是外部密钥库，这里是它的回落位）──
# 主路径：在网页「添加密钥」→ 写入【外部密钥库】
#         (src/keystore.py，%USERPROFILE%\.opc_health\keys.json)，不进仓库。
# 回落路径：容器 / CI / 无用户目录场景下没有 ~/.opc_health，改用下面这组环境变量
#         （LLM_API_KEY 为触发条件；BASE_URL 缺省用 OpenAI 官方地址）。
# 二者同时存在时，【密钥库优先】。
LLM_API_KEY: str = os.environ.get("OPC_LLM_API_KEY", "")
LLM_BASE_URL: str = os.environ.get("OPC_LLM_BASE_URL", "")
LLM_MODEL: str = os.environ.get("OPC_LLM_MODEL", "")

# ── 反射式护栏（Loop Controller）──
# 最多打回轮数：产出不通过校验时编码回灌模型，令其重新生成（非重新检索）。
GUARDRAIL_MAX_REFLECT: int = _as_int("OPC_GUARDRAIL_MAX_REFLECT", "2")
# R3 来源可达校验：默认【关】。开启后校验每条来源链接可访问（需联网探测），
# 联网探测在弱网/受限网络下易产生假阳性 → 默认关闭，需要时再开。
GUARDRAIL_VERIFY_URLS: bool = _as_bool("OPC_GUARDRAIL_VERIFY_URLS", "0")

# ── LLM 运行保障（赛题「进阶3」：成本记录 / 缓存 / 限速）──
# 已接入 src/cost_gate.py：每日 token 预算 + 相同请求缓存去重 + 每分钟调用限速；
# 超限返回明确降级码（不崩溃、不卡死）。实时用量可在 /health 查看。
LLM_DAILY_BUDGET_TOKENS: int = _as_int("OPC_LLM_DAILY_BUDGET_TOKENS", "1000000")
LLM_CACHE_TTL_SECONDS: int = _as_int("OPC_LLM_CACHE_TTL_SECONDS", "86400")
# 每分钟模型调用上限：注意一轮对话内部可能调用 2~3 次（分析→生成→打回重整），
# 故默认 30（约 10 轮/分钟），既防死循环又不会让使用者连续提问时被误挡。
LLM_RATE_LIMIT_PER_MIN: int = _as_int("OPC_LLM_RATE_LIMIT_PER_MIN", "30")

# ── 网关边界（输入护栏：空/超长/限流）──
MAX_MESSAGE_LEN: int = _as_int("OPC_MAX_MESSAGE_LEN", "500")
RATE_LIMIT_PER_MIN: int = _as_int("OPC_RATE_LIMIT_PER_MIN", "300")

# ── CORS 白名单（逗号分隔；默认本地前端端口，prod 禁用 *）──
_cors_raw = os.environ.get("OPC_CORS_ORIGINS", "")
if _cors_raw:
    CORS_ORIGINS: list[str] = [o.strip() for o in _cors_raw.split(",") if o.strip()]
else:
    CORS_ORIGINS = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8137",
        "http://localhost:8137",
    ]
if CORS_ORIGINS == ["*"] and ENV == "prod":
    logger.warning("prod 环境 CORS 为 * 不安全，请用 OPC_CORS_ORIGINS 配置白名单")


# ── 密钥脱敏 ───────────────────────────────────────────────────────────
# 运行时登记的密钥（来自外部密钥库 / 「添加密钥」时的候选密钥）。
# 目的：即便密钥只存在于内存，也绝不允许它以明文出现在任何日志里。
_RUNTIME_SECRETS: list[str] = []


def register_secret(secret: str | None) -> None:
    """登记一个需要在日志中脱敏的密钥（幂等；短于 4 位忽略以免误伤）。"""
    s = (secret or "").strip()
    if len(s) >= 4 and s not in _RUNTIME_SECRETS:
        _RUNTIME_SECRETS.append(s)


def secret_redaction(text: str) -> str:
    """日志脱敏：把任何已加载的密钥从日志文本中抹除，避免密钥进日志。"""
    redacted = text
    for secret in (LLM_API_KEY, *_RUNTIME_SECRETS):
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted
