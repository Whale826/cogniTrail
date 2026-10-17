"""生成一个用于测试的演示仓库（samples/demo-repo）。

为什么要这个：
    手册第 7 章说，别拿你真正在乎的项目去试错。
    这个脚本两秒钟造出一个带两次提交的小仓库，第二次提交里**故意埋了几个决策点**
    （阈值、缓存、批量、超时、降级兜底、数据切分），这样卡片生成器一跑就有东西可抽。

    python samples/make_demo_repo.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DEMO = _ROOT / "samples" / "demo-repo"

V1_MAIN = '''"""最小分类器训练脚本（演示用，不依赖任何第三方库）。"""

from src.classifier.loader import load_rows

THRESHOLD = 0.5
EPOCHS = 20
LEARNING_RATE = 0.01


def score(features, weights):
    return sum(f * w for f, w in zip(features, weights))


def predict(features, weights):
    return 1 if score(features, weights) >= THRESHOLD else 0


def train(dataset, weights):
    for _ in range(EPOCHS):
        for features, label in dataset:
            error = label - predict(features, weights)
            weights = [w + error * f * LEARNING_RATE for w, f in zip(weights, features)]
    return weights


def report(dataset, weights):
    correct = sum(1 for features, label in dataset if predict(features, weights) == label)
    return correct / len(dataset) if dataset else 0.0


if __name__ == "__main__":
    data = load_rows("data/train.csv")
    fitted = train(data, [0.0] * 8)
    print("accuracy =", report(data, fitted))
'''

V2_MAIN = '''"""最小分类器训练脚本（演示用，不依赖任何第三方库）。"""

from src.classifier.loader import load_rows

THRESHOLD = 0.35
EPOCHS = 20
LEARNING_RATE = 0.01
BATCH_SIZE = 500
CACHE_SIZE = 256


def score(features, weights):
    return sum(f * w for f, w in zip(features, weights))


def predict(features, weights):
    return 1 if score(features, weights) >= THRESHOLD else 0


def batched(dataset, batch_size=BATCH_SIZE):
    for start in range(0, len(dataset), batch_size):
        yield dataset[start:start + batch_size]


def train(dataset, weights):
    cache = {}
    for _ in range(EPOCHS):
        for batch in batched(dataset):
            for features, label in batch:
                key = tuple(features)
                if key not in cache:
                    cache[key] = predict(features, weights)
                error = label - cache[key]
                weights = [w + error * f * LEARNING_RATE for w, f in zip(weights, features)]
                if len(cache) > CACHE_SIZE:
                    cache.clear()
    return weights


def report(dataset, weights):
    if not dataset:
        return 0.0
    correct = sum(1 for features, label in dataset if predict(features, weights) == label)
    return correct / len(dataset)


if __name__ == "__main__":
    data = load_rows("data/train.csv")
    if not data:
        print("没有读到训练数据")
    else:
        fitted = train(data, [0.0] * 8)
        print("accuracy =", report(data, fitted))
'''

V1_CONFIG = '''"""训练配置。"""

SEED = 42
SPLIT_RATIO = 0.8
'''

V2_CONFIG = '''"""训练配置。"""

SEED = 42
SPLIT_RATIO = 0.8
VALIDATION_SPLIT = 0.2
STRATIFY_BY_LABEL = True
'''

V1_LOADER = '''"""读训练数据。"""


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split(",")
            if len(parts) == 2:
                rows.append(([float(x) for x in parts[0].split(";")], int(parts[1])))
    return rows
'''

V2_LOADER = '''"""读训练数据。"""

MAX_RETRIES = 3
TIMEOUT_SECONDS = 30
SKIP_MALFORMED = True


def load_rows(path, retries=MAX_RETRIES):
    """加了重试和降级：读不到就返回空列表，让上层自己决定怎么处理。"""
    for attempt in range(retries):
        try:
            rows = []
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.strip().split(",")
                    if len(parts) != 2:
                        if SKIP_MALFORMED:
                            continue
                        raise ValueError("坏行: " + line[:40])
                    rows.append(([float(x) for x in parts[0].split(";")], int(parts[1])))
            return rows
        except OSError:
            if attempt == retries - 1:
                return []
    return []
'''

README = """# demo-repo

一个用来测试知迹（CogniTrail）的最小仓库。

