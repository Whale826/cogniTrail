"""把本机已经装好的 AI 命令行接进来，直接和 AI 对话。

为什么单独一个模块，而不是塞进 llm.py：
    llm.py 走 HTTP API —— 要有 Key、要能出网、按 token 计费。
    CLI 是另一种东西：它跑在本机、用的是用户**已经登录过**的那个账号，
    不需要在这里填任何 Key。两者的失败方式、超时尺度、可用性判断完全不同，
    混在一起会让两边都难维护。
    好处也很实在：能直接问「这个项目里 X 是怎么实现的」——
    因为 CLI 是在项目目录里跑的，它自己会去读代码。

一个刻意的取舍：只用**非交互模式**（一次提问、一次回答）。
    不去驱动 CLI 的交互界面（那界面是给人在终端里用的，
    程序靠往伪终端里灌按键去模仿它，非常脆，CLI 一升级就坏）。
    所以这里调的是 `claude -p` / `codex exec` 这类一次性的入口。

另一条刻意的线：**不替用户绕过权限**。
    不传 --dangerously-skip-permissions / --force / --yolo 之类的开关。
    这个按钮的定位是「问问题」，不是「让另一个 AI 去改我的文件」。
    想看它改代码，请自己在终端里用 CLI。
    （测试里有一条专门守着这个：那几个开关不许出现在任何 chat_args 里。）

关于「别人的电脑」——这个模块最难的部分不是调用，是**找到命令**：
    同一个 CLI，不同人装在不同地方：npm 全局目录、pnpm、Volta、Bun、
    Scoop / Chocolatey、公司内网的绿色版、甚至同一个盘里的自建目录。
    而 GUI 从桌面图标启动时，PATH 常常只有系统默认那一份。
    所以这里做了三层，且**运行期每次都重新读**（改完不用重启）：
      ① 手动指定的路径（.env 的 COGNITRAIL_CLI_PATHS，最优先）
      ② 额外搜索目录（.env 的 COGNITRAIL_CLI_DIRS）
      ③ 一份常见安装位置的清单 + PATH
    再找不到就给出「你该做什么」，而不是只说一句找不到。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: 探测与对话的超时。探测只是跑一下 --version，几秒就够；
#: 对话要等模型把话说完（CLI 启动本身也要一两秒），给宽一点。
PROBE_TIMEOUT = 20
CHAT_TIMEOUT = 300

#: 自定义命令那个 provider 的固定 key
CUSTOM_KEY = "custom"

#: 要接的几个 CLI。`chat_style` 决定 prompt 怎么递进去：
#:   stdin   —— 从标准输入喂（不经过命令行，省掉一整类引号转义问题）
#:   argv    —— 作为参数传（这个 CLI 没提供可靠的管道入口）
#:
#: 参数都是照着各自的官方文档写的，**不要凭印象加**：
#:   多余的开关会让对方机器上这个功能看起来「坏了」，比不支持更糟。
#:   claude        `claude -p`（prompt 走 stdin）
#:   codex         `codex exec <prompt>`
#:   gemini        `gemini -p <prompt>`
#:   qwen          `qwen -p <prompt>`
#:   opencode      `opencode -p <prompt>`
#:   cursor-agent  `cursor-agent -p <prompt>`（不加 --force，它就只提议不改文件）
PROVIDERS: tuple[dict, ...] = (
    {
        "key": "claude",
        "label": "Claude Code",
        "commands": ("claude", "claude-code"),
        #: 探测用 / 对话用
        "version_args": ("--version",),
        "chat_args": ("-p",),
        "chat_style": "stdin",
        "hint": "用你本机 Claude Code 已登录的账号，不必在这里再填 Key",
    },
    {
        "key": "codex",
        "label": "Codex CLI",
        "commands": ("codex",),
        "version_args": ("--version",),
        "chat_args": ("exec",),
        "chat_style": "argv",
        "hint": "用你本机 Codex CLI 已登录的账号",
    },
    {
        "key": "gemini",
        "label": "Gemini CLI",
        "commands": ("gemini",),
        "version_args": ("--version",),
        "chat_args": ("-p",),
        "chat_style": "argv",
        "hint": "Google 的 Gemini CLI，用本机已登录的账号",
    },
    {
        "key": "qwen",
        "label": "Qwen Code",
        "commands": ("qwen", "qwen-code"),
        "version_args": ("--version",),
        "chat_args": ("-p",),
        "chat_style": "argv",
        "hint": "通义千问的 Qwen Code（headless 模式）",
    },
    {
        "key": "opencode",
        "label": "OpenCode",
        "commands": ("opencode",),
        "version_args": ("--version",),
        "chat_args": ("-p",),
        "chat_style": "argv",
        "hint": "注意：它在非交互模式下会自行批准自己的操作，问问题可以，别拿它改项目",
    },
    {
        "key": "cursor-agent",
        "label": "Cursor CLI",
        "commands": ("cursor-agent",),
        "version_args": ("--version",),
        "chat_args": ("-p",),
        "chat_style": "argv",
        "hint": "不加 --force，所以它只给建议、不会真的改你的文件",
    },
)

BY_KEY: dict[str, dict] = {p["key"]: p for p in PROVIDERS}

_NPM_EXT = (".cmd", ".exe", ".bat", ".ps1", "")

#: 用户手动指定的路径 / 目录 / 自定义命令从 .env 读，**每次调用都重读**，
#: 这样在界面上填完点一下「重新检测」就生效，不用重启程序。
_SETTING_NAMES = ("CLI_PATHS", "CLI_DIRS", "CLI_CUSTOM")


class CLIBridgeError(RuntimeError):
    """CLI 这一侧出的问题（没装、没登录、超时）。"""


# --------------------------------------------------------------------------
# 读配置（容错：读不到就用默认值，绝不因为配置问题抛异常）
# --------------------------------------------------------------------------
def _setting(name: str, default):
    try:
        from src import config
    except Exception:
        return default
    return getattr(config, name, default)


def manual_paths() -> dict[str, str]:
    """手动指定的 CLI 路径：{key: 路径}。"""
    raw = _setting("CLI_PATHS", {}) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if str(v).strip()}


def extra_dirs() -> list[Path]:
    """额外要去找命令的目录（用户在 .env / 界面里加的）。"""
    raw = _setting("CLI_DIRS", ()) or ()
    out: list[Path] = []
    for item in raw:
        text = str(item).strip()
        if text:
            out.append(Path(text).expanduser())
    return out


def custom_spec() -> dict:
    """把 .env 里的自定义命令拼成一个和内置项同构的 spec。没有就返回空 dict。"""
    data = _setting("CLI_CUSTOM", {}) or {}
    if not isinstance(data, dict) or not str(data.get("exe") or "").strip():
        return {}
    return {
        "key": CUSTOM_KEY,
        "label": str(data.get("label") or "自定义命令").strip(),
        "commands": (str(data["exe"]).strip(),),
        "version_args": ("--version",),
        "chat_args": tuple(_split_args(str(data.get("args") or ""))),
        "chat_style": "stdin" if str(data.get("style") or "").lower() == "stdin" else "argv",
        "hint": "你在「CLI 设置」里填的自己那个命令",
        #: 自定义命令不保证支持 --version，所以版本探不通不算它坏
        "version_optional": True,
    }


def _split_args(text: str) -> list[str]:
    """把 `ask --json` 这样的字符串切成参数表。支持用双引号包住带空格的一段。"""
    out: list[str] = []
    buf = ""
    quoted = False
    for char in str(text or ""):
        if char == '"':
            quoted = not quoted
            continue
        if char.isspace() and not quoted:
            if buf:
                out.append(buf)
                buf = ""
            continue
        buf += char
    if buf:
        out.append(buf)
    return out


def by_key(provider: str | dict | None) -> dict:
    """按 key / 标签找一个 spec；也认自定义命令。找不到返回空 dict。"""
    if isinstance(provider, dict):
        return dict(provider)
    text = str(provider or "").strip()
    if not text:
        return {}
    low = text.lower()
    for spec in PROVIDERS:
        if spec["key"] == low or spec["label"].lower() == low:
            return dict(spec)
    spec = custom_spec()
    if spec and (low == CUSTOM_KEY or low == spec["label"].lower()):
        return spec
    return {}


def catalog() -> list[dict]:
    """界面上「有哪些可以选」的清单（内置 + 自定义）。"""
    items = [{"key": s["key"], "label": s["label"], "hint": s.get("hint", "")} for s in PROVIDERS]
    spec = custom_spec()
    if spec:
        items.append({"key": spec["key"], "label": spec["label"], "hint": spec.get("hint", "")})
    return items


# --------------------------------------------------------------------------
# 找程序
# --------------------------------------------------------------------------
def _windows_dirs() -> list[Path]:
    home = Path.home()
    appdata = Path(os.getenv("APPDATA") or (home / "AppData" / "Roaming"))
    local = Path(os.getenv("LOCALAPPDATA") or (home / "AppData" / "Local"))
    scoop = Path(os.getenv("SCOOP") or (home / "scoop"))
    return [
        appdata / "npm",                       # npm -g 的默认位置（claude / codex 都在这儿）
        local / "pnpm",
        local / "Yarn" / "bin",
        local / "Programs",
        local / "Microsoft" / "WindowsApps",
        scoop / "shims",
        Path("C:/ProgramData/scoop/shims"),
        Path("C:/ProgramData/chocolatey/bin"),
        home / ".bun" / "bin",
        home / ".volta" / "bin",
        home / ".cargo" / "bin",
        home / ".local" / "bin",
        home / ".cursor" / "bin",
        home / ".npm-global" / "bin",
    ]


def _posix_dirs() -> list[Path]:
    home = Path.home()
    return [
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
        home / ".local" / "bin",
        home / ".bun" / "bin",
        home / ".volta" / "bin",
        home / ".cargo" / "bin",
        home / ".npm-global" / "bin",
    ]


#: 记住上一次「找过哪些目录」，找不到时好告诉用户我们找过哪儿
_LAST_LOOKED: list[str] = []


def search_dirs() -> list[Path]:
    """自动探测会去看的所有目录（含用户自己加的），去重、只留存在的。"""
    global _LAST_LOOKED
    raw = list(_windows_dirs() if os.name == "nt" else _posix_dirs())
    raw += extra_dirs()
    # 项目里本地装的 CLI 也认（别人可能 `npm i -D @anthropic-ai/claude-code`）
    raw.append(Path.cwd() / "node_modules" / ".bin")
    # 我们自己的 exe / 脚本所在目录
    try:
        raw.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass

    seen: set[str] = set()
    out: list[Path] = []
    for folder in raw:
        try:
            key = str(folder).lower()
            if key in seen or not folder.is_dir():
                continue
        except OSError:
            continue
        seen.add(key)
        out.append(folder)
    _LAST_LOOKED = [str(p) for p in out]
    return out


def _from_override(name: str, override: str) -> str:
    """按用户手写的路径找命令：路径可以是文件本身，也可以是它所在的目录。"""
    target = Path(str(override)).expanduser()
    try:
        if target.is_file():
            return str(target)
        if target.is_dir():
            for ext in _NPM_EXT:
                candidate = target / f"{name}{ext}"
                if candidate.is_file():
                    return str(candidate)
    except OSError:
        return ""
    return ""


def resolve_exe(name: str, override: str = "") -> str:
    """找这个命令，找不到返回空字符串。

    顺序：手动指定的路径 → PATH → 常见安装目录清单 → 用户加的目录。
    为什么不只用 shutil.which：GUI 从桌面图标启动时，PATH 往往是系统默认的一份，
    不含用户自己装的 npm 全局目录 —— 于是「终端里明明有 claude」，
    但按钮点了说没装。所以自己再找一遍。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    if override:
        found = _from_override(text, override)
        if found:
            return found
    found = shutil.which(text)
    if found:
        return found
    for folder in search_dirs():
        for ext in _NPM_EXT:
            candidate = folder / f"{text}{ext}"
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return ""


