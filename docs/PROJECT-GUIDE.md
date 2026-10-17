# 知迹 CogniTrail · 源码学习指南

> 适用版本 0.12.2 · 全项目约 15,000 行 Python（不含打包产物），**零第三方依赖**（只用标准库 + git + tkinter）。
> 这份文档按「一条流水线」的顺序讲解每个文件：数据从哪来 → 怎么变成卡片 → 怎么审计 → 怎么展示 → 界面怎么串起来。

---

## 0. 一张图看懂整个工程

```
┌─────────────────────── 数据采集层 ───────────────────────┐
│ sources/  三种 AI 工具会话的「适配器」                     │
│   codex.py / workbuddy.py / claude.py                    │
│   _tools.py  工具调用 → 补丁(Patch) 的通用翻译器           │
│ session_source.py  统一的会话模型（Session/Turn/Patch）    │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌─────────────────────── 生成层 ───────────────────────────┐
│ llm.py            调 DeepSeek（真模型 / Mock 双轨）        │
│ cardgen.py        从 git diff 生成卡片（最早的入口）        │
│ cardgen_session.py 从会话轮次生成卡片（主入口，最核心）      │
│ workflow.py       会话 → 工作流总结                        │
│ questions.py      卡片 → 复测问题                          │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌─────────────────────── 审计层（本项目灵魂） ──────────────┐
│ schema.py    卡片的数据结构 + 证据闸门（文件:行号 必须真实） │
│ audit.py     关键度定档 / 造假痕迹扫描 / 审计意见           │
│ quota.py     按用户水平决定「给几张、留哪张」                │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌─────────────────────── 策展与展示层 ─────────────────────┐
│ curate.py        卡片库 → 有序 + 融合冲突 + 演进链          │
│ projectmap.py    项目主线（阶段清单），卡片挂靠点            │
│ dashboard.py     卡片 → dashboard.html 决策面板             │
│ probe.py         延迟复测（3 天后再问你一遍）                 │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌─────────────────────── 入口与界面层 ─────────────────────┐
│ cognitrail.py    CLI 总入口（argparse 子命令 + 交互菜单）    │
│ gui.py           Tk 图形界面（最大的文件，3400 行）           │
│ watch.py         守护进程：盯着会话文件变化                   │
│ pipeline.py      最短演示链路（diff → 卡片 → 面板）           │
└──────────────────────────────────────────────────────────┘

支撑设施：
  config.py   所有路径与 .env 配置     tone.py   专家/小白两档措辞
  cli_bridge.py  调本机 AI 命令行      doctor.py 环境自检
```

**推荐学习路线**（按依赖顺序，由浅入深）：

1. `cognitrail.py` → 2. `config.py` → 3. `schema.py` → 4. `sources/` + `session_source.py`
→ 5. `llm.py` → 6. `cardgen_session.py` → 7. `audit.py` → 8. `quota.py`
→ 9. `dashboard.py` → 10. `curate.py` / `projectmap.py` → 11. `gui.py` → 12. `tools/`

---

## 1. src/ —— 主包（27 个文件）

### 1.1 `cognitrail.py`（814 行）— CLI 总入口

**职责**：所有子命令的注册与分发，控制台版的交互菜单，也是 `cmd_gui` 之外一切功能的粘合层。`VERSION` 定义在这里（注意：`gui.py` 里还有一份，升版本必须两处同改）。

| 函数 | 作用 |
|---|---|
| `build_parser()` | 注册全部子命令：run / watch / gui / workflow / curate / audit / sessions / projectmap / panel / probe / doctor / cli-chat |
| `main()` / `_dispatch()` | 无参数时进交互菜单 `_launcher()`；有参数走 argparse |
| `cmd_run()` | 核心链路：解析会话 → 生成卡片 → 排探针 → 渲染面板 → 打审计意见 |
| `cmd_watch()` / `cmd_gui()` | 转发到 watch.Watcher / gui.main |
| `cmd_workflow()` | 会话 → 工作流总结（落 docs/workflows/） |
| `cmd_curate()` | 卡片库整理（CURATED.md） |
| `cmd_audit()` | 对已有卡片出审计报告 |
| `_schedule_probes()` | 卡片写盘后按 delay_days 预约复测题 |

