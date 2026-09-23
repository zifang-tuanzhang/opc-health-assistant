# -*- coding: utf-8 -*-
"""启动项目（中文启动器）。

把源码取到本地后，双击「启动项目.bat」即可：
  1) 自动建 Python 虚拟环境（.venv）
  2) 从目录内预置的 wheels/ 离线安装依赖（不联网；无网也能跑）
  3) 后端启动后自动扫描本机密钥库（%USERPROFILE%\\.opc_health\\keys.json）
  4) 自动打开浏览器到交互界面

密钥不在项目里：使用者在网页里点「添加密钥」即可，无需翻任何文件。

全程只依赖「系统已装 Python 3.10+」与「一份源码」。无任何手动下载步骤。

── 两处运行期加固（2026-09-23）────────────────────────────────────────────
A. 【直连探测】就绪探测不再读进程环境里的 HTTP(S)_PROXY。
   与 src/search.py、src/llm_client.py 同源的「直连纪律」：本机/使用者的机器若残留代理或
   VPN 出口，探测请求会被带到非预期出口而超时；旧版会因此误判「后端启动超时」并退出，
   直接表现为【双击后浏览器打不开】。改为绕过代理直连 127.0.0.1 探测。
B. 【端口自适应】默认 8137 若被别的程序占用（例如同机另一个比赛项目的容器映射），
   不再硬失败：自动顺延到下一个空闲端口并用该端口开浏览器；若 8137 上跑的**就是本项目**，
   则直接复用（不重复起进程、不抢端口）。若连续 20 个端口全被占，则**明确报错退出**，
   不静默回落到已知被占端口（那样会白等探测超时才失败、报错也不清晰）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "02_源码"
VENV = ROOT / ".venv"
WHEELS = ROOT / "wheels"
REQ = SRC / "requirements.txt"
HOST = "127.0.0.1"


def _env_port() -> int:
    """默认端口：读环境变量 OPC_PORT（与 02_源码/src/config.py 的 OPC_PORT 同名对齐）。

    为什么必须有这个函数：`resolve_port()` 在端口全被占时的报错会提示用户
    「set OPC_PORT=9000」。若启动器自己不读该变量，用户照做会**原样再失败一次**
    —— 那条提示就成了死路。非法值一律回落 8137，不抛异常。
    """
    raw = os.environ.get("OPC_PORT", "8137")
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return 8137
    return port if 0 < port < 65536 else 8137


DEFAULT_PORT = _env_port()
PORT = DEFAULT_PORT  # 运行期可能被 resolve_port() 改写
_PORT_SCAN_LIMIT = 20  # 默认端口被占时，最多顺延探测多少个端口


def url(port: int | None = None) -> str:
    """交互界面地址（按最终使用的端口生成）。"""
    return f"http://{HOST}:{port or PORT}/"


def _direct_opener() -> urllib.request.OpenerDirector:
    """构造【忽略环境代理】的 opener（直连纪律，见模块顶部 A 条）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _health_probe(port: int, timeout: float = 2.0) -> dict | None:
    """探测某端口上的 /health；是本项目则返回其 JSON，否则返回 None。"""
    try:
        with _direct_opener().open(f"http://{HOST}:{port}/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    # 用本项目独有的字段组合认身份，避免把别的服务误认成本项目
    if isinstance(data, dict) and "api_version" in data and "search_backends" in data:
        return data
    return None


def _port_free(port: int) -> bool:
    """端口是否空闲（能否以该地址独占绑定）。

    为什么**刻意不设 SO_REUSEADDR**（套接字选项，允许复用处于 TIME_WAIT 的地址）：
    Windows 上它的语义比 Linux 宽——**当占用方自己也设了该选项时**，后来者再带着该选项
    去 bind 同一个地址会**成功抢到**。若本函数带上该选项，就会把一个确实被占用的端口
    误判成「空闲」→ 端口扫描挑中被占端口 → 后端反而起不来（又变成「打不开」）。
    不设该选项时，绑定到在用端口会正常报错（WSAEADDRINUSE），判断才可信。

    谁会给端口设上这个选项（实测口径，勿写成绝对结论）：**Linux 上主流服务器（含
    uvicorn）默认就设**；**Windows 上 asyncio / uvicorn 反而不设**（asyncio 源码里
    该开关是 `os.name == "posix"`）。但 Windows 上"别的程序"（如同时跑的另一个服务、
    容器端口转发、部分数据库）设了它也完全常见，所以这条防线在实战中仍然必要。

    平台差异（如实记录，勿当绝对结论）：判据是「以 127.0.0.1 独占绑定是否成功」。
    Windows 允许在 `0.0.0.0` 已被占用时另行绑定一个具体地址，故此时判为「空闲」——
    实测这与 uvicorn 能否真正绑定该地址的结果一致。Linux 上 wildcard 占用会让具体地址
    的绑定也失败，于是判为「占用」（更保守，最多多顺延一个端口，无害）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def resolve_port() -> tuple[int | None, bool]:
    """挑一个可用端口。

    返回 (端口, 是否为「本项目已在运行」)。**区间内找不到空闲端口时端口为 None。**
    优先复用默认端口上【已在本机运行的本项目】实例；否则在默认端口起的连续区间里
    找第一个空闲端口，彻底避免「端口被别的项目占了 → 起不来 → 打不开」。
    """
    if _health_probe(DEFAULT_PORT) is not None:
        return DEFAULT_PORT, True
    for port in range(DEFAULT_PORT, DEFAULT_PORT + _PORT_SCAN_LIMIT):
        if _port_free(port):
            return port, False
    # 扫描窗口内全被占用：返回 None，交给调用方**明确报错**。
    # 不再默默回落到已知被占的默认端口——那会白等满探测超时（30s）才失败，且报错不清晰。
    return None, False


def log(sym: str, text: str, color: str = "") -> None:
    tag = {"ok": "✔", "warn": "⚠", "err": "✘", "info": "·", "": "·"}.get(sym, "·")
    print(f"  {tag} {text}")


def find_python() -> str | None:
    for cand in ("py", "python", "python3"):
        p = shutil.which(cand)
        if p:
            # 校验版本 >= 3.10
            try:
                out = subprocess.run(
                    [p, "-c", "import sys;print(sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=15,
                )
                maj, mino = eval(out.stdout.strip())
                if (maj, mino) >= (3, 10):
                    return p
            except Exception:
                pass
    return None


def venv_python() -> Path:
    return VENV / "Scripts" / "python.exe" if os.name == "nt" else VENV / "bin" / "python"


def bootstrap_venv(py: str) -> None:
    first_run = not VENV.exists()
    if first_run:
        log("info", "首次运行：创建虚拟环境 .venv ...")
        subprocess.run([py, "-m", "venv", str(VENV)], check=True)
    vpy = venv_python()
    # 升级 pip：只在【首次建环境】时尝试，且限时短、不重试。
    # 动因（2026-09-24 复现评审方流程时实测）：旧版每次启动都执行联网升级 pip，
    # 无网/弱网环境下 pip 的重试退避会让「双击后迟迟不出浏览器」。而本项目依赖
    # 全部来自仓库内 wheels/，venv 自带的 pip 已足以安装，这一步纯属可选增强，
    # 不应阻塞启动。
    if first_run:
        subprocess.run(
            [str(vpy), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "--timeout", "8", "--retries", "0", "--upgrade", "pip"],
            capture_output=True, text=True,
        )
    # 优先离线安装（闭环），失败回退在线
    log("info", "安装依赖（优先离线 wheels）...")
    offline = subprocess.run(
        [str(vpy), "-m", "pip", "install", "--no-index", "--disable-pip-version-check",
         "--find-links", str(WHEELS), "-r", str(REQ)],
        capture_output=True, text=True,
    )
    if offline.returncode != 0:
        log("warn", "离线 wheels 不完整，回退在线安装（需联网一次）...")
        online = subprocess.run([str(vpy), "-m", "pip", "install", "-r", str(REQ)],
                                capture_output=True, text=True)
        if online.returncode != 0:
            log("err", "依赖安装失败，请检查网络或 Python 版本。")
            sys.exit(1)
    log("ok", "依赖就绪")


def start_backend(vpy: str, port: int) -> subprocess.Popen:
    log("info", f"启动后端（端口 {port}，自动扫描本机密钥库）...")
    proc = subprocess.Popen(
        [str(vpy), "-m", "uvicorn", "src.server:app", "--host", HOST, "--port", str(port)],
        cwd=str(SRC),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return proc


def wait_ready(port: int, timeout: int = 30) -> bool:
    """等待后端就绪。

    注意：探测必须【直连】——旧版用 urllib.urlopen 会读进程环境里的 HTTP(S)_PROXY，
    本机/使用者的机器残留代理时探测会被带偏而超时，导致「后端启动超时 → 不打开浏览器」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_probe(port) is not None:
            return True
        time.sleep(1)
    return False


def main() -> None:
    global PORT
    print("  ┌────────────────────────────────────────────────")
    print("  │ OPC 接单吧 · AI 医院资源查询与便民就医助手 · 启动器")
    print("  └────────────────────────────────────────────────")

    py = find_python()
    if not py:
        log("err", "未检测到 Python 3.10+。请先安装 Python（勾选 Add to PATH）。")
        input("按回车退出...")
        sys.exit(1)
    log("ok", f"Python 就绪：{py}")

    # 先定端口：默认端口若已被【本项目】占用则复用，被【别的程序】占用则自动顺延。
    port, already_running = resolve_port()
    if port is None:
        log("err", f"端口 {DEFAULT_PORT} 起连续 {_PORT_SCAN_LIMIT} 个端口都被占用，无法启动。")
        log("info", "请先释放端口，或指定端口后重试：先在命令行执行 set OPC_PORT=9000 再双击本启动器。")
        input("按回车退出...")
        sys.exit(1)
    PORT = port
    if already_running:
        log("ok", f"检测到本项目已在端口 {port} 运行，直接复用（不重复启动）。")
        log("info", f"打开浏览器：{url(port)}")
        webbrowser.open(url(port))
        print("  ────────────────────────────────────────────────")
        print("  服务由先前的启动器实例托管，本窗口可直接关闭。")
        print("  ────────────────────────────────────────────────")
        return
    if port != DEFAULT_PORT:
        log("warn", f"默认端口 {DEFAULT_PORT} 被其他程序占用，已自动改用端口 {port}。")

    bootstrap_venv(py)
    vpy = str(venv_python())

    proc = start_backend(vpy, port)
    if not wait_ready(port):
        log("err", "后端启动超时，请查看上方日志。")
        input("按回车退出...")
        proc.terminate()
        sys.exit(1)
    log("ok", "后端已启动")

    log("info", f"打开浏览器：{url(port)}")
    webbrowser.open(url(port))
    print("  ────────────────────────────────────────────────")
    print("  浏览器打开后：若提示「未配置密钥」，点「添加密钥」选厂商粘贴即可。")
    print(f"  若浏览器未自动弹出，请手动访问：{url(port)}")
    print("  关闭本窗口将停止服务。Ctrl+C 也可停止。")
    print("  ────────────────────────────────────────────────")

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    log("info", "已停止服务。")


if __name__ == "__main__":
    main()
