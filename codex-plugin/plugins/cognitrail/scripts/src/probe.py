"""增量三：延迟理解探针（对应手册 §6.3）—— 本项目的杀手锏

手册原话：「任务做完后隔几天再回来复测这件事，全网没有任何实现。」
    所有人都在盯着「用 AI 的时候别变笨」，没人管「用完之后我到底还记得多少」。
    而点击行为是可以伪造的，**唯一无法伪造的信号，是几天后你还改不改得动。**

    python src/probe.py --list                 # 看今天该问什么
    python src/probe.py                        # 逐条问你，录入回答
    python src/probe.py --schedule docs/.../x.md --delay-days 3
    python src/probe.py --seed                 # 塞 3 条马上到期的题，用来试流程
    python src/probe.py --stats                # 看「接住层数」的历史
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config  # noqa: E402

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS probes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL,
    repo        TEXT DEFAULT '',
    question    TEXT NOT NULL,
    angle       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    due_at      TEXT NOT NULL,
    answered_at TEXT,
    answer      TEXT,
    score       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_due ON probes(answered_at, due_at);
"""


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    # 按 DB_PATH 的实际位置建目录 —— exe 形态下它在 ~/.cognitrail 而不是项目 data/
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    return connection


# --------------------------------------------------------------------------
# 排期
# --------------------------------------------------------------------------
def schedule(card_id: str, questions: list[str], delay_days: int = config.DEFAULT_DELAY_DAYS,
             repo: str = "", angles: list[str] | None = None, verbose: bool = True) -> int:
    """把问题排进日程：delay_days 天后再来问我。返回排了几条。"""
    connection = conn()
    due = (_dt.datetime.now() + _dt.timedelta(days=delay_days)).isoformat(timespec="seconds")
    created = _now()
    angles = angles or []

    for index, question in enumerate(questions):
        connection.execute(
            "INSERT INTO probes(card_id, repo, question, angle, created_at, due_at) "
            "VALUES(?,?,?,?,?,?)",
            (card_id, repo, question, angles[index] if index < len(angles) else "", created, due),
        )
    connection.commit()
    connection.close()
    if verbose:
        print(f"{len(questions)} 个问题已排期，{delay_days} 天后到期（{due[:10]}）")
    return len(questions)


def schedule_card(card_path: str | Path, delay_days: int = config.DEFAULT_DELAY_DAYS,
                  repo: str = "") -> int:
    """给一张卡片出题并排期（含 3 个预设问题 + 4 个追问角度）。"""
    from src import questions as q

    path = Path(card_path)
    text = path.read_text(encoding="utf-8")

    base = q.make_questions(text)
    ladder = q.ladder_for(text)

    items = list(base) + [f"[{name}] {question}" for name, question in ladder]
    angles = ["预设"] * len(base) + [name for name, _ in ladder]

    return schedule(path.stem, items, delay_days, repo, angles)


def schedule_questions(card_id: str, base_questions: list[str],
                       ladder: list[tuple[str, str]],
                       delay_days: int = config.DEFAULT_DELAY_DAYS,
                       repo: str = "", verbose: bool = False) -> int:
    """pipeline 用的版本：问题和追问链已经在手上了，别再出一遍。

    默认不打印 —— 一次任务会排很多张卡片，逐张打印会刷屏。
    """
    items = list(base_questions) + [f"[{name}] {question}" for name, question in ladder]
    angles = ["预设"] * len(base_questions) + [name for name, _ in ladder]
    return schedule(card_id, items, delay_days, repo, angles, verbose=verbose)


# --------------------------------------------------------------------------
# 查询 / 作答
# --------------------------------------------------------------------------
def due_now(force: bool = False) -> list[sqlite3.Row]:
    """列出今天该问我的问题。force=True 时忽略到期时间，全部列出。"""
    connection = conn()
    if force:
        rows = connection.execute(
            "SELECT id, card_id, question, angle, due_at FROM probes "
            "WHERE answered_at IS NULL ORDER BY due_at, id"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT id, card_id, question, angle, due_at FROM probes "
            "WHERE answered_at IS NULL AND due_at <= ? ORDER BY due_at, id",
            (_now(),),
        ).fetchall()
    connection.close()
    return list(rows)


