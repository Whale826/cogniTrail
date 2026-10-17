# 知迹 CogniTrail · P0 实施计划

> 计划日期：2026-10-13
> 对应手册：《知迹CogniTrail_后续开发操作手册》第 4–8 章（已归档至 `docs/design/`）
> P0 的定位：**它不是产品，而是一次可证伪的验证** —— 回答唯一重要的问题：
> 「这些卡片有没有让使用者被追问时多接住一层。」

---

## 0. 环境实测结论（2026-10-13 当天）

| 手册第 2 章检查项 | 实测结果 | 处理 |
|---|---|---|
| `git --version` | 2.53.0.windows.2 | ✅ 已装 |
| `node -v` | v22.22.2 | ✅ 已装（本机无 JS 模块需求，忽略） |
| `npm -v` | 随 Node 安装 | ✅ |
| `python --version` | 3.13.14 | ✅ 已装 |
| `pip --version` | 26.1.2 | ✅ |
| `code --version` | 未在 PATH 中找到 | ⚠️ P0 用不到，暂不处理 |
| DeepSeek API Key | 尚无 | ⏳ 先用离线 Mock，见 §4 |

**结论：手册阶段①（装备）已完成，无需重复。直接从阶段④⑤开始。**

---

## 1. 范围边界

### 1.1 P0 只做四件事（来自《项目综合与最终收窄》§4）

1. 读目标仓库 + 读它最近的 git diff
2. 从这次改动里找出 **3–5 个决策点**
3. 生成「预设问答卡」：决定 / 理由 / 替代方案 / 代价 + 4 角度追问链，**每条结论必须带 `文件:行号`**
4. 写进目标仓库的 `docs/decisions/`，并给出一页汇总

### 1.2 明确不做（已裁决，不再讨论）

| 砍掉的 | 理由 |
|---|---|
| VS Code 侧栏 / 项目地图 / 任务进度视图 | 需要主动打开 = 会被卸载；且 DeepWiki 等已占位 |
| 选项机制（让用户在过程内做选择） | 选择必须真实改变执行才不是装饰，而执行控制是全项目最难的一块 |
| 执行控制 / 暂停恢复 / 幂等 / 超时 | 同上，P0 不承诺可靠同步 |
| 多语言 / 多 IDE / 多 Agent 兼容 | 难度「很高」，与验证目标无关 |
| 「用户是否真的懂了」的点击类指标 | 点击行为恰是「机械点击替代理解」的产物，什么都不能证明 |

---

## 2. 交付物与验收标准

| 编号 | 交付物 | 文件 | 验收标准（做完的样子） |
|---|---|---|---|
| **F0** | 工程骨架 | 本目录结构 | `python src/cardgen.py --help` 能跑，不报缺模块 |
| **F1** | 决策卡片生成器 | `src/cardgen.py` | 跑一条命令，从一份 diff 里吐出 **≥3 张**卡片，每张的 `文件:行号` 都能被脚本定位到 |
| **F2** | 预设问答 + 追问链 | `src/questions.py` | 给一张卡片，输出「第一轮 3 问 + 4 个角度追问链」 |
| **F3** | 延迟理解探针 | `src/probe.py` | `data/probe.db` 有数据；3 天后 `probe.py` 能列出到期问题并逐条问你 |
| **F4** | 端到端流水线 | `src/pipeline.py` | 一条命令跑完 diff → 卡片 → 问题 → 排期 → 汇总页 |
| **F5** | 一下午实验 | `docs/experiments/` | **3 张卡片截图 + 一份问答记录 + 一个「接住层数」数字** |
| **F6** | 对外说明 | `README.md` | 手册 §8.1 那版 README，可以直接粘到 GitHub |

F0–F4 是代码，**F5 才是唯一能证明「它到底有没有用」的动作**（手册第七章）。

---

## 3. 目录结构

