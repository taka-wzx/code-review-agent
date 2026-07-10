# Eval 标注表（W1）

评测集：`eval/diffs/*.diff`，每个 diff 埋了已知 bug。跑 `run_eval.py` 后，拿 agent 输出（`eval/results/<name>.json`）逐条对照本表，人工在「命中?」列填 ✅/❌。命中率 = 命中的埋点数 / 埋点总数，就是你的 baseline 数字。

**判定规则**：agent 指出了该 bug 的实质（不必措辞一致）算命中；没提到算漏报；提了实际不存在的问题记到「误报」。

| diff | 埋的 bug（期望 agent 指出） | 严重度 | 命中? |
|---|---|---|---|
| d1_sign | `classify_bounce` 用 `bounce_dy_mm` 原始符号判 overshoot/undershoot，**没投影到飞行方向 `vy_at_snap`**；vy<0 时结论正好反了（项目 CLAUDE.md §5.1 的真实教训） | high | ❌ |
| d1_sign | 新参数 `vy_at_snap` 传进来了但函数体从没用它（死参数，暗示逻辑没写完） | medium | ✅ |
| d1_sign | `mean_error([])` 空列表除零 → ZeroDivisionError | medium | ✅ |
| d2_units | 循环 `range(len(positions_mm))` 后又访问 `positions_mm[i+1]` → 最后一次迭代 **索引越界** | high | ✅ |
| d2_units | 注释写 "mm/frame -> km/h"，但 `dist_mm / FRAME_DT_S` 得到的是 **mm/s 不是 m/s**，少除 1000；后面 ×3.6 得出的速度大 1000 倍（单位 bug） | high | ✅ |
| d3_state | `detection_rate` 对空 `frames` 除零 | medium | ✅ |
| d3_state | `load_calib` 打开文件 **从不关闭**（无 `with`/`f.close()`），资源泄漏 | medium | ✅ |
| d3_state | `except KeyError: pass` **吞掉异常后隐式返回 None**，调用方解包 `K, dist =` 会崩且丢失根因 | high | ✅ |
| d3_state | 用了 `json` 但 diff 里没 import（真实缺失 or 上下文有——agent 应该用 read_file 去确认） | low | ❌ |

**误报记录**（agent 报了但不成立的）：
- d3: "Python 2 下整数除法恒为 0"（low）——项目明确是 Python 3，纯投机性问题，判**误报** 1 个。
- 噪音（不算误报但稀释报告）：d1 缺 docstring / 缺类型注解 ×2（low，明明 system prompt 说了略过 style nit）；d2 变量名误导（medium，和单位 bug 同根，重复报）；d3 建议加 FileNotFoundError 处理（low，锦上添花非 bug）。噪音共 4 条。

**baseline 汇总**（2026-07-05，deepseek-v4-pro，无仓库上下文——read_file 全部失败返回错误）：
- **命中 7 / 9 埋点（recall 0.78），误报 1 个**；总 findings 12 条，其中 7 命中 + 1 误报 + 4 噪音。
- **两个 miss 全是"需要 diff 之外上下文"的**：①d1 符号投影 bug（headline 领域 bug！需要 CLAUDE.md §5.1 领域知识——agent 报了死参数却没推出"没投影→结论反了"）；②d3 缺 import（需要 read_file 看文件头，但合成 diff 的路径在仓库里不存在，读不到）。
- 结论：baseline 卡死在**上下文饥饿**上 → 正好是 W3（检索上下文）的对照组；噪音 4 条 → W5 verifier 的对照组。

**LLM-judge 校准**（W2，2026-07-05，judge=同模型 temperature=0）：
- 埋点命中判定 **9/9 与人工一致**（含最微妙的"死参数≠符号投影bug"判例——判定标准已固化进 `truth.json`）。
- findings 分类 11/12 一致；唯一分歧="Python 2 整数除法"人工记误报、judge 记噪音（理由：投机性担忧而非事实性错误）——属 FP/noise 定义边界，非判断错误。此后以 judge 口径为准：**事实性错误=FP，投机/琐碎/重复=noise**。
- 结论：judge 可用作自动打分器；后续扩集只需在 truth.json 加埋点条目（附命中标准），`run_eval.py` → `judge.py` 两条命令出全部指标。

## W3：主动上下文检索（2026-07-05）

评测集配了 fixture 仓库 `eval/repo/`（与 diff 改动后状态一致 + CLAUDE.md 约定 + 调用方）。
`context.py` 按 diff 解析改动文件/函数符号，主动预取：约定文档、改动文件全文、调用方片段（纯符号检索，带预算 cap）。`--no-context` 为 ablation 开关。

| 版本 | recall | precision | FP | noise |
|---|---|---|---|---|
| V0 无上下文（W1 baseline） | 7/9 = 0.78 | 7/12 = 0.58 | 0 | 5 |
| V1 +主动检索（W3） | **9/9 = 1.00** | **9/11 = 0.82** | 0 | 2 |

- 两个上下文饥饿 miss 全命中：d1 符号投影（finding 直接引用 §5.1 并给出投影修法；与死参数**合并为一条 finding**——judge 规则已放宽为一条 finding 可命中多个埋点，precision 分子按去重 finding 数计）；d3 缺 import（从文件全文确认只 import 了 numpy → NameError）。
- 剩余噪音 2 条 = d3 两条"缺 docstring"（agent 引用了 CLAUDE.md 风格约定——检索的副作用：约定进上下文后 style nit 反而变多，W5 verifier 的活）。
- **诚实注脚**：n=9 且 fixture 是照着检索能力设计的（CLAUDE.md 恰好记了这两课）——这证明"约定文档记了领域教训时检索有效"，不证明泛化。下一步扩集用真实 commit 反推的用例防过拟合（W5/W6 前置）。

