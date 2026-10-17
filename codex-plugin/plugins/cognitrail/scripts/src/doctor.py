"""知迹 CogniTrail · 自检工具。

它只回答一个问题：**我这台机器，现在能不能跑出真实卡片？**

用法：
    python src/doctor.py                       # 用 .env 里的默认仓库，顺便联网测 Key
    python src/doctor.py --repo D:\\code\\proj    # 指定要分析的仓库
    python src/doctor.py --offline             # 跳过联网，只查本地

为什么要有这个文件：
    你把项目发给别人（或者换了台电脑），第一件事一定是「跑不起来」。
    与其让对方把报错截图发给你，不如让他先跑一条命令 —— 
    它会逐项说清哪一项过了、哪一项没过、以及没过时该改哪个文件。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config  # noqa: E402

OK = "  [OK]  "
BAD = "  [!!]  "
WARN = "  [~]   "


class Report:
    """收集检查结果，最后统一打印。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []  # (级别, 标题, 说明/建议)

    def ok(self, title: str, detail: str = "") -> None:
        self.rows.append(("ok", title, detail))

    def bad(self, title: str, detail: str = "") -> None:
        self.rows.append(("bad", title, detail))

    def warn(self, title: str, detail: str = "") -> None:
        self.rows.append(("warn", title, detail))

    def print(self) -> tuple[int, int]:
        bad = sum(1 for level, _, _ in self.rows if level == "bad")
        warn = sum(1 for level, _, _ in self.rows if level == "warn")
        for level, title, detail in self.rows:
            mark = {"ok": OK, "bad": BAD, "warn": WARN}[level]
            print(f"{mark}{title}")
            if detail:
                for line in detail.splitlines():
                    print(f"          {line}")
        return bad, warn


def _mask(key: str) -> str:
    """只露头尾，中间打码 —— 截图发出去也不怕。"""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:7]}{'*' * 8}{key[-4:]}"


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------
def check_python(rep: Report) -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        rep.ok(f"Python {major}.{minor}", f"解释器：{sys.executable}")
    else:
        rep.bad(f"Python {major}.{minor} 版本过低", "需要 3.10 及以上（用到了 `X | None` 类型写法）")

    try:
        import dotenv  # noqa: F401
    except ImportError:
        rep.ok("零第三方依赖", "没有装 python-dotenv —— 用内置解析器读 .env，功能不受影响")
    else:
        rep.ok("依赖检查通过", "python-dotenv 已安装（可选增强）")


def check_git(rep: Report) -> None:
    """git 现在只是「可选」——数据源早就换成会话文件了。"""
    exe = shutil.which("git")
    if not exe:
        rep.warn(
            "找不到 git（不影响使用）",
            "知迹现在从 Codex / WorkBuddy / Claude 的会话文件取内容，不需要 git。\n"
            "只有老的那条 git diff 链路才依赖它。",
        )
        return
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        rep.ok(f"git 可用（{out.stdout.strip()}）", exe)
    except Exception as exc:  # pragma: no cover
        rep.warn("git 无法执行（不影响使用）", str(exc))


def check_sources(rep: Report) -> None:
    """三个会话源查一遍 —— 这才是现在真正的「数据从哪来」。"""
    try:
        from src import sources
    except Exception as exc:  # pragma: no cover
        rep.bad("多源模块加载失败", f"{type(exc).__name__}: {exc}")
        return

    found = []
    for name in sources.ORDER:
        adapter = sources.get(name)
        label = sources.LABELS.get(name, name)
        if not adapter.available():
            rep.warn(f"{label}：没有会话目录", f"{adapter.root()}\n用过一次就会自动出现")
            continue
        try:
            files = adapter.list_sessions()
        except Exception as exc:
            rep.bad(f"{label}：读会话目录失败", f"{adapter.root()}\n{exc}")
            continue
        if files:
            latest = files[0]
            info = sources.peek(latest, name)
            rep.ok(
                f"{label}：{len(files)} 份会话",
                f"{adapter.root()}\n最近一份：{latest.name}"
                + (f"\n项目：{info.get('cwd')}" if info.get("cwd") else ""),
            )
            found.append(name)
        else:
            rep.warn(f"{label}：目录是空的", str(adapter.root()))

    if found:
        rep.ok("至少有一个源可用", "可用的源：" + "、".join(sources.LABELS[n] for n in found))
    else:
        rep.bad(
            "三个源都没有会话",
            "先用一次 Codex / WorkBuddy / Claude，再回来跑自检。\n"
            "知迹只读它们自己的会话文件，不联网、不改动它们。",
        )


def check_env(rep: Report) -> None:
    # 用 config.ROOT 而不是 __file__ 的上一级 —— exe 形态下后者指向临时目录
    env_file = config.ROOT / ".env"
    if env_file.is_file():
        rep.ok(".env 存在", str(env_file))
    else:
        rep.warn(
            "没有 .env 文件",
            "现在会走离线 Mock。要调真模型：\n"
            "复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY，并把 COGNITRAIL_MOCK 改成 0",
        )

    if config.API_KEY:
        rep.ok(f"已配置 API Key：{_mask(config.API_KEY)}", f"长度 {len(config.API_KEY)}")
    else:
        rep.warn("没有配置 API Key", "没有 Key 也能用 —— 会自动回退到离线 Mock")

    if config.MOCK:
        rep.warn(
            f"当前是离线 Mock 模式（原因：{config.MOCK_REASON}）",
            "Mock 卡片的 status 会标成 mock，不会被当成真实产出",
        )
    else:
        rep.ok(f"在线模式：{config.MODEL} @ {config.BASE_URL}")


