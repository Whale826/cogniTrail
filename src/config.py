"""统一配置：路径、模型、Mock 开关。

手册 §6.1 / §6.2 / §6.3 里，三个脚本各自 load_dotenv() 一遍、各自 new 一次 client。
这里抽成一处：改 base_url 或模型名，只改这一个文件。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 让 `python src/xxx.py` 和 `python -m src.xxx` 两种跑法都能 import 到 src.*
_SOURCE_ROOT = Path(__file__).resolve().parent.parent

# 打包成 exe 之后（PyInstaller 会把代码解到临时目录），__file__ 指向的是
# 一个随时会被删掉的路径。所以这两种形态必须分开处理：
#   - 源码跑：项目根 = src 的上一级
#   - exe 跑：项目根 = exe 所在目录（.env 就该放在 exe 旁边）
FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_ROOT_FALLBACK = _SOURCE_ROOT
if FROZEN:
    RUNTIME_ROOT = Path(sys.executable).resolve().parent
else:
    RUNTIME_ROOT = _SOURCE_ROOT

if str(_SOURCE_ROOT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT_FALLBACK))

# ---------------- 路径 ----------------
# 默认按「src/ 的上一级」定位项目根。
# 但插件形态下，引擎被拷进了 Codex 的插件目录，工作目录又在别处 ——
# 所以留一个 COGNITRAIL_HOME 覆盖口：设了它，就把它当作项目根。
_ENV_HOME = os.getenv("COGNITRAIL_HOME", "").strip()
ROOT = Path(_ENV_HOME).expanduser().resolve() if _ENV_HOME else RUNTIME_ROOT

#: 全局状态目录（探针库、watch 进度）。放用户目录，保证 exe 也能写。
#: 为什么不放在 ROOT/data：exe 被放在 Program Files 之类只读位置时就写不动了。
STATE_DIR = Path(os.getenv("COGNITRAIL_STATE", "") or (Path.home() / ".cognitrail")).expanduser()

DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DECISIONS_DIR = DOCS_DIR / "decisions"
EXPERIMENTS_DIR = DOCS_DIR / "experiments"
SAMPLES_DIR = ROOT / "samples"


def _db_path() -> Path:
    """探针库。源码跑就放项目里（方便看），exe 跑就放用户目录（保证可写）。"""
    if FROZEN:
        return STATE_DIR / "probe.db"
    return DATA_DIR / "probe.db"


DB_PATH = _db_path()
WATCH_STATE = STATE_DIR / "watch-state.json"


# ---------------- .env ----------------
def _load_env(path: Path) -> None:
    """优先用 python-dotenv；没装就退回到一个极简解析器，保证零依赖也能跑。"""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path)
        return
    except ImportError:
        pass

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


# .env 的查找顺序（先找到的先生效）：
#   1. 项目根/.env            —— 正常跑的时候走这条
#   2. 当前工作目录/.env       —— 在别的仓库里跑时，可以就地放一份
#   3. ~/.cognitrail/.env     —— 全局一份，插件形态下用这条
for _candidate in (ROOT / ".env", Path.cwd() / ".env", Path.home() / ".cognitrail" / ".env"):
    _load_env(_candidate)


# ---------------- 小工具 ----------------
def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


# ---------------- 模型 ----------------
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip() or "https://api.deepseek.com"
MODEL = os.getenv("COGNITRAIL_MODEL", "deepseek-chat").strip() or "deepseek-chat"

_MOCK_ENV = _truthy(os.getenv("COGNITRAIL_MOCK", "0"))

# 显式开了 Mock，或者根本没配 Key —— 都走离线模式
MOCK = _MOCK_ENV or not API_KEY
MOCK_REASON = (
    "COGNITRAIL_MOCK=1（.env 里显式开启）"
    if _MOCK_ENV
    else ("未配置 DEEPSEEK_API_KEY，已自动回退" if not API_KEY else "")
)

# ---------------- 卡片写法（语言基调） ----------------
# pro = 专业模式（技术口吻，说清哪个文件哪一行）
# plain = 小白模式（大白话，讲清是什么问题、有什么影响）
# 这里存的是**用户的选择**，具体那两段提示词在 src/tone.py 里。
# 默认给 pro：卡片是给项目留的凭据，既有卡片也一直是这个口吻，
# 不该因为升级了程序就让新卡片突然换一种写法。想要通俗的在界面上一键切。
TONE = (os.getenv("COGNITRAIL_TONE", "pro").strip() or "pro").lower()


# ---------------- CLI 桥（「和 AI 对话」接本机命令行） ----------------
# 为什么需要这几个口子：自动探测靠的是 PATH + 几个常见安装目录，
# 而**别人机器上**的 CLI 可能装在任意位置（自建目录、Scoop、公司盘、绿色版…）。
# 探测不到时，最省事的办法是让人直接把路径写下来，
# 而不是逼他去改系统 PATH（很多人不会改，也不该为了我们的程序去改）。
def _split_multi(raw: str) -> list[str]:
    """按 `;` 或 `,` 切开，去掉空白、引号和空项。"""
    items: list[str] = []
    for chunk in str(raw or "").replace(",", ";").split(";"):
        text = chunk.strip().strip('"').strip("'")
        if text:
            items.append(text)
    return items


def _parse_kv(raw: str) -> dict[str, str]:
    """`claude=C:\\x\\claude.cmd;codex=D:\\y\\codex.exe` → dict。

    值里允许有反斜杠和冒号（Windows 路径），但不能含 `;` / `,`。
    """
    out: dict[str, str] = {}
    for chunk in str(raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        out[key.strip().lower()] = value.strip().strip('"').strip("'")
    return out


def _parse_custom_cli(raw: str) -> dict:
    """自定义 CLI（一个 JSON 对象）。

    `.env` 里写成一行 JSON —— 界面上的「CLI 设置」会帮忙写，
    手写也行：`COGNITRAIL_CLI_CUSTOM={"label":"内网网关","exe":"mycli.exe","args":"ask","style":"stdin"}`
    解析不出来就当没配（不让一行手写错的配置把整个功能炸掉）。
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    exe = str(data.get("exe") or "").strip()
    if not exe:
        return {}
    style = str(data.get("style") or "").strip().lower()
    return {
        "label": str(data.get("label") or "").strip() or "自定义命令",
        "exe": exe,
        "args": str(data.get("args") or "").strip(),
        "style": "stdin" if style == "stdin" else "argv",
    }


