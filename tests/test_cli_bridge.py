"""CLI 桥的测试：探测、错误识别、提示词拼装、失败路径。

    python tests/test_cli_bridge.py

这里**刻意不去真调 claude / codex**：
    真调要花用户的额度、依赖他有没有登录，而且结果不确定 ——
    那样的测试只会在别人机器上红。所以只用 `sys.executable`
    伪造一个「一定存在、一定能跑」的命令来验证探测和调用的骨架，
    再单测纯函数（错误识别、prompt 拼装、Windows 的 .cmd 包装）。

最要紧的断言：
    1. **没装不炸**：一个不存在的命令，探测要给出结构化的「不可用 + 怎么装」，
       而不是抛异常，也不是装作可用。
    2. **报错不算回答**：claude 在未登录时会把 `Failed to authenticate. API Error: 403`
       当成正常输出、退出码还是 0 —— 必须识别成错误，不许当回答展示。
    3. **不替用户绕过权限**：命令行里不许出现 --dangerously-skip-permissions
       这类开关（这个按钮的定位是问问题，不是让另一个 AI 去改文件）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import cli_bridge  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


# --------------------------------------------------------------------------
def test_resolve() -> None:
    print("\n[1/5] 找命令：找不到就老实说找不到")
    check("不存在的命令 → 空串", cli_bridge.resolve_exe("这个命令肯定不存在-xyz-123") == "")
    python = cli_bridge.resolve_exe(Path(sys.executable).name) or cli_bridge.resolve_exe(sys.executable)
    check("能找到一个确定存在的命令（python）", bool(python), python)
    check("返回的是路径而不是命令名", os.path.sep in python or "/" in python, python)


def test_probe_missing() -> None:
    print("\n[2/5] 探测：没装也要给出结构化答复 + 怎么装")
    spec = {"key": "nope", "label": "假 CLI", "commands": ("肯定没有这个命令-xyz",),
            "version_args": ("--version",), "chat_args": (), "chat_style": "argv"}
    info = cli_bridge.probe(spec)
    check("ready=False", info["ready"] is False)
    check("说清是「没找到」", "没找到" in info["note"], info["note"])
    check("给出了安装/排查方向", "PATH" in info["note"], info["note"])
    check("path 是空串而不是 None", info["path"] == "")

    unknown = cli_bridge.probe("这个 provider 不存在")
    check("不认识的 provider 也不抛异常", unknown["ready"] is False and "不认识" in unknown["note"],
          unknown["note"])


def test_probe_works() -> None:
    print("\n[3/5] 探测：命令存在且能跑通时，要拿到版本")
    spec = {"key": "py", "label": "假 Python CLI",
            "commands": (Path(sys.executable).name,),
            "version_args": ("--version",), "chat_args": (), "chat_style": "argv"}
    info = cli_bridge.probe(spec)
    if not info["path"]:
        # 极端情况下 PATH 里没有 python（用绝对路径跑的），退回绝对路径再试
        spec["commands"] = (sys.executable,)
        info = cli_bridge.probe(spec)
    check("ready=True", info["ready"] is True, info["note"])
    check("拿到了版本号", "Python" in info["version"], info["version"])
    check("note 里带上了版本", "Python" in info["note"], info["note"])


def test_error_recognition() -> None:
    print("\n[4/5] 报错识别：CLI 把错误当正文吐出来时，不许当成回答")
    cases = [
        ("Failed to authenticate. API Error: 403 Upstream request failed", True),
        ("Error: something went wrong", True),
        ("unauthorized", True),
        ("You have hit your rate limit, please wait", True),
        ("1+1 等于 2。", False),
        ("这个项目的认证逻辑在 src/auth.py 里，用的是 JWT。", False),
        ("我把重试次数从 3 改成了 1，这样超时就少等一会儿。", False),
        ("很长的一段正常回答，只是里面凑巧提到了 api error 这个词。" * 20, False),
        ("", False),
    ]
    for text, want in cases:
        got = cli_bridge.looks_like_error(text)
        label = ("应当认出是报错" if want else "不该误判成报错") + f" :: {text[:34]}"
        check(label, got is want, f"want={want} got={got}")

    info = cli_bridge.detect()
    check("detect() 返回每一项都带 ready / note",
          all({"ready", "note"} <= set(item) for item in info), str(info)[:200])


def test_prompt_and_cmd() -> None:
    print("\n[5/7] 提问怎么递进去 / 命令行怎么拼")
    prompt = cli_bridge.build_prompt("这个项目为什么要两次校验？",
                                     project=r"C:\proj", context="卡片说：加了幂等校验")
    check("prompt 里带上了项目路径", "C:\\proj" in prompt, prompt[:120])
    check("prompt 里带上了问题", "为什么要两次校验" in prompt)
    check("prompt 里带上了背景", "幂等校验" in prompt)

    # Windows：npm 装的是 .cmd，必须经 cmd.exe 转一手，否则 CreateProcess 拒绝执行
    argv = cli_bridge._argv("C:\\some\\claude.cmd", ["-p"])
    if os.name == "nt":
        check(".cmd 被 cmd.exe 包了一层", argv[1] == "/c", str(argv))
        check("原始参数没丢", argv[-1] == "-p", str(argv))
    else:
        check("非 Windows 上原样执行", argv == ["C:\\some\\claude.cmd", "-p"], str(argv))
    check("普通可执行文件不加包装",
          cli_bridge._argv("/usr/bin/claude", ["-p"]) == ["/usr/bin/claude", "-p"])

    # 不替用户绕过权限 —— 这条是刻意的产品决策，别被后来的改动悄悄推翻。
    # 判的是**真正会执行的参数**（注释里提到这些词是为了说明「不传」，那不算）。
    for spec in cli_bridge.PROVIDERS:
        args = [str(a) for a in (*spec["chat_args"], *spec["version_args"])]
        for forbidden in ("dangerously-skip-permissions", "allowedTools", "yolo",
                          "full-auto", "bypassPermissions"):
            check(f"{spec['key']} 不会传 {forbidden}",
                  not any(forbidden in a for a in args), str(args))

    # 调用不存在的 provider：结构化失败，不抛异常
    outcome = cli_bridge.chat("随便问问", provider="不存在的")
    check("不认识的 provider → ok=False", outcome["ok"] is False)
    check("带上了可读的原因", "不认识" in outcome["error"], outcome["error"])
    check("字段齐全", {"ok", "text", "error", "provider", "cmd"} <= set(outcome),
          str(sorted(outcome)))


def test_more_providers() -> None:
    print("\n[6/7] 支持更多 CLI（别人的电脑上不一定是 Claude / Codex）")
    keys = [spec["key"] for spec in cli_bridge.PROVIDERS]
    for key in ("claude", "codex", "gemini", "qwen", "opencode", "cursor-agent"):
        check(f"内置支持 {key}", key in keys, str(keys))
    for spec in cli_bridge.PROVIDERS:
        check(f"{spec['key']} 有 chat_args", bool(spec["chat_args"]), str(spec))
        check(f"{spec['key']} 的 chat_style 合法",
              spec["chat_style"] in ("stdin", "argv"), spec["chat_style"])
        check(f"{spec['key']} 有版本探测参数", bool(spec["version_args"]), str(spec))
    check("每个都能按 key 找到", all(cli_bridge.by_key(s["key"]) for s in cli_bridge.PROVIDERS))
    check("按标签也能找到（界面上显示的是标签）", cli_bridge.by_key("Claude Code").get("key") == "claude")
    catalog = cli_bridge.catalog()
    check("catalog 每项都有 key/label", all({"key", "label"} <= set(item) for item in catalog),
          str(catalog)[:200])
    info = cli_bridge.detect()
    check("detect() 里带上了「路径是怎么来的」",
          all("origin" in item for item in info), str(info)[:160])


def test_for_other_machines() -> None:
    print("\n[7/7] 别人的电脑：手动指定路径 / 额外搜索目录 / 自定义命令")
    import tempfile

    from src import config

    paths_backup = dict(getattr(config, "CLI_PATHS", {}) or {})
    dirs_backup = tuple(getattr(config, "CLI_DIRS", ()) or ())
    custom_backup = dict(getattr(config, "CLI_CUSTOM", {}) or {})
    tmp = Path(tempfile.mkdtemp(prefix="cli-bridge-test-"))
    try:
        # ---- ① 手动指定路径：能压过自动探测 ----
        config.CLI_PATHS = {"claude": sys.executable}
        info = cli_bridge.probe("claude")
        check("手动指定的路径被用上了", info["path"] == sys.executable, info["path"])
        check("而且标明它是手动指定来的", info["origin"] == "手动指定", info["origin"])
        check("用了手动路径照样能探到版本", info["ready"] and "Python" in info["version"],
              f"{info['ready']} {info['version']}")

        # 手动指定的路径是错的、自动也找不到 → 要说清是那个路径的问题 + 怎么办
        config.CLI_PATHS = {"gemini": str(tmp / "不存在的目录" / "gemini.exe")}
        ghost = {"key": "gemini", "label": "假 Gemini", "commands": ("肯定没有-gemini-xyz",),
                 "version_args": ("--version",), "chat_args": ("-p",), "chat_style": "argv"}
        miss = cli_bridge.probe(ghost)
        check("ready=False", miss["ready"] is False)
        check("说清是「你指定的那个路径」用不了", "用不了" in miss["note"], miss["note"])
        check("给出「填路径」这条路", "COGNITRAIL_CLI_PATHS" in miss["note"], miss["note"])
        check("也提到界面上的「CLI 设置」", "CLI 设置" in miss["note"], miss["note"])
        check("还说清找过多少地方", "找过" in miss["note"], miss["note"])

        # ---- ② 额外搜索目录 ----
        fake = tmp / ("fakecli.cmd" if os.name == "nt" else "fakecli")
        fake.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
        config.CLI_DIRS = (str(tmp),)
        found = cli_bridge.resolve_exe("fakecli")
        check("额外目录里的命令能被找到", bool(found), found)
        check("找到的就是那个文件", Path(found).name.startswith("fakecli"), found)
        check("search_dirs() 里包含这个额外目录",
              any(str(tmp).lower() == str(p).lower() for p in cli_bridge.search_dirs()),
              str([str(p) for p in cli_bridge.search_dirs()])[:200])

        # ---- ③ 自定义命令（不是上面那几个 CLI 时用） ----
        config.CLI_CUSTOM = {"label": "我的网关", "exe": sys.executable,
                             "args": '--version "带 空格"', "style": "argv"}
        spec = cli_bridge.custom_spec()
        check("custom_spec 读得出来", spec.get("key") == cli_bridge.CUSTOM_KEY, str(spec))
        check("参数被切成表（引号里的空格不切）",
              spec["chat_args"][0] == "--version" and spec["chat_args"][1] == "带 空格",
              str(spec["chat_args"]))
        check("按 key 能找到它", cli_bridge.by_key("custom").get("label") == "我的网关")
        check("按名称也能找到它", cli_bridge.by_key("我的网关").get("key") == "custom")
        check("catalog 里出现了它", any(i["key"] == "custom" for i in cli_bridge.catalog()))

        info = cli_bridge.probe("custom")
        check("自定义命令能探通", info["ready"] is True, info["note"])
        check("自定义命令不强制要求 --version 能跑",
              "version_optional" in info and info["version_optional"] is True
              or "Python" in info["version"], str(info)[:160])

        # 自定义命令给了个跑不起来的可执行文件 → 也不许抛异常
        config.CLI_CUSTOM = {"label": "坏的", "exe": str(tmp / "没有这个.exe"),
                             "args": "", "style": "argv"}
        broken = cli_bridge.probe("custom")
        check("自定义命令指到不存在的文件 → 结构化失败，不抛异常",
              broken["ready"] is False and bool(broken["note"]), str(broken)[:160])

        config.CLI_CUSTOM = {}
        check("清掉之后就不再出现在清单里", cli_bridge.custom_spec() == {})

        # ---- ④ 问 AI 时也带当前写法 ----
        plain_q = cli_bridge.build_prompt("这句什么意思", tone_mode="plain")
        pro_q = cli_bridge.build_prompt("这句什么意思", tone_mode="pro")
        check("两档问出来的提示词不同", plain_q != pro_q)
        check("小白档要求用大白话答", "大白话" in plain_q, plain_q[:200])
        check("专家档不要求大白话", "大白话" not in pro_q)
        check("两档都带上问题本身", "这句什么意思" in plain_q and "这句什么意思" in pro_q)
    finally:
        config.CLI_PATHS = paths_backup
        config.CLI_DIRS = dirs_backup
        config.CLI_CUSTOM = custom_backup
        cli_bridge._LAST_LOOKED.clear()


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  CLI 桥 测试")
    print("=" * 62)
    test_resolve()
    test_probe_missing()
    test_probe_works()
    test_error_recognition()
    test_prompt_and_cmd()
    test_more_providers()
    test_for_other_machines()

    print("\n" + "=" * 62)
    if FAILED:
        print(f"  有 {len(FAILED)} 项失败：")
        for name in FAILED:
            print(f"    - {name}")
        print("=" * 62)
        raise SystemExit(1)
    print(f"  全部通过：{PASSED} 项检查")
    print("=" * 62)
    raise SystemExit(0)