```
trace/                      ← 项目根（= 本次会话工作区）
├─ PLAN.md                  本计划
├─ README.md                对外说明（手册 §8.1）
├─ requirements.txt         openai / python-dotenv
├─ .env.example             Key 模板（真 .env 永不提交）
├─ .gitignore               挡住 .env / probe.db / __pycache__
├─ src/
│  ├─ config.py             统一读配置与路径（取代三个脚本各写一遍）
│  ├─ schema.py             决策卡片字段定义 + 证据闸门（对齐 AgDR）
│  ├─ llm.py                LLM 调用封装 + 离线 Mock 回退
│  ├─ cardgen.py            F1 决策卡片生成器
│  ├─ questions.py          F2 预设问答 + 追问链
│  ├─ probe.py              F3 延迟理解探针
│  └─ pipeline.py           F4 端到端
├─ docs/
│  ├─ decisions/            产出：决策卡片 *.md + SUMMARY.md
│  ├─ design/               前置材料（设计方案 / UML / 竞品调研 / 手册）
│  └─ experiments/          F5 实验记录
├─ data/                    probe.db
├─ samples/                 测试用最小仓库（别拿真实项目试错）
└─ tests/                   冒烟测试
```

---

## 4. 关键技术决策

| # | 决策点 | 选择 | 理由 | 状态 |
|---|---|---|---|---|
| D1 | 大模型 | DeepSeek（OpenAI 兼容接口） | 手册 §6.0 选定，国内直连、便宜 | ⏳ 待 Key |
| D2 | 没有 Key 时怎么办 | `COGNITRAIL_MOCK=1` 离线回退 | 不花钱先跑通「格式 + 闸门 + 落盘」；拿到 Key 改一行切换 | ✅ 已实现 |
| D3 | 卡片字段 | 直接采用 AgDR 字段名 | 手册 §5.2：不自创格式，答辩时一句话显得专业 | ✅ |
| D4 | 证据闸门 | **文件存在 + 行号在文件范围内** | 手册 §5.3 的实现只查文件存在；补上行号范围校验才真挡住「编造坐标」 | ✅ **比手册更严，见 §7 偏差说明** |
| D5 | 落盘格式 | Markdown + YAML front matter | 人可读、Git 原生、机器可解析（对照 `mbeacom/adrkit`） | ✅ |
| D6 | 状态存储 | SQLite（`data/probe.db`） | 手册 §8.4 选定 | ✅ |
| D7 | 卡片正文 | 附一段「证据摘要」（新增行摘录） | 卡片自包含，符合「每条结论都可以点开到具体行」 | ✅ |
| D8 | 是否 fork claude-mem | **不 fork**，只借架构思路 | 手册 §4.5 降级方案：站在前人肩膀上 ≠ 必须 fork 别人的代码 | ✅ |
| D9 | 终端美化 | 不用 `rich`，用纯文本输出 | 少一个依赖，规避 Windows 控制台中文编码坑 | ✅ **与手册不同，见 §7** |

---

## 5. 里程碑

| 里程碑 | 内容 | 产出 | 状态 |
|---|---|---|---|
| M0 | 工程骨架 + 配置 + Mock 层 | 目录就绪、`--help` 可跑 | ✅ 本次完成 |
| M1 | F1 卡片生成器 + 证据闸门 | `docs/decisions/` 出现卡片 | ✅ 本次完成 |
| M2 | F2 预设问答 + 追问链 | 打印 3 问 + 4 角度 | ✅ 本次完成 |
| M3 | F3 延迟探针 | `probe.db` 建立、可排期可追问 | ✅ 本次完成 |
| M4 | F4 端到端 pipeline | 一条命令跑完全链 | ✅ 本次完成 |
| M5 | **一下午验证实验** | 3 张卡 + 问答记录 + 接住层数 | ⏳ 需 API Key |
| M6 | README + 推到 GitHub | 一个可以发给老师的链接 | ⏳ |

**M5 的判据（手册 §7.4）**
- 接住 **0–1 层** → 卡片刻得不对，改提示词再来
- 接住 **2 层** → 最有价值的结果，直接留作答辩材料
- 接住 **3–4 层** → 换一个更生的项目再测

---

## 6. 风险与对策

| 风险 | 程度 | 对策 |
|---|---|---|
| 手上没有 API Key，实验做不了 | 高 | Mock 已能跑通全链；申请步骤见 §8，成本几毛钱 |
| Mock 卡片被误当成真实产出 | 中 | Mock 模式下卡片 `status` 字段写 `mock`，且终端打印醒目横幅 |
| 闸门太严，卡片被大量丢弃 | 中 | 丢弃时打印具体原因（文件不存在 / 行号越界），按提示调提示词 |
| 被问「这不就是提示词包吗」 | 高 | 对外说明第一屏讲清两个不可替代动作：**读真实工件出题** + **对照证据坐标判卷** |
| 「看过解释 ≠ 掌握」 | 高 | 本项目产出的是**问题**不是解释；唯一指标是「数天后能否自己改」 |