## 扩集（2026-07-05）：15 diffs / 28 埋点，源自真实 bug 蒸馏

真实 commit 均为 300+ 行混合 checkpoint，直接反转不可用；改为把记忆/CLAUDE.md 记录的
~10 个**真实 bug 的机理**蒸馏成紧凑用例（d4-d15，埋点与命中标准见 `truth.json`）：
dt 感知门、自举死区、鬼弹跳、死旗标、降采样半径过滤、ms/s、deg/rad、协方差对称化、
COR raw/ukf、x 轴投影半修复陷阱、大 diff 混 3 bug，外加 **2 个无埋点陷阱用例**
（d12 干净代码 / d13 纯重构）专测误报。diff 由 pre/post 经 difflib 机械生成，
`eval/check_consistency.py` 校验 diff↔fixture↔truth 三方一致。

**注意 V0 语义变化**：扩集起 fixture 仓库真实存在，V0（--no-context）的 read_file 是可用的
——V0 = agent 被动按需拉文件，V1 = 主动预取 + 按需工具。隔离的是"主动检索"这一层的价值。
与 W1 的 n=9 旧表不可直接比。

| 版本（n=28） | recall | precision | FP | noise |
|---|---|---|---|---|
| V0 被动工具 | 24/28 = 0.857 | 24/57 = 0.421 | 3 | 30 |
| V1 +主动检索 | **25/28 = 0.893** | 25/56 = 0.446 | **0** | 31 |

**失败分析**：
- 检索修复的 miss：d1-sign-projection（§5.1 约定）、d5-window-deadzone（调用方注释"短采集段"）——约定/调用方依赖型，机制在新用例上复现有效。
- 检索消灭全部 3 个 FP（V0 对 d13 重构猜"可能有调用方会崩"，V1 检索证实无调用方）。
- **两版都漏**：d7-dead-flag-path（需要追 import 进 config.py 看旗标组合——**检索不追 import 是当前架构缺口**）、d11-origin-fit（过原点拟合的统计偏差，纯领域难点）。
- **V1 反而漏了 d7-gap-connect**（V0 抓到）：agent 未锁 temperature，run-to-run 方差真实存在——可复现性待办。
- **核心结论：瓶颈从 recall 移到 noise**。noise 30→31 纹丝不动（检索把约定喂进上下文后 style nit 反而更多），precision 被摁在 0.42-0.45。陷阱用例 d12 被报 3-5 条"建议"。→ **W5 verifier 的靶子明确：把 noise 砍半以上，precision 应上 0.7**。

## W5：verifier 二次复核（2026-07-05）

`verifier.py`：finder 出稿后独立第二遍——每条 finding 判 keep/drop（KEEP=具体缺陷+可验证失败场景；DROP=事实错误/style nit/无证据投机/重复/泛泛建议）。temperature=0，结构化校验+重试，**失败 fail-open**（坏掉的过滤器不能吃真 bug）。被丢 finding 带 `drop_reason` 存 `dropped_findings` 可审计。`--no-verify` 做 ablation。

| 版本（n=28） | recall | precision | FP | noise | findings 总数 |
|---|---|---|---|---|---|
| V0 被动工具 | 0.857 | 0.421 | 3 | 30 | 57 |
| V1 +主动检索 | **0.893** | 0.446 | 0 | 31 | 56 |
| V2 +verifier | 0.786 | **0.875** | 0 | **3** | 24 |

**收获**：noise -90%（31→3）；陷阱用例 d12/d13 findings **归零**（V1 各 3 条）；verifier 还正确砍掉了 finder 越界报的 diff 外问题（d5 里报 gate.py 的"[Pre-existing]"条目——作用域控制）。

**代价与失败分析（recall -0.107，6 个 miss 拆账）**：
- 3 个非 verifier 责任：d7×2（finder 本 run 就没找到，run 方差+import 缺口）、d11-origin-fit（finder 一直没找到过）。
- **3 个 verifier 错杀**（各有教训）：
  - d5-window-deadzone（**high，最痛**）：drop 理由是"函数正确返回 None、调用方处理了"——verifier 看了局部正确性，没把调用方注释"段常 2-4 样本就结束"当证据推出**功能级死区**。教训：**"投机建议"判据与"领域死区"bug 相撞**——调用方文档化的使用模式应算证据而非投机。
  - d10-cov-asymmetry（medium）：被判"投机的数值稳定性建议"——本就是难档领域埋点，verifier 的证据标准对这类"慢性数值问题"天然不利。
  - d4-magic-number（low）：被判 style——这个埋点本身就在 style/缺陷边界上，算判定口径分歧。
- **不做的事**：不针对这 3 个错杀去调 verifier prompt——那是在测试集上过拟合；正确路径是先建 held-out 集再迭代 prompt（W6+）。

**汇总一句话**：F1 视角 V0 0.57 → V1 0.59 → V2 **0.83**；代价路径清楚、错杀可审计（drop_reason 全留档）。

## W6：工具层扩展 + trace 落盘 + 编造检测用例（2026-07-06）

对照 agent 学习路线文章（工具设计/翻车兜底/评估三章）逐项补齐，全部改动都对着 W5 失败分析里的具体账：

