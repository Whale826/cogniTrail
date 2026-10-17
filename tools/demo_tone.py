# -*- coding: utf-8 -*-
"""演示「卡片写法」两档到底差在哪，并把产物留在 out/tone-demo/ 里。

    python tools/demo_tone.py

跑完看这几样：
    out/tone-demo-log.txt                    跑的过程 + 两档提示词的关键段落对比
    out/tone-demo/docs/decisions/*.md        同一份改动、两档各写一遍的卡片
    out/tone-demo/docs/decisions/dashboard.html   面板（小白写法的那张会带标签）

为什么要固化成脚本而不是手敲一遍：
    每次改提示词都想确认「两档真的分开了、而且没把事实字段弄丢」，
    手敲一遍要建仓库、造提交、切环境变量，容易漏。跑这个脚本就有结论。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO = ROOT / "out" / "tone-demo"
LOG = ROOT / "out" / "tone-demo-log.txt"

#: 演示用的改动：一段**确实有取舍**的代码 —— 重试次数、超时、
#: 还有一处「把异常吞掉」的写法，好让审计那边也有话说。
_BEFORE = '''"""把一个批次的记录推到远端。"""
import os

RETRIES = 3
TIMEOUT = 30
ENDPOINT = os.getenv("ENDPOINT", "http://127.0.0.1:9000")


def push(rows):
    if not rows:
        return 0
    if not ENDPOINT:
        raise ValueError("ENDPOINT 没配")
    return len(rows)


def fetch(url):
    return url
'''

_AFTER = '''"""把一个批次的记录推到远端。"""
import os

RETRIES = 1
TIMEOUT = 8
ENDPOINT = os.getenv("ENDPOINT", "http://127.0.0.1:9000")


def push(rows):
    try:
        if not rows:
            return 0
        return len(rows)
    except Exception:
        return 0


def fetch(url):
    return url
'''

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def force_rmtree(path: Path) -> None:
    """删目录。带 .git 的目录里常有只读文件，直接删会 OSError。"""
    if not path.exists():
        return

    def on_error(_func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            os.remove(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=on_error)


def sh(*cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(cmd), cwd=str(cwd), capture_output=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        say(f"  ! {' '.join(cmd)} → rc={proc.returncode}")
        say("    " + (proc.stderr or "").strip()[:300])
    return proc


def build_repo() -> None:
    force_rmtree(DEMO)
    (DEMO / "src").mkdir(parents=True, exist_ok=True)
    (DEMO / "src" / "push.py").write_text(_BEFORE, encoding="utf-8")
    sh("git", "init", "-q", cwd=DEMO)
    sh("git", "config", "user.email", "demo@example.com", cwd=DEMO)
    sh("git", "config", "user.name", "demo", cwd=DEMO)
    sh("git", "add", "-A", cwd=DEMO)
    sh("git", "commit", "-q", "-m", "初始：重试 3 次、超时 30 秒", cwd=DEMO)

    (DEMO / "src" / "push.py").write_text(_AFTER, encoding="utf-8")
    sh("git", "add", "-A", cwd=DEMO)
    sh("git", "commit", "-q", "-m", "重试降到 1 次、超时降到 8 秒", cwd=DEMO)


def between(text: str, start: str, end: str) -> str:
    """从提示词里截一段出来，方便并排看两档的差别。"""
    at = text.find(start)
    if at < 0:
        return "（这段没找到）"
    rest = text[at:]
    stop = rest.find(end, len(start))
    return rest[: stop if stop > 0 else 700].strip()


def main() -> int:
    # 把配置写到演示目录里，绝不碰用户真实的 ~/.cognitrail 与项目 .env
    os.environ["COGNITRAIL_HOME"] = str(DEMO)
    os.environ["COGNITRAIL_STATE"] = str(DEMO / "state")
    os.environ["COGNITRAIL_MOCK"] = "1"
    os.environ.pop("DEEPSEEK_API_KEY", None)

    from src import cardgen, cli_bridge, config, dashboard, session_source, tone

    say("=" * 70)
    say("  卡片写法（专业 / 小白）演示")
    say("=" * 70)

    # ---------------- 一、两档给模型的提示词差在哪 ----------------
    say("\n① 同一轮对话，两档给模型的提示词差在哪")
    turn = session_source.Turn(
        turn_id="demo",
        user_message="接口老是超时，把重试次数从 3 改成 1。",
        agent_messages=["我把 RETRIES 从 3 改成 1，TIMEOUT 从 30 改成 8。"],
    )
    config.TONE = tone.PRO
    pro_prompt = cardgen_session_prompt(turn)
    config.TONE = tone.PLAIN
    plain_prompt = cardgen_session_prompt(turn)

    say(f"\n  [专业模式] 写法那一段：\n"
        + "\n".join("    " + ln for ln in
                    between(pro_prompt, "【写作口径 · 专业模式】", "【与写法无关").splitlines()))
    say(f"\n  [小白模式] 写法那一段：\n"
        + "\n".join("    " + ln for ln in
                    between(plain_prompt, "【写作口径 · 小白模式】", "【与写法无关").splitlines()))
    same = between(pro_prompt, "【与写法无关", "请找出这一轮里的")
    say(f"\n  两档**共有**的那段（凭据字段要求，逐字相同）：{same != '（这段没找到）'}")
    say("    " + "\n    ".join(same.splitlines()[:6]))

    # ---------------- 二、同一份改动，两档各写一遍 ----------------
    say("\n② 同一份 git 改动，两档各写一遍卡片（Mock 模式，不联网）")
    build_repo()
    out_dir = DEMO / "docs" / "decisions"

    results = {}
    for mode in (tone.PRO, tone.PLAIN):
        force_rmtree(out_dir)
        config.TONE = mode
        result = cardgen.generate(DEMO, base="HEAD~1", head="HEAD", max_cards=3,
                                  log=lambda *_: None)
        results[mode] = result
        cards = result.get("cards") or []
        say(f"\n  [{tone.label(mode)}] 生成 {len(cards)} 张，丢弃 {len(result.get('dropped') or [])} 条")
        opinion = (result.get("audit") or {})
        if opinion:
            say(f"      审计意见：{opinion.get('label')} —— {opinion.get('detail', '')[:80]}")
        for card in cards:
            say(f"      · {card.file}:{card.lines}  tone={card.tone or '(空)'}")
            say(f"        {card.decision[:70]}")

    # 把小白那档的产物留下来给面板用（有 tone 标签可看）
    force_rmtree(out_dir)
    config.TONE = tone.PLAIN
    cardgen.generate(DEMO, base="HEAD~1", head="HEAD", max_cards=3, log=lambda *_: None)

    files = sorted(p.name for p in out_dir.glob("*.md"))
    say(f"\n  落盘的卡片：{files}")
    for path in sorted(out_dir.glob("*.md")):
        head = path.read_text(encoding="utf-8").split("---")[1]
        tone_line = next((ln for ln in head.splitlines() if ln.startswith("tone:")), "（没有 tone 行）")
        say(f"      {path.name} → {tone_line.strip()}")

    # ---------------- 三、面板 ----------------
    say("\n③ 面板：小白写法的那张会带一个标签")
    page = out_dir / "dashboard.html"
    dashboard.main(["--repo", str(DEMO)])
    html_text = page.read_text(encoding="utf-8") if page.is_file() else ""
    say(f"  面板已生成：{page}（{len(html_text)} 字符）")
    say(f"  里面出现「小白写法」标签 = {'小白写法' in html_text}")

    # ---------------- 四、本机 CLI ----------------
    say("\n④ 本机 CLI 能不能接进来对话")
    for item in cli_bridge.detect():
        mark = "可用" if item["ready"] else "不可用"
        say(f"  [{mark}] {item['label']:<12} {item.get('version') or ''}")
        say(f"          {item.get('path') or '（没找到）'}")
        if not item["ready"]:
            say(f"          {item.get('note', '')[:110]}")

    # ---------------- 五、界面上的可选中 ----------------
    say("\n⑤ 界面里三处只读文本（对话 / 卡片详情 / 日志）")
    import re

    gui_src = (ROOT / "src" / "gui.py").read_text(encoding="utf-8")
    # 只看「把 tk.Text 设成 disabled」那种写法 —— 注释里提到、以及按钮上的
    # state="disabled"（那是该禁用的）都不算。
    bad = re.findall(r"self\.text_\w+\.configure\([^)]*state=\"disabled\"", gui_src)
    say(f"  把文本框设成 disabled 的地方：{len(bad)} 处（应为 0 —— 有的话那处就选不中了）")
    for item in bad:
        say("      " + item)
    readonly = re.findall(r"self\._make_readonly\((self\.\w+)\)", gui_src)
    say(f"  走了「只读但可选中」的控件：{readonly}")
    say(f"  其中带右键复制菜单的方法调用：{gui_src.count('self._attach_copy_menu(')} 处（1 处定义 + 调用点）")

    say("\n" + "=" * 70)
    say("  演示结束")
    say("=" * 70)
    return 0


def cardgen_session_prompt(turn) -> str:
    from src import cardgen_session

    return cardgen_session.build_turn_prompt(turn, 1)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback

        lines.append("演示挂掉了：\n" + traceback.format_exc())
        code = 1
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n（日志：{LOG}）")
    raise SystemExit(code)