它只有两次提交，第二次提交里埋了几处典型的决策点：分类阈值、批量大小、缓存上限、
数据切分方式，以及读文件失败时的重试与降级兜底。

不要改这个仓库 —— 它是测试夹具，随时可能被 `make_demo_repo.py` 重建。
"""


def _run(*args: str, root: Path = DEMO) -> None:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")


def _write(rel: str, content: str, root: Path = DEMO) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _clean_outputs(root: Path = DEMO) -> None:
    """清掉上一轮跑 pipeline 留下的卡片。

    只逐个删文件、不整目录删 —— 卡片数量很少，逐个删不会触发
    系统的批量删除保护（那个保护一旦被触发，整个 build 都会失败）。
    """
    decisions = root / "docs" / "decisions"
    if not decisions.is_dir():
        return
    for item in sorted(decisions.rglob("*")):
        if item.is_file():
            try:
                item.unlink()
            except OSError:
                pass


def build(dest: Path | None = None) -> Path:
    """生成演示仓库。`dest` 不给就建在 `samples/demo-repo`。

    刻意**不整目录删了重建**：
    Windows 上批量删目录容易触发系统的删除保护，删到一半失败会让目录变半残，
    而且 `shutil.rmtree` 抛出的异常会把调用它的测试一起带崩。
    所以工作区的四个文件就地覆盖写、旧历史继续往后接。

    不变式靠**写入顺序**保证，不靠历史干净：

        先写 V1 → 提交；再写 V2 → 提交

    于是无论上一次留下的工作区是 V1 还是 V2、上一次是不是被打断，
    最后一对提交里**必然有一笔是真 diff**（V2→V1 或 V1→V2），
    `HEAD~1..HEAD` 永远非空。旧版把这一步交给「历史每次重建」，
    结果每次 rebuild 都要整目录删 `.git`，在 Windows 上又慢又容易被拦。

    `dest` 是给测试用的：每个用例在自己的临时目录里建仓库，
    互不干扰，也不会被上一次跑剩下的脏状态带偏。
    """
    root = Path(dest) if dest is not None else DEMO
    root.mkdir(parents=True, exist_ok=True)
    _clean_outputs(root)

    if not (root / ".git").is_dir():
        _run("init", "-b", "main", root=root)
    # 只改这个仓库的局部配置，不动你的全局 git 配置
    _run("config", "user.email", "demo@cognitrail.local", root=root)
    _run("config", "user.name", "CogniTrail Demo", root=root)

    _write("README.md", README, root)
    _write("src/classifier/train.py", V1_MAIN, root)
    _write("src/classifier/config.py", V1_CONFIG, root)
    _write("src/classifier/loader.py", V1_LOADER, root)
    _write("src/classifier/__init__.py", "", root)

    _run("add", "-A", root=root)
    _commit("初始版本：一个朴素的最小分类器", root)

    _write("src/classifier/train.py", V2_MAIN, root)
    _write("src/classifier/config.py", V2_CONFIG, root)
    _write("src/classifier/loader.py", V2_LOADER, root)

    _run("add", "-A", root=root)
    _commit("让 AI 优化了一版：调阈值、分批、加缓存、加重试与降级", root)

    # 哨兵：夹具自己坏了要立刻喊，别让调用方去猜
    diff = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--stat"],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if diff.returncode != 0 or not diff.stdout.strip():
        raise RuntimeError("演示仓库的 HEAD~1..HEAD 是空 diff，夹具没建好")

    return root


def _commit(message: str, root: Path) -> None:
    """有东西就提交，没东西就跳过 —— 不制造空提交，也不因「无改动」失败。

    `git commit` 在「nothing to commit」时返回 1、报错还写在 stdout，
    `_run` 只看 stderr，于是异常信息是空的（踩过）。
    上一次 build 恰好把工作区留在了 V1，这一笔就是空的 —— 跳过它，
    后面那笔 V1→V2 仍然是实打实的 diff，`HEAD~1..HEAD` 照样成立。
    """
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if staged.returncode == 0 and _has_commit(root):
        return
    _run("commit", "-m", message, root=root)


def _has_commit(root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return result.returncode == 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    path = build()
    print(f"演示仓库已生成：{path}")
    print("\n现在可以跑：")
    print(f"  python src/pipeline.py --repo \"{path}\"")