def _argv(exe: str, args: tuple[str, ...] | list[str]) -> list[str]:
    """拼出真正要执行的命令行。

    Windows 上 `claude` 其实是 `claude.cmd`（npm 的批处理包装），
    而 CreateProcess **不能直接执行 .cmd/.bat**，必须经 cmd.exe 转一手。
    这里只做这一件事 —— 不把用户输入拼进命令行（prompt 走 stdin 或 argv 列表），
    所以不需要担心注入。
    """
    if os.name == "nt" and str(exe).lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(exe), *args]
    return [str(exe), *args]


def _run(cmd: list[str], *, timeout: int, stdin_text: str = "",
         cwd: str | Path | None = None) -> tuple[int, str, str]:
    """跑一条外部命令，返回 (退出码, stdout, stderr)。任何异常都翻成文本，不往外抛。"""
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text if stdin_text else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            # Windows 上别弹出黑框
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"超时（超过 {timeout} 秒没有结果）"
    except FileNotFoundError:
        return -2, "", "找不到这个命令"
    except OSError as exc:
        return -3, "", f"{type(exc).__name__}: {exc}"


def _first_line(text: str, limit: int = 120) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------
def _missing_note(spec: dict, override: str) -> str:
    """找不到命令时，要说清「找过哪里」和「你该做什么」。"""
    names = " / ".join(spec["commands"])
    key = spec["key"]
    head = (
        f"「{override}」用不了（路径不存在，或者那个目录里没有 {names}）。"
        if override else
        f"没找到这个命令：{names}。"
    )
    looked = _LAST_LOOKED[:3]
    where = ("；".join(looked) + " …") if looked else "（没有可搜索的目录）"
    return (
        head + "\n"
        f"    找过：PATH 和 {len(_LAST_LOOKED)} 个常见安装位置（{where}）\n"
        "    三种办法，任选一种，改完点「重新检测」，不用重启：\n"
        "      ① 界面点「CLI 设置…」，直接填它在你机器上的完整路径；\n"
        f"      ② .env 里写 COGNITRAIL_CLI_PATHS={key}=完整路径；\n"
        "      ③ 把它所在的目录加到 .env 的 COGNITRAIL_CLI_DIRS（多个用 ; 隔开），"
        "或直接加进系统 PATH。"
    )


