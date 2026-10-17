# 知迹 CogniTrail

在 AI 帮你干完活之后，帮你弄清楚「到底做了什么、为什么」。

## 它解决什么问题

用 AI 做完一个任务，任务完成了，但你没学会。
三个月后有人问你细节，你答不上来。这叫认知负债。

## 它做什么

- 同时盯着 **Codex / WorkBuddy / Claude** 三种工具的会话，一轮结束就留下痕迹
- 从会话里提取**决策卡片**（做了什么 / 为什么 / 替代方案 / 代价）
- 每张卡片都挂到**项目主线的某一步**（阶段 + 这一步卡在什么问题），不是一堆孤立碎片
- 每张卡片必须钉住 `文件:行号`，定位不到的自动丢弃
- **像审计一样核对**：写代码的是 AI，读卡片的人相当于审计师 ——
  所以每条决策点都要给一条**能被脚本回文件核对的事实断言**，
  关键度（关键决策 / 一般 / 记录）由脚本按规则定档，模型只能往上抬、不能往下压
- **看得出有没有「参假」**：原有校验被拿掉、异常被吞掉、凭据写死、
  用 `--no-verify` 绕过流程、断言与代码对不上 —— 这些会被标出来并单列，
  同时给一句**审计意见**（无保留 / 带解释性说明 / 保留）
- 卡片之间**前后打架的自动融合**，并写明早期做法**错在哪**
- **两种写法由你选**：小白模式（大白话：这是什么问题、做了什么、对你有什么影响）
  或专家模式（技术口吻：哪个文件、哪一行的什么选择）。
  换的只是措辞 —— `file` / `claim` 这些凭据字段在哪一档都照原样给，
  卡片上会记下它属于哪一档。窗口顶部有一条独立的「写法」栏（两个选项的文字自己
  就写着区别），管的是**卡片、整理报告、工作流总结、以及问 AI 时的答话口吻**
- 窗口里的文字**都能选中复制**（对话、卡片详情、日志、报告），右键有「复制选中 / 全选」
- 本机装了 AI 命令行的话，可以在窗口里**直接和 AI 对话**
  （用你已登录的账号，不必再填 Key；这个通道只问答，不会去改你的文件）。
  自动认得 **Claude Code / Codex / Gemini / Qwen Code / OpenCode / Cursor**；
  认不出来的（自建目录、Scoop、公司内网盘、绿色版…）可以在「CLI 设置…」里
  直接填路径 / 加搜索目录 / 定义一个自定义命令，保存即生效、不用重启
- 生成**预设问题 + 追问链**，不是给你解释，是来问你
- **3 天后自动复测** —— 这是全网还没有人做过的一件事

## 它不做什么

不替你写代码，不教你写代码，不打断你。
它审的不是「代码本身对不对」——那是代码质量 / 安全扫描器的事；
它审的是**「这次改动是怎么被决定下来的」**，依据是代码里的事实，而不是 AI 的说法。
它只在你做完之后工作。不装插件、不需要会员、不需要给任何工具授权。

## 快速开始

### 方式一 · 单文件 exe（推荐）

构建一次会出**两个** exe：

| 文件 | 用途 |
|---|---|
| `dist/知迹CogniTrail.exe` | **窗口版**（默认用这个）。双击就是一个界面，没有黑框 |
| `dist/cognitrail.exe` | 控制台版。跑命令、写脚本用 |

窗口版长这样（0.11.0 起为白色主题，左栏操作 + 右侧三块）：