---

## 7. 与手册的两处有意偏差

1. **D4 证据闸门更严**：手册 §5.3 的 `evidence_ok` 只检查 `1 <= line_no <= total`；实现上追加了「结束行不得越界」与「起始行不得越界」两项判断，并返回**失败原因**，而不是静默丢弃。丢弃原因会打印到终端，便于调提示词。
2. **D9 不用 rich**：手册 §6.0 装了 `rich`。P0 的输出量很小，纯文本足够；少一个依赖，也避免 Windows 控制台的中文编码问题（`sys.stdout.reconfigure(encoding="utf-8")` 已处理）。

---

## 8. 拿到 API Key 后的三步（M5 起跑线）

1. 打开 <https://platform.deepseek.com/> → 注册登录 → 左侧「API keys」→「创建 API key」
2. 复制那串 `sk-` 开头的字符（**只显示一次**），填进 `.env` 的 `DEEPSEEK_API_KEY=`，并把 `COGNITRAIL_MOCK` 改为 `0`
3. 充值最小额度（跑完整个原型约几毛钱），然后在 `samples/demo-repo` 或你自己的项目上执行：

```bash
python src/pipeline.py --repo samples/demo-repo
```

`.env` 已被 `.gitignore` 挡住，不会进版本库。

---

## 9. 产品形态决策（2026-10-13 追加）

### 9.1 触发

用户提出三条新要求：不是 exe、要能嵌进 Codex 桌面端、别人下载即可用。

### 9.2 关键发现：Codex 桌面端有原生插件机制

在本机 `~/.codex/` 里实测确认：

| 位置 | 作用 |
|---|---|
| `~/.codex/config.toml` | 用 `[marketplaces.*]` + `[plugins."名@市场"]` 注册插件 |
| `<市场>/.agents/plugins/marketplace.json` | 市场清单，声明有哪些插件 |
| `<插件>/.codex-plugin/plugin.json` | 插件说明书（名称、图标、默认提示、能力） |
| `<插件>/skills/<名>/SKILL.md` | 技能正文 —— **和我们已经在写的东西是同一个格式** |
| `<插件>/skills/<名>/agents/openai.yaml` | 插件列表里的显示名与默认提示 |

另有一个重要发现：Codex 有**内置浏览器面板**（自带 browser 插件的技能文档里写明
「websites shown side by side inside Codex」）。这意味着第三方插件虽然没有私有的
侧栏 API，但**可以把自绘的 HTML 面板显示在 Codex 里**。

### 9.3 决策

| # | 决策点 | 选择 | 理由 |
|---|---|---|---|
| D10 | 产品形态 | **P0 引擎不变，外面套一层 Codex 插件** | 引擎（卡片/闸门/探针）已验证可用，不该重写；插件只负责「让它在 Codex 里出现」 |
| D11 | 面板怎么显示 | Codex 内置浏览器打开 `dashboard.html` | 没有第三方侧栏 API，这是能拿到的最接近「侧边栏」的形态 |
| D12 | 引擎语言 | 继续 Python | 不重写。插件自带一份拷贝，靠 `tools/build_plugin.py` 同步，真身仍在 `src/` |
| D13 | 「别人下载即可用」怎么实现 | 两条通道：技能（`install_codex_skill.py`）+ 插件（`codex plugin marketplace add`） | 见 §9.6 实测。原先以为只有 config.toml 手写一条路，实测发现官方 CLI 命令更省事，且技能通道根本不碰配置 |
| D14 | 依赖 | **砍到零第三方依赖** | 真模型调用从 `openai` 包改成标准库 `urllib`；`.env` 已有内置解析器。上手步骤从 3 步压到 0 步 |
| D15 | Key 的必要性 | **插件形态下不需要 Key** | 在 Codex 里跑时，分析 diff 的模型就是 Codex 自己。Key 只在独立 CLI 模式下有意义 |

### 9.4 新增目录