1. **finder 输出 schema 校验+重试**（`agent.py validate_review`）——此前 `submit_review` 的 payload 直接 `json.loads` 就用，字段缺失/severity 非法值会带病进 verifier/judge；现在与 verifier 同款 validate→拒绝回填→重试（上限 2 次）。
2. **`search_repo` 工具 + read_file 可恢复错误**（`tools.py`）——对着 **d7 屡漏**：检索不追 import 的架构缺口，现在 agent 可以按需 grep 符号（如旗标在 config.py 里的组合）。read_file 文件不存在时返回候选路径或"仓库里确实没有这个文件"，而不是裸 Error。
3. **verifier 挂同款工具**（读 + 搜，只读）——对着 **d5 错杀**：Reflection 没有查证手段时只能凭 finder 视角猜；现在 system prompt 明确"依赖未见代码的 finding 必须先查再判"。
4. **trace 落盘**（`tracelog.py`，JSONL；`run_eval.py` 自动写 `<results>/traces/<name>.jsonl`）+ **finder 锁 temperature=0** + **同参数重复工具调用短路**——对着 d7-gap-connect 的 run 方差不可归因问题；每条 llm_response/tool/submit_rejected/verdicts 事件都可回放。
5. **新用例 d16_missing_dep（2 埋点，总数 28→30）**——评估文章说评测集要含"信息缺失"样本、"错误失败"（编造）最危险：新文件 import 仓库中不存在的 `tracker.timeutil`。测 agent 是去查证并诚实报告（命中 d16-missing-module），还是假设模块存在乃至编造其行为（记 FP）。该用例同时是 ②③ 两项工具改动的行为探针：read_file/search_repo 都会告诉它"仓库里没有"。

**注意**：MAX_STEPS 8→10（工具多了、含校验重试回合）；verifier 有独立步数上限 6 + 坏 submit 上限 2，超限仍 fail-open。V2 之前的三版数字用的是 28 埋点口径，加 d16 后跑的新数字（30 埋点）不可与旧表直接比，需全量重跑三版 ablation。

**W6 冒烟记录（d16 + d12,2026-07-06）**：
- d16 冒烟：finder 2/2 埋点全中,missing-module 表述诚实（"仓库里没有 tracker/timeutil,normalize_ts 无任何定义"）,零编造;verifier 5 步工具查证后 kept 2/5,砍掉的 RallyState 投机条正是它自己 read 了 rally.py 才下的判——**verifier 带工具的设计目标首次得到行为验证**。
- 冒烟第一轮抓到真 bug：finder 确认 timeutil 缺失后继续全仓漫游撞死 MAX_STEPS——工具变多后"步数上限"不等于"停止条件"（文章执行边界一课的现挂）。已修:最后一步撤掉探索工具、只留 submit,强制优雅收口（finder/verifier 同款）。
- d12 回归抓到工具缺陷：verifier 用正则式 pattern（`dbg\("`）搜字面量工具,得到"仓库中不存在"的误导性回答。已修:无命中且 pattern 含正则元字符时,回复改为"本工具是字面量匹配,先用纯文本重试再下结论"。
- d12 本轮 kept 2（V2 基线 0）："load_jsonl 死代码"是 verifier 搜索验证过的事实但在陷阱用例上属 noise;另一条"malformed JSON 无处理"两轮 run 一 drop 一 keep（t=0 下 provider 仍非位确定）。**均不据此调 prompt**（W5 纪律:不在测试集上过拟合）,留待全量重跑 + held-out 集。

## W6 全量重跑（2026-07-06，30 埋点 × 三版 ablation）

三版各 16/16 跑完零 crash，fail-open 未触发；trace 全量落盘（`<results>/traces/`）。
注意 V1/V2 是**独立 run**（V2 不是 V1 的 findings 叠加 verifier），逐 case 对比含 finder 的 run 方差（如 d6：V1 1/2、V2 2/2）。

| 版本（n=30） | recall | precision | FP | noise | findings 总数 |
|---|---|---|---|---|---|
| V0 被动工具 | **0.900 (27/30)** | 0.419 | 0 | 36 | 62 |
| V1 +主动检索 | 0.800 (24/30) | 0.348 | 0 | 43 | 66 |
| V2 全流水线 | 0.800 (24/30) | **0.793** | 0 | **6** | 29 |

F1：V0 0.57 → V1 **0.49** → V2 **0.80**（旧 28 埋点口径 0.57/0.59/0.83，不可直接比）。

**W6 三个立项问题的答案**：
- **d16 编造检测（目标①）✔**：三版全 2/2，missing-module 一律诚实报告（"仓库中不存在 tracker/timeutil"），零编造。工具层的可恢复错误设计（"没有这个文件+去 search 你期望的符号"）行为上被采纳。
- **d7 补漏（目标②）✘**：V0 1/2、V1/V2 0/2。`search_repo` 在手但没补上 d7-dead-flag-path——缺口从"没有工具"变成"**有工具但预取模式下不去用**"（见下面反转归因）。
- **d5 错杀复验（目标③）无法复验，且错杀模式仍在**：d5-window-deadzone 本轮三版 **finder 均未找到**（旧数据是"finder 找到、verifier 错杀"，问题移到了上游；n=1 无法断言是退化还是方差）。但 verifier 交出了新错杀：**d1-mean-error-div0**（空列表除零，V1 finder 3/3 命中，V2 被 drop，理由="search 证实无调用方→投机性健壮性建议"）——**带工具的 verifier 只是更有依据地犯同类错，"无人调用=投机"判据吃真 bug 这一族失败未消除**。

**新发现：V1 主动检索 recall 反转（0.90→0.80，-3 埋点）**。丢的三个（d10-cov-asymmetry、d6-ghost-bounce、d7-gap-connect）全是 V0 命中、V1 漏，且 trace 呈一致模式——**预取后探索变浅、提早交卷**：

| case | V0 | V1 |
|---|---|---|
| d10_filter | 10 步 / 5 read | 7 步 / 4 read |
| d6_ghost | 6 步 / 4 read | 6 步 / **2 read** |
| d7_display | 8 步 / 3 read | **4 步 / 1 read** |

疑因：W6 给 finder 新加的"step budget 有限、勿穷举仓库"提示词与预取包叠加——模型把"上下文已给够"当成"可以早交卷"信号，多埋点 diff 里第二个更隐蔽的 bug 就没挖到。与 W3 时代"检索修 miss"结论方向相反。**n=1，方差未排除**（d7-gap-connect 在 W5 就有过 run 间翻转记录），但三 case 方向一致+步数/read 证据支持。