```
┌────────────────────────────────────────────────────────────────────┐
│ 知迹 CogniTrail ☑Codex ☑WorkBuddy ☑Claude 项目[__]刷新 [设置 Key] │
│ 写法   ○专家模式 —— 技术口吻   ○小白模式 —— 大白话                 │
├──────────────┬─────────────────────────────────────────────────────┤
│ [开始监控]   │ 对话                                                │
│ ☐自动生成卡片│  生成范围：第[14]轮 ~ 第[14]轮 [全选][生成这段的决策]│
│ 记录         │  › 用户   把分类阈值调低一点，召回太差了              │
│  生成卡片    │  ‹ AI     这里我把阈值从 0.5 降到 0.35……              │
│  整理决策    ├─────────────────────────────────────────────────────┤
│  总结工作流  │ 决策详情                                             │
│  和 AI 对话  │  为什么 / 还考虑过什么 / 代价与风险                   │
│ 查看         │  证据  src/config.py:214            [在文件里打开]   │
│  日志        ├─────────────────────────────────────────────────────┤
│  自检 …      │ 决策卡片摘要   项目[__________] [读卡片][打开面板]   │
│ 会话         │  状态      阶段  来源  证据坐标     决策              │
│  时间 来源…  │  active   S2    WB    x.py:3:7      把阈值降到 0.35  │
│  10-16 WB …  │  已被#4取代 …                                        │
├──────────────┴─────────────────────────────────────────────────────┤
│ 状态栏：在线 · 模型 | 监控中 · 手动生成 | 写法 专家模式 | 卡片 N 张 │
│ （日志在独立窗口：点左栏「日志」按钮才出现，清空日志也在那里）       │
└────────────────────────────────────────────────────────────────────┘
```

**记录决策是「先选段、再生成」**：打开窗口会开始盯着会话，但**默认不会**
自己往项目里写卡片（底部那个「自动生成卡片」勾选框默认关）。
点一个会话 → 在对话上方定好「生成范围」（也可以直接用鼠标在对话里刷选，
范围自动跟上）→ 点「生成卡片」，只记录这一段里的取舍。
这样卡片是**有意识记下来的**，不会一开监控就灌一堆没人看的。

更详细的三件事：

- **选段生成** —— 默认只选最新那一轮。要整段就在范围框里改，或点「全选」。
  生成前会自动读一遍**项目主线**（阶段清单，没有就现建一份），
  卡片因此能挂到「这个项目走到第几步」上，而不是只记「改了哪一行」。
- **整理决策**（底部）—— 卡片多了点它：让模型通读该项目全部卡片，
  排成**有逻辑的顺序**（先定什么、后定什么，不是按时间流水）、
  **融合**说同一件事和互相冲突的卡片、并写出**演进链**：
  早期用了什么做法、**为什么被否**（错在哪 / 环境变了 / 代价太大）、最终改成什么。
  产物是 `<项目>/docs/decisions/CURATED.md`，同时生成 `curated.json`，
  窗口里的卡片列表会按整理结果重排，被取代的标「已被#N取代」、被剔除的标「已剔除」。
- **项目主线**（`cognitrail projectmap`）—— 从 README / 计划文件 / 已有卡片 /
  git 提交里提炼「项目目标 + 阶段清单（每阶段的典型卡点）」，
  落盘 `docs/decisions/project-map.json`。**它只建一次，之后一直用它**，
  阶段名才稳定；想改直接编辑那个 json，工具不会覆盖你的修改（`--refresh` 才会重算，且先备份）。
- **审计**（`cognitrail audit`）—— 一句「这批卡片我到底能不能照单全收」。
  它按规则算出**关键决策是哪几条**（动到地基、动到高风险面的，会被定成关键，
  模型想往下压也压不动）、**有没有参假的痕迹**（拿掉校验 / 吞异常 / 写死凭据 /
  绕过流程 / 断言与代码对不上），以及**证据够不够给无保留**。
  结论全部由脚本算，模型不参与；命中可疑 ≠ 认定为造假，是**提醒你亲自看一眼**。
- **设置 Key**（顶部）—— 点开一个小窗，把 API Key 粘进去，点「保存并测试」
  当场生效，不用改文件、不用重启。它写的是本机的 `.env`（优先 exe 旁边，
  写不动就退到 `~/.cognitrail/.env`），Key 不会发到任何地方。
- **总结工作流**（底部）—— 把选中的那一次对话整理成一份总结，
  固定三段：**作者要求做**（1、2、3 点）／**AI 从哪 → 到哪**（起点、终点、
  详细经过）／**名词解释**（只解释材料里真的出现过的行话）。
  结果会弹一个窗口给你读，同时落到 `<你的项目>/docs/workflows/`。

控制台版双击会先弹一个黑框（这是控制台程序的本性），里面是菜单：