### 1.2 `config.py`（317 行）— 全局配置中枢

**职责**：所有路径常量、.env 解析、Mock 开关。**每个模块都 import 它**，改这里影响全局。

| 函数/常量 | 作用 |
|---|---|
| `ROOT` / `STATE_DIR` / `DECISIONS_DIR` | 运行根目录（exe 形态下不同：exe 旁 vs 项目内）、`~/.cognitrail/`、`<项目>/docs/decisions/` |
| `_load_env()` | 内置极简 .env 解析（不依赖 python-dotenv） |
| `API_KEY` / `MOCK` / `MOCK_REASON` | 没 Key 自动回退 Mock 模式，且记住原因（界面会解释给你听） |
| `TONE` | 写法档位（pro/plain），来源 `.env` 的 `COGNITRAIL_TONE` |
| `apply_runtime()` | 运行中改了配置（如界面里设 Key）后热更新 |
| `update_env()` | 把改动写回 .env（优先 exe 旁，写不动退 `~/.cognitrail/.env`） |
| `enable_utf8_stdout()` / `banner()` | Windows 控制台 UTF-8 修正；启动横幅 |

### 1.3 `schema.py`（464 行）— 卡片的数据结构 + 证据闸门

**职责**：定义「决策卡片」长什么样，以及**证据必须能被脚本回文件核对**这条铁律。

| 成员 | 作用 |
|---|---|
| `FIELD_ORDER` | 卡片字段白名单顺序（file/lines/decision/why/quote/alternatives/…）。**加字段要同时改四处**：这里 + `Card` 字段 + `from_llm()` + `dashboard.parse_card()` |
| `class Card` | 卡片对象。`from_llm()` 从模型 JSON 构造，`to_markdown()` 落盘成带 front-matter 的 md |
| `evidence_ok()` | **证据闸门**：`file:lines` 定位不到文件/行 → 拒收。起始行和结束行都校验 |
| `parse_lines()` / `count_lines()` | 行号区间解析、文件真实行数 |
| `strip_audit_only()` | 剥掉模型不该填的审计字段（signals/verdict/tier_why 只能由脚本写） |

### 1.4 `sources/` 子包（4 个文件）— 会话采集适配器

**职责**：三种 AI 工具的会话文件格式各不相同，这里统一翻译。

| 文件 | 作用 |
|---|---|
| `__init__.py` | 适配器注册表（`ADAPTERS`/`ORDER`），对外提供 `list_all()` / `newest()` / `parse_any()` / `peek()` —— 上层永远只面对这个统一接口 |
| `session_source.py` 其实不在子包里 | 见 1.5 |
| `codex.py`（85 行） | 读 `~/.codex/sessions/**/rollout-*.jsonl`，认 `task_complete` 为一轮结束 |
| `claude.py`（231 行） | 读 `~/.claude/projects/**/*.jsonl`，解析 message.content 块数组 |
| `workbuddy.py`（225 行） | 读 WorkBuddy 自己的会话日志 |
| `_tools.py`（339 行） | **通用翻译器**：把各工具的工具调用（Write/Edit/Bash/apply_patch…）统一翻成 `Patch`（增删了哪些文件哪些行）。`TurnAssembler` 把零散 JSON 行拼成完整轮次 |

### 1.5 `session_source.py`（585 行）— 统一会话模型

**职责**：定义三个核心数据类，并自带一个 Codex 解析器（历史原因，codex 的逻辑早期写在这里）。

- `Patch`：一次文件改动（文件名、增删行、diff 文本、call_id）
- `Turn`：一轮对话（用户说了什么 / AI 答了什么 / 改了哪些文件 / 有没有结论）
- `Session`：整个会话（轮次列表、项目路径、元信息）
- 关键函数：`parse_session()`（JSONL → Session，`max_turns` 控制只读最后 N 轮）、`clean_user_message()`（剥掉系统注入的标签噪声）、`read_new_lines()`（watch 增量读取用）

### 1.6 `llm.py`（324 行）— 模型调用（真/假双轨）