def probe(provider: dict | str) -> dict:
    """探一个 CLI：装没装、能不能跑、什么版本、路径是怎么来的。

    永远返回一个结构化的 dict，不抛异常 —— 界面上这个按钮的意义就是
    「看看行不行」，行不行都得有个明确答复。
    """
    spec = by_key(provider)
    if not spec:
        return {"key": str(provider), "label": str(provider), "ready": False,
                "path": "", "version": "", "note": "不认识的 CLI", "origin": ""}

    key = spec["key"]
    override = manual_paths().get(key, "")
    exe = ""
    origin = ""
    if override:
        exe = _from_override(spec["commands"][0], override)
        if exe:
            origin = "手动指定"
    if not exe:
        for command in spec["commands"]:
            exe = resolve_exe(command)
            if exe:
                origin = "自动找到"
                break

    result = {
        "key": key,
        "label": spec["label"],
        "path": exe,
        "origin": origin,
        "version": "",
        "ready": False,
        "note": "",
        "hint": spec.get("hint", ""),
        "optional_version": bool(spec.get("version_optional")),
    }
    if not exe:
        search_dirs()  # 刷一下「找过哪些目录」，好写进提示里
        result["note"] = _missing_note(spec, override)
        return result

    # 手动指定的路径没能用上，但自动探测找到了 —— 得说清楚，否则用户会以为
    # 自己刚填的路径生效了，下回换了机器又困惑。
    prefix = ""
    if override and origin != "手动指定":
        prefix = f"（你手动指定的「{override}」用不了，下面这个是自动找到的）\n    "

    code, out, err = _run(_argv(exe, spec["version_args"]), timeout=PROBE_TIMEOUT)
    version = _first_line(out) or _first_line(err)
    result["version"] = version
    if code == 0:
        result["ready"] = True
        result["note"] = prefix + "可用" + (f"（{version}）" if version else "")
    elif result["optional_version"]:
        # 自定义命令：不保证有 --version。文件在就允许用，但把话说清楚。
        result["ready"] = True
        result["note"] = prefix + "文件在，能用；但它没回应 --version，无法确认版本"
    else:
        result["note"] = prefix + (
            f"命令在（{exe}），但 `{' '.join(spec['version_args'])}` 没跑通："
            f"rc={code} {version or err.strip()[:120]}"
        )
    return result