```
知迹 CogniTrail · 从 Codex / WorkBuddy / Claude 的会话里自动留痕决策
  [1] 打开窗口（图形界面：看对话、看卡片、选一段生成、点着用）（直接回车选这个）
  [2] 开始守护 —— 开着它，然后用 Codex / WorkBuddy / Claude
  [3] 处理最新一份会话，立刻出决策卡片
  [4] 看看本机有哪些会话（三个工具一起）
  [5] 自检环境（Key / 会话 / 依赖）
  [6] 重新生成决策面板
  [7] 把最新一份会话总结成「工作流」
  [8] 整理决策卡片（排序 / 筛选 / 融合冲突、说明谁推翻了谁）
  [9] 建立 / 查看项目主线（阶段清单，卡片挂在它上面）
  [10] 审计卡片：关键决策是哪几条、有没有参假的痕迹、能不能给无保留
  [11] 看看本机的 Claude / Codex 命令行能不能用（能用就直接问它）
  [q] 退出
```

命令行等价写法：

```bash
cognitrail.exe gui                        # 打开窗口
cognitrail.exe watch                      # 实时守护
cognitrail.exe watch --source claude      # 只盯 Claude
cognitrail.exe sessions                   # 三个工具的会话一起列
cognitrail.exe run --session latest       # 处理最新一份会话
cognitrail.exe curate                     # 把已有卡片整理成有序清单
cognitrail.exe audit                      # 审计：关键决策 / 可疑痕迹 / 审计意见
cognitrail.exe cli-chat --check            # 看看本机 Claude / Codex 命令行能不能用
cognitrail.exe cli-chat "这个项目怎么跑的"   # 能用就直接问它一句
cognitrail.exe projectmap --show          # 看项目的阶段清单（没有就现建）
cognitrail.exe projectmap --refresh --write-md   # 重算阶段清单，另写一份给人看的
cognitrail.exe workflow --show            # 总结最新一份会话的工作流
cognitrail.exe doctor                     # 检查环境是否就绪
cognitrail.exe panel --open               # 打开决策面板
cognitrail.exe probe --list               # 看延迟复测题
```

自己构建：`python tools/build_exe.py`

> 窗口版必须用一个**带 tkinter** 的解释器打包（Windows 官方 Python 自带）。
> 脚本会自动找 `~/.workbuddy/binaries/python/envs/tk`，找不到会给出建 venv 的命令。

> ⚠️ `.env` 里有你的 API Key。`dist/` 是用来分发的，分享它之前先把 `dist/.env` 删掉
> （构建脚本会在检测到时大声提醒）。

### 方式二 · 源码

```bash
# 1. 直接开窗口（零第三方依赖，不装任何包也能跑）
python src/gui.py

# 2. 或者用命令行
python src/cognitrail.py watch

# 3. 拿演示仓库先跑通，不花钱、不需要 API Key
python samples/make_demo_repo.py
python src/pipeline.py --repo samples/demo-repo
```

> 源码跑窗口需要解释器带 tkinter。Windows 官方 Python 有；
> 精简版 / 嵌入式发行版通常没有。

### 接真实模型

复制 `.env.example` 为 `.env`，填入你的 DeepSeek Key：

```
DEEPSEEK_API_KEY=sk-你的密钥
COGNITRAIL_MOCK=0
```

> 没有 Key 时程序不会报错，会自动回退到**离线启发式**模式（卡片 `status` 标为 `draft`），
> 用来先验证「格式 + 闸门 + 落盘」这条链路。exe 形态下 `.env` 放在 exe 旁边，
> 或放在 `~/.cognitrail/.env`。
>
> 最省事的配法：打开窗口 → 点顶部 **「设置 Key」** → 粘进去 → 点「保存并测试」。
> 它会写进上面那个 `.env`，并**让当前这个进程立刻生效** —— 不用改文件、不用重启。

## 数据从哪来

**直接读三个工具自己写的会话文件**，一行一个 JSON，明文：