def answer(probe_id: int, text: str, score: int | None = None) -> None:
    connection = conn()
    connection.execute(
        "UPDATE probes SET answer=?, answered_at=?, score=? WHERE id=?",
        (text, _now(), score, probe_id),
    )
    connection.commit()
    connection.close()


def stats() -> dict:
    connection = conn()
    row = connection.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) AS answered, "
        "SUM(CASE WHEN answered_at IS NULL AND due_at <= ? THEN 1 ELSE 0 END) AS overdue, "
        "AVG(score) AS avg_score "
        "FROM probes",
        (_now(),),
    ).fetchone()
    connection.close()
    return dict(row) if row else {}


def seed(count: int = 3) -> int:
    """塞几条马上到期的题，用来验证「排期 → 到期 → 作答」这条链。"""
    demo = [
        "（演示题）这处改动是你的决定，还是 AI 的决定？一句话说清它做了什么。",
        "（演示题）卡片给出的理由，你现在能不看卡片复述出来吗？",
        "（演示题）如果不用这个做法，你会怎么改？当时为什么没选它？",
    ]
    return schedule("DEMO", demo[:count], delay_days=0, repo="", angles=["预设"] * count)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def _ask_round(rows: list[sqlite3.Row]) -> None:
    print(f"\n今天有 {len(rows)} 个问题要问你。凭记忆回答，不要查资料。\n")
    for index, row in enumerate(rows, 1):
        label = f"[{row['angle']}]" if row["angle"] else ""
        print("-" * 58)
        print(f"{index}/{len(rows)}  卡片 {row['card_id']}  {label}")
        print(f"{row['question']}\n")
        try:
            text = input("你的回答：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已中断，未作答的问题会留到下次。")
            return
        if not text:
            print("（空回答，跳过，下次还会问你）")
            continue
        try:
            raw = input("自评：这层接住了吗？（0=完全没接住 … 4=答得很顺，回车跳过）：").strip()
            score = int(raw) if raw else None
        except (EOFError, KeyboardInterrupt, ValueError):
            score = None
        answer(row["id"], text, score)
        print("已记录。")


def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(prog="probe", description="延迟理解探针：几天后回来问你一次")
    parser.add_argument("--list", action="store_true", help="只列出来，不进入问答")
    parser.add_argument("--force", action="store_true", help="忽略到期时间，把没答过的都列出来")
    parser.add_argument("--schedule", metavar="卡片文件", help="给一张卡片出题并排期")
    parser.add_argument("--repo", default=config.DEFAULT_REPO, help="该卡片所属的仓库")
    parser.add_argument("--delay-days", type=int, default=config.DEFAULT_DELAY_DAYS)
    parser.add_argument("--seed", action="store_true", help="塞几条马上到期的演示题")
    parser.add_argument("--stats", action="store_true", help="看历史统计")
    args = parser.parse_args(argv)

    if args.schedule:
        if not Path(args.schedule).is_file():
            print(f"找不到卡片文件：{args.schedule}")
            return 1
        print(f"数据库：{config.DB_PATH}")
        schedule_card(args.schedule, args.delay_days, args.repo)
        return 0

    if args.seed:
        print(f"数据库：{config.DB_PATH}")
        seed()
        print("已塞入演示题，现在跑 `python src/probe.py` 就能看到它们。")
        return 0

    if args.stats:
        data = stats()
        print(f"数据库：{config.DB_PATH}")
        print(f"  题目总数   {data.get('total') or 0}")
        print(f"  已作答     {data.get('answered') or 0}")
        print(f"  已到期未答 {data.get('overdue') or 0}")
        avg = data.get("avg_score")
        print(f"  平均自评分 {'—' if avg is None else f'{avg:.2f} / 4'}")
        return 0

    rows = due_now(force=args.force)
    if not rows:
        print("今天没有到期的复测题。明天再来。")
        print("\n想先试一下流程：python src/probe.py --seed 然后 python src/probe.py")
        return 0

    if args.list:
        for row in rows:
            label = f"[{row['angle']}] " if row["angle"] else ""
            print(f"{row['id']:>4}  {row['card_id']}  {label}{row['question']}")
        return 0

    _ask_round(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
