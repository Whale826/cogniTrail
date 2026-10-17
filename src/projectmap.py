"""项目主线（project spine）：让每张决策卡片挂到「这个项目的哪一步」。

为什么需要这个模块（这是卡片「太杂」的第一个根因）：
    cardgen_session 是**逐轮**抽卡的，一张卡片只知道「我在第 3 轮改了 .env 的第 7 行」。
    十几轮下来几十张卡，全是「改了哪里」，没有一张能回答：
        「这件事在项目里属于哪一步？为什么此刻必须做它？」
    于是读者看到的是一份**按时间流水排的碎片清单**，而不是一个项目的推进过程。

    修法不是在卡片上多写一句解释 —— 那样每张卡各写各的，阶段名还是不统一。
    修法是先给项目建一条**主线**：项目目标 + 若干阶段（每阶段有自己的目标与典型卡点），
    存成一个 json；之后生成卡片时把它喂给模型，模型只能从这条主线里挑一个阶段。
    这样几十张卡片引用的是**同一套阶段名**，才拼得出一张项目地图。

    为什么落盘成 json 而不是每次现算：
        阶段名必须**稳定**。每次重算，同一个阶段这次叫「打通模型通道」下次叫
        「修复 API 连接」，卡片之间就对不齐了。所以算一次、存下来、之后一直用它，
        直到用户显式 `--refresh`。

    为什么允许手改：
        自动提炼只是起点。用户比模型更清楚自己的项目分几步 ——
        直接编辑 `project-map.json` 就是权威版本，本模块不会覆盖它（除非 --refresh，
        且覆盖前会先备份一份 .bak）。

用法：
    python src/projectmap.py --repo <项目根>            # 没有就建，有就直接读
    python src/projectmap.py --repo <项目根> --refresh  # 丢掉重算（先备份旧的）
    python src/projectmap.py --repo <项目根> --show     # 打印出来看
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, llm  # noqa: E402

MAP_NAME = "project-map.json"

#: 阶段上限。超过 7 个就没人记得住了，卡片也会被摊得太薄。
MAX_STAGES = 7
MIN_STAGES = 3


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
MAP_PROMPT = """你在为一个工程 / 研究项目建立一条「决策留痕的主线」。

下面会给你这个项目的材料（README、计划书、已经记录下来的决策、工作流总结）。
请把它归纳成**几个阶段** —— 阶段 = 这个项目从开始到现在，走过的几个大步骤。

只输出一个 JSON 对象，不要任何多余文字，格式：

{{
  "project": "项目名（20 字内，从材料里读，不要自己编）",
  "goal": "这个项目最终要做成什么（30 字内，一句话）",
  "stages": [
    {{"id": "S1",
      "name": "阶段名（8 字内，要短、要稳、以后会有几十张卡片引用它）",
      "goal": "这一步要达成什么（25 字内）",
      "problems": ["这一步最容易卡住的问题，1~3 条，每条 20 字内"]}}
  ]
}}

硬性要求：
1. stages 给 {min_stages}~{max_stages} 个，**按时间或依赖顺序**排：先发生的、更基础的排前面。
2. 阶段要按「项目推进到哪一步」切，不是按文件、不是按技术栈。
   反例：「修改配置文件」「重构代码」「写测试」——这是动作，不是阶段。
   正例：「确定产品形态」「打通模型通道」「卡住：闸门太严」这种能看出推进的切法。
3. name 必须短。它会成为几十张卡片的标签，长了会糊在一起。
4. problems 写这一步**真的卡过人的地方**（材料里出现过的困难），没有就留空数组，不要编。
5. 最后一个阶段要**一直覆盖到项目现在的位置**（材料里最新的那几件事），
   不要只归纳早期 —— 只写到「跑通了」就把最近的进展丢了。