| 工具 | 会话在哪 | 轮次结束的信号 |
|---|---|---|
| Codex | `~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl` | `event_msg/task_complete` |
| WorkBuddy | `~/.workbuddy/projects/<项目>/<会话>.jsonl` | 下一条用户消息 / 文件安静 6 秒 |
| Claude | `~/.claude/projects/<项目>/<会话>.jsonl` | `system/turn_duration` |

- 不装插件、不需要会员、不需要授权 —— 那是些明文文件，工具自己写的
- 不依赖 git —— 所以写论文、做调研、写文档这类**没有 diff** 的工作同样能留痕
- 不用等 commit —— 轮次一结束内容就已落盘
- 只读，不改它们一个字；不联网

三种工具的 schema 不一样，所以 `src/sources/` 下是三个各自独立的适配器，
共用同一套 `Session / Turn / Patch` 模型：

```
src/sources/__init__.py    注册表 + 统一接口 + peek 缓存
src/sources/codex.py       包住原本的 session_source.py
src/sources/workbuddy.py   message / reasoning / function_call / ai-title
src/sources/claude.py      user / assistant / system(turn_duration)
src/sources/_tools.py      Write/Edit/MultiEdit/Bash → Patch 的翻译层（两个源共用）
```

**加第四个工具时只要再写一个 60 行的文件**，watch、窗口、卡片生成都不用动。

## 证据闸门

**这是本项目的生命线**，也是唯一能防止「AI 编造解释」的工程手段。

会话里的改动不带绝对行号，所以闸门让模型给**证据原文（quote）**，
由脚本自己去真实文件里搜：

```
quote 搜得到 → 用真实行号替换，卡片成立
quote 搜不到 → 丢弃，不进面板
```

另有一种锚点 `anchor=session`：决策只存在于对话里、没落到文件上时，
锚到**会话 jsonl 的真实行号**——依然是可验证的坐标。

两种锚点，同一底线：**坐标必须真实存在。**

## 和 AI 对话（接本机命令行）

窗口底部的「和 AI 对话」用的是你**本机已经登录过**的 CLI —— 不用在这里填任何 Key。
它只走**非交互模式**（`claude -p` / `codex exec` / `gemini -p` …），一次提问一次回答，
不去驱动那个给人用的交互界面（靠灌按键模仿 TUI 很脆，CLI 一升级就坏）。

**不替用户绕过权限**：不传 `--dangerously-skip-permissions` / `--force` / `--yolo`
之类的开关。这个按钮的定位是「问问题」，不是「让另一个 AI 去改你的文件」
（`tests/test_cli_bridge.py` 里有一条专门守着这件事）。

会自动找这些命令：

| key | 命令 | 提问怎么递 |
|---|---|---|
| `claude` | `claude` / `claude-code` | 走标准输入（`-p`） |
| `codex` | `codex` | 参数（`exec <prompt>`） |
| `gemini` | `gemini` | 参数（`-p <prompt>`） |
| `qwen` | `qwen` / `qwen-code` | 参数（`-p <prompt>`） |
| `opencode` | `opencode` | 参数（`-p <prompt>`） |
| `cursor-agent` | `cursor-agent` | 参数（`-p <prompt>`，不加 `--force`） |
| `custom` | 你自己填的 | 你选（参数 / 标准输入） |

**「别人的电脑上找不到」才是这个功能真正的难点** —— 同一个 CLI，有人装在 npm 全局目录，
有人装 pnpm / Volta / Bun，有人用 Scoop / Chocolatey，有人放公司内网盘或自建 bin 目录，
而 GUI 从桌面图标启动时 PATH 常常只有系统默认那一份。所以做了三层，且**每次调用都重读配置**：

1. 手动指定的路径：`.env` 的 `COGNITRAIL_CLI_PATHS=claude=C:\...\claude.cmd;codex=...`
2. 额外搜索目录：`COGNITRAIL_CLI_DIRS=C:\tools\bin;D:\npm`（`;` 或 `,` 分隔）
3. 内置的常见位置清单 + PATH + 当前项目里的 `node_modules/.bin`

界面上对应「CLI 设置…」：每个命令一行（路径输入 + 浏览… + 测试），
外加「额外搜索目录」和「自定义命令」三格。保存写进本机 `.env`，**不用重启** ——
探测结果里会标明这个路径是「手动指定」还是「自动找到」。
找不到时不会只说一句找不到：它会说清找过哪些目录，并给出三条可操作的办法。