| 函数 | 作用 |
|---|---|
| `_chat()` | 用**标准库 urllib** 直连 DeepSeek（这是「零依赖」的关键），失败给中文解释 `_explain()` |
| `complete_json()` | 发 prompt → 剥 markdown 围栏 → `json.loads`，是所有真模型调用的唯一出口 |
| `mock_extract_cards()` | Mock 模式：按正则从 diff 里「启发式地」抽卡片（阈值变化、删除校验等信号） |
| `mock_make_questions()` | Mock 模式的复测题 |

### 1.7 `cardgen.py`（476 行）— 最早期的卡片入口（git diff 版）

从 git 提交的 diff 生成卡片。现在主流程已被 `cardgen_session` 取代，但仍保留：单文件可独立跑（`python -m src.cardgen --repo …`），是理解「卡片怎么来的」最短教材。
核心：`get_diff()` → `split_by_file()`（按文件切 diff）→ `generate()`（先收全候选 → 配额筛选 → 写盘）→ `_write()`。

### 1.8 `cardgen_session.py`（1200 行）— **主生成引擎（全项目最核心）**

**职责**：从会话轮次生成审计级决策卡片。一条流水线全在这里。

| 函数 | 作用 |
|---|---|
| `generate_from_session()` | 主流程：`gate()` 闸门初筛 → 逐轮建 prompt（`build_turn_prompt`）→ 模型抽卡 → `schema.Card` 构造（自动过证据闸门）→ `audit.audit_card()` 审计 → `quota.select()` 按水平配额 → 写盘 + `write_summary()` |
| `gate()` | 闸门：没有改动、没有结论性发言的轮次直接跳过（省模型调用） |
| `existing_digest()` / `_resolve_supersedes()` | 把已有卡片做成摘要喂给模型 → 新卡片能声明「取代 #N」（冲突融合的第一步） |
| `find_quote_lines()` | 模型给的 `quote` 必须真能在文件里找到，行号由此**回算**而不是信模型 |
| `find_stage()`→`_guess_stage()` | 靠 projectmap 把卡片挂到项目主线的某个阶段 |
| `next_index()` | 多源共用输出目录时的序号续编 |
| `write_summary()` | SUMMARY.md：`candidates=` 参数保证审计意见按**全部候选**算，而不是按配额截断后写盘的算（防「用配额洗白审计意见」） |
| `mock_extract_cards()` | Mock 版抽卡（走同一套闸门和配额） |

### 1.9 `audit.py`（961 行）— 审计引擎（项目的灵魂）

**职责**：所有结论**由脚本按规则算**，模型只能往上抬关键度、不能往下压。

| 函数 | 作用 |
|---|---|
| `class Subject` | 被审对象：文件路径 + 增删行 + 提交说明 |
| `scan_fraud()` | 造假痕迹扫描：删校验、吞异常（except: pass）、写死凭据、`--no-verify` 绕流程、强制提交等，逐条命中给标签 |
| `risk_profile()` / `sensitive_file()` | 动到高风险面（认证/支付/删除/配置…）的识别 |
| `materiality()` | 关键度定档：算出「该是几级」，`merge_tier()` 再和模型声称的级别合并 —— **模型只能抬不能压** |
| `check_claim()` | 断言核对：`claim` 里的标识符能在证据行里找到多少（阈值 0.6），对不上记「断言不实」 |
| `audit_card()` | 单卡总入口，产出 `verdict`/`signals`/`tier`/`tier_why` 写回卡片 |
| `is_audited()` | **只认 verdict/signals，不认 tier**（tier 模型也能填） |
| `opinion()` | 整批审计意见：无保留 / 带解释性说明 / 保留（争议卡片单列） |
| `counts_line()` | 计数文案的唯一出口（`关键决策 N 条 · 需人工复核 N 条（其中存疑 N 条）…`），别处不得另写 |
| `subject_from_diff()` / `subject_from_patch()` | 从 diff / Patch 构造 Subject |
| `diff_lines()` | 解析 @@ 行号头，算出新增行的真实行号 |

### 1.10 `quota.py`（722 行）— 按用户水平配额

**职责**：回答「这次给几张卡片、留哪几张」。两个轴：**专业度**（长期画像）× **关切度**（当下状态）。