def detect() -> list[dict]:
    """把内置的几个 CLI（外加自定义的那个）都探一遍。"""
    specs = list(PROVIDERS)
    custom = custom_spec()
    if custom:
        specs.append(custom)
    return [probe(spec) for spec in specs]


def any_ready(reports: list[dict] | None = None) -> dict | None:
    """挑一个能用的出来（界面默认选它）。"""
    for item in reports if reports is not None else detect():
        if item.get("ready"):
            return item
    return None


# --------------------------------------------------------------------------
# 对话
# --------------------------------------------------------------------------
def build_prompt(question: str, *, project: str | Path | None = None,
                 context: str = "", tone_mode: str | None = None) -> str:
    """把「在哪儿问、问什么、用什么口吻答」拼成一段话。

    带一句项目位置是有意的：CLI 在这个目录里跑，它会自己去读文件，
    所以「这个项目为什么这么写」这类问题才答得上来。
    带上当前的写法档位：用户选了小白模式，那这儿问出来的答案也该是大白话 ——
    否则同一个窗口里卡片是白话、答疑是术语，看着像两个程序。
    """
    parts: list[str] = []
    if project:
        parts.append(f"项目根目录：{project}")
    parts.append("你是被叫来答疑的。请直接回答下面的问题，用中文，不要复述这段话。")
    try:
        from src import tone

        style = tone.one_line(tone_mode)
    except Exception:
        style = ""
    if style:
        parts.append(style)
    if context.strip():
        parts.append("【已知背景】\n" + context.strip())
    parts.append("【问题】\n" + (question or "").strip())
    return "\n\n".join(parts)