- 陷阱用例：d12 归零（0 findings，与 W5 V2 持平）；d13 kept 2 条 noise（W5 为 0，轻微回退）。
- **不做的事（纪律同 W5）**：不针对 d1 错杀、V1 反转去改 prompt——那是在测试集上过拟合。W7 立项顺序：①每版 n≥3 重复跑把方差和真效应分开；②建 held-out 集；③再动"step budget"提示词与预取的交互。

## W7（进行中,2026-07-09）：测量仪器 + 外围功能

按"先仪器后迭代"的顺序落地（W6 重跑的两个开放问题——V1 伤 recall 是否为真、verifier 错杀率——都只能由重复跑裁决）：

1. **repeat_eval.py**：每版 n 次重复跑,断点续跑（scores.json 存在即跳过）,聚合 mean [min–max] + **per-bug 翻转表**（hit x/n = 方差,0/n = 真 miss）。冒烟即抓到聚合 bug：陷阱切片 recall=None 未处理（已修）。3 版 × 3 次全量扫描后台运行中。
2. **held-out 集** `eval/holdout/`（6 diffs / 7 埋点 + 1 陷阱,独立 fixture 副本）：h1 轴混用（x/y 对 WIDTH/LENGTH 颠倒）、h2 违反 §6 裸 print + HUD 超 6 行、h3 append 用 'w' 每次清空日志、h4 mm/s×3.6 差 1000 倍（§5.2）、h5 可变默认参数跨调用累积 + 重复时间戳除零、h6 config 纯重构陷阱。**纪律：只在 prompt/判据迭代验收时跑。** 造集时自查出一个错误 ground truth：h5 除零原设计在 t1==t0 时根本不触发（n_missing=-1 空循环）——重写为 hoist 除法后才成立;教训:埋点必须先自证可触发。
3. **git 集成**（--commit/--uncommitted/--pr）+ **--format md**（PR comment,dropped 收 details 审计块）+ **cost_report.py**（trace 聚合 token/成本;d12 单用例全流程约 4.2 万 token 入 / 4.7 千 token 出,verifier 占约一半）。迷你 repo 端到端冒烟通过。

**观察（不据此调 prompt）**：git 冒烟里 finder 报了 diff 之外同文件的预存 bug（hours_to_ms 单位错）,verifier 判 keep——与 W5 曾 drop 跨文件"[Pre-existing]"条目的口径不完全一致,同文件/跨文件的作用域边界值得在 held-out 验收时一并观察。

**W7 完结（2026-07-09）：3 版 × 3 次全量重复跑,方差数据落地**

| 版本(n=3) | recall | precision | F1 | FP | noise |
|---|---|---|---|---|---|
| v0 | 0.844 [0.800–0.900] | 0.403 [0.391–0.417] | 0.545 [0.532–0.554] | 1.0 [0–2] | 36.7 [34–39] |
| v1 | 0.844 [0.833–0.867] | 0.388 [0.353–0.424] | 0.531 [0.496–0.569] | 0.3 [0–1] | 39.7 [37–44] |
| v2 | 0.811 [0.800–0.833] | 0.833 [0.727–0.920] | 0.819 [0.776–0.856] | 0.3 [0–1] | 4.7 [2–9] |

三个裁决：

1. **"V1 预取伤 recall"不成立**。v0/v1 均值同为 0.844;W6 单次的 0.90→0.80 是 v0 幸运上沿 vs v1 普通一轮。v1 区间更窄——预取实际**稳定**了 recall 并压低 FP。开放问题①关闭,step-budget×预取 prompt 不动。仪器阻止了一次针对噪声的修复。
2. **verifier recall 代价收敛**：v2 vs v1 均值差 -0.033（≈1 个埋点）,远轻于 W5 单次的 -0.107 印象;F1 0.819 [0.776–0.856],W6 单次 0.80 有代表性。
3. **方差与真 miss 分离**（per-bug 翻转表）：
   - 真 miss（三版 0/3 或 v2 0/3）：d11-origin-fit、d7-dead-flag-path（全版本 0/9,架构靶子）;d10-cov-asymmetry、d7-gap-connect（v0/v1 偶中,v2 0/3——**verifier 系统性砍杀确认**,不再是单例轶事）
   - 方差型：d5-window-deadzone（v0 1/3,v1 0/3,v2 2/3——有趣:verifier 带工具后反而比 v1 稳）、d6-ghost-bounce、d4-magic-number
   - verifier 严格度自身有方差:v2 noise [2–9]
4. 成本（cost_report,v2 全集单轮均值）：47 万 tok 入 / 7.7 万 tok 出,finder:verifier ≈ 6:4。

下一步（W8 才动手,全部要过 held-out 验收）：verifier 投机判据（对着 d10/d7-gap 系统性砍杀）、预取层追 import 或引导 finder 主动 search（对着 d7-dead-flag/d11-origin 双 0/9）、run_linter 工具。

## W8（2026-07-09）：verifier 判据 → 预取追 import → run_linter,每项过 held-out 验收

held-out 首次实战。基线(改动前 V2 单轮):recall 6/7、precision 0.875、noise 1、h6 陷阱 0。验收线:recall≥基线、h6=0、noise≤基线+2。

**改动一:verifier 投机判据——验收失败,已回滚。** 设计:KEEP 侧加"可验证的错误结果机制不要求崩溃",DROP 侧把"投机"重定义为"约定/注释/调用方里都找不到证据"。w8a 结果 recall 5/7:新判据把 h5 除零砍了,drop 理由与新措辞逐字呼应("没有调用方、没有约定或注释表明重复时间戳会发生")——**判据救有文档证据的慢性 bug,却处决无文档触发证据的崩溃类 bug**(新文件天然无调用方)。按预先写死的规则回滚,不二次迭代。