6. 只依据材料，不要引入材料里没有的阶段。
7. 全部用中文，不要把 JSON 包在 ``` 里。

——————————
"""


# --------------------------------------------------------------------------
# 材料
# --------------------------------------------------------------------------
#: 取材用的文件（按优先级）。README/PLAN 放在最前，因为它们最像「项目全貌」。
_DOC_FILES = ("README.md", "PLAN.md", "docs/design", "docs/experiments")


def _read_text(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit]


def _headings(text: str, level: int = 2) -> list[str]:
    """抽出**恰好这个层级**的标题行。

    只要二级：一级是文档标题（「知迹 CogniTrail · P0 实施计划」），
    三级是细节小节 —— 拿它们当阶段候选都不对。
    另外必须跳过 ``` 代码块内部：README 里 `# 1. 直接开窗口` 是 bash 注释，
    不是标题；不跳过的话它会被当成一个「阶段」，离线提炼就出了怪名字。
    """
    out: list[str] = []
    fenced = False
    pattern = re.compile(rf"^#{{{level}}}\s+(.+?)\s*$")
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = pattern.match(line)
        if match:
            out.append(match.group(1))
    return out


def _field_of(card_text: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*(.+)$", card_text, re.MULTILINE)
    return match.group(1).strip().strip('"').strip("'") if match else ""


def gather_materials(repo: Path, max_chars: int = 16000) -> str:
    """把这个项目「是什么、走过哪些路」的材料凑成一段文本。

    刻意不塞整份文件：阶段提炼靠的是**骨架**（标题、已记录的决策、git 提交），
    把几万字正文塞进去既贵又容易被细节带偏。

    但 **PLAN 的标题要收全**（正文只取开头）：计划文件往往是「越往后越新」，
    只截前 6000 字的话，最近那几步（比如「形态变更」「修双击闪退」）
    根本进不了材料，提炼出来的主线就停在项目早期 —— 这个坑真踩过。
    """
    parts: list[str] = []

    readme = _read_text(repo / "README.md", 4000)
    if readme:
        parts.append("【README 摘要】\n" + readme.strip()[:4000])

    plan_text = _read_text(repo / "PLAN.md", 120000)
    if plan_text:
        heads = _headings(plan_text, 2)
        if heads:
            parts.append(
                "【计划文件的全部章节（越往后越新）】\n"
                + "\n".join(f"- {h}" for h in heads[:40])
            )
        parts.append("【计划文件开头】\n" + plan_text[:4000].strip())

    # docs/design、docs/experiments 之类：只取标题，取的是「章节骨架」
    design = repo / "docs" / "design"
    if design.is_dir():
        names: list[str] = []
        for file in sorted(design.glob("*.md"))[:6]:
            names.extend(_headings(_read_text(file, 6000), 2))
        if names:
            parts.append("【设计文档的章节】\n" + "\n".join(f"- {n}" for n in names[:30]))

    # 已经记录下来的决策：这是「项目实际走过的路」最硬的证据
    cards_dir = repo / "docs" / "decisions"
    if cards_dir.is_dir():
        decisions: list[str] = []
        for file in sorted(cards_dir.glob("*.md")):
            if file.name in {"SUMMARY.md", "CURATED.md"}:
                continue
            text = _read_text(file, 2000)
            decision = _field_of(text, "decision")
            stage = _field_of(text, "stage")
            if decision:
                decisions.append(f"- {stage + ' | ' if stage else ''}{decision[:80]}")
            if len(decisions) >= 40:
                break
        if decisions:
            parts.append("【已经记录下来的决策（按时间）】\n" + "\n".join(decisions))

    # 工作流总结的标题
    flows = repo / "docs" / "workflows"
    if flows.is_dir():
        titles: list[str] = []
        for file in sorted(flows.glob("*.md"))[:10]:
            for line in _read_text(file, 800).splitlines():
                if line.startswith("# "):
                    titles.append("- " + line[2:].strip())
                    break
        if titles:
            parts.append("【工作流总结】\n" + "\n".join(titles))

    # git 提交历史：最能看出「分几步走」
    log = _git_log(repo)
    if log:
        parts.append("【git 提交历史（最近 30 条）】\n" + log)

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…（材料过长，已截断）"
    return body


def _git_log(repo: Path, limit: int = 30) -> str:
    if not (repo / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", f"-{limit}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def _one_line(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize(data, repo: Path) -> dict:
    """把模型返回的东西洗成「一定是这个形状」。缺什么补什么，不抛异常。"""
    project = repo.name or "未命名项目"
    goal = ""
    stages: list[dict] = []

    if isinstance(data, dict):
        project = _one_line(data.get("project") or project, 30) or project
        goal = _one_line(data.get("goal") or "", 60)
        raw_stages = data.get("stages")
        if isinstance(raw_stages, list):
            for item in raw_stages:
                if not isinstance(item, dict):
                    continue
                name = _one_line(item.get("name") or "", 18)
                if not name:
                    continue
                problems = item.get("problems")
                if isinstance(problems, str):
                    problems = [problems]
                stages.append(
                    {
                        "id": _one_line(item.get("id") or "", 6),
                        "name": name,
                        "goal": _one_line(item.get("goal") or "", 40),
                        "problems": [
                            _one_line(p, 40) for p in (problems or []) if str(p).strip()
                        ][:3],
                    }
                )

    # 编号统一重排（模型给的 id 可能重复、可能缺失），保证 S1..Sn 连续
    for index, stage in enumerate(stages[:MAX_STAGES], 1):
        stage["id"] = f"S{index}"

    # 注意 str(None) == "None" 是个真值 —— 直接 str() 会把缺字段变成字面量 "None"，
    # 于是「来源」那一栏写着 None。必须先 or 掉空值再 str。
    raw_source = data.get("_source") if isinstance(data, dict) else ""
    source = str(raw_source or "").strip() or ("offline" if config.MOCK else "model")

    return {
        "project": project,
        "goal": goal,
        "repo": str(repo),
        "stages": stages,
        "source": source,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "edited": False,
    }


# --------------------------------------------------------------------------
# 离线版：从文档骨架里抽阶段
# --------------------------------------------------------------------------
#: 这些标题是「工程杂项」，不是项目推进的阶段，抽阶段时要跳过。
_SKIP_HEADINGS = re.compile(
    r"目录结构|目录树|模块|测试|许可|license|卡片格式|设计文档|项目状态|"
    r"快速开始|它做什么|它不做什么|数据从哪来|风险|里程碑|交付物|验收标准|"
    r"范围|环境|未决|已放弃|说明|安装|打包|常见问题|附录|参考|更新日志|"
    r"偏差|结论|对比|清单|定义|总览|概览"
)


def _clean_heading(text: str) -> str:
    """去掉「10. 形态变更：改成 exe」这类编号、括注和多余标点。

    括注（「（2026-10-13 追加）」「（M5 起跑线）」）要剥掉：
    它们带日期和版本，留着的话同一个阶段每次摘出来都不一样 ——
    而阶段名必须稳定，这是它能当卡片标签的前提。
    """
    text = re.sub(r"^\s*[\d]+[.、)）]?\s*", "", text.strip())
    text = re.sub(r"[（(][^（()）]{0,30}[)）]\s*$", "", text).strip()
    text = re.sub(r"^[（(【\[](.+?)[）)】\]]$", r"\1", text)
    return text.strip(" ·:：-—")


def _stages_from_headings(headings: list[str], limit: int = MAX_STAGES) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in headings:
        name = _clean_heading(raw)
        if not name:
            continue
        if _SKIP_HEADINGS.search(name):
            continue
        # 长标题不丢，截短 —— 丢掉的话「拿到 API Key 后的三步（M5 起跑线）」
        # 这种真阶段就被误杀了，最后只剩两三个阶段，等于没抽
        name = _one_line(name, 13).rstrip("…").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": f"S{len(out) + 1}", "name": name, "goal": "", "problems": []})
        if len(out) >= limit:
            break
    return out


#: 什么都抽不到时的兜底阶段（通用推进顺序，至少保证卡片有阶段可用）
_FALLBACK_STAGES = (
    "确定目标与范围",
    "打通基础设施",
    "核心功能实现",
    "验证与交付",
)


def _section_goals(text: str) -> dict[str, str]:
    """每个标题下面第一句正经正文，当作这一节的「目标」。

    离线模式下有它，阶段才不会只有一个光秃秃的名字 ——
    卡片上的 stage 写着「S3 产品形态决策」而没有任何解释，读者还是看不懂。
    """
    goals: dict[str, str] = {}
    current = ""
    fenced = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        heading = re.match(r"^#{1,2}\s+(.+?)\s*$", line)
        if heading:
            current = _clean_heading(heading.group(1))
            continue
        if fenced or not current or current in goals:
            continue
        body = line.strip()
        if not body or body[0] in "|>-*#!`":
            continue
        goals[current] = _one_line(re.sub(r"[*`]", "", body), 40)
    return goals


def _lookup_goal(goals: dict[str, str], name: str) -> str:
    """按前缀找目标 —— 阶段名被截短过，不能要求完全相等。"""
    if not name:
        return ""
    for key, value in goals.items():
        if key.startswith(name[:6]) or name.startswith(key[:6]):
            return value
    return ""


def mock_build(repo: Path, log=print) -> dict:
    """不花钱的骨架：项目名与目标从 README 取，阶段从 PLAN/README 的章节里抽。"""
    log("离线模式：从文档骨架里抽项目主线（配 Key 后重跑会更准）")

    readme = _read_text(repo / "README.md", 6000)
    plan = _read_text(repo / "PLAN.md", 12000)

    project = repo.name or "未命名项目"
    for line in readme.splitlines():
        if line.startswith("# "):
            project = _clean_heading(line[2:]) or project
            break

    goal = ""
    for raw in readme.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "-", "|", "```", "!")):
            continue
        goal = _one_line(line, 60)
        break
    if not goal:
        for line in plan.splitlines():
            if line.startswith("# "):
                goal = _one_line(_clean_heading(line[2:]), 60)
                break

    stages = _stages_from_headings(_headings(plan, 2))
    goals = _section_goals(plan)
    if len(stages) < MIN_STAGES:
        stages = _stages_from_headings(_headings(readme, 2))
        goals = _section_goals(readme)
    if len(stages) < MIN_STAGES:
        stages = [
            {"id": f"S{i}", "name": name, "goal": "", "problems": []}
            for i, name in enumerate(_FALLBACK_STAGES, 1)
        ]
        goals = {}

    for stage in stages:
        stage["goal"] = _lookup_goal(goals, stage["name"])
        stage["problems"] = []

    return normalize(
        {
            "project": project,
            "goal": goal,
            "stages": stages,
            "_source": "offline",
        },
        repo,
    )


# --------------------------------------------------------------------------
# 建 / 读 / 存
# --------------------------------------------------------------------------
def _map_path(repo: Path, folder: Path | None = None) -> Path:
    return (Path(folder) if folder else Path(repo) / "docs" / "decisions") / MAP_NAME


def load(
    repo: str | Path,
    folder: Path | None = None,
    *,
    refresh: bool = False,
    create: bool = True,
    log=print,
) -> dict | None:
    """拿到这个项目的阶段清单。

    create=False 时，没有就返回 None（只读场景用，比如只想看阶段名）。
    """
    repo = Path(repo).expanduser().resolve()
    path = _map_path(repo, folder)

    if path.is_file() and not refresh:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict) and data.get("stages"):
            return data

    if not create:
        return None

    data = build(repo, folder, log=log)
    return data


def build(repo: str | Path, folder: Path | None = None, *, log=print) -> dict:
    """问一次模型（或离线兜底），并把结果落盘。"""
    repo = Path(repo).expanduser().resolve()
    path = _map_path(repo, folder)

    # 覆盖前先备份 —— 用户手改过的阶段名是最有价值的东西，不能悄悄弄丢
    if path.is_file():
        backup = path.with_name(f"{MAP_NAME}.bak")
        try:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            log(f"旧的阶段清单已备份：{backup.name}")
        except OSError:
            pass

    materials = gather_materials(repo)
    if materials.strip():
        prompt = MAP_PROMPT.replace("{min_stages}", str(MIN_STAGES)).replace(
            "{max_stages}", str(MAX_STAGES)
        ) + materials
        data = llm.complete_json(
            prompt,
            temperature=0.3,
            mock_fn=lambda: mock_build(repo, log=log),
        )
    else:
        log("这个项目里没有可用的文档（README / PLAN / 决策 / git 历史），走离线兜底。")
        data = mock_build(repo, log=log)

    result = normalize(data, repo)
    if not result["stages"]:
        result = normalize(mock_build(repo, log=lambda *_: None), repo)
        result["source"] = "offline"

    save(path, result)
    log(f"项目主线已建立：{result['project']} · {len(result['stages'])} 个阶段 → {path}")
    return result


def save(path: Path, data: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 阶段匹配：把模型给的阶段名对齐到清单里
# --------------------------------------------------------------------------
def labels(pmap: dict | None) -> list[str]:
    """所有阶段的标签，形如 ['S1 确定产品形态', ...]。这就是卡片 stage 字段的取值域。"""
    return [label_of(stage) for stage in ((pmap or {}).get("stages") or [])]


def label_of(stage: dict) -> str:
    name = str(stage.get("name") or "").strip()
    sid = str(stage.get("id") or "").strip()
    return f"{sid} {name}".strip() if name else sid


def stage_id(label: str) -> str:
    match = re.match(r"^(S\d+)\b", str(label or "").strip())
    return match.group(1) if match else ""


def _squash(text: str) -> str:
    return re.sub(r"[\s\-_·、,，。.:：]+", "", str(text or "")).lower()


def _overlap(a: str, b: str) -> int:
    """a、b 的最长公共子串长度（只用来做中文里的近似匹配）。"""
    if not a or not b:
        return 0
    best = 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def match_stage(value: str, pmap: dict | None) -> str:
    """把模型给的 stage 归到清单里，返回清单里的标准标签。

    为什么允许「其他：xxx」这种落法而不是直接丢弃：
        模型有可能真的识别出一个清单里没有的阶段（说明主线该更新了）。
        丢掉它等于否认事实；直接采用又会污染阶段名的一致性。
        所以归到「其他：」下 —— 用户看一眼就知道「该 refresh 了」，卡片本身也不丢。
    """
    raw = " ".join(str(value or "").split())
    # 模型按提示词写「新阶段：xxx」时，把前缀剥掉再匹配 ——
    # 否则「新阶段：打通模型通道」会匹配不上清单里的「S2 打通模型通道」
    raw = re.sub(r"^(新阶段|新增阶段|其他|其它)\s*[:：]\s*", "", raw).strip()
    stages = (pmap or {}).get("stages") or []
    if not stages:
        return raw or "未归类"
    if not raw:
        return "未归类"

    squashed = _squash(raw)
    for stage in stages:
        label = label_of(stage)
        if squashed in {_squash(label), _squash(stage.get("name")), _squash(stage.get("id"))}:
            return label

    for stage in stages:
        name = _squash(stage.get("name"))
        sid = _squash(stage.get("id"))
        if sid and sid == squashed:
            return label_of(stage)
        if name and (name in squashed or squashed in name):
            return label_of(stage)

    # 中文近似：共享一段 ≥3 字的连续文字就算同一个阶段
    for stage in stages:
        name = _squash(stage.get("name"))
        if _overlap(name, squashed) >= 3:
            return label_of(stage)

    return f"其他：{raw[:24]}"


def render_markdown(pmap: dict) -> str:
    """给人看的一页 —— 顺手可以贴进 README 或答辩材料。"""
    lines = [f"# 项目主线 · {pmap.get('project') or '（未命名）'}", ""]
    if pmap.get("goal"):
        lines += [f"**项目目标**：{pmap['goal']}", ""]
    if pmap.get("repo"):
        lines += [f"- 项目目录：`{pmap['repo']}`"]
    lines += [f"- 生成方式：{'离线提炼' if pmap.get('source') == 'offline' else '模型 ' + config.MODEL}",
              f"- 生成时间：{pmap.get('generated_at', '')}",
              ""]
    lines.append("## 阶段")
    lines.append("")
    for stage in pmap.get("stages") or []:
        lines.append(f"### {label_of(stage)}")
        if stage.get("goal"):
            lines.append(f"- 目标：{stage['goal']}")
        problems = stage.get("problems") or []
        if problems:
            lines.append("- 这一步容易卡住的问题：")
            for problem in problems:
                lines.append(f"  - {problem}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 这份主线是决策卡片的坐标系：每张卡片的 `stage` 字段都从上面这几步里选。")
    lines.append("> 想改就直接编辑 `docs/decisions/project-map.json`，本工具不会覆盖你的修改。")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="projectmap",
        description="建立 / 查看这个项目的「主线」（阶段清单），卡片靠它挂到项目上",
    )
    parser.add_argument("--repo", default="", help="项目根，默认当前目录")
    parser.add_argument("--out", default="", help="卡片目录，默认 <项目>/docs/decisions")
    parser.add_argument("--refresh", action="store_true", help="丢掉现有清单重算（先备份）")
    parser.add_argument("--show", action="store_true", help="把主线打印出来")
    parser.add_argument("--write-md", action="store_true",
                        help="另外写一份给人看的 PROJECT-MAP.md")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    folder = Path(args.out).expanduser() if args.out else None

    print(config.banner() + "\n")
    print(f"项目：{repo}\n")

    try:
        pmap = load(repo, folder, refresh=args.refresh)
    except llm.LLMError as exc:
        print(f"模型调用失败：{exc}")
        return 2

    if pmap is None:
        print("没能建立项目主线。")
        return 1

    for stage in pmap.get("stages") or []:
        line = f"  {label_of(stage)}"
        if stage.get("goal"):
            line += f" —— {stage['goal']}"
        print(line)
    print(f"\n共 {len(pmap.get('stages') or [])} 个阶段 · 来源：{pmap.get('source')}")

    if args.show:
        print()
        print(render_markdown(pmap))

    if args.write_md:
        target = (Path(folder) if folder else repo / "docs" / "decisions") / "PROJECT-MAP.md"
        target.write_text(render_markdown(pmap), encoding="utf-8")
        print(f"\n已写出：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