#: 手动指定的 CLI 路径：`claude=...;codex=...`（最优先，压过自动探测）
CLI_PATHS: dict[str, str] = _parse_kv(os.getenv("COGNITRAIL_CLI_PATHS", ""))
#: 额外要去找命令的目录（`;` 或 `,` 分隔）
CLI_DIRS: tuple[str, ...] = tuple(_split_multi(os.getenv("COGNITRAIL_CLI_DIRS", "")))
#: 一个完全自定义的命令（不是上面列的那几个 CLI 时用）
CLI_CUSTOM: dict = _parse_custom_cli(os.getenv("COGNITRAIL_CLI_CUSTOM", ""))


# ---------------- 默认值 ----------------
DEFAULT_REPO = os.getenv("COGNITRAIL_REPO", "").strip()
DEFAULT_DELAY_DAYS = _int_env("COGNITRAIL_DELAY_DAYS", 3)
DEFAULT_MAX_CARDS = _int_env("COGNITRAIL_MAX_CARDS", 5)

#: 卡片量按什么档给：auto（按提问判断）| L0 只问结果 | L1 不懂技术 | L2 懂一点 | L3 工程师
#: 它只管「给几张、留哪张」，不改变卡片里写的事实。
CARD_LEVEL = os.getenv("COGNITRAIL_LEVEL", "auto").strip() or "auto"