| 函数 | 作用 |
|---|---|
| `level_of_text()` | 从会话文本推断用户专业度 L0~L3：**只看术语/代码坐标/约束表达，不看句子长度**（红线） |
| `class Profile` / `load_profile()` / `save_profile()` | 长期画像（`~/.cognitrail/level.json`），升 2 降 3、单次最多一档，冷启动前 2 次不判断 |
| `interest_of()` | 关切度：**只由负信号判 low**（只问结果/连着关卡片），没信号一律 mid |
| `quota()` | 查配额表（L2 最高：一般关切 L0=1/L1=4/L2=5/L3=4），`min(配额, supply)`，**不补足** |
| `value()` | 单卡价值分：有发现的卡片（doubt/高危痕迹）**不占配额、不被裁** |
| `select()` | 截断算法：按价值取前 N + stage 轮转 + 同文件上限 2（**不是**按顺序取前 N） |
| `choose()` | 对外一步到位：读画像 → 判级 → 算关切 → 配额 → 筛选 |

### 1.11 `curate.py`（1016 行）— 卡片库整理

**职责**：卡片多了以后「通读全库 → 排成有逻辑的顺序 → 融合冲突 → 写演进链」。

- `reverify_card()`：**不对称复核** —— 断言对不上记高危；核对通过**什么都不写**、仍算「未审计」（核对通过 ≠ 审计通过，这是刻意设计）
- `curate()`：build_prompt（带审计摘要）→ 模型排序融合 → `normalize()`（模型输出收敛到白名单结构，步骤<2 不算演进、首尾 cards 相交整条丢弃）
- `render_markdown()` / `save()`：产出 `CURATED.md` + `curated.json`，窗口据此把被取代的卡片标「已被#N取代」

### 1.12 `projectmap.py`（673 行）— 项目主线

- `build()`：从 README/PLAN/已有卡片/git log 提炼「目标 + 3~7 个阶段」，落 `project-map.json`；**只建一次，用户手改是权威**（`--refresh` 才重算且先写 .bak）
- `match_stage()`：卡片阶段名归一化（编辑距离/包含匹配），归不进落「其他：」
- `render_markdown()`：产出给人看的 `PROJECT-MAP.md`

### 1.13 `dashboard.py`（710 行）— 决策面板

- `parse_card()`：md → dict（**显式白名单**解析 front-matter + 正文段落 + diff 代码块）
- `load_cards()` / `load_marks()` / `load_probes()`：读卡片、人工标记（读没读过）、复测结果
- `render_html()`：按阶段分组渲染成**单文件** `dashboard.html`（CSS 内联），卡片带状态徽标（active/已被取代/已剔除/存疑）

### 1.14 `probe.py`（247 行）— 延迟复测

SQLite（`~/.cognitrail/probe.db`）实现「3 天后再问你一遍」：
`schedule()`（按 delay_days 预约）→ `due_now()`（到期的题）→ `answer()`（记录回答+自评分）→ `stats()`。`_ask_round()` 是控制台版的问一轮交互。

### 1.15 `questions.py`（189 行）— 出题

`make_questions()`：卡片 → 3 个复测题（真模型 / 正则 fallback 双轨）；`ladder_for()`：追问链（是什么 → 为什么 → 换个场景还会吗）。

### 1.16 `workflow.py`（435 行）— 工作流总结

会话 → 固定三段的总结：**作者要求做 / AI 从哪到哪 / 名词解释**（只解释材料里真出现过的行话，`_detect_glossary()` 用术语表 `_GLOSSARY` 匹配）。`save()` 落 `<项目>/docs/workflows/`。

### 1.17 `watch.py`（323 行）— 守护进程

`class Watcher`：轮询三个工具的会话目录（`sources.list_all()`），用**文件签名**（mtime+size）判断变化，轮次结束（settle 6 秒不再变）就回调。`refresh_panel()` 顺手重渲染面板。状态存 `~/.cognitrail/watch-state.json`（每个文件读到第几行，增量解析用）。

### 1.18 `pipeline.py`（184 行）— 最短演示链路

`git diff → cardgen.generate → probe.schedule → dashboard.render → write_summary`，一条 main() 串到底。**给新人/演示用**，也是测试的固定靶子。

### 1.19 `gui.py`（3441 行）— Tk 图形界面（最大的文件）