命令行上也能用：

```bash
cognitrail cli-chat --check                     # 看看本机装了哪些、能不能用
cognitrail cli-chat --provider gemini "这项目怎么跑起来"   # 直接问一句
```

## 模块

| 模块 | 作用 |
|---|---|
| `src/sources/` | 多源适配层：Codex / WorkBuddy / Claude 收在同一套接口下 |
| `src/session_source.py` | Codex 会话解析器（零依赖、容错），也是多源共用的数据模型 |
| `src/projectmap.py` | 项目主线：项目目标 + 阶段清单（每阶段的典型卡点），卡片挂靠它 |
| `src/tone.py` | 写法两档（专家 / 小白）× 两种用途（卡片 / 报告）的语言基调 —— 所有提示词共用这一份 |
| `src/audit.py` | 审计引擎：重要性定档 / 反舞弊痕迹 / 认定核对 / 审计意见（纯脚本，零模型依赖） |
| `src/cli_bridge.py` | 探测并调用本机已装的 AI 命令行（支持手动指定路径 / 额外目录 / 自定义命令；只问答，不带绕过权限的开关） |
| `src/cardgen_session.py` | 会话 → 决策卡片（含 quote 型闸门；生成时就挂阶段、记卡点、标出推翻了谁） |
| `src/workflow.py` | 会话 → 工作流总结（作者要求做 / 从哪到哪 / 名词解释；含离线版） |
| `src/curate.py` | 决策卡片 → 有序清单（排序 / 筛选 / 融合冲突 / 演进链：早期做法为什么被否） |
| `src/watch.py` | 实时守护：多源一起盯，一轮结束就落卡片并刷新面板 |
| `src/gui.py` | 图形窗口（Tkinter，零第三方依赖）：对话 / 选段生成 / 卡片 / 整理决策 / 设置 Key / 工作流总结 / 和 AI 对话；所有文字可选中复制 |
| `src/cognitrail.py` | 统一入口（命令行 / 控制台 exe 的主程序） |
| `src/cardgen.py` | 旧链路：读 git diff → 卡片 |
| `src/questions.py` | 卡片 → 预设问题 + 4 角度追问链 |
| `src/probe.py` | 问题排期到 SQLite，数天后自动来问 |
| `src/dashboard.py` | 决策面板（单文件 HTML，双击即开；按阶段筛选、标出已被取代的卡片） |

## 卡片格式

采用 AgDR（Agent Decision Record）字段规范，落盘在 `docs/decisions/` 下。
比 AgDR 多几个字段：`source` 记这张卡是从哪个工具的会话里抽出来的；
`project` / `stage` / `problem` 是**项目坐标**（属于哪个项目、主线的哪一步、当时卡在哪）；
`supersedes` / `supersedes_why` 记「它推翻了哪张卡、为什么推翻」；
`tier` / `claim` / `control` / `signals` / `verdict` / `tier_why` 是**审计字段**（由脚本算，模型不参与）；
`tone` 记这张卡是按哪种写法写的（见「卡片写法」）：

```markdown
---
id: D-20261014-173936
source: workbuddy
project: 知迹 CogniTrail
stage: "S2 打通模型通道"
problem: "官方地址直连返回 401，整条 pipeline 跑不通"
file: .env
lines: 7
decision: 绕过 Codex 自带代理，改用本机 Clash 香港节点直连
why: Codex 自带代理把请求转发到错误端口导致 OpenAlex 返回代理隧道错误；香港节点只改变出口网络、不改变数据库权限
alternatives: 继续走自带代理（被否，返回隧道错误）；直接抓出版社 PDF（被否，403/Cloudflare）
risk: 依赖本机 Clash 配置，环境变化即失效
tone: pro
supersedes: 2026-10-13-02-把_BASE_URL_默认写成官方.md
supersedes_why: 早期那版把探测到的本地代理端口写成默认值，别人下载后连不上，不适合作为分发默认值
status: active
created: 2026-10-14
---
```