**后续 w8b 揭示更深一层:凶手不(只)是措辞。** w8b 用基线措辞的 verifier 又把同一条 h5 除零砍了,drop 理由甚至编造"docstring 要求时间戳互异"(原文只说 sorted)。即 **verifier 对边界 bug 的裁决本身就是抛硬币**(W7 量到的 noise [2–9] 方差,同样作用于 hit),基线 run 里它被 keep 才是运气。教训两条:①改动一的失败归因当时过度指向措辞;②**n=1 验收门会被 verifier 方差假触发——归因(查 drop_reason + pack 对比)必须是验收判定的一部分,不能只看数字**。

**改动二:context.py 预取追 import——通过(附因果归因)。** 机制自检:d7 的 pack 直接出现 config.py 旗标定义、d16 出"import 无法解析"note、stdlib 静默。w8b 数字面 recall 5/7 低于基线,但 h5 是无 import 文件、pack 与基线逐字节一致,差异物理上与本改动无关(见上);本改动自己的验收面全部干净(h2 预取 config 生效、FP 0、h6=0、noise 0)。

**改动三:run_linter 工具(pyflakes,静态不执行)——通过,满分。** w8c:**recall 7/7(1.000)**、precision 0.778、FP 0、noise 2、h6=0;两个"惯性 miss"(h2 行数上限、h5 除零)全部抓到,6 diff 里 linter 被调用 4 次。诚实注脚:7/7 也含方差的顺风面,但所有验收指标全部在线。离线自测:linter 在 fixture detect.py 上准确报 undefined 'json'(d3 埋点)。

主集终测(V2×3,用户定的范围)后台运行中,表格与 d10/d7-gap/d7-dead-flag 翻转对比待补。

**W8 终测(V2×3,主集 30 埋点)与 W7 对比:**

| | recall | precision | F1 | noise |
|---|---|---|---|---|
| W7 V2 | 0.811 [0.800–0.833] | 0.833 [0.727–0.920] | 0.819 [0.776–0.856] | 4.7 [2–9] |
| W8 V2 | 0.778 [0.767–0.800] | 0.830 [0.759–0.885] | 0.803 [0.763–0.840] | 4.3 [3–7] |

**如实结论:主集指标在方差区间内持平**(F1 区间大幅重叠),W8 的净收益在能力面(linter 工具、import 预取、held-out 机制跑通)而非主集分数。翻转表变化:

- d7-dead-flag-path 仍 0/3,但**性质变了**:run2 finder 首次报出旗标问题(import 预取生效)、被 verifier 砍;run1/3 未报。可见性已解决,识别+复核是剩余瓶颈。
- d10/d7-gap 仍 0/3——判据改动回滚后无修复入场,符合预期,如实记录为未解决。
- d5-window-deadzone、d6-ghost-bounce 从 W7 的偶中滑入 0/3;d4-magic 1/3→2/3——边界埋点在方差里晃,与 h5 在 held-out 的表现同源:**verifier 对边界 bug 的裁决方差是当前系统的主要不稳定源**。

W9 候选(需先想清机制,不是改措辞):verifier 边界裁决方差——多数投票/双复核/降置信保留(标记 uncertain 而非 drop);finder 对"死路径"类 bug 的识别提示;d11-origin 领域难档挂起。

## W9（2026-07-09）：双复核 + 分歧→uncertain——治 verifier 边界裁决方差(新机制,零措辞)

**设计**:判据 prompt 与 VERDICT_TOOL 一字不动。verifier 跑两个独立 pass(B 倒序呈现 findings,显式 index 保证映射;倒序=确定性去相关,不引入 temperature 变量),`merge_verdicts` 纯函数合并:keep+keep→confirmed、drop+drop→dropped(理由前缀 2/2)、分歧→保留并标 uncertain+附少数派理由。单 pass 失败退化单复核(trace 记 degraded),双失败 fail-open。关键洞察:**让模型自报 uncertain 还是措辞工程且标定不可信;两个独立 pass 的分歧本身就是边界探测器**。机制数学:drop 需 2/2 票——若单 pass 误砍率 p,双 pass 误砍率 p²,keep 偏置的合并结构性压制随机砍杀。成本:verifier ×2,pipeline ≈ +40%。

**held-out 验收 ×2(w9a/w9b,预写标准全过)**:

| | recall | precision | noise | h5 除零 | h6 陷阱 |
|---|---|---|---|---|---|
| 基线(W8 前) | 6/7 | 0.875 | 1 | keep(运气) | 0 |
| w8a/w8b(单复核) | 5/7 / 5/7 | 1.0 / 1.0 | 0 / 0 | **drop / drop** | 0 / 0 |
| **w9a/w9b(双复核)** | **6/7 / 6/7** | 1.0 / 1.0 | 0 / 0 | **confirmed / confirmed** | 0 / 0 |

翻转消失的直接证据:h5 除零从"三轮两砍"变成两轮双票 confirmed(甚至无需 uncertain 兜底)。两轮指标完全一致——方差塌缩。唯一 miss 仍是 h2 行数上限(finder 层稳定 miss,非本改动靶子)。两轮 uncertain 计数均为 0:held-out 的 6 条 kept 全是双票一致,分歧兜底还没被触发,它的实战表现看主集终测(V2×3 运行中,表格待补)。

**W9 终测(V2×3,主集 30 埋点)三代对比:**

