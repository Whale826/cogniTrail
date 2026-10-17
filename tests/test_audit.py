"""审计化改造的测试：反舞弊、重要性不可降级、审计意见、以及整条链跑通。

    python tests/test_audit.py

最要紧的断言（对应这次重构的验收标准）：
    1. **参假拦得住**：一份「删掉校验 + 吞异常 + 写死密钥」的改动，
       必须命中对应痕迹，卡片上出现「存疑」，整理报告里把它单列出来。
    2. **关键度不可被模型降级**：模型把动过默认值的卡片标成 minor，
       脚本必须归并成 key，并把理由写在卡片上。
    3. **口是心非留痕**：卡片声称 0.35，证据里只有 0.5 → 命中「断言与代码对不上」。
    4. **审计结论只由脚本写**：模型自己在返回里塞 signals / verdict，一律不采信。
    5. **没配 Key 也要算得出来**：tier / signals / verdict 不依赖模型。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-audit-test-", dir=str(Path.home())))
os.environ["COGNITRAIL_HOME"] = str(_TMP)
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ["COGNITRAIL_MOCK"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import audit, cardgen, cardgen_session, curate, dashboard, session_source  # noqa: E402
from src.schema import Card, strip_audit_only  # noqa: E402

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
# 造一份「参了假」的改动
# --------------------------------------------------------------------------
_BEFORE = '''import json
import os

API_KEY = os.getenv("API_KEY", "")


def save(path, rows):
    if not path:
        raise ValueError("路径为空")
    if len(rows) > 1000:
        raise ValueError("单批最多 1000 行")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    return len(rows)
'''

# 三处「参假」：拿掉 1000 行的上限校验、吞掉写盘异常、把真 Key 写成默认值
_AFTER = '''import json
import os

API_KEY = os.getenv("API_KEY", "sk-abcdefghijklmnop")


def save(path, rows):
    if not path:
        raise ValueError("路径为空")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
    except Exception:
        pass
    return len(rows)
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def make_repo() -> Path:
    repo = _TMP / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "save.py").write_text(_BEFORE, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "audit@test.local")
    _git(repo, "config", "user.name", "audit")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (repo / "src" / "save.py").write_text(_AFTER, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "调整保存逻辑，加兜底")
    return repo


# --------------------------------------------------------------------------
# 1. 引擎规则
# --------------------------------------------------------------------------
def test_rules() -> None:
    print("\n[1/7] 审计引擎规则（自检）")
    check("引擎自检全部通过", audit._self_test() == 0)


# --------------------------------------------------------------------------
# 2. 卡片结构：审计字段写进 front matter 与正文
# --------------------------------------------------------------------------
def test_card_fields() -> None:
    print("\n[2/7] 卡片结构：审计字段")
    card = Card(
        file="src/config.py", lines="10",
        decision="把默认地址改成官方地址",
        why="本机代理不稳定",
        claim="默认地址由 http://127.0.0.1:8000 改成 https://api.deepseek.com",
        control="没有动到取 Key 的兜底逻辑",
        tier="key", signals="疑似写死凭据", verdict="doubt",
        tier_why="动到了地基类文件（配置口径）：src/config.py",
    )
    text = card.to_markdown("+ DEFAULT_BASE_URL = 'https://api.deepseek.com'")

    check("front matter 里有 tier", "\ntier: key\n" in text, text[:600])
    check("front matter 里有 claim", "claim: " in text)
    check("front matter 里有 signals", "signals: " in text)
    check("front matter 里有 verdict", "\nverdict: doubt\n" in text)
    check("有「审计核对」小节", "## 审计核对" in text)
    check("核对小节写了重要性", "**关键决策**" in text)
    check("核对小节写了核对结论", "**存疑**" in text)
    check("有「可核对的断言」小节", "## 可核对的断言" in text)
    check("有「动到的保护」小节", "## 这次改动动到的保护" in text)
    check("字段顺序：tier 排在 file 之前",
          text.index("\ntier:") < text.index("\nfile:"), text[:400])

    # 没有审计字段的老卡片，不能因为新增字段就冒出空小节
    plain = Card(file="src/a.py", lines="3", decision="改个名", why="统一命名")
    plain_text = plain.to_markdown()
    check("没有审计字段时不出空小节", "## 审计核对" not in plain_text, plain_text)
    check("没有断言时不出断言小节", "## 可核对的断言" not in plain_text)

    # 模型自己塞审计结论 → 一律不采信
    sneaky = Card.from_llm(
        {"file": "src/a.py", "lines": "3", "decision": "x", "why": "y",
         "signals": "我自己说没问题", "verdict": "pass", "tier_why": "我说的"},
        audit={"tier": "key", "signal_labels": ["吞掉失败"], "verdict": "doubt",
               "tier_why": "脚本说的"},
    )
    check("模型的 signals 被脚本的覆盖", sneaky.signals == "吞掉失败", sneaky.signals)
    check("模型的 verdict 被脚本的覆盖", sneaky.verdict == "doubt", sneaky.verdict)
    check("模型的 tier_why 被脚本的覆盖", sneaky.tier_why == "脚本说的", sneaky.tier_why)
    check("strip_audit_only 摘掉了这三个字段",
          set(strip_audit_only({"signals": 1, "verdict": 2, "tier_why": 3, "file": "f"})) == {"file"})


# --------------------------------------------------------------------------
# 3. 「参假」拦得住
# --------------------------------------------------------------------------
def test_fraud_signals() -> None:
    print("\n[3/7] 参假痕迹：拿掉校验 / 吞异常 / 写死凭据")
    added, removed = audit.diff_lines(
        "diff --git a/src/save.py b/src/save.py\n"
        "--- a/src/save.py\n+++ b/src/save.py\n"
        "@@ -1,6 +1,6 @@\n"
        "-    if len(rows) > 1000:\n-        raise ValueError('上限')\n"
        "+    except Exception:\n+        pass\n"
        '+API_KEY = os.getenv("API_KEY", "sk-abcdefghijklmnop")\n'
    )
    check("新增行取到了", any("except Exception" in line for line in added), str(added))
    check("删除行取到了", any("1000" in line for line in removed), str(removed))

    codes = {s["code"] for s in audit.scan_fraud(
        audit.Subject(file="src/save.py", added=added, removed=removed)
    )}
    check("抓到拿掉校验", "guard-removed" in codes, str(codes))
    check("抓到吞异常", "silent-failure" in codes, str(codes))
    check("抓到写死凭据", "hardcoded-secret" in codes, str(codes))

    # 测试目录里的 print 不算残留（否则每个测试文件都会报）
    test_file = audit.scan_fraud(audit.Subject(
        file="tests/test_x.py", added=["    print(rows)"],
    ))
    check("测试文件里的 print 不算调试残留",
          "debug-residue" not in {s["code"] for s in test_file}, str(test_file))


# --------------------------------------------------------------------------
# 4. 关键度不可被降级 / 口是心非
# --------------------------------------------------------------------------
def test_materiality_and_claim() -> None:
    print("\n[4/7] 重要性不可降级 + 口是心非")

    subject = audit.Subject(
        file="src/config.py", lines="10",
        evidence="DEFAULT_BASE_URL = 'https://api.deepseek.com'",
        added=["DEFAULT_BASE_URL = 'https://api.deepseek.com'"],
        removed=["DEFAULT_BASE_URL = 'http://127.0.0.1:8000'"],
    )
    result = audit.audit_card(subject, {
        "decision": "把默认地址改成官方地址",
        "claim": "默认地址由 http://127.0.0.1:8000 改成 https://api.deepseek.com",
        "tier": "minor",
    })
    check("模型标 minor，脚本仍定 key", result["tier"] == "key", result["tier"])
    check("降级被记进定档理由",
          any("不得降级" in w for w in result["tier_why"]), str(result["tier_why"]))
    check("断言核对通过", result["check_status"] == "pass", result["check_note"])
    check("结论不是存疑", result["verdict"] != "doubt", result["verdict"])

    # 口是心非：声称 0.35，代码里只有 0.5
    liar = audit.audit_card(
        audit.Subject(file="src/rank.py", lines="12",
                      evidence="threshold = 0.5", added=["threshold = 0.5"]),
        {"decision": "降低阈值", "claim": "阈值由 0.5 改成 0.35"},
    )
    check("命中「口是心非」",
          "口是心非：断言与代码对不上" in (liar["signal_labels"] or []), str(liar["signal_labels"]))
    check("结论是存疑", liar["verdict"] == "doubt", liar["verdict"])
    check("口是心非会让意见变成保留",
          audit.opinion([liar])["level"] == "保留", audit.opinion([liar])["label"])

    # 档位只能往上抬：模型主张更高且给了断言 → 采纳
    docs = audit.audit_card(
        audit.Subject(file="README.md", added=["- 重写了安装说明"], evidence="- 重写了安装说明"),
        {"decision": "改安装说明", "claim": "把安装步骤从 3 步改成 2 步", "tier": "key"},
    )
    check("纯文档默认是记录性", audit.materiality(
        audit.Subject(file="README.md", added=["- x"]))[0] == "minor")
    check("模型有断言时可以往上抬", docs["tier"] == "key", docs["tier"])


# --------------------------------------------------------------------------
# 5. 端到端：cardgen 跑一份参假的 diff
# --------------------------------------------------------------------------
def test_cardgen_end_to_end() -> None:
    print("\n[5/7] 端到端：cardgen 跑一份参假的 diff")
    repo = make_repo()
    result = cardgen.generate(repo, "HEAD~1", "HEAD", log=lambda *_: None)

    check("至少生成 1 张卡片", len(result["cards"]) >= 1, str(len(result["cards"])))
    if not result["cards"]:
        return

    labels = {label for item in result["audits"] for label in item["signal_labels"]}
    check("审计扫出了「原有校验被拿掉」", "原有校验被拿掉" in labels, str(labels))
    check("审计扫出了「吞掉失败」", "吞掉失败" in labels, str(labels))
    check("审计扫出了「疑似写死凭据」", "疑似写死凭据" in labels, str(labels))

    card = result["cards"][0]
    check("卡片被定成关键决策", card.tier == "key", card.tier)
    check("卡片结论是存疑", card.verdict == "doubt", card.verdict)
    check("卡片上写了可疑痕迹", bool(card.signals), card.signals)

    opinion = result["audit"]
    check("审计意见是「保留」", opinion["level"] == "保留", opinion["label"])
    check("意见里点名了高危张数", (opinion["counts"] or {}).get("high", 0) >= 1, str(opinion["counts"]))

    # 真的落盘了，而且卡片文件里带审计小节
    written = sorted((repo / "docs" / "decisions").glob("2*.md"))
    check("卡片写到了 docs/decisions", len(written) >= 1, str(written))
    if written:
        text = written[0].read_text(encoding="utf-8")
        check("卡片文件里有「审计核对」小节", "## 审计核对" in text)
        check("卡片文件里记了存疑", "verdict: doubt" in text, text[:500])


# --------------------------------------------------------------------------
# 6. 整理与面板：意见、关键决策、可疑项
# --------------------------------------------------------------------------
def test_curate_and_panel() -> None:
    print("\n[6/7] 整理与面板：意见 / 关键决策 / 可疑项")
    folder = _TMP / "repo" / "docs" / "decisions"
    cards = dashboard.load_cards(_TMP / "repo", folder)
    check("能从卡片文件读回审计字段", bool(cards) and cards[0].get("tier") == "key",
          str(cards[:1]))

    result = curate.normalize({"summary": "x", "ordered": [], "dropped": []}, cards)
    opinion = result.get("opinion") or {}
    check("整理结果带上了审计意见", bool(opinion.get("label")), str(opinion))
    check("关键决策被单列出来", len(result.get("key_cards") or []) >= 1,
          str(result.get("key_cards")))
    check("可疑项被单列出来", len(result.get("suspicious") or []) >= 1,
          str(result.get("suspicious")))

    md = curate.render_markdown(result, cards, _TMP / "repo")
    check("顶端写了审计意见", "**审计意见：" in md, md[:900])
    check("有「关键决策」一节", "## 五、关键决策：哪几条真正决定项目走向" in md)
    check("有「高风险与可疑项」一节", "## 六、高风险与可疑项" in md)
    check("有未更正错报台账", "6.3 未更正错报台账" in md)
    check("原有四节没被动过",
          all(h in md for h in (
              "## 一、有序决策（按前后依赖排列）",
              "## 二、冲突与融合说明",
              "## 三、决策演进：早期做法为什么被推翻",
              "## 四、被剔除的卡片",
          )))

    target = curate.save(folder, md, result, cards)
    check("CURATED.md 写出来了", target.is_file())
    payload = json.loads((folder / "curated.json").read_text(encoding="utf-8"))
    check("curated.json 里有 opinion", bool((payload.get("opinion") or {}).get("label")),
          str(payload.get("opinion")))
    check("curated.json 里有 key_cards", len(payload.get("key_cards") or []) >= 1)
    check("curated.json 里有 suspicious", len(payload.get("suspicious") or []) >= 1)

    # 面板：意见横幅 + 存疑标签 + 两个筛子
    html_text = dashboard.render_html(
        _TMP / "repo", cards, {"items": [], "due": 0, "answered": 0, "upcoming": 0, "avg": None},
    )
    check("面板顶部有审计意见横幅", 'class="opinion' in html_text, html_text[:200])
    check("面板上有「关键决策」标签", ">关键决策<" in html_text)
    check("面板上有「存疑」标签", ">存疑<" in html_text)
    check("面板多出「只看关键」「只看存疑」两个筛子",
          'data-filter="key"' in html_text and 'data-filter="doubt"' in html_text)
    check("面板卡片带 data-risk 供筛选", 'data-risk="doubt"' in html_text)

    # 审计改造之前的老卡片：没有核对字段，绝不能说「无保留」
    legacy = audit.opinion([{"file": "src/a.py", "decision": "老卡片"}])
    check("老卡片被判为「未审计」而不是「无保留」", legacy["level"] == "未审计", legacy["label"])
    check("老卡片的计数里点了名", legacy["counts"]["unaudited"] == 1, str(legacy["counts"]))


# --------------------------------------------------------------------------
# 7. 手写卡片（插件形态）：复核只往可疑的方向写
# --------------------------------------------------------------------------
def test_handwritten_cards() -> None:
    print("\n[7/7] 手写卡片：复核只往可疑的方向写")

    repo = _TMP / "hand" / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "rank.py").write_text("threshold = 0.5\nMAX = 100\n", encoding="utf-8")

    def hand(**kw) -> dict:
        card = {
            "file": "src/rank.py", "lines": "1", "decision": "调阈值",
            "claim": "", "tier": "", "verdict": "", "signals": "", "tier_why": "",
            "path": "", "status": "active", "stage": "", "problem": "",
        }
        card.update(kw)
        return card

    # —— 「有 tier」根本不能证明「审过」 ——
    # 插件形态下 agent 手写的卡片正是这样：有 tier，没别的。
    # 早先这里把 tier 也算作已审计，于是这批卡片能拿到「无保留」——
    # 那是这个模块最不该犯的错，所以判据只认脚本会写的那两格。
    check("只有 tier 的手写卡片不算「已审计」", not audit.is_audited({"tier": "key"}))
    check("有 verdict 才算已审计", audit.is_audited({"verdict": "pass"}))
    check("有 signals 才算已审计", audit.is_audited({"signals": "吞掉失败"}))
    fake_clean = audit.opinion([{"file": "src/a.py", "tier": "key", "decision": "手写卡片"}])
    check("只有 tier 的手写卡片 → 未审计，不许说无保留",
          fake_clean["level"] == "未审计", fake_clean["label"])

    # —— 断言与代码对不上：手写流程里最该抓的一种 ——
    liar = hand(claim="阈值由 0.5 改成 0.35")
    patch = curate.reverify_card(liar, repo)
    check("手写卡片也会被复核出口是心非", patch.get("verdict") == "doubt", str(patch))
    check("口是心非被记成可疑痕迹", "口是心非" in str(patch.get("signals") or ""), str(patch))
    check("口是心非会被定成关键决策", patch.get("tier") == "key", str(patch.get("tier")))

    # —— 核对通过：什么都不写（核对通过 ≠ 审计通过）——
    ok_patch = curate.reverify_card(hand(claim="threshold = 0.5"), repo)
    check("核对通过时不写 verdict", not ok_patch.get("verdict"), str(ok_patch))
    check("核对通过时仍补上脚本算的档位", bool(ok_patch.get("tier")), str(ok_patch))
    after = {**hand(claim="threshold = 0.5"), **ok_patch}
    check("核对通过后依然算「未审计」", not audit.is_audited(after), str(after))

    # —— 断言里没有可核对的字面量 → 证据不充分 ——
    vague = curate.reverify_card(hand(claim="把阈值调得更合理一些"), repo)
    check("没有字面量的断言 → 证据不充分", vague.get("verdict") == "insufficient", str(vague))

    # —— 坐标失效 → 证据不充分（别静默放过）——
    gone = curate.reverify_card(hand(claim="threshold = 0.5", file="src/gone.py"), repo)
    check("坐标指向不存在的文件 → 证据不充分", gone.get("verdict") == "insufficient", str(gone))

    # —— 引擎审过的卡片不许被复核覆盖 ——
    check("引擎审过的卡片不再复核",
          curate.reverify_card(hand(claim="阈值由 0.5 改成 0.35", verdict="pass"), repo) == {})

    # —— 关键决策在**全部卡片**里挑：重不重要不需要 diff 也算得出来 ——
    info = curate.audit_summary([hand(tier="key", claim="threshold = 0.5")], repo)
    check("未审计但被定成关键的卡片，仍单列进「关键决策」",
          len(info["key"]) == 1, str(info["key"]))
    check("它自己的意见仍是「未审计」",
          info["opinion"]["level"] == "未审计", info["opinion"]["label"])

    # —— 「还有几张从来没核过」在每一种结论里都要说出来 ——
    # 只在「无保留」时提一句不够：正因为有没核过的，结论才更该打折，
    # 而给出「保留」时读者最容易以为「反正都查过了」。
    mixed = audit.opinion([
        {"file": "src/a.py", "decision": "查过且有问题", "verdict": "doubt", "signals": "吞掉失败"},
        {"file": "src/b.py", "decision": "从没核过", "tier": "key"},
    ])
    check("给保留意见时也点名「还有几张没核」",
          mixed["level"] == "保留" and "另有 1 张卡片没有核对结论" in mixed["detail"],
          mixed["detail"])
    clean_mixed = audit.opinion([
        {"file": "src/a.py", "decision": "查过且没问题", "verdict": "pass"},
        {"file": "src/b.py", "decision": "从没核过", "tier": "key"},
    ])
    check("给无保留时也说明有没核的",
          clean_mixed["level"] == "无保留" and "另有 1 张卡片没有核对结论" in clean_mixed["detail"],
          clean_mixed["detail"])

    # —— 走一遍真实链路：手写卡片落盘 → 读回 → 复核 → 出意见 → 上面板 ——
    folder = repo / "docs" / "decisions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-10-15-00-调阈值.md").write_text(
        "---\n"
        "id: D-20261015-01\n"
        "tier: normal\n"
        "file: src/rank.py\n"
        "lines: 1\n"
        'decision: "把阈值调低"\n'
        'why: "召回太差"\n'
        'claim: "阈值由 0.5 改成 0.35"\n'
        "status: active\n"
        "created: 2026-10-15\n"
        "---\n\n"
        "## 证据坐标\n\n`src/rank.py:1`\n\n"
        "## 这次做了什么\n\n把阈值调低\n",
        encoding="utf-8",
    )
    hand_cards = dashboard.load_cards(repo, folder)
    check("手写卡片被读回来", len(hand_cards) == 1, str(hand_cards[:1]))

    report = curate.audit_summary(hand_cards, repo)
    check("手写卡片复核后出了保留意见",
          report["opinion"]["level"] == "保留", str(report["opinion"]))
    check("手写卡片的可疑项被单列", len(report["suspicious"]) == 1, str(report["suspicious"]))

    html_text = dashboard.render_html(
        repo, hand_cards,
        {"items": [], "due": 0, "answered": 0, "upcoming": 0, "avg": None},
    )
    check("面板上也复核了手写卡片（与命令行口径一致）",
          'class="opinion bad"' in html_text, html_text[:300])
    check("面板把手写卡片的存疑标出来", 'data-risk="doubt"' in html_text)

    # —— 同一个计数只能有一种说法：四处输出共用 audit.counts_line ——
    # 早先同一个数字被叫过「需人工复核」和「证据不充分」两个名字。
    line = audit.counts_line(
        {"key": 2, "high": 2, "flagged": 2, "unaudited": 1, "dropped": 3}
    )
    check("计数行标出「存疑是需人工复核的子集」",
          "需人工复核 2 条（其中存疑 2 条）" in line, line)
    check("计数行带上未审计与未取证",
          "未审计 1 条" in line and "未取证 3 条" in line, line)
    check("没有存疑时不出现空括号",
          "其中存疑" not in audit.counts_line({"flagged": 1}), audit.counts_line({"flagged": 1}))

    # —— SUMMARY.md 的意见也必须由审计引擎推，不许自己写第二套 ——
    summary_path = cardgen_session.write_summary(
        folder,
        [Card(file="src/rank.py", lines="1", decision="手写卡片", why="x")],
        session_source.Session(path=Path("fake.jsonl"), cwd=str(repo), originator="codex"),
    )
    summary_text = summary_path.read_text(encoding="utf-8")
    check("汇总里的审计意见是「未审计」而不是「无保留」",
          "**审计意见：未审计**" in summary_text, summary_text[:600])


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  审计化改造 测试")
    print("=" * 62)
    test_rules()
    test_card_fields()
    test_fraud_signals()
    test_materiality_and_claim()
    test_cardgen_end_to_end()
    test_curate_and_panel()
    test_handwritten_cards()

    print("\n" + "=" * 62)
    if FAILED:
        print(f"  有 {len(FAILED)} 项失败：")
        for name in FAILED:
            print(f"    - {name}")
        print("=" * 62)
        raise SystemExit(1)
    print(f"  全部通过：{PASSED} 项检查")
    print("=" * 62)
    print(f"  （临时目录保留，可手动删：{_TMP}）")
    raise SystemExit(0)