阶段名（`stage`）必须来自 `<项目>/docs/decisions/project-map.json` ——
那是这个项目的**主线**：项目目标 + 几大阶段 + 每阶段的典型卡点。
这样几十张卡片引用的是同一套坐标，才拼得出一张项目地图；
每个阶段各写一个说法，就又变回一堆碎片了。

## 写法（两档：专家 / 小白）

同一件事，可以说成两种语气。这个选择**只改措辞，不改事实**：

| | 小白模式（`plain`） | 专家模式（`pro`） |
|---|---|---|
| 读者 | 不一定懂编程的人：产品、运营、写作者 | 有经验的工程师 |
| 「做了什么」 | 网络不好时不再反复等，直接给一个空结果 | 在 `load_config()` 里把 timeout 的默认值由 30 改成 60 |
| 长度 | 讲清「原来怎样、现在怎样、对我有什么影响」 | 一两句说完，信息密度优先 |

**管到哪儿：** 决策卡片、整理报告（`CURATED.md`）、工作流总结，以及「和 AI 对话」
时让 CLI 用什么口吻答。两档各有两套收尾注（`src/tone.py` 的 `kind=`）：

- `kind="card"` —— 卡片：还要点名 `file` / `quote`（diff 版是 `lines`）/ `claim` /
  `control` / `stage` 这些**审计凭据**，哪一档都照原样给足。
- `kind="prose"` —— 报告 / 总结 / 答疑：不要那套字段，但另一条同样硬：
  数字、路径、结论的把握程度**一个字都不许因为要通俗就改动或省略**。

`kind` 写错或漏传一律按 `card`（更严的那份）算 —— 退到「更严」只是多要几个字段，
退到「更松」却可能让一张卡片悄悄少掉审计要核的东西。

怎么切：

- 界面上：顶部那条独立的「写法」栏（两个选项的文字自己写着区别），
  状态栏也会显示当前档位；「和 AI 对话」窗口右上角还有一份，方便就地切
- 命令行 / 配置文件：`.env` 里的 `COGNITRAIL_TONE=pro|plain`
  （老写法 `专业模式` / `专家模式` 两种中文都认）

已经落盘的卡片**不会**被改写 —— 想换口吻就重新生成一份新的。
每张卡片会用 `tone:` 记下自己是哪一档写的：少了这一格，后来的人看到
「页面更快但会少拿几条数据」这种句子，分不清那是刻意的白话还是模型说不出准话。

想直观看差别：

```bash
python tools/demo_tone.py       # 两档各写一遍同一份改动，产物在 out/tone-demo/
```

## 测试

一条命令跑完（自动发现 `tests/` 下所有 `.py`，不用记清单）：

```bash
python tools/run_tests.py           # 10 份测试，全绿才算没坏
python tools/run_tests.py --with-gui    # 再把窗口冒烟测试也带上
```

各自覆盖的东西：

| 文件 | 管什么 |
|---|---|
| `tests/smoke_test.py` | git diff 链路（改动 → 卡片 → 闸门） |
| `tests/test_session.py` | Codex 链路（会话 → 卡片 → 闸门 → 出题） |
| `tests/test_sources.py` | 多源链路（WorkBuddy / Claude / 注册表 / 多源守护） |
| `tests/test_workflow.py` | 工作流总结 + 「设置 Key」的热更新 |
| `tests/test_curate.py` | 决策整理（排序 / 融合 / 演进链 / 选段生成） |
| `tests/test_projectmap.py` | 项目主线 + 卡片的阶段 / 卡点 / 取代关系 |
| `tests/test_cli.py` | 统一入口（双击不闪退、菜单流程） |
| `tests/test_audit.py` | 审计：反舞弊痕迹 / 关键度不可降级 / 审计意见 |
| `tests/test_tone.py` | 两档写法 + 两种用途：换措辞但凭据字段不变、kind 写错退到更严的那份、切档落盘 | 
| `tests/test_cli_bridge.py` | CLI 桥：探测 / 报错识别 / 命令行拼装 / 手动路径 / 额外目录 / 自定义命令 |

界面本身另有一份冒烟脚本（会真把窗口开起来点一遍），它**硬校验**这些：