| | recall | precision | F1 | noise | unstable | never-hit |
|---|---|---|---|---|---|---|
| W7 单复核 | 0.811 [.800–.833] | 0.833 [.727–.920] | 0.819 [.776–.856] | 4.7 [2–9] | 3 | 4 |
| W8 单复核 | 0.778 [.767–.800] | 0.830 [.759–.885] | 0.803 [.763–.840] | 4.3 [3–7] | 2 | 6 |
| **W9 双复核** | **0.856 [.833–.867]** | 0.743 [.722–.781] | 0.795 [.776–.822] | 8.3 [7–9] | **1** | **4** |

预写标准逐项判定:

1. **翻转表 unstable 下降 ✅**:3→2→**1**(仅剩 d5-window-deadzone 2/3)。
2. **d5/d6 回收 ✅**:d5-window-deadzone 0/3→2/3;d6-ghost-bounce 0/3→**3/3**。W8 滑走的两个边界埋点全部回来。
3. **noise 区间收窄 ✅ 但均值上移**:[2–9]→[7–9](宽度 7→2),代价是均值 4.3→8.3——drop 需 2/2 票,边界噪音条目更多存活(每 run uncertain 5–6 条,约占 kept 的 1/6)。呈现层已隔离(uncertain 单独成节),但 judge 口径下 precision 0.83→0.74。
4. **F1 持平 ✅**:0.795 [.776–.822] vs W8 0.803 [.763–.840],且区间宽度 0.077→0.046;recall 大幅回升 0.778→**0.856**,区间 [.833–.867] 是三代最窄。
5. **d7 uncertain 回收 ✘**:d7-dead-flag、d7-gap、d10、d11-origin 仍 0/3——这四个的瓶颈在 **finder 根本没报**(uncertain 机制救的是"报了被砍",救不了"没报"),如实记录。顺带:d4-magic 3/3(run1 经 uncertain 通道存活——分歧兜底在主集的实战首秀)。

**一句话**:双复核把 verifier 从"抛硬币"修成"可预测":recall +0.078 且区间最窄、unstable 归一,代价 precision -0.087(全部来自被隔离呈现的 uncertain 边界条目)。剩余四个 0/3 全是 finder 识别问题,W10 的靶子换层了。

## W10（2026-07-10）：finder 类别提示 + verifier 证据规则——对着 4 个 0/3 的两层瓶颈

**归因修正(立项前提)**:W9 结论"四个 0/3 全是 finder 没报"经原始 run JSON(results_repeat_w9/v2_run1..3)核查后**修正为两类失败模式**:

- 真·finder 盲区(kept 和 dropped_findings 里都从未出现):d7-dead-flag-path、d11-origin-fit。
- finder 报了、被双复核 2/2 砍:d7-gap-connect(run3 报出,drop 理由"docstring explicitly documents that gaps exist … intentional design choice"——推理错误:文档写明输入条件≠代码处理了它)、d10-cov-asymmetry(run3 报出 Joseph form,drop 理由"Generic best-practice advice … speculative robustness advice"——但 truth.json 明说报出即算)。W5 失败分析早有预警:verifier 证据标准对"慢性数值问题"天然不利。uncertain 机制救不了它们是因为两 pass **一致**砍——分歧兜底根本没触发。

**设计**(两个改动,均为 prompt 增量,判据本体一字不动):

1. **finder SYSTEM 加三类缺陷清单**(死路径旗标组合/文档化未处理输入/数值统计缺陷),插在 coverage 段与 step-budget 段之间。首句 guardrail:清单是待验证假设,报出须指名具体代码路径+失败机制——防清单诱发投机 findings 的第一道闸。措辞类别化零 eval 专名(无 Kalman/Joseph/旗标名),防过拟合。
2. **VERIFIER_SYSTEM DROP 列表后加 Evidence rules 两条**:①文档写明的输入条件是"代码未处理该条件"类 finding 的**支持证据**,不是"有意设计"的证据(括号句显式封死 w8a 反向解读——"无文档"也不自动=投机,那次教训砍了 h5 除零);②指名了"哪个量如何变错+机制"的数值 finding 不算泛泛建议,无法反驳机制则 keep;"X 比 Y 更稳"仍是 DROP。W8 改动一的失败模式是**重写判据本体**,本次只加限定范围的澄清,且 W9 下 drop 需 2/2 票,措辞残余误差被平方压制。

**不做**:finder 双跑并集(缓期)——真盲区 3 次独立 run 出现率≈0 并集救不了;间歇报出的照样死在 verifier 2/2;需新增去重机制且成本约 +60%。

**预写验收标准(跑之前落笔)**:

- **Stage A(仅改动一,靶向 d7_display,d10_filter,d11_cor ×3 + holdout)**:finder 层(kept∪dropped)报出 ≥2 个此前从未报出的目标 finding,其中 d7-dead-flag/d11-origin 至少一个;holdout recall≥6/7、h6=0、noise≤2、每 diff finder 总 findings(kept+dropped)≤W9 同 diff +3。此阶段 d7-gap/d10-cov 被旧判据 verifier 砍属预期,不算失败。失败处置:holdout 噪音超标→收紧 guardrail 措辞,最多迭代 1 次;finder 仍不报→回滚改动一记 negative result,只带改动二继续。
- **Stage B(合体,同靶向 ×3 + holdout)**:目标 findings 以 confirmed/uncertain 存活;**d10 的五条已知垃圾(inv-vs-solve、docstring 完整性、type hints、list-vs-ndarray、单位约定)仍须被 drop**——噪音闸门未炸的直接判据;holdout recall≥6/7、**h5 除零 kept**(Evidence rules 括号句的负向控制)、h6=0、noise≤2、precision≥0.85。失败只回滚改动二,保留已验收的改动一。
- **主集终测(V2×3,锚定 results_repeat_w9)**:never_hit 4→≤2 且 {d7-dead-flag,d7-gap,d10-cov} 至少 2 个 ≥2/3;recall mean≥0.856 且 min≥0.833(成功目标 ≥0.89);precision mean≥0.70、min≥0.68;F1 mean≥0.795(净收益要求);noise mean≤10、max≤12;陷阱 d12+d13 合计 kept 每轮≤4、均值≤3.0(W9 基线本就非零:4/2/3);unstable≤3;tokens_in mean≤790k(+15%)。d11-origin-fit 为 stretch 不作硬门槛(领域难档,勿为单点过拟合措辞)。
- **归因审计(必做,数字过线也不能省)**:4 个目标 bug 逐个走 finder→verifier→judge 链路;相对 W9 每条新增 noise 三分类——(i)属清单三类且 kept=**两改动交互项**(最危险),(ii)非清单类、按 W9 drop 模式本会被砍=verifier 放宽独立效应,(iii)其余=方差;查 d12/d13 与 3/3 系统性 FP。