**职责**：白色 Codex 风桌面应用。一个大类 `CogniTrailApp` 按区域组织方法：

| 区域 | 关键方法 | 说明 |
|---|---|---|
| 布局与 DPI | `_dpi_aware()` / `_dpi_factor()` / `_fit_window()` / `_build_layout()` | 200% 缩放适配；固定尺寸控件先 pack、expand 的最后 |
| 左栏 | `_load_sessions()` / `_render_session_list()` / `_fill_sessions()` | 会话搜索 + 单列列表 + 本地工作区入口 |
| 对话区 | `_fill_conversation()` / `_on_talk_selection()` / `_specified_turns()` / `_select_all_turns()` | 渲染对话、鼠标刷选定生成范围 |
| 生成动作 | `_on_generate()` / `_on_curate()` / `_on_workflow()` / `start_monitor()` / `_monitor_loop()` | 都在后台线程跑，`queue.Queue` 丢消息回主线程 `_drain()` 排空（Tk 不允许跨线程碰控件） |
| 卡片条 | `_reload_cards()` / `_fill_cards()` / `_on_card_click()` / `_show_card()` / `_apply_curated()` | 底部横向滚动卡片摘要条 → 右侧决策详情 |
| 写法切换 | `_toggle_tone_pop()` / `_on_tone_change()` | 顶栏按钮 + 弹出层（档位开关必须一眼可见的教训） |
| AI 对话 | `_on_ai_chat()` / `_chat_detect()` / `_on_ask_send()` | 走 cli_bridge 调本机 CLI，只问答不改文件 |
| 设置 | `_on_set_key()` / `_on_cli_settings()` / `_refresh_key_label()` | 设 Key 即存即测；CLI 路径/目录/自定义 |
| 新手引导 | `start_tour()` / `_tour_render()` / `_tour_step()` | 首次启动分步引导（标记文件 `~/.cognitrail/onboarded`） |
| 自检 | `_selftest()` | 产出结构化自检报告（版本/子系统/布局/入口可见性），`check_exe.py` 和 `gui_smoke.py` 都校验它 |

通用设施：`_make_readonly()`（只读文本仍可选中复制）、`_render_md()`（轻量 markdown 渲染到 Text）、`_attach_copy_menu()`（右键复制菜单）。

### 1.20 `tone.py`（174 行）— 两档写法

- `block(kind="card"|"prose")`：给提示词追加的措辞要求。**卡片档**必须带凭据字段要求；**prose 档**（curate/workflow）**不能**带 claim/control —— 否则教模型编字段
- `one_line()`：AI 对话用的单行口吻；`normalize()` / `current()` / `set_mode()`：档位读写
- 铁律：**只改措辞不改事实**，`file/quote/lines/claim` 两档都照原样给

### 1.21 `cli_bridge.py`（799 行）— 本机 AI 命令行桥

**职责**：在窗口里直接问本机已登录的 AI CLI（claude/codex/gemini/qwen/opencode/cursor-agent）。

- `detect()` / `probe()`：在约定目录搜可执行文件（`.cmd` 要 `[COMSPEC, "/c", exe, …]` 包一层才能被 CreateProcess 执行）
- `chat()`：非交互模式（`claude -p` / `codex exec`），**不传任何绕过权限的开关**
- `looks_like_error()`：CLI 未登录会把报错当正常输出且退出码为 0，必须靠文本特征认出来
- 用户自定义：`.env` 的 `CLI_PATHS` / `CLI_DIRS` / `CLI_CUSTOM`，调用时重读、不重启

### 1.22 `doctor.py`（332 行）— 环境自检

逐项检查 Python/git/三种会话源/Key/网络/目标仓库，`class Report` 汇总 `[OK]/[!!]/[~]` 三态。`check_network()` 真发一次最小请求试 Key。

---

## 2. tools/ —— 构建与运维脚本（14 个）