```
codex-plugin/                        插件（可以整个拷给别人）
├─ .agents/plugins/marketplace.json
├─ README.md                         安装说明
└─ plugins/cognitrail/
   ├─ .codex-plugin/plugin.json
   ├─ skills/cognitrail/SKILL.md     ★ 技能正文
   ├─ skills/cognitrail/agents/openai.yaml
   ├─ scripts/cognitrail.py          入口：gate / panel / probe / cards
   ├─ scripts/src/                   引擎拷贝（由 tools/build_plugin.py 同步）
   └─ assets/                        图标（tools/make_icons.py 生成，零依赖画 PNG）
tools/
├─ build_plugin.py                   同步 src/ → 插件
└─ make_icons.py                     生成插件图标
src/
├─ doctor.py                         环境自检（新增）
└─ dashboard.py                      决策面板渲染（新增）
```

### 9.5 安装实测结论（2026-10-13 当晚，本机真机执行）

**结论：Codex 有两条互不相同的安装通道，原先我们只知道一条，而且理解偏了。**

| | 通道 A · 技能 | 通道 B · 插件 |
|---|---|---|
| 生效条件 | `<用户目录>/.codex/skills/<名>/SKILL.md` 存在 | 注册市场 **+** 在 Codex 里安装插件 |
| 装完什么样 | 进入 Codex「可用技能」列表，自然语言或 `/` 触发 | 插件列表一条：图标、显示名、默认提示按钮 |
| 安装方式 | `python tools/install_codex_skill.py` | `codex plugin marketplace add "<绝对路径>"` |
| 当场可验证 | ✅ 可（见下） | ⚠️ 需在图形界面确认 |

实测过程与证据：

1. `~/.codex/config.toml.bak-20260903-223908`（Codex 自己写的备份）里确实存在
   `[marketplaces.openai-bundled]` + `[plugins."browser@openai-bundled"] enabled = true`
   → 证明 config.toml 手写那条路是真的，不是臆测。
2. 但**光注册市场 ≠ 装好插件**：注册后 `~/.codex/plugins/cache/` 下没有我们的目录，
   `codex debug prompt-input` 里也搜不到 `cognitrail`。
3. 走技能通道后，`codex debug prompt-input` 立刻出现：
   `- cognitrail: (file: r0/cognitrail/SKILL.md)`（`r0` = `C:/Users/你的用户名/.codex/skills`）
   → **这是模型真正能看到的内容**，比任何界面截图都硬。
4. 装到 `~/.codex/skills/cognitrail/scripts/` 的引擎真跑通了：
   真坐标 `OK`（退出码 0）、越界行号 `DROP … 起始行 999 超出文件总行数 6`（退出码 1）。

修正的两处认知：

- ❌ 原来的 README 说「config.toml 加 4 行」就完事 —— 实际还差「在 Codex 里装插件」这一步，会让人以为装完就用不了。
- ❌ 我的第一版 `marketplace.json` 把 `policy.authentication` 写成 `NONE`，Codex 直接拒收：
  `unknown variant 'NONE', expected 'ON_INSTALL' or 'ON_USE'`。已改成 `ON_INSTALL`，
  `installation` 改成 `INSTALLED_BY_DEFAULT`。

新增一个必须记住的坑：

> `~/.codex/config.toml` 曾被 **CC Switch** 整个重写，把 `[marketplaces.*]` / `[plugins.*]` 段冲掉，
> 插件通道会**静默失效**，而技能通道不受影响。
> → 本机以通道 A 为主。核查命令：`grep -c "marketplaces.cognitrail-local" ~/.codex/config.toml`

### 9.6 仍未决定

- 是否把引擎从 Python 移植到 Node（Codex 插件原生是 Node；但会牺牲已验证的代码）。
  当前判断：**先不移植**，零依赖的 Python 现在能跑。

---

## 10. 形态变更：改成 exe + 读 Codex 会话（2026-10-13 晚，本机实测后追加）

### 10.1 触发

三条用户约束把方案逼到了新形态：

1. 「我要的是**桌面端**，不是 CLI」——之前只确认了「桌面端会写会话文件」，没验证过它写了什么。
2. 「codex 对**非会员**不能用插件」——用户查证属实。本机 `codex login status` 返回
   `Logged in using an API key`，走的是 API Key 通道而非 ChatGPT 会员，**插件市场用不了**。