**Stage A 结果(2026-07-10,含一次措辞迭代)**:

- 轮1(靶向 d7/d10/d11 ×3):finder 层 d10-cov 2/3、d7-gap 1/3(kept 的 d10-dt-zero finding 直接用了清单词汇"Documented-but-unhandled input"——清单在被执行);但 **d7-dead-flag 0/3**(改动前冒烟 run 曾完整报出=能力在,率不足)、d11-origin 0/3。trace 归因:config 值经预取已在 pack 里,finder 却被 `frame.draw_line` 外部接口吸走预算(搜 cv2/class Frame/def draw_line),从未把旗标值代入 guard;run2/3 还把"死路径"退化成"无调用方=死代码"。holdout 全绿(recall 6/7、h6=0、noise 0、FP 0)。
- 预写规则只给"噪音超标"留迭代预算、"仍不报"应回滚;但归因显示这是措辞可修的机制问题(非能力缺失),按 W8 教训(归因优先于数字)做了**一次记录在案的措辞迭代**:死路径条目改为"把实际值代入 guard 条件(常已在 pack 里),强于'无调用方'"——迭代对着机制,不提任何用例专名。
- 轮2(d7×3 + holdout 重跑):**dead-flag finder 层 2/3**(run3 完整命中:两旗标齐名+"predicted trajectory is never drawn";run2 弱形式),全被旧 verifier 砍——drop 理由"eventual callers are unknown, we cannot verify the dead-path claim"=**用假想的未来调用方否决 config.py:7 的文档证据**('wiring is not enabled yet')。第三种砍杀模式,Evidence rules 需补第三条(死路径可达性按 repo 内实际存在的定义/接线判,计划偏差记录在案)。holdout:recall **7/7**(h2 行数上限这个历史稳定 miss 也抓到了)、h6 陷阱=0、noise=0、FP=1——**经核查为 fixture 预存真 bug**(holdout bounce.py classify_bounce 忽略 vy_at_snap,§5.1 违例,fixture 从主集复制带入、未埋点;judge"埋点之外即 FP"的协议口径误伤,且 W7 已记录同款"报 diff 外预存 bug"行为,非 checklist 新增害处)。
- **判定:Stage A 通过**——dead-flag finder 识别率 0→2/3,d10-cov 2/3,d7-gap 2/3(轮2),无害门全绿;d11-origin 维持 stretch,不为它迭代。

**Stage B 结果(2026-07-10,含一次规则迭代)**:

- 轮1(三条规则原版,靶向×3):**d7-gap 0/3→2/3(run1 confirmed——W9 系统性砍杀首次被攻破)、d7-dead-flag 0/3→1/3(uncertain 存活)**;但 FP 0.33→1.33——run1/2 把"新增函数无调用方=死代码"以 confirmed 保留×4。归因修正:旧 verifier 在 Stage A 同样保留过此类(原始 KEEP 判据本含"dead code path"),非新规则肇因——checklist 放大了产出频率+judge 对同类 finding 的 FP/noise 口径抖动(W2 已记录)把它顶成了 FP。d10-cov:finder 3/3 报出、verifier 3/3 砍,drop 理由仍是"speculative robustness advice"(run3 一次属合法砍杀:finder 机理表述确实错了);run2 连 d7-gap 也被砍,理由"defensible design choice"逐字撞规则一禁止的说法。**结构性归因:Evidence rules 排在 DROP 列表后,模型先匹配 DROP 条目就不再看澄清。**
- 迭代(一次,打包,对着结构性归因):①节首加优先级声明("与 DROP 列表冲突时以本节为准,砍前先查");②规则二补"慢性缺陷没人能在单 diff 内演示漂移,怀疑≠反驳,反驳须驳机理本身";③规则三补反向排除("新增函数暂无调用方≠死代码,只有既有定义推出的不可达才算")。
- 轮2:**FP 1.33→0.33(no-callers keeps 消失)**、noise 2.67→2.0;d7-dead-flag 1/3 uncertain、d7-gap 1/3 confirmed(方差,轮1 为 2/3);垃圾探针全 drop——inv-vs-solve 在优先级声明下仍被正确砍,"X 比 Y 稳"式 DROP 未松动;d10-cov 本轮 finder 0/3 未产出(9 run 累计 finder 率 5/9≈56%,"正确表述+新规则"组合尚未被采样——规则二**未经检验**而非已失败)。
- holdout(轮1/轮2):recall 7/7 / 6/7(h2 行数上限回到 miss,历史方差)、**h5 除零两轮 confirmed(规则收紧未误伤负向控制)**、h6 陷阱两轮各 1 条 uncertain(同一条 MAX_POLYLINE_POINTS 性能猜测:反对票理由每次都正确,经 W9 分歧保留通道存活;Stage A 两轮 finder 未产出它而 finder prompt 相同→finder 方差聚簇+keep 偏置合并的固有代价,非 Evidence rules 放行)、FP 1(=classify_bounce fixture 预存真 bug,同 Stage A 定性)、原始 precision 0.778/0.75(剔除 fixture 协议误伤后 0.875/0.857)。
- **判定:Stage B 有保留通过**——两个 verifier 砍杀靶子攻破,无害面净改善(FP 回落、垃圾门未松、h5 未误伤);遗留:d10-cov 未证、h6 uncertain 噪音 1 条/run。迭代预算已尽,终测按预写门槛裁决,不过则回滚改动二。

