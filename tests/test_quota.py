"""「卡片该给几张」的测试。

    python tests/test_quota.py

最要紧的几条断言（对应这次改动的验收标准）：
    1. **两端都少、中间最多**：工程师和不懂技术的人都只给几张，
       半懂的人给得最多。如果哪天有人把它改回「专业→越少」的单调曲线，
       这条会红。
    2. **档位只能慢慢动**：升档要连续 2 次证据，降档要 3 次 ——
       专家问一句「这啥」不该被打成外行。
    3. **不硬凑**：候选只有 2 张就给 2 张，绝不为了填满配额放宽闸门。
    4. **可疑痕迹不占配额、也不会被裁**：为了少给几张而把
       「原有的校验被删掉了」裁掉，是最坏的省事。
    5. **顺序无关**：同一批候选，不管什么顺序进来，选出来的是同一批。
       现在的实现是「按文件顺序取前 N 张」，会把其他阶段全部挤掉。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import quota  # noqa: E402

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


def card(**kw) -> dict:
    """造一张最小候选卡（只要 select 会读的字段）。"""
    base = {
        "file": "src/a.py", "lines": "1-2", "stage": "S1",
        "decision": "把重试次数从 3 改成 1", "tier": "normal",
        "why": "", "alternatives": "", "impact": "", "risk": "", "control": "",
        "claim": "retries 由 3 改成 1",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
def test_read_levels() -> None:
    print("\n[1] 四档判定：看术语、坐标、约束，不看句子长短")
    cases = {
        quota.L3: [
            "为什么把 retry 的 backoff 改成指数退避？这里的幂等性怎么保证，会不会放大下游压力",
            "config.py:42 把 timeout 从 30 改成 60，会影响 load_config() 的调用方吗",
            "这个改动删掉了路径为空的校验，属于绕过原有保护吗",
            "不要把 COGNITRAIL_TONE 的默认值改掉，兼容老的 .env 配置",
        ],
        quota.L2: [
            "这个接口的超时时间为什么要改",
        ],
        quota.L1: [
            "为什么页面打开变慢了",
            "这个按钮点了没反应，能帮我看看吗",
            "这玩意到底有什么用啊",
            # 纯需求描述、不含任何技术信号 —— 落 L1 是**预期行为**，不是漏判。
            # 它影响的是「筛选偏好」（更看重影响面），跟「给几张」只差 1 张。
            "我想让卡片按阶段排一下，现在顺序有点乱",
        ],
        quota.L0: ["好了吗", "能跑吗", "结果呢"],
    }
    wrong = []
    for want, texts in cases.items():
        for text in texts:
            got = quota.read(text).level
            if got != want:
                wrong.append(f"{want}→{got}: {text[:26]}")
    check(f"{sum(len(v) for v in cases.values())} 条样例全部判对", not wrong, "；".join(wrong))


def test_no_keyword_stuffing() -> None:
    print("\n[2] 堆名词骗不过去：术语 + 「看不懂」必须回落")
    text = "这个幂等是什么意思，我看不懂，说人话"
    got = quota.read(text)
    check("带强术语但明说看不懂 → 不判专家", got.level != quota.L3,
          f"判成了 {got.level}（{got.why}）")
    check("解释类提问落到 L1", got.level == quota.L1, f"判成了 {got.level}")


def test_band_asymmetry() -> None:
    print("\n[3] L0 只能给短句：长句抱怨是「在乎」，不是「只问结果」")
    check("短句「好了吗」→ L0", quota.read("好了吗").level == quota.L0)
    long_novice = "这个我用不明白，点哪里都没有反应，为什么它一直转圈，我想知道该怎么办"
    check("长句外行描述 → L1（不是 L0）", quota.read(long_novice).level == quota.L1,
          quota.read(long_novice).level)


def test_profile_cold_start() -> None:
    print("\n[4] 冷启动：头两次不下判断")
    prof = quota.Profile(level=quota.L2)
    prof.observe("好了吗")
    prof.observe("能跑吗")
    check("两次外行提问之后仍停在先验", prof.level == quota.L2, prof.level)


def test_profile_hysteresis() -> None:
    print("\n[5] 迟滞：升 2 次、降 3 次，且一次只动一档")
    prof = quota.Profile(level=quota.L2)
    prof.observe("好的")  # 冷启动 1
    prof.observe("好的")  # 冷启动 2
    expert = "src/a.py:12 的 load_config() 里 timeout 改 60，会影响幂等吗"
    prof.observe(expert)
    check("第一次专业证据不跳档", prof.level == quota.L2, prof.level)
    prof.observe(expert)
    check("连续两次才升到 L3", prof.level == quota.L3, prof.level)

    prof.observe("好了吗")
    prof.observe("能跑吗")
    check("两次外行证据不降档（要三次）", prof.level == quota.L3, prof.level)
    prof.observe("结果呢")
    check("第三次才降一档", prof.level == quota.L2, prof.level)


def test_profile_once_per_step() -> None:
    print("\n[6] 一次最多动一档：不能从 L3 直接掉到 L0")
    prof = quota.Profile(level=quota.L3, samples=10)
    for text in ("好了吗", "能跑吗", "结果呢", "行不行"):
        prof.observe(text)
    check("连说四句「好了吗」也只降到 L2", prof.level == quota.L2, prof.level)


def test_pinned() -> None:
    print("\n[7] 用户自己选的档位优先，自动判断不再改它")
    prof = quota.Profile(level=quota.L1)
    prof.pin(quota.L3)
    for _ in range(6):
        prof.observe("好了吗")
    check("钉住后不自动降档", prof.level == quota.L3, prof.level)
    check("钉住标记会落盘", quota.Profile.from_dict(prof.to_dict()).pinned is True)


def test_quota_table() -> None:
    print("\n[8] 配额表：两端少、中间多，且不补足")
    assert quota.quota(quota.L3, quota.MID, 99) == 4
    check("工程师·一般 → 4 张", quota.quota(quota.L3, quota.MID, 99) == 4)
    check("工程师·要细看 → 6 张", quota.quota(quota.L3, quota.HIGH, 99) == 6)
    check("不懂技术·一般 → 4 张", quota.quota(quota.L1, quota.MID, 99) == 4)
    check("懂一点·一般 → 5 张", quota.quota(quota.L2, quota.MID, 99) == 5)
    check("只问结果·一般 → 1 张", quota.quota(quota.L0, quota.MID, 99) == 1)
    check("候选只有 2 张就给 2 张", quota.quota(quota.L2, quota.MID, 2) == 2)
    check("候选为 0 就给 0 张", quota.quota(quota.L2, quota.HIGH, 0) == 0)
    check("中间层配额最高", quota.quota(quota.L2, quota.MID, 99)
          > max(quota.quota(quota.L3, quota.MID, 99), quota.quota(quota.L1, quota.MID, 99)))


def test_value_by_level() -> None:
    print("\n[9] 同一个决策，三种人该看到的理由不同")
    engineering = card(decision="把退避策略由固定 1s 改成指数退避", tier="normal",
                       alternatives="也考虑过固定 3s，但下游恢复需要更久",
                       control="原有的重试上限被保留", why="下游会在 5 分钟内恢复")
    cosmetic = card(decision="把变量名 data 改名为 payload", tier="normal",
                    impact="只影响本文件", why="更清楚")
    check("工程师眼里：工程决策 > 改名",
          quota.value(engineering, quota.L3) > quota.value(cosmetic, quota.L3))
    check("工程师眼里改名为负分", quota.value(cosmetic, quota.L3) < 0)

    user_facing = card(decision="把导出上限从 1000 条改成 500 条", tier="normal",
                       impact="用户导出数据会变少，超过 500 条的客户需要分批", risk="客户投诉")
    internal = card(decision="把工具函数抽到 utils 里", tier="normal", impact="")
    check("不懂技术的人眼里：影响用户的 > 内部重构",
          quota.value(user_facing, quota.L1) > quota.value(internal, quota.L1))
    check("只问结果的人：只留影响可见的那张",
          quota.value(user_facing, quota.L0) > 0 > quota.value(internal, quota.L0))


def test_flagged_exempt() -> None:
    print("\n[10] 可疑痕迹不占配额，也不被裁")
    risky = card(file="src/auth.py", lines="88", tier="minor",
                 decision="删掉了路径为空的校验，改成静默返回",
                 signals="原有校验被拿掉")
    items = [card(file=f"src/m{i}.py", lines="1") for i in range(6)] + [risky]
    sel = quota.select(items, quota.L0, quota.LOW)  # 配额 1
    files = [i["file"] for i in sel.kept]
    check("配额 1 时，存疑那张仍然在", "src/auth.py" in files, str(files))
    check("普通卡只给 1 张", len([f for f in files if f != "src/auth.py"]) == 1, str(files))
    check("统计里单独标出存疑", len(sel.flagged) == 1)

    many = [card(file=f"src/x{i}.py", lines="1", signals="原有校验被拿掉") for i in range(7)]
    sel = quota.select(many, quota.L2, quota.MID)
    check("存疑最多给 3 张", len(sel.flagged) == quota.MAX_FLAGGED, str(len(sel.flagged)))
    check("其余合成 overflow 计数", sel.overflow == 4, str(sel.overflow))


def test_file_cap() -> None:
    print("\n[11] 多文件时同文件最多 2 张；整个候选只有一个文件时不限")
    hot = [card(file="src/big.py", lines=str(i), decision=f"第 {i} 处调整") for i in range(5)]
    others = [card(file=f"src/o{i}.py", lines="1", stage=f"S{i}") for i in range(3)]
    sel = quota.select(hot + others, quota.L2, quota.MID)
    big = [i for i in sel.kept if i["file"] == "src/big.py"]
    check("同文件最多 2 张", len(big) == quota.FILE_CAP, str(len(big)))
    check("被挡下的给了原因",
          all("配额外" in reason or "同" in reason for _, reason in sel.dropped))

    # 一条 diff 全落在一个文件里是常态（改一个模块）。
    # 那时候「最多 2 张」只会把真正的第 3 处决策砍掉 —— 那不是多样性，是阉割。
    only = quota.select(hot, quota.L2, quota.MID)
    check("候选全在一个文件时不受 2 张限制", len(only.kept) == 5, str(len(only.kept)))


def test_stage_round_robin() -> None:
    print("\n[12] 按阶段轮转：一个阶段写得多，不该把别的阶段挤掉")
    items = [card(file=f"src/a{i}.py", lines="1", stage="S1") for i in range(8)]
    items.append(card(file="src/z.py", lines="9", stage="S4", tier="minor"))
    sel = quota.select(items, quota.L2, quota.LOW)  # 配额 2
    stages = {i["stage"] for i in sel.kept}
    check("两个阶段都进得来", stages == {"S1", "S4"}, str(stages))


def test_order_independent() -> None:
    print("\n[13] 顺序无关：打乱候选，选出来的还是同一批")
    pool = [
        card(file="src/a.py", lines="1", stage="S1", tier="key"),
        card(file="src/b.py", lines="2", stage="S1"),
        card(file="src/c.py", lines="3", stage="S2",
             alternatives="也考虑过缓存，见 docs"),
        card(file="src/d.py", lines="4", stage="S2",
             impact="用户导出会变少"),
        card(file="src/e.py", lines="5", stage="S3", decision="把变量名 a 改名为 b"),
    ]
    base = {id(i) for i in quota.select(pool, quota.L3, quota.MID).kept}
    ok = True
    for seed in range(6):
        shuffled = pool[:]
        random.Random(seed).shuffle(shuffled)
        got = {id(i) for i in quota.select(shuffled, quota.L3, quota.MID).kept}
        if got != base:
            ok = False
            break
    check("6 次打乱结果一致", ok)


def test_interest() -> None:
    print("\n[14] 关切度是「这一次」的，不是画像")
    check("只想看结论 → low", quota.interest_of(["好了吗"]) == quota.LOW)
    check("追问具体哪一行 → mid 以上",
          quota.interest_of(["具体是哪一行改的？依据是什么"]) != quota.LOW)
    check("说清用途 + 连续追问 → high",
          quota.interest_of(["我要给客户解释", "具体怎么改的", "证据在哪"]) == quota.HIGH)
    check("连着关掉三张卡 → low",
          quota.interest_of(["改了什么"], {"dismissed": 3}) == quota.LOW)
    check("点开过详情 → 加分",
          quota.interest_of(["改了什么"], {"detail_open": 2}) != quota.LOW)


def test_line() -> None:
    print("\n[15] 说清楚「为什么只给了这几张」")
    items = [card(file=f"src/m{i}.py", lines="1") for i in range(9)]
    sel = quota.select(items, quota.L2, quota.MID)
    line = sel.line(quota.L2, quota.MID)
    check("话里带档位和候选数", "懂一点" in line and "9" in line, line)
    check("话里带被略过的张数", "未列" in line, line)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  卡片配额（按水平自适应）测试")
    print("=" * 62)
    test_read_levels()
    test_no_keyword_stuffing()
    test_band_asymmetry()
    test_profile_cold_start()
    test_profile_hysteresis()
    test_profile_once_per_step()
    test_pinned()
    test_quota_table()
    test_value_by_level()
    test_flagged_exempt()
    test_file_cap()
    test_stage_round_robin()
    test_order_independent()
    test_interest()
    test_line()

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