def _extract_stream_text(line: str) -> str:
    """从 stream-json 的一行里尽量挖出正文。

    格式随 CLI 版本会变，所以这里只认「明显是正文」的那几种形状，
    挖不出来就返回空串（外层会把它当噪音丢掉），绝不把 JSON 原文当答案显示给用户。
    """
    line = (line or "").strip()
    if not line or not line.startswith("{"):
        return ""
    try:
        data = json.loads(line)
    except ValueError:
        return ""

    if not isinstance(data, dict):
        return ""
    kind = data.get("type")

    # 最终结果：直接用完整文本
    if kind == "result":
        return str(data.get("result") or "")

    # 增量：assistant 消息里的 text 块
    message = data.get("message")
    if isinstance(message, dict):
        chunks = message.get("content")
        if isinstance(chunks, list):
            texts = [
                str(block.get("text") or "")
                for block in chunks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "".join(t for t in texts if t)

    delta = data.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        return str(delta.get("text") or "")
    return ""


def chat(question: str, *, provider: str = "claude", project: str | Path | None = None,
         context: str = "", timeout: int = CHAT_TIMEOUT, tone_mode: str | None = None,
         stream: bool = False, on_delta=None) -> dict:
    """问一句，等回答。

    stream=True 时走 `--output-format stream-json`，边出边回调 on_delta(chunk)，
    界面就能像终端一样逐段显示。**默认不开**：实测同一个问题，
    stream-json 那条路要 141 秒才回来，普通模式 3.2 秒 ——
    多出来的时间花在 CLI 自己身上，对体验是净亏。想试就显式传 stream=True。

    返回 {"ok": bool, "text": str, "error": str, "provider": str, "cmd": str}。
    """
    spec = by_key(provider)
    if not spec:
        return {"ok": False, "text": "", "error": f"不认识的 CLI：{provider}", "provider": provider, "cmd": ""}

    info = probe(spec)
    if not info["ready"]:
        return {"ok": False, "text": "", "error": info["note"], "provider": provider,
                "cmd": info.get("path", "")}

    exe = info["path"]
    args = list(spec["chat_args"])
    if stream and spec["key"] == "claude":
        # -p 后面没有参数时，prompt 从 stdin 进来
        args += ["--output-format", "stream-json", "--verbose"]

    prompt = build_prompt(question, project=project, context=context, tone_mode=tone_mode)

    if spec["chat_style"] == "stdin":
        stdin_text = prompt
    else:
        args.append(prompt)
        stdin_text = ""

    cmd = _argv(exe, args)
    shown = " ".join([Path(str(exe)).name, *args[:2]]) + " …"

    if not stream or spec["key"] != "claude":
        code, out, err = _run(cmd, timeout=timeout, stdin_text=stdin_text, cwd=project)
        text = (out or "").strip()
        if code != 0 and not text:
            return {"ok": False, "text": "", "error": _explain_failure(err, code), "provider": provider,
                    "cmd": shown}
        # 退出码 0 也可能是报错（CLI 把错误当正文写出来了）—— 单判一次
        if looks_like_error(text):
            return {"ok": False, "text": "", "error": _as_error(text, spec["label"]),
                    "provider": provider, "cmd": shown}
        return {"ok": True, "text": text, "error": "", "provider": provider, "cmd": shown}

    # —— 流式：逐行读 stdout ——
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(project) if project else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except OSError as exc:
        return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}",
                "provider": provider, "cmd": shown}

    try:
        if stdin_text:
            proc.stdin.write(stdin_text)
        proc.stdin.close()
    except (OSError, ValueError):
        pass

    collected: list[str] = []
    final = ""
    for raw in proc.stdout or []:
        chunk = _extract_stream_text(raw)
        if not chunk:
            continue
        if raw.strip().startswith('{"type":"result"') or '"type": "result"' in raw:
            final = chunk
            continue
        collected.append(chunk)
        if on_delta:
            try:
                on_delta(chunk)
            except Exception:
                pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        err = f"超时（超过 {timeout} 秒没有结果）"
        partial = final or "".join(collected)
        return {"ok": bool(partial), "text": partial, "error": err, "provider": provider, "cmd": shown}

    stderr_text = ""
    try:
        stderr_text = (proc.stderr.read() or "") if proc.stderr else ""
    except (OSError, ValueError):
        pass

    text = (final or "".join(collected)).strip()
    if proc.returncode != 0 and not text:
        return {"ok": False, "text": "", "error": _explain_failure(stderr_text, proc.returncode),
                "provider": provider, "cmd": shown}
    if looks_like_error(text):
        return {"ok": False, "text": "", "error": _as_error(text, spec["label"]),
                "provider": provider, "cmd": shown}
    return {"ok": True, "text": text, "error": "", "provider": provider, "cmd": shown}