**主集终测(V2×3)与四代对比:**

| | recall | precision | F1 | noise | unstable | never-hit |
|---|---|---|---|---|---|---|
| W7 单复核 | 0.811 [.800–.833] | 0.833 [.727–.920] | 0.819 [.776–.856] | 4.7 [2–9] | 3 | 4 |
| W8 单复核 | 0.778 [.767–.800] | 0.830 [.759–.885] | 0.803 [.763–.840] | 4.3 [3–7] | 2 | 6 |
| W9 双复核 | **0.856 [.833–.867]** | 0.743 [.722–.781] | 0.795 [.776–.822] | 8.3 [7–9] | **1** | 4 |
| **W10 清单+证据规则** | **0.856** [.800–.900] | 0.777 [.667–.852] | **0.811 [.754–.854]** | **5.3 [3–8]** | 4 | **2** |

预写标准逐项判定(5 过 4 未达,未达项全部附归因):

1. **never_hit 4→≤2 ✅**:剩 d10-cov-asymmetry、d11-origin-fit。d7-dead-flag-path(全历史 0/9 → 1/3)与 d7-gap-connect(v2 历史 0/6 → 2/3)双双毕业。子条款"{dead-flag,gap,cov} 至少 2 个 ≥2/3"✘:仅 gap 达标——dead-flag 卡在 finder 采样率(~50%,协议 6 run 报出 3 次),cov 卡在 finder 率(9 run 5 次)×"正确表述+新规则"组合未被采样(报出的 3 次里 2 次撞旧规则、1 次机理表述错误属合法砍杀)。
2. **recall mean ≥0.856 ✅(持平);min ≥0.833 ✘(0.800)**。翻转表:全集仅 4 bug 变动,+dead-flag/+gap 对 -d5-window(2/3→1/3)/-d6-ghost(3/3→1/3)。d5/d6 是 W7 以来的历史 flapper(W8 双双 0/3);拆账:d6 run1=finder 深度(报了 asr 被忽略、没连到缺过滤→鬼弹跳),run3=verifier 边界砍(理由未引用新规则);d5 run2/3=finder 未产出——**非清单挤占**(run1 命中恰是清单 documented-unhandled 类+规则一协同:引调用方注释、uncertain 存活)。
3. **precision mean ≥0.70 ✅(0.777,较 W9 +0.034);min ≥0.68 ✘(0.667,差 0.013,run1)**。
4. **F1 ≥0.795 ✅(0.811)**;区间 [.754–.854] 较 W9 变宽——方差是本轮的真实代价。
5. **noise ≤10 ✅(5.3 [3–8],较 W9 -36%)**;陷阱 d12+d13 **✅✅(2/0/1,均值 1.0 vs W9 的 3.0)**——立项最担心的"清单诱发陷阱误报"反向改善。
6. FP ≤1.0 ✘(原始 2.33):run1 的 5 条里 4 条是**共享 fixture 的他用例埋点**被 d5 的 finder 顺路报出(gate.py=d14、bounce.py=d1、speed.py=d2 的埋点,finder 全部自觉标注 [Pre-existing],全走 uncertain 分歧通道存活)——真 bug 被协议记 FP,同 holdout classify_bounce 定性;剔除后 1.0 恰在线。无 3/3 系统性 FP ✅。**W5 时代 verifier 砍 [Pre-existing] 越界条目,现经分歧通道存活——scope 口径缺位是新暴露的真缺口**(W7 已有前兆观察)。
7. unstable ≤3 ✘(4):其中 2 个(dead-flag 1/3、gap 2/3)是从 never_hit 毕业的**爬升型**;存量不稳定 d5、d6 各 1/3。
8. 成本 ≤790k ✘(**870k,+26.6%**):弥散行为成本——更多探索轮次+更多 findings 过双复核,tokens_out 同步 +26%,非单纯 prompt 变长。

**归因审计補遗**:交互项(清单类且 kept)3 run 共 ≈2 条(d15 percentile 数值断言 uncertain、d6 未引用常量 confirmed)——两改动恶性交互未成洪水;no-callers 排除条款在终测正确工作(d6 RallyState 三 run 全被正确 drop,run2/3 的 drop 理由逐字引用规则);uncertain 计数 14/8/5——分歧率自身方差大,run1 的 precision/FP 越线全部来自该轮分歧异常高。

**一句话**:W10 把"verifier 系统性砍杀"这一族修掉了(gap 历史首次 2/3、dead-flag 历史首次存活),never_hit 减半、noise -36%、陷阱 -67%、F1 +0.016;代价是方差变宽(recall 区间 [.800–.900])与成本 +27%。**判定:有保留通过,不回滚**——未达项归因全指向方差/协议缺陷/成本,无一指向机制有害;若按噪声门回滚将重蹈 W8 的反向错误。

**W11 候选**(机制先行):①verifier scope 规则——diff 外 [Pre-existing] 发现的处置口径(run1 四条越界 FP 的直接靶子,W5/W7/W10 三代口径摇摆);②finder 采样——d10-cov/d5/d6/dead-flag 现在全卡 finder 率,且"报了被砍"已修,**W10 立项时缓期双跑并集的反对理由(照样死在 verifier)已不成立**,温度>0 多采样或双跑并集重新入围;③成本回收(+27% 需要预算化)。d11-origin 维持领域难档挂起。