3. 「**搞成一个 exe**，直接获取正在回复的 codex 内容」——不要安装流程，双击就能用。

结论：**通道 A（技能）和通道 B（插件）都作废**，因为两者都要求用户进入 Codex 的插件/技能体系。
改成读 Codex 自己写在磁盘上的会话文件——**不依赖插件、不依赖会员、不依赖授权**。

### 10.2 实测证据（这是本节最重要的一段）

会话文件位置：`~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl`，一行一个 JSON。

```
$ python tools/inspect_codex_session.py
会话 id : 01a12057-3432-7a22-80f3-5aa538857a46
originator: Codex Desktop        ← 桌面端确实写这里，不是 CLI 专属
source   : vscode
cwd      : C:\Users\你的用户名\Desktop\某个项目
```

对决策留痕有价值的三类记录：

| 记录 | 内容 | 用途 |
|---|---|---|
| `event_msg/agent_message` | 助手可见回复**原文** | 它自己解释「为什么这么做」 |
| `response_item/custom_tool_call` (`apply_patch`) | 实际增删行 | 钉住「改了什么」 |
| `event_msg/patch_apply_end` | 落盘确认 + 真实文件名 | 闸门交叉验证 |
| `event_msg/user_message` | 用户原始请求 | 每张卡的「任务背景」 |

**两个额外收获：**

- 会话里 `cwd` 是**论文项目**（`Trae创新竞赛论文`），没有 git。也就是说数据源从
  `git diff` 换成会话之后，覆盖面从「代码仓库」扩到了「任何 Codex 会话」——
  写论文、查资料、做分析这一类没有 diff 的工作，现在同样能留痕。
- 「正在回复」不用等 commit：轮次一结束（`task_complete`）内容就已落盘。

**修正一处此前的错误推断**：我一度以为「10/9 之后没有会话 = 桌面端不写会话文件」。
实际是用户 10/9 之后没用过 Codex。`originator: Codex Desktop` 是直接反证。

### 10.3 闸门重写（关键改动）

原闸门要求卡片给出 `文件:行号`。在会话场景里行号不来自 diff——`apply_patch` 的
`@@ 上下文 @@` 只有上下文、没有绝对行号。照搬旧闸门，模型只能编行号，而编出来的
会被全部丢弃，等于什么都没留下。

改成让模型给 **证据原文（quote）**，由我们自己去文件里搜：

```
quote 在文件里搜得到  → 用真实行号替换，卡片成立
quote 搜不到          → 丢弃，不进面板
```

同时新增 **`anchor=session`**：有些决策只存在于对话里（「我决定不用方案 A」），
根本没落到文件上。这类锚到**会话 jsonl 的真实行号**——依然是可验证的坐标，
比直接丢掉强。

于是闸门从「一种证据」变成「两种证据」，但**「坐标必须真实存在」这条底线没有松**。

### 10.4 新增文件

```
src/session_source.py      Codex 会话解析器（零依赖，容错：写到一半的行、字段缺失都能跳过）
src/cardgen_session.py     会话 → 卡片；含 quote 型闸门与 session 锚点
src/watch.py               实时守护：盯着会话文件，一轮结束就落卡片并刷新面板
src/cognitrail.py          统一入口（sessions / run / watch / panel / probe / doctor）
tools/build_exe.py         PyInstaller 单文件打包
tools/inspect_codex_session.py  只读的会话结构探针（本次调研用的就是这个）
assets/cognitrail.ico      多尺寸图标（手写 ICO，仍然零依赖）
```

`src/config.py` 增加 `FROZEN` 分支：exe 形态下 `ROOT` = exe 所在目录、
`DB_PATH` 与 `STATE_DIR` 落到 `~/.cognitrail/`。**否则 PyInstaller 会把
`probe.db` 写进临时解包目录，进程一退就没了。**

### 10.5 验证结果

| 项目 | 结果 |
|---|---|
| `tests/test_cli.py` | **19 项通过**（钉住「双击不闪退」） |
| `tests/test_session.py` | **29 项通过**（含「编造原文必须被拒绝」） |
| `tests/smoke_test.py`（旧链路回归） | **26 项通过** |
| 离线模式（无 Key） | 5 张草稿卡，`status: draft` |
| 在线模式（真模型） | 5 张真卡，`why` 是「Codex 自带代理把请求转发到错误端口导致 OpenAlex 返回代理隧道错误」这类具体表述 |
| 幂等性 | 同一个会话连跑两次，第二次 0 新增（进度存在 `~/.cognitrail/watch-state.json`） |
| exe | `dist/cognitrail.exe`，**8.9 MB 单文件**，`sessions` / `run` / `watch` / `doctor` 全部实测通过 |