def _explain_failure(stderr_text: str, code: int) -> str:
    """把 CLI 的报错翻成「你该做什么」。"""
    blob = (stderr_text or "").strip()
    low = blob.lower()
    if code == -1 or "timed out" in low:
        return "等太久了，没有结果。（网络慢或模型忙，稍后再试）"
    if code == -2:
        return "找不到这个命令。"
    if "not logged in" in low or "login" in low or "unauthorized" in low or "401" in low:
        return ("这个 CLI 还没登录（或登录过期）。先在自己的终端里跑一次它、完成登录，"
                "再回到这里试。\n" + blob[:400])
    if "rate limit" in low or "429" in low:
        return "触发了接口的频率限制，等一会儿再问。\n" + blob[:400]
    if blob:
        return f"CLI 报错（退出码 {code}）：\n{blob[:600]}"
    return f"CLI 没有输出，退出码 {code}。"


#: 这些词出现在一段**很短的**回复里，基本可以断定那是报错而不是回答
_ERROR_MARKERS: tuple[str, ...] = (
    "api error", "failed to authenticate", "authentication_error", "invalid api key",
    "not logged in", "unauthorized", "permission denied", "subscription is required",
    "rate limit", "quota exceeded", "insufficient quota", "no such file",
)


def looks_like_error(text: str) -> bool:
    """这段文字是 CLI 的报错吗？

    为什么必须判一次（实测踩到的坑）：
        claude 在没登录 / 没订阅的时候，会把
        「Failed to authenticate. API Error: 403 ...」当成**正常回答**写到 stdout，
        退出码还是 0。如果不认，界面上就会把这行错误当成 AI 的回答展示给用户 ——
        用户会以为 AI 在说胡话，而真正的问题（凭据）反而被藏起来了。

    判据刻意保守：只有**短**文本里出现明确的错误特征才算，
    免得把 AI 正常回答里恰好提到「认证」「权限」的那句话误判成报错。
    """
    blob = (text or "").strip()
    if not blob or len(blob) > 400:
        return False
    low = blob.lower()
    if low.startswith(("error", "failed", "api error")):
        return True
    return any(marker in low for marker in _ERROR_MARKERS)