# ---------------- 运行期热更新（界面里填 Key 用） ----------------
def apply_runtime() -> None:
    """按当前环境变量重算模型相关的全局量。

    为什么必须有这个函数：
        窗口版 exe 是个长驻进程。用户在界面里填了 Key、只把 .env 写下去
        是不够的 —— 当前这个进程里的 `config.API_KEY` 还是空的，
        它仍然会走离线草稿，用户看到的现象是「填了没用」。
        所以写完 .env 必须调一次它，把模块内的这几个全局量刷新掉。
    """
    global API_KEY, BASE_URL, MODEL, MOCK, MOCK_REASON, _MOCK_ENV, TONE
    global CLI_PATHS, CLI_DIRS, CLI_CUSTOM
    API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
    BASE_URL = (
        os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        or "https://api.deepseek.com"
    )
    MODEL = os.getenv("COGNITRAIL_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    TONE = (os.getenv("COGNITRAIL_TONE", "pro").strip() or "pro").lower()
    # CLI 桥那三样也要跟着刷 —— 用户可能刚在「CLI 设置」里填了路径，
    # 不刷的话得重启程序才生效（同一类坑：填了没用）。
    CLI_PATHS = _parse_kv(os.getenv("COGNITRAIL_CLI_PATHS", ""))
    CLI_DIRS = tuple(_split_multi(os.getenv("COGNITRAIL_CLI_DIRS", "")))
    CLI_CUSTOM = _parse_custom_cli(os.getenv("COGNITRAIL_CLI_CUSTOM", ""))
    _MOCK_ENV = _truthy(os.getenv("COGNITRAIL_MOCK", "0"))
    MOCK = _MOCK_ENV or not API_KEY
    MOCK_REASON = (
        "COGNITRAIL_MOCK=1（.env 里显式开启）"
        if _MOCK_ENV
        else ("未配置 DEEPSEEK_API_KEY，已自动回退" if not API_KEY else "")
    )


def key_hint() -> str:
    """界面显示用：当前这把 Key 长什么样（只露头尾），没配就返回「未配置」。"""
    key = API_KEY
    if not key:
        return "未配置"
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}（{len(key)} 位）"


def env_write_target() -> Path:
    """该把 .env 写到哪里。

    优先写 exe 旁边 / 项目根（与《使用说明》一致），
    那个位置写不动（例如 exe 被放进 Program Files）就退到 `~/.cognitrail/.env`——
    它同样在查找链里，而且一定可写。
    """
    for candidate in (ROOT / ".env", STATE_DIR / ".env"):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / ".cognitrail-write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return STATE_DIR / ".env"


def update_env(updates: dict[str, str]) -> Path:
    """把若干键写进 .env（保留文件里其它内容与注释），并让当前进程立刻生效。

    只做「按行替换同名键 / 末尾追加新键」，不重排、不删注释 ——
    用户自己手写的注释不该被程序搞乱。
    """
    target = env_write_target()
    lines: list[str] = []
    replaced: set[str] = set()
    if target.is_file():
        for raw in target.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(raw)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                lines.append(f"{key}={updates[key]}")
                replaced.add(key)
            else:
                lines.append(raw)
    for key, value in updates.items():
        if key not in replaced:
            lines.append(f"{key}={value}")
    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")

    for key, value in updates.items():
        os.environ[key] = value
    apply_runtime()
    return target


# ---------------- 输出辅助 ----------------
def enable_utf8_stdout() -> None:
    """Windows 控制台默认可能是 GBK，打印中文会炸。统一掰成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def banner() -> str:
    """启动横幅：让使用者一眼看出现在跑的是真模型还是假数据。"""
    if MOCK:
        return (
            "=" * 62
            + "\n  离线 Mock 模式 —— 不联网、不花钱、产出的是假数据\n"
            + f"  原因：{MOCK_REASON}\n"
            + "  用途：先跑通「格式 + 闸门 + 落盘」，卡片 status 会标为 mock\n"
            + "  切换真模型：把 .env 里的 COGNITRAIL_MOCK 改成 0 并填入 Key\n"
            + "=" * 62
        )
    return f"在线模式 · {MODEL} @ {BASE_URL}"