- 底栏 / 写法条 / 各块正文区（对话、卡片详情、日志）的高度都 `> 1` 且可见
  —— 出过两次「被压成 1 像素、按钮看不见」的布局 bug，这条就是防它复发；
- 对话窗口的输入框高度 `> 1` 且可见、还能真的收到键盘输入
  —— 出过「CLI 明明找到了，却没法输入问题」（输入框被上面的对话区挤没了）；
- 报告窗口 / CLI 设置窗口底部的按钮都可见；
- 三处只读文本是「可选中」而不是 `disabled`；
- 最后真调一次自检，检查那份报告的格式（发版校验要靠它认版本号）。

> 它会自己调 `SetProcessDpiAwareness(2)`：**不调的话 Windows 会把屏幕和字体
> 一起虚拟化，布局余量比真实环境宽松**，上面第 2 条就会在坏掉的情况下照样通过。

```bash
python tools/gui_smoke.py       # 需要带 tkinter 的解释器；失败退出码 1
```

打包出来的窗口版没法从命令行看输出，所以它留了一个自检出口：
设 `COGNITRAIL_GUI_SELFTEST=1` 再运行，界面会真的搭起来、
读一遍会话和卡片，写一份报告到 `~/.cognitrail/gui-selftest.txt` 然后自己关掉。

每次发版还有一条命令要跑（版本号 / PE 子系统 / 窗口版自检）：

```bash
python tools/check_exe.py       # 任一项不过就退出码 1
```

第 1 步查的是**控制台版**的版本号，第 2 步只查窗口版的 PE 头 —— 都不看窗口版的
版本号。所以第 3 步额外要求自检报告里有一行 `版本：X.Y.Z`，且等于源码版本。
这是补一个真踩过的洞：控制台版已经升到 0.9.0、窗口版还停在前一版
（新 exe 确实构建出来了，但拷进 `dist/` 时被占用、没覆盖成功），
而「三连」照样全过 —— 结果窗口标题栏一直写着旧版本号。

## 设计文档

见 `docs/design/`。决策记录格式遵循 AgDR 规范。

## 项目状态

当前为 **P0 → P0.5**：一次可证伪的验证，不是产品。
实现计划、实测证据与形态修正见 [`PLAN.md`](PLAN.md)。

## 项目结构

```
src/                 主包（零第三方依赖，纯标准库 + git + tkinter）
  cognitrail.py      CLI 总入口（子命令 + 交互菜单）
  config.py          路径与 .env 配置中枢
  schema.py          卡片数据结构 + 证据闸门（文件:行号 必须真实）
  sources/           Codex / WorkBuddy / Claude 会话采集适配器
  session_source.py  统一会话模型（Session / Turn / Patch）
  llm.py             DeepSeek 调用（真模型 / 离线 Mock 双轨）
  cardgen_session.py 从会话轮次生成卡片（主引擎）
  audit.py           审计：关键度定档 / 造假痕迹 / 审计意见
  quota.py           按用户水平（专业度 × 关切度）决定给几张卡片
  curate.py          卡片库整理：排序 / 融合冲突 / 演进链
  projectmap.py      项目主线（阶段清单），卡片挂靠点
  dashboard.py       卡片 → 单文件 dashboard.html 决策面板
  probe.py           延迟复测（SQLite，默认 3 天后再问你一遍）
  questions.py       复测题 + 追问链
  workflow.py        会话 → 工作流总结
  watch.py           守护进程：盯会话文件变化
  pipeline.py        最短演示链路（diff → 卡片 → 面板）
  gui.py             Tk 图形界面
  tone.py            专家 / 小白两档写法（只改措辞不改事实）
  cli_bridge.py      调本机 AI 命令行（claude / codex / gemini / …）
  doctor.py          环境自检
tools/               构建与运维（打包 / 发版校验 / 冒烟 / 测试入口 / 清卡）
tests/               测试（python tools/run_tests.py）
samples/             演示仓库生成器（不花钱跑通全流程）
codex-plugin/        Codex 原生插件形态
docs/PROJECT-GUIDE.md  ★ 逐文件源码学习指南（先读这个）
docs/design/         设计方案与产品调研
```

## License

MIT