def _as_error(text: str, provider_label: str) -> str:
    """把「被当成正文输出的报错」翻成一句人话，后面附原文。"""
    return (
        f"{provider_label} 能跑起来，但它连不上模型服务（这次没有拿到回答）。"
        "常见原因：这个 CLI 还没登录、登录过期，或它的配置指向了一个你没有订阅的网关。\n"
        "→ 先在自己的终端里直接跑一次这个 CLI，把它调通，再回到这里问。\n"
        f"原文：{text.strip()[:400]}"
    )


# --------------------------------------------------------------------------
# 命令行入口（想不开窗口、直接在终端问一句的时候用）
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    from src import config

    config.enable_utf8_stdout()

    parser = argparse.ArgumentParser(description="看看本机能不能用已装的 AI 命令行对话")
    parser.add_argument("--check", action="store_true", help="只探测，不提问")
    parser.add_argument("--provider", default="",
                        help="claude / codex / gemini / qwen / opencode / cursor-agent / custom，默认自动挑一个能用的")
    parser.add_argument("--project", default="", help="在哪个目录里问（CLI 会读那里的文件）")
    parser.add_argument("question", nargs="*", help="要问的问题")
    args = parser.parse_args(argv)

    reports = detect()
    print("=" * 62)
    print("  本机 CLI 探测")
    print("=" * 62)
    for item in reports:
        mark = "OK  " if item["ready"] else "FAIL"
        print(f"[{mark}] {item['label']:<14} {item.get('path') or '（没找到）'}")
        if item.get("origin"):
            print(f"       （{item['origin']}）")
        for line in str(item["note"]).splitlines():
            print("       " + line)
    print()
    if not any(item["ready"] for item in reports):
        print("一个都没找到。往下看每一条里的「三种办法」——")
        print("最省事的是在 .env 里写一行：")
        print("  COGNITRAIL_CLI_PATHS=claude=C:\\Users\\你\\AppData\\Roaming\\npm\\claude.cmd")

    if args.check or not args.question:
        if not args.check:
            print("（没有给问题。想提问就直接把问题写在后面，例如：）")
            print('  cognitrail cli-chat --provider claude "这个项目的闸门是怎么校验的"')
        return 0 if any(item["ready"] for item in reports) else 1

    provider = args.provider or (any_ready(reports) or {}).get("key", "")
    if not provider:
        print("一个可用的 CLI 都没有，先装一个再回来。")
        return 1

    question = " ".join(args.question)
    print(f"[问] {provider} ← {question}\n")
    outcome = chat(question, provider=provider, project=args.project or Path.cwd(), stream=False)
    if outcome["ok"]:
        print(outcome["text"])
        return 0
    print("失败：" + outcome["error"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