def _post_chat(base_url: str, key: str, model: str, proxy: dict | None, timeout: int = 30):
    """发一个最小请求。返回 (成功?, 说明)。"""
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        # 官方 DeepSeek 两种都认；带 /v1 更通用
        pass
    url = url + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "只回两个字：收到"}],
            "max_tokens": 8,
        }
    ).encode()

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy or {}))
    try:
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return True, f"模型={data.get('model')}  回话={content!r}  usage={data.get('usage')}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return False, f"HTTP {exc.code}  {body}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_network(rep: Report, offline: bool) -> None:
    if offline:
        rep.warn("已跳过联网检查（--offline）")
        return

    if not config.API_KEY:
        rep.warn("没有 Key，跳过联网检查")
        return

    print("          …正在连 %s（约 10 秒）" % config.BASE_URL)
    ok, detail = _post_chat(config.BASE_URL, config.API_KEY, config.MODEL, proxy=None)

    if ok:
        rep.ok(f"接口连通、Key 有效（直连）", detail)
        return

    # 直连失败 —— 可能本机需要代理。回头看环境变量里有没有。
    env_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if env_proxy:
        ok2, detail2 = _post_chat(
            config.BASE_URL, config.API_KEY, config.MODEL,
            proxy={"https": env_proxy, "http": env_proxy},
        )
        if ok2:
            rep.ok(f"接口连通、Key 有效（经代理 {env_proxy}）", detail2)
            return

    rep.bad("接口调用失败", detail + "\n\n逐条排查：\n"
            "  1. Key 是不是这家平台的？DeepSeek 官方 Key 只对 https://api.deepseek.com 有效\n"
            "  2. 若用中转/聚合服务，把 DEEPSEEK_BASE_URL 改成对方给的地址\n"
            "  3. 模型名对不对？官方是 deepseek-chat / deepseek-reasoner\n"
            "  4. 需要代理才能出网的话，设置 HTTPS_PROXY 环境变量后重试")
    rep.warn("不影响离线验证", "仍可用 COGNITRAIL_MOCK=1 跑通格式、闸门、落盘")


def check_repo(rep: Report, repo_arg: str) -> None:
    raw = repo_arg or config.DEFAULT_REPO
    if not raw:
        # 提示里给「当前形态下真能敲的那条命令」—— exe 形态下说 python src/... 是废话
        howto = ("cognitrail run --repo <你的项目路径>" if config.FROZEN
                 else "python src/cognitrail.py run --repo <你的项目路径>")
        rep.warn("没有指定目标项目", f"跑一次：{howto}")
        return

    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        rep.bad(f"仓库目录不存在：{repo}")
        return

    git_dir = repo / ".git"
    if not git_dir.exists():
        rep.ok(
            f"目标项目可用：{repo}",
            "它不是 git 仓库 —— 没关系，知迹现在不靠 git（会话文件里就有全部内容）",
        )
    else:
        rep.ok(f"目标仓库可用：{repo}")

    if git_dir.exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--oneline", "-n", "5"],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace",
            )
            commits = [line for line in out.stdout.splitlines() if line.strip()]
            if commits:
                rep.ok(f"最近 {len(commits)} 次提交", "\n".join(commits))
            else:
                rep.warn("这个仓库还没有任何提交", "不影响使用（卡片的证据来自会话 + 当前文件）")
        except Exception as exc:
            rep.warn("读提交记录失败", str(exc))

    decisions = repo / "docs" / "decisions"
    if decisions.is_dir():
        cards = sorted(decisions.glob("*.md"))
        cards = [c for c in cards if c.name != "SUMMARY.md"]
        rep.ok(f"已有 {len(cards)} 张决策卡片", str(decisions))
    else:
        rep.ok("尚未生成卡片", f"首次运行会创建 {decisions}")


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="知迹 CogniTrail 自检 —— 检查这台机器能不能跑出真实卡片",
    )
    parser.add_argument("--repo", default="", help="要分析的仓库路径")
    parser.add_argument("--offline", action="store_true", help="跳过联网检查")
    args = parser.parse_args()

    config.enable_utf8_stdout()

    print("=" * 62)
    print("  知迹 CogniTrail · 环境自检")
    print("=" * 62)

    rep = Report()
    check_python(rep)
    check_sources(rep)
    check_env(rep)
    check_git(rep)
    check_repo(rep, args.repo)
    check_network(rep, args.offline)

    print("-" * 62)
    bad, warn = rep.print()
    print("-" * 62)

    if bad == 0 and warn == 0:
        print("结论：一切就绪，可以开工了。")
    elif bad == 0:
        print(f"结论：可以跑，但有 {warn} 处提醒（多数不影响离线验证）。")
    else:
        print(f"结论：有 {bad} 项阻塞、{warn} 项提醒，先处理 [!!] 那几行。")
    print()
    print("接下来：")
    print("  双击 知迹CogniTrail.exe 打开窗口（看对话和卡片）")
    print("  或   cognitrail watch            开着它，然后用 Codex / WorkBuddy / Claude")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
