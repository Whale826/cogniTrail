# 知迹 CogniTrail · Codex 插件

把一次代码改动里的**决策点**留成可复测的卡片：决定了什么、为什么、放弃了什么、代价是什么。
每张卡片必须钉住真实的 `文件:行号` —— 定位不到的一律丢弃。

> 知迹不总结代码。它记录「决定了什么」，然后在几天后把问题拿回来问你。

---

## 别人怎么用

Codex 里有**两条互不相同**的通道，别混。它们各管一件事，可以同时开。

| | 通道 A · 技能 | 通道 B · 插件 |
|---|---|---|
| 生效条件 | `~/.codex/skills/cognitrail/SKILL.md` 存在即可 | 注册市场 **+** 在 Codex 里把插件装上 |
| 装完什么样 | 进入 Codex 的可用技能列表，说一句话就触发 | 插件列表里一条：图标、显示名、默认提示按钮 |
| 安装命令 | `python tools/install_codex_skill.py` | `codex plugin marketplace add "<codex-plugin 的绝对路径>"` |
| 能否当场验证 | 能（见下） | 要在 Codex 界面里看一眼 |

> 想立刻就能用，走**通道 A**。想要插件列表里那条带图标的，再补**通道 B**。

---

### 通道 A · 装成技能（推荐，可验证）

在项目根目录执行：

```bash
python tools/install_codex_skill.py
```

它把 `SKILL.md`、`agents/` 和引擎一起拷到 `~/.codex/skills/cognitrail/`，
让技能自带引擎、不依赖你电脑上那份项目目录。

**验证装没装上**（不用开图形界面）：

```bash
codex debug prompt-input | grep cognitrail
```

看到 `- cognitrail: (file: r0/cognitrail/SKILL.md)` 就是装好了。
这一条比什么都可靠 —— 它打印的是**模型真正能看到的内容**。

### 通道 B · 注册成插件

```bash
codex plugin marketplace add "C:\你的路径\codex-plugin"
```

看到 `Added marketplace 'cognitrail-local'` 就成了。这一步只**注册市场**，
不会自动装插件 —— 之后打开 Codex，在插件列表里找到「知迹 CogniTrail」装上即可。

市场清单里这条插件的安装策略是 `INSTALLED_BY_DEFAULT`，
所以正常情况下它会直接处于已安装状态；如果没自动装上，手动点一下安装。

---

## 装完怎么用

打开 Codex，重启一次（让它重新读技能目录），然后：

- 直接说：「记录这次改动的决策卡片」
- 或者打：`/cognitrail`

**不需要 API Key** —— 在 Codex 里分析 diff 的模型就是 Codex 自己。

---

## 需要额外装什么吗

**不需要。** 插件自带引擎，只用 Python 标准库 —— 没有 pip 依赖、没有 Node 依赖。
只要 `python` 和 `git` 在 PATH 里就能跑（Codex 里绝大多数机器都满足）。

**也不需要 API Key。** 在 Codex 里跑知迹时，分析 diff 的模型就是 Codex 自己。
API Key 只在独立命令行模式下才有意义。

---

## 它会产出什么

在**你正在改的那个仓库**里：

```
<你的仓库>/
└─ docs/decisions/
   ├─ 2026-10-13-00-新增验证集比例_VALIDATION_SPLIT_0_2.md
   ├─ 2026-10-13-01-重试次数设为常量_MAX_RETRIES_3.md
   ├─ SUMMARY.md          一页汇总
   └─ dashboard.html      决策面板（就是「打开 Codex 就能看见知迹」的那个界面）
```

探针的排期与作答存在 `~/.cognitrail/probe.db`，跟着用户走，不污染仓库。

---

## 手动跑引擎（调试用）

插件的引擎也可以单独跑，用来查问题：

```bash
cd <插件目录>/plugins/cognitrail

# 校验一个坐标是不是真的存在
python scripts/cognitrail.py gate --repo C:\path\to\repo --file src/config.py --lines 4-5

# 列出某个仓库里的卡片
python scripts/cognitrail.py cards --repo C:\path\to\repo

# 生成面板并起本地服务（端口给 Codex 内置浏览器用）
python scripts/cognitrail.py panel --repo C:\path\to\repo --serve 8787

# 看今天有没有到期的复测题
python scripts/cognitrail.py probe --list --force
```

---

## 卸载

```bash
# 通道 A
python tools/install_codex_skill.py --uninstall

# 通道 B
codex plugin marketplace remove cognitrail-local
```

仓库里的 `docs/decisions/` 是你的产出，上面两条都不会动它。

---

## 一个真实的坑：config.toml 会被别的工具重写

本机实测：`~/.codex/config.toml` 曾在某次启动时被 **CC Switch**（一个模型配置切换器）
整个覆盖，原有的 `[marketplaces.*]` 和 `[plugins.*]` 段被一起冲掉了。

后果：插件通道**静默失效**，而技能通道（`~/.codex/skills/`）不受影响。

所以如果你用 CC Switch 之类会重写 config.toml 的工具，请以通道 A 为主。
装完顺手核查一次：

```bash
grep -c "marketplaces.cognitrail-local" ~/.codex/config.toml   # 1 = 还在，0 = 被冲掉了
```

---

## 结构

```
codex-plugin/
├─ .agents/plugins/marketplace.json          市场清单（Codex 靠它发现插件）
├─ plugins/cognitrail/
│  ├─ .codex-plugin/plugin.json              插件说明书
│  ├─ skills/cognitrail/
│  │  ├─ SKILL.md                            ★ 技能的正文，改行为改这里
│  │  └─ agents/openai.yaml                  插件列表里的显示名与默认提示
│  ├─ scripts/cognitrail.py                  引擎入口（gate / panel / probe / cards）
│  ├─ scripts/src/                           引擎本体（从项目 src/ 同步，勿手改）
│  └─ assets/                                图标
└─ README.md                                 本文件
```

`scripts/src/` 是拷贝。要改引擎逻辑，改项目根的 `src/`，然后跑：

```bash
python tools/build_plugin.py
```