真模型这次挡下了 1 条：模型把会话里的原话**改写**过，quote 在 jsonl 里搜不到 → 丢弃。
这正是闸门该做的事（宁可少一张卡，不留一张编的）。

### 10.6 放弃的两条路（记录在案，避免以后重新踩）

- **通道 A（`~/.codex/skills/`）**：能用，且当场可验证；但它只是个技能，
  打开 Codex 不会「出现」任何东西——不满足「打开就能看见」的诉求。
- **通道 B（插件 + 市场）**：本机是 API Key 账号，插件通道不可用。
  即便可用，CLI 也只负责注册市场，装插件另外要在图形界面里点一次。


## 11. 双击闪退（2026-10-13 晚，用户实报后修）

### 11.1 现象与根因

用户报：**双击 `cognitrail.exe`，出现一个黑框然后闪退。**

复现后确认**不是崩溃**（退出码 0）：双击 == 无参数运行，而当时的实现无参数只
`parser.print_help()` 然后 `return 0`。Windows 一看进程结束就关掉控制台窗口，
于是「黑框一闪」。

顺带用 PE 头确认了 exe 是 **Subsystem=3（CONSOLE）**——所以双击会开控制台，
这也解释了为什么能看到黑框。

### 11.2 修法

| 改动 | 理由 |
|---|---|
| 无参数 → **进交互菜单**，回车 = 开始守护 | 双击必须「停下来等人」，不能跑完就走 |
| 菜单跑完一项 → 回到菜单，并**等一次回车** | 一次性输出不会被窗口关闭吞掉 |
| 无参数但**非交互**（管道 / 计划任务）→ 仍打印帮助 | 不破坏脚本化调用，也不阻塞 |
| 任何异常 → 打印 traceback 后再等一次回车 | 真出错时用户能看见原因，而不是又「闪退」 |
| 新增 `--menu` | 管道里也能测菜单（`_can_interact()` 需要 tty） |

### 11.3 顺手修掉的两个真 bug

1. **`使用说明.txt` 第一行是 `=知迹 CogniTrail` 重复 40 次。**
   原因：Python 相邻字符串字面量的拼接**优先于** `*`，
   `"知迹 CogniTrail\n" "=" * 40` 被解析成 `("知迹 CogniTrail\n=") * 40`。
   这个 bug 从第一版就在，只是从没人打开过那个文件。已全仓 grep 确认仅此一处。

2. **`samples/make_demo_repo.py` 的 `build()` 整目录 `rmtree`**，
   在 Windows 上撞上系统删除保护，异常把调用它的冒烟测试一起带崩
   （`[safe-delete] … Some operations were aborted`）。
   改成**就地覆盖**：目录复用、同名文件覆盖、`.git` 沿用、旧卡片逐个删。
   `HEAD~1..HEAD` 仍是「V1 → V2」，与从零重建等价。

### 11.4 验证

| 项目 | 结果 |
|---|---|
| `tests/test_cli.py`（新增） | **19 项通过** |
| `tests/test_session.py` | 29 项通过 |
| `tests/smoke_test.py` | 26 项通过 |
| exe 无参数 + 管道 | 打印帮助 + 提示「双击会进菜单」，退出码 0 |
| exe `--menu` 喂入 `3` 再 `q` | 显示菜单 → 列出 80 份会话 → 回到菜单 → 正常退出 |
| `使用说明.txt` | 38 行，横线正常 |
| PE Subsystem | 3（CONSOLE），双击确实会开控制台 |

### 11.5 未决

- 菜单里回车 = 守护（watch）。若用户更希望**双击直接进守护、不出现菜单**，
  改 `_MENU` 默认项即可，一行的事。
- `dist/.env` 里已有用户的真 Key（用户自己按 `使用说明.txt` 配的）。
  `dist/` 是分发目录，分享前必须删掉 `.env` —— README 和使用说明都已加了警示。