| 文件 | 作用 |
|---|---|
| `build_exe.py` | PyInstaller 打包：一次出两个 exe（窗口版 / 控制台版）。懒 import 的模块要登记进 `HIDDEN`。成品先写 `build/dist-<tag>/` 再拷进 `dist/`，被占用就把旧 exe 改名让路 |
| `check_exe.py` | **发版验证三连**：版本号与源码一致 / PE 子系统正确（窗口版=2 控制台=3）/ 窗口版自检报告合法。任一不过 rc=1 |
| `gui_smoke.py` | 真开窗口点交互的冒烟测试。失败必须 `return 1`（没有退出码 = 没检查） |
| `run_tests.py` | 跑 `tests/` 全部测试并汇总，`--with-gui` 加窗口冒烟 |
| `clean_cards.py` | 清理重复卡片（同证据+正文相似 ≥0.72；或同天同序号+同证据）。`--apply` 只移到回收目录写 MANIFEST，**不删** |
| `backfill_session.py` | 给早期卡片补 `session:` 字段的一次性迁移 |
| `build_plugin.py` | 把 `src/` 同步成 codex-plugin 自带引擎 |
| `install_codex_skill.py` | 把知迹装成 Codex 个人技能 |
| `make_icons.py` | 纯标准库生成插件图标 |
| `shot_gui.py` | 开真实窗口截图（海报/README 用） |
| `demo_tone.py` | 演示两档写法差异，产物在 out/tone-demo/ |
| `inspect_codex_session.py` | 看 Codex 会话文件里存了什么（调研工具） |
| `_probe_geom.py` / `_probe_lock.py` | 临时探针（查布局/查文件占用），可忽略 |

---

## 3. tests/ —— 测试（13 个文件，约 466 项检查）

| 文件 | 覆盖 |
|---|---|
| `test_audit.py` | 定档/造假扫描/审计意见。**两条哨兵**：只有 tier 的手写卡片必须判未审计；核对通过后依然算未审计 |
| `test_quota.py` / `test_quota_flow.py` | 配额表、画像升降档、红线（不看句长、有发现豁免不占额） |
| `test_session.py` | Codex 会话解析、轮次切分、增量读取 |
| `test_sources.py` | 三适配器统一接口 |
| `test_curate.py` | 演进链硬校验（步骤<2 不算演进；首尾 cards 相交整条丢） |
| `test_projectmap.py` | 主线构建与阶段归一 |
| `test_tone.py` | 两档措辞、prose 档禁止 claim |
| `test_cli_bridge.py` | exe 搜索、错误识别 |
| `test_cli.py` / `smoke_test.py` / `test_workflow.py` / `test_clean_cards.py` | CLI 链路 / 冒烟 / 工作流 / 清卡 |

跑法：`python tools/run_tests.py`。

---

## 4. samples/ 与 codex-plugin/

- `samples/make_demo_repo.py`：生成一个带 git 历史的演示仓库（V1→V2 两次提交靠**写入顺序**保证），配套 `demo-repo/README.md` 讲它讲了什么。跑 `python src/pipeline.py --repo samples/demo-repo` 全流程不花钱
- `codex-plugin/`：把知迹装成 Codex 原生插件（`plugins/cognitrail/`：SKILL.md + 自带一份 `scripts/src/` 引擎副本，由 `tools/build_plugin.py` 同步）

---

## 5. 数据落在哪（运行时产物）

| 位置 | 内容 |
|---|---|
| `~/.cognitrail/` | probe.db（复测题）、watch-state.json（监控进度）、level.json（画像）、.env 兜底 |
| `<你的项目>/docs/decisions/` | 决策卡片 md、dashboard.html、SUMMARY.md、CURATED.md、project-map.json |
| `<你的项目>/docs/workflows/` | 工作流总结 |
| `dist/` | 两个 exe（分发前删掉 `.env`！） |

---

## 6. 学习时的三条主线（建议各跟一遍）

1. **一张卡片的诞生**：`gui._on_generate` → `cardgen_session.generate_from_session` → `schema.Card`（闸门）→ `audit.audit_card`（定档+痕迹）→ `quota.select`（配额）→ `_write` 落盘 → `dashboard.render_html`。这条链串起了 80% 的代码。
2. **一次审计的视角**：拿一张已有卡片，跑 `python src/cognitrail.py audit`，对照 `audit.opinion()` 的输出逐字段回源码看它是怎么算出来的。
3. **一次复测的闭环**：卡片写盘 → `probe.schedule` → 三天后 `probe.due_now` → 回答 → `dashboard` 上显示复测结果。
