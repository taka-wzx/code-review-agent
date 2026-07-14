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

## W11(2026-07-10):成本优先——兔子洞刹车 + 计量固化 + 预算纪律

用户定 W11 范围=成本优先,新机制(scope 规则/双跑并集)缓期 W12+。**离线归因先行**(W9/W10 全量 trace 对比,零 LLM 成本):

- +183k/轮中 finder 占 +128k(70%):calls 73→90、步深 4.58→5.62、单 call 3.63k→4.38k(额外 calls ≈61k + 单 call 增长 ≈67k,其中清单 prompt ≈18k、其余为深轮次会话重放)。verifier +55k 全是单 call 增量(证据规则×102 calls+更长 findings),calls 持平——**验收过的措辞不为省 token 改写(W8 教训),不动**。
- **兔子洞是长期洼地非 W10 新增**:finder not-found 搜索 W9 71/run、W10 79/run,3+ 连败变体链两代都 ~12.7 条/run(链内 calls 29→33)。样本:d7 的 draw_line→.draw_line→def draw_line→cv2→class.*Frame。

**改动**:T1 工具层连败刹车(ToolSession 会话级计数,search_repo 连续 3 次 clean miss 起在结果尾注入 nudge:"一次未命中已证不存在,缺失本身可报告,勿再试变体";regex 用法错误类 miss 不计数——工具自己让它重试一次纯文本,合法;命中或其他工具成功即清零;prompt 零改动)。T2 cost_report 固化归因能力(repeat 根聚合/--baseline 对比/连败链计数)。T3 预算规则:**机制类改动 tokens_in 预算 ≤+10%/W,超出需当轮回收或明示豁免**。

**预写验收(靶向为主;成本专项不跑 2M token 全量终测,全量口径留给 W12 重新基线化,记录在案)**:

1. 靶向 trio×3(vs w10b2):tokens_in 降 ≥10%;行为无回退——d7-gap judge 层 ≥1/3、dead-flag finder 层 ≥1/3、noise ≤基线+1、FP ≤基线。
2. d16 探针×2:d16-missing-module 2/2 命中、诚实无编造——**d16 正是靠"搜不到"下结论的用例,刹车不得误伤缺失检测**(nudge 措辞已内置"缺失可报告")。
3. holdout×1:recall ≥6/7、h5 kept、h6 ≤1 uncertain、noise ≤2、FP ≤1(classify_bounce fixture 伪影)。
4. 失败处置:行为回退→只回滚 T1(T2/T3 零风险保留),nudge 措辞迭代预算 1 次。

T1 本地自测 7 项全过(3 连败触发/计数递增/命中清零/regex miss 不计/其他工具成功清零/repeat 短路不受扰);trace 事件补显式 miss/miss_streak 字段(旧 trace 用 result_chars 启发,W11 起精确)。

**验收结果(2026-07-10)**:

1. **行为门全过**:d16 探针 2/2 confirmed、表述诚实零编造(刹车未误伤缺失检测);holdout recall 6/7(唯一 miss 仍是 h2 行数上限)、h5 2/2、**h6 陷阱 0 条**(W10-B 的性能猜测未复现,佐证其方差定性)、FP 0、noise 1;trio 无回退——dead-flag finder 层 **1/3→3/3**、gap 3/3,judge 层持平(1/3、2/3),noise 2.33、FP 0.33 均在线。
2. **机制指标达成**:连败链 calls 三组件一致下降——finder 10.3→9.0、verifierA 4.3→2.3、verifierB 5.0→3.3(合计 **-27%~-46%**;ToolSession 共享,verifier 侧同获刹车,全量口径链存量 ~83 calls/run)。
3. **成本门 ✗,记为门槛设计失误**:trio tokens_in +3.3%(163.6k vs 158.4k)而非 -10%。归因:①效应量(全量估 -4~7%)在 3-diff 切片 σ±16%(历史 130-183k)下不可分辨——**预写门槛在该切片上统计不可达成,W8 的 n=1 教训在门槛设计侧重演**;②读数被收益推高——d7 finder 32.2k→47.6k 正是因为三轮全执行了旗标核查(dead-flag 3/3 的代价),小切片上成本与收益读数纠缠。真实全量成本效果留待 W12 重新基线化时 `cost_report --baseline` 一条命令校验(记录在案)。
4. **判定:T1 保留**——无害已证+机制指标一致下降;回滚一个行为无害、机制有效的改动去满足统计不可达成的门,是 W8 教训的反向重演。T2 复现 W9→W10 归因表逐位一致(finder +128,109/总 +183,281=+26.7%)后固化。

**预算规则(T3,即日生效)**:机制类改动 tokens_in 预算 **≤+10%/W**(全量 V2×3 口径),超出需当轮专项回收或在 cases.md 明示豁免及理由;每轮 W 终测必须跑 `cost_report --baseline <上代 results_repeat>` 出对比表入档。成本门槛今后只在全量口径上设定,靶向切片只看机制指标(如链 calls)。

## W12(2026-07-11):finder 双跑并集 + 文件级 scope 口径——对着采样层缺口与三代 FP 摇摆

**重新基线化(T0,W12 代码落地前跑,commit f263e70)**:

- 质量面:recall 0.845 [.800–.867]、precision 0.787 [.694–.885]、F1 0.811 [.771–.840]、FP 0.3 [0–1]、noise 6.7 [3–10];unstable:d4-magic 2/3、d7-gap 2/3;never_hit:d10-cov、d11-origin、**d5-window、d7-dead-flag**——后两个从 W10 的间歇命中(1/3)恶化为 0/3,采样层缺口坐实,双跑立项前提成立。陷阱 d12+d13 kept 1/3/1(均值 1.67)。
- **W11 悬置的成本裁决(√)**:tokens_in 870.0k→**795.0k(-8.6%)**、tokens_out -10.4%;3+连败链 calls finder 35.7→6.3、verifierA 21.7→2.0、verifierB 25.7→2.0(**合计 -83%**)。W11 切片门"+3.3% 不达 -10%"确系门槛设计失误——刹车在全量口径效果显著且三组件一致。
- 事故记录:首次基线跑中途发现被并行的 W12 开发代码污染(进程管理事故:被停止的 repeat_eval 进程树实际存活,重新拉起的子进程从磁盘读到了半成品新代码;run2/3 trace 含 finder2 事件,作废重跑;run1 的 16 个结果文件经三重核查——无 oos 字段、无 finder2 事件、每 trace 恰一个 review 事件——为纯 W11 产物,保留重评)。防复发:**基线/对照一律从 `git archive` 冻结副本执行**,开发树与评测树物理隔离。

**设计**(四取舍均为用户预先裁决):

1. **双跑**:run1 保持 temperature=0(锚点,失败照旧致命、错误文案不变);run2 temperature=0.7(采样多样性),任何失败 fail-open 降级为仅锚点(镜像 verifier 的降级语义,采样跑永远不能弄坏 review)。
2. **结构化去重**(findings.py 纯函数,零额外 LLM):issue 文本 token-set Jaccard 双档判定——jac ≥.40 任意行距,或 jac ≥.25 且 |Δ行|≤4;阈值实测自 results_repeat_w10 跨 run 对(同 bug 对 .26–.67 @ Δ0–7,异 bug 同行对 ≤.22),宁漏合不错合(错合只可能吞 run2 副本,锚点发现永不被动)。残余重复靠 verifier 既有 duplicate→DROP 规则兜底(冒烟已证:4 条实际重复结构层合 1、verifier 兜 3)。run2 新增项打 `origin:"finder2"` 溯源键供归因审计。
3. **文件级 scope 代码判定**:并集后、验证前,file 不在 diff 改动文件集(context.parse_diff)内的发现降级 `out_of_scope_findings`——不验证(其 verdict 三代都是抛硬币,这正是要消除的摇摆)、不计 precision。行级 scope 被否(同 bug 行号跨 run 漂移 ±1–7,合法发现常引 hunk 内上下文行);verifier prompt scope 条款被否(scope 是确定性集合成员问题,摇摆恰因让 LLM 判;VERIFIER_SYSTEM 一字不动,TestVerifierGolden 零改动通过=反向证明)。d16 安全性已验:缺失模块发现三 run 全引改动文件本身,文件级 scope 不误伤,unit test 钉死该模式。
4. **成本豁免(本条即记录)**:全额双跑,预估 tokens_in +50~70%,超出 ≤+10%/W 预算——W12 定性为召回主题周,明示豁免;实际增幅照常 `cost_report --baseline results_repeat_w11` 入档,scope 降级免验证部分回收 verifier 成本。

**协议变更(显式标注,自 W12 起)**:precision 分母不再包含被降级的 diff 外发现(judge 侧 `n_out_of_scope` 单列入档)——W12 的 precision 与 W10/W11 不直接可比;比较时同时看 oos 列。共享 fixture 真 bug 被记 FP 的历史协议缺陷同步消除。

**预写验收(跑之前落笔,锚定 T0=results_repeat_w11)**:

1. **定向切片**:trio(d5,d6,d7)×3 + d10×3——d5-window/d6-ghost judge 层各 ≥2/3;d7-dead-flag finder 层 ≥2/3、judge 层 ≥1/3;d10-cov finder 层 ≥2/3(单跑率 ~56%→双跑期望 ~80%);每 diff 总产出(kept+dropped+oos)≤ T0 同 diff 均值 +4(d5≤8.0、d6≤8.0、d7≤6.7、d10≤10.0);run2 须至少贡献 1 条并集新发现(查 origin);d16×2:2/2 命中、零编造、缺失模块发现 in-scope。
2. **全量 V2×3**(results_repeat_w12,`cost_report --baseline results_repeat_w11`):recall mean ≥0.865(T0+0.02)且 min ≥0.833;FP mean ≤1.0 且 d5 型越界移入 oos(oos 预期在受影响轮 ≥2);noise mean ≤8.7(T0+2);陷阱 d12+d13 kept 均值 ≤1.67;never_hit 不增(d11 挂起豁免);F1 mean ≥0.801(注明换分母)。成本无 ≤+10% 门(豁免见上),如实记录。
3. **holdout**(仅验收):recall ≥6/7、h5 kept、h6 ≤1 uncertain 且 0 confirmed FP;classify_bounce fixture 伪影应现身 out_of_scope_findings 而非 FP。
4. **失败处置(预写)**:recall 未达→先按 origin 标签归因 run2 实际贡献,再考虑动阈值;precision/noise 爆→先查 run2-only 发现在 uncertain 通道的存活占比(备用杠杆:origin=finder2 须 2/2 confirmed 才保留——本周不实现,只记录);疑似错合→只调 sim_far/sim_near,迭代预算 1 次。

**定向切片结果(2026-07-11,commit 08cc291)**:

- **采样层目标全数攻克(finder 层)**:d5-window-deadzone judge 层 **0/3→2/3**(唯一 miss 轮属方差)、d6-ghost 3/3;**d7-dead-flag finder 层 0/3→3/3、d10-cov finder 层 ~56%→3/3**——双跑每轮都把两者送进 verifier(dead-flag 常常 run1+run2 各报一条,cov 的 Joseph form 三轮齐报)。run2 并集贡献遍布(14 个切片 13 个 ≥1 条新发现;d6 run2 一条 finder2 发现以 kept 存活)。产出量均值全部在线(d5 5.0/d6 5.3/d7 6.7/d10 8.7 vs 门 8.0/8.0/6.7/10.0,d7 压线)。d16 探针 2/2、零编造、缺失模块发现 in-scope(oos=0)——文件级 scope 与刹车均未误伤缺失检测。scope 降级实战首秀:d5/d6 各轮 0–1 条越界发现落 oos(gate.py/speed.py 他用例埋点,正是 W10 终测的 FP 来源)。
- **dead-flag/cov judge 层 0/3 ✘——"报了被砍"回归,归因至 verifier 证据规则不触发**:两者三轮全部 2/2 砍。drop 理由逐字复现三代砍杀话术:dead-flag run2"no call sites … depends on hypothesizing a future caller"(撞规则三明令禁止的推理)、run1/run3"documented in config comments = intentional design / live kill-switch"(撞规则一);cov 三轮"generic numerical best-practice, no concrete failure identified"(撞规则二"怀疑≠反驳,须驳机理本身")。两 pass 一致砍,分歧兜底不触发。**定性:W12 机制(采样层)达成立项目标;残余瓶颈换层至 verifier 规则执行率,与 W10 Stage B 轮2 的"规则未被采样"同源——verifier prompt 本周界定不动(TestVerifierGolden 零改动=反向证明),记 W13 候选:证据规则触发率(优先级声明已存在,考虑规则内嵌反例措辞或 DROP 条目交叉引用)。**
- 切片判定:**通过(带 dead-flag judge 层保留)**,按预写处置不动阈值、不迭代 prompt,进入全量终测。

**主集终测(V2×3,2026-07-12)与五代对比:**

| | recall | precision | F1 | noise | FP | unstable | never-hit |
|---|---|---|---|---|---|---|---|
| W9 双复核 | 0.856 [.833–.867] | 0.743 [.722–.781] | 0.795 [.776–.822] | 8.3 [7–9] | — | 1 | 4 |
| W10 清单+证据规则 | 0.856 [.800–.900] | 0.777 [.667–.852] | 0.811 [.754–.854] | 5.3 [3–8] | 2.3 | 4 | 2 |
| W11 重基线(T0) | 0.845 [.800–.867] | 0.787 [.694–.885] | 0.811 [.771–.840] | 6.7 [3–10] | 0.3 | 2 | 4 |
| **W12 双跑+scope** | **0.900 [.867–.933]** | 0.770 [.757–.788]* | **0.830 [.813–.840]** | 8.0 [7–9] | **0.0 [0–0]** | 2 | **2** |

\* W12 起 precision 换分母(oos 移出),与前代不直接可比;oos 5.3 [2–9] 条/轮单列。

**预写闸门逐项判定(7/7 全过)**:recall mean 0.900 ≥0.865 ✅、min 0.867 ≥0.833 ✅;FP 0.0 ≤1.0 ✅(scope 降级后三轮全 0——W10 终测 run1 四条越界 FP 这一族整体消失,d5 型越界稳定现身 oos);noise 8.0 ≤8.7 ✅;陷阱 d12+d13 kept 1/0/2(均值 1.0 ≤1.67)✅;never_hit 4→2 不增 ✅(**d5-window 毕业至 2/3、d7-dead-flag 历史首次 judge 层存活 1/3**;剩 d10-cov、d11-origin);F1 0.830 ≥0.801 ✅ 且区间 [.813–.840] 为五代最窄。

**成本(豁免周,如实入档)**:tokens_in 795.0k→**1348.8k(+69.7%)**,在预估 +50~70% 包络顶格。构成:finder2 +407k(与 finder 几乎等重:calls 90 vs 92、步深 5.62 vs 5.75——采样跑并未更浅);verifier +102k(并集变大:A/B calls 各 +7.3/+5.3);oos 免验证的回收被并集增量吞没。连败链 calls 维持低位(finder 8.0/finder2 8.3 vs W10 时代 35.7)——W11 刹车在温度 0.7 下依然有效。

**origin 归因审计(必做项)**:finder2 judge 层直接产出=d5-window@run2(**独立命中,立项头号靶**)+d11-ukf-vz@run2、d15-writer-leak@run3(协同);其余增益(d4-magic、d7-gap 归稳,d5@run1、dead-flag@run2 经锚点跑命中)为锚点侧方差红利——temp=0 的 provider 非确定性本轮站在收益侧,不据此夸大机制贡献,机制的硬证据在 finder 层(切片 dead-flag/cov 0/3→3/3)。run2 kept 中 6 条 finder2 发现全走 uncertain 通道(风险清单预判命中),noise 仍在门内,"origin=finder2 须 2/2"备用杠杆未启用、维持记录在案。

**holdout(仅验收,×1)**:recall **7/7**(h2 行数上限这个历史 flapper 也命中)、precision 8/8、FP 0、noise 0;h5 除零 kept(负向控制连续三代未误伤);**h6 陷阱 kept=0**(优于 ≤1 门)——W10-B 的 MAX_POLYLINE_POINTS 性能猜测本轮由 finder2 报出、被 scope 降级接走落 oos,不再走 uncertain 通道;**classify_bounce fixture 伪影如闸门预期现身 out_of_scope_findings**(h4,finder2 报出)——三代协议 FP 摇摆在设计好的通道里终结。

**一句话**:W12 把"finder 采样层"这一族修掉了(d5 毕业、dead-flag 历史首次 judge 层存活、切片 finder 层 dead-flag/cov 双双 0/3→3/3),文件级 scope 把三代摇摆的越界发现引入专用通道(主集 FP 三轮全 0、holdout 双伪影落 oos),recall 0.845→**0.900**、F1 0.811→**0.830** 且区间五代最窄;代价是成本 +69.7%(豁免入档)与 verifier 砍杀模式在 dead-flag/cov 上的回归暴露(证据规则不触发,W13 头号候选)。**判定:通过,不回滚。**

**W13 候选**(机制先行):①verifier 证据规则触发率——dead-flag/cov 现在 finder 层 3/3 但 2/2 被砍,drop 理由逐字复现规则明令禁止的话术(优先级声明在但不生效;考虑规则内嵌反例或 DROP 条目处交叉引用);②成本回收——双跑 +69.7% 需预算化消化(候选:run2 步预算减半/共享锚点会话前缀/条件二跑);③uncertain 通道容量——run 间 oos 2–9 与 uncertain 波动仍是 precision 方差主源。d11-origin 维持领域难档挂起。

## W13(2026-07-12):verifier 回放台架 + 砍杀台架 + 规则触发率哨兵

**立项**(用户定三取舍):范围=回放台架+规则触发率(成本回收留 W14);杠杆=代码级 drop-reason 哨兵先行(prompt 零改动)+ ≤1 次预算内 prompt 迭代兜底;验收口径=**配对回放×3 主判 + 全量 V2×1 理智检查 + holdout×1**——verifier-only 周不再必跑全量×3。

**核心洞察(评测成本结构性修复)**:result JSON 的 findings+dropped_findings 拼回即 verifier 完整候选列表,build_context 确定性可重建 finder 输入——verifier 改动可回放存盘 finder 输出(省 ~60% token),且配对比较(两变体看相同候选)把 finder 方差从对比中剔除。分层金字塔:sweep(0 token,亚秒)→ 砍杀台架(~0.3M/轮)→ 配对回放×3(~2.0M)→ 全量×1+holdout(仅验收)。迭代单元 1.55M→0/0.3M(-80%);A/B 对比 2.0M vs 等效 live 双变体 ~9.3M(-78%)。

**设计**:①哨兵 `rescue_forbidden_drops`(verifier.py 纯函数,post-merge 与 degraded 两分支接线):2/2 drop 且理由命中禁止话术模式→降级 uncertain(dissent_reason 带 `[sentinel:<tag>]` 前缀,机器可剥),模式派生自规则原文的合取(情态动词式 future-caller × dead-path issue;generic-best-practice × 具名不变量 issue),重复守卫永不救;已对 W12 全部 139 条已录 drop 理由离线验证:恰命中 4 条目标(d7r1/r3 dead-flag、d10r1/r2 cov)、守卫拦 1、其余 0(d16-fsum 带机理反驳的合法 drop、d11-div0 合法反驳、裸"future caller"反向规则 drop 均不触发)。②回放台架 replay_verifier.py:单次 live 执行产 B 视图,A 视图由 rescue 标记降回派生(配对由构造保证);--sweep 纯离线内环。③砍杀台架 bench_verifier.py + eval/bench_verifier.json(10 case 冻结自 W12 真实文件,含 5 个负控位,确定性断言无 LLM judge)。④顺带:prompt-cache 计量(trace 条件字段+cost_report cache% 列,喂 W14)。已知边界:d10-cov run3 与 d11-origin 为 finder 侧未产出,verifier 改动救不了,闸门按构造豁免。

**预写闸门(跑之前落笔)**:

- **G-sweep(确定性)**:对 results_repeat_w12 恰救 {d7r1,d7r3,d10r1-cov,d10r2-cov},守卫拦 d7r1 finder2 副本,139 条其余 0。
- **G-bench**:10 case 一轮内全部 survives(dead-flag×3、cov×2)+ 全部负控(never_rescued/stays_dropped)通过;哨兵模式迭代 ≤2 次(每次先过 G-sweep);仍不过→触发预写 prompt 兜底(证据规则导言加自检句),一轮验证。
- **G-replay(主判据,写在逐 bug 配对表上)**:dead-flag ≥2/3 B run 命中(W12 1/3);cov 在有候选的 2/2 B run 命中;配对无回退(A 命中的 bug B 无一丢失);FP 全 6 dir=0;每 run noise_B ≤ noise_A+2 且 mean ≤10;precision_B ≥ precision_A−0.03。
- **G-full(全量×1)**:recall ≥0.867(W12 min);FP=0;noise ≤10;tokens_in ≤ W12 均值 +2%(哨兵零新增调用);candidate_findings 全员在场;sentinel_rescue 事件逐条人工核对;finder 未报出 dead-flag/cov 不判负(归因记账)。
- **G-holdout**:recall ≥6/7、FP=0、h6 kept=0;holdout 上每条救援逐条审计,救出 FP 即判负→收紧模式(重过 G-sweep)或 revert;兜底 prompt 若曾触发且 holdout 回退,按 W8 协议回滚。

**验收结果(2026-07-12,commit 5fca0e9)**:

- **哨兵两次预算内迭代**(均先过 G-sweep 再跑台架,VERIFIER_SYSTEM 一字未动、兜底 prompt 未动用):iter1(T3 设计期)issue 门加"loss 动词"——`_INVARIANT_VOCAB_RE` 单独会误救 d10 的 inv-vs-solve 负控(issue 只把"positive definite"当上下文而非声明其丢失);iter2(bench round1 后)——round1 暴露两种禁止话术的新措辞(numeric "speculative robustness / no concrete defect"、dead-path "intentional scaffolding / not wired yet"),两 reason 模式加宽,同时把 dead-path 的 issue 门**从松散的 dead/flag 收紧为"具名 ALL-CAPS 常量=布尔的 config 禁用条件"**——靠 issue 门把纯 no-callers 的合法 drop("new code gets wired up later")挡在外面。归因先行:两个 round1 失败经诊断脚本抓 live drop 理由确认均为"pattern-too-narrow"(禁止话术漏配)而非新的合法 drop,故迭代而非回滚。
- **G-sweep ✅**:对 W12 全部 139 条已录 drop,恰救 4 条(d7r1/r3 dead-flag、d10r1/r2 cov)+ 守卫拦 1(d7r1 config 禁用死路径的 duplicate 副本),其余 0 误触。
- **G-bench ✅ 24/24**(~0.34M/轮):dead-flag×3、cov×2 目标 survives,5 个负控(no-callers、inv-vs-solve、fsum 累加、div-zero 合法反驳、trap)全部 never_rescued/stays_dropped。
- **G-replay(主判据)✅ 全五项**:配对回放×3 把 finder 方差从对比剔除后,哨兵净效应清晰:

| | A(哨兵关) | B(哨兵开) | 逐 run 哨兵救援(=A 漏的 bug) |
|---|---|---|---|
| recall | 0.889 [.833–.933] | **0.922 [.867–.967]** | run1/2=d10-cov,run3=dead-flag |
| precision | 0.765 | **0.772**(不降反升) | 每轮恰 1 次,全部命中真 bug |
| F1 | 0.822 | **0.840** | |
| FP | 0 [0–0] | 0 [0–0] | 配对零回退(A 命中 B 无一丢) |
| noise | 8.0 [7–9] | 8.0 [7–9] | 与 A 完全持平 |

  逐 bug:**d10-cov-asymmetry(W12 全灭 0/3)在有候选的 2/2 B run 被救活**(run3 finder 未产出该候选,候选层核实=finder 侧 miss,按构造豁免);**d7-dead-flag 在 B 3/3 命中**(W12 1/3)。哨兵只在 verifier 砍掉时兜底、verifier 自己保住时不动(run1/2 的 dead-flag 即 verifier 自保)——救回的是真 bug 命中而非噪音,故 noise 持平、precision 反升。
- **G-holdout ✅**:recall **7/7**、FP 0、h6 陷阱 kept=0(两条入 oos);**holdout 上哨兵零触发**——分布外数据无过度触发、未制造 FP。
- **G-full(全量×1 漂移检查)有保留通过**:recall 0.867(=W12 min 地板)、noise 5.0、candidate_findings 16/16 在场、哨兵恰 1 救(d7 dead-flag,正确)。两条字面未达但**归因均指向 W13 无关的单run finder/verifier 方差**:①FP=1 是 d3 的 `f.ball` finder 幻觉(`rescue=-`,verifier 误确认)——哨兵只增不减、candidate_findings 惰性,这条 FP 在 pre-W13 同 finder 输出下会一字不差出现;②tokens_in +4.9%(vs 三run均值)全是 finder 探索方差(步深 6.12 vs 5.75),哨兵/持久化按构造零 API 调用。d10-cov 本run 候选层缺失=finder 侧 miss(闸门豁免);d5/d6 单run flapper 方差(T6 配对里正常)。附:**T1 cache% 生效,全量 90% 命中**——原始 tokens_in 高估真实计费约一个量级,W14 成本回收基线到位。

**判定:W13 通过,不回滚。** 主判据 G-replay 把 finder 方差剔除后给出干净净效应(B recall +0.033、F1 +0.018、零 FP、noise 持平、每轮恰 1 救且命中真 bug);G-sweep/G-bench/G-holdout 三道无害门全绿;G-full 两条字面未达项归因全指向 W13 无关方差,机制本身(哨兵零 LLM 调用、纯后处理)不可能是肇因。**d10-cov-asymmetry(三代 0/x 的领域难档之一)首次被机制救活**,dead-flag judge 层 1/3→3/3。VERIFIER_SYSTEM 一字未动、兜底 prompt 未动用——纯代码级 drop-reason 哨兵即达标。

**评测基建产出(本周真正的杠杆)**:分层验收金字塔成文并首次跑通——

| 层 | 工具 | 成本/轮 | 用途 |
|---|---|---|---|
| 0 | `python -m unittest`(66 测试) | 0(秒级) | 纯函数/协议契约 |
| 1 | `replay_verifier.py --sweep` | 0(亚秒) | 哨兵模式对全部已录 drop 的确定性内环 |
| 2 | `bench_verifier.py run`(10 case) | ~0.34M | live 砍杀台架,确定性断言无 LLM judge |
| 3 | `replay_verifier.py --judge`(配对 A/B×3) | ~2.0M | verifier 改动主判据,finder 方差剔除 |
| 4 | 全量 V2×1 + holdout | ~2.3M | 漂移理智检查(仅验收) |

**协议变更**:verifier-only 周不再必跑全量 V2×3;主判据下沉到配对回放(层 3),迭代内环在层 1/2(-80%/次)。**finder 改动周仍回全量 V2×3**(回放冻结的是 finder 措辞,finder 变了台架/回放需重 build)。W13 全周实耗 ~5.4M(含台架 2 轮),但迭代单元从 1.55M(全量×1)降到 0/0.34M。

**W14 候选**:①双跑成本回收(W12 遗留 +69.7%;cache 命中 90% 意味真实计费远低于原始 token,先用 T1 计量重估真实成本再决定是否需要 run2 步预算减半/条件二跑);②d5/d6 finder flapper 稳定化(本周 T7 单run 双漏,采样层仍有余量);③哨兵推广观察——目前仅两族(dead-path/numeric),W14 视 live 数据看是否有第三族禁止话术。d10-cov 的 finder 产出率(本run 缺失)与 d11-origin 维持挂起。

## W14(2026-07-13):双跑成本裁决(关闭)→ d5/d6 flapper 稳定化

**立项(用户定两取舍,2026-07-13)**:①T0 判"真实计费不构成问题"后本周主体切 **d5/d6 flapper 稳定化**(finder 改动周协议:全量 V2×3 验收);②预写关闭阈值 = **全量单轮真实计费 ≤ ¥2**(16 diffs,双跑+verifier,in+out 合计,judge 未 trace 除外并注明)。关键分析(立项时预判):各组件 cache% 若均匀,双跑的相对增幅在真实计费口径下仍是 ~+70%,只有绝对金额缩小——裁决变量是绝对材料性。

**T1(cost_report 缓存感知计价,commit 10055df)**:`--price-hit` 单列 cache-hit 输入单价;逐事件 `miss_in = tokens_in - cache_hit`,无 cache 字段的旧 trace 全按 miss 计(保守上界);真实成本列进 print_summary/逐组件/--baseline 差值;`billed_cost` 纯函数 + 7 个零 API 单测(73/73 全绿)。

**T2(T0 备忘录,零 LLM,数据源 results_repeat_w13/v2_run1)**:

价目(deepseek-v4-pro,USD/1M,官方 pricing 页 2026-07-13 拉取):hit **$0.003625**、miss $0.435、out $0.87——**hit 价是 miss 的 1/120**,比 V3 时代的 1/10 深一个量级。汇率按 7.15 折 CNY(页面为 USD 计价,汇率波动 ±2% 不改变裁决)。

| 组件 | tok_in | hit% | tok_out | USD | CNY |
|---|---|---|---|---|---|
| finder | 422,140 | 92.9% | 53,428 | 0.0609 | 0.435 |
| finder2 | 415,527 | 92.2% | 52,923 | 0.0616 | 0.440 |
| verifierA | 256,235 | 86.1% | 40,977 | 0.0519 | 0.371 |
| verifierB | 321,256 | 86.5% | 53,277 | 0.0662 | 0.473 |
| **TOTAL** | **1,415,158** | **90.0%** | **200,605** | **$0.2406** | **¥1.72** |

必报变量:**¥/全量轮 = 1.72**;¥/单次 review = 0.11 均值(min 0.06 / max 0.19);**finder2 边际份额 = 25.6%**(原始 in-token 口径 41% → 真实口径缩小但不消失,立项预判成立);分组件 cache% 高度均匀(92.9/92.2/86.1/86.5)。**结构性发现:输出占真实账单 72.6%**(输入被 1/120 hit 价压到 27.4%)——今后成本工作的杠杆在 tokens_out,不在 tokens_in。跨 diff 伪影核查:finder run1 step-1 命中共 39.7k tok(占 in 2.8%,时间序首 diff 即 96% 命中=缓存跨会话暖启),全部改按 miss 计的生产保守估价 $0.258 ≈ **¥1.84,仍在阈值内**——结论适用于生产单次 review。judge 未 trace(阈值定义已排除):全 miss 上界估 +$0.08/轮,连它算上 ≈ ¥2.3,不影响按预写口径的裁决。

**裁决:¥1.72 ≤ ¥2,①关闭。** W12 双跑 +69.7%(原始 tokens_in)豁免转为"实测不重要"的永久结论:一次完整验收(V2×3+holdout)≈ ¥6,W13 全周实耗 ~5.4M raw ≈ ¥7 量级。run2 步预算减半/条件二跑不做(B 分支未触发);"共享锚点会话前缀"由 provider cache 事实兑现(finder2 hit 92.2%)。本周主体按立项切 d5/d6。

**T4(d5/d6 归因先行,零 LLM,10 个 live run:W10×3/W11×3/W12×3/W13×1 + 回放 B×3 佐证)**:

| run | d5-deadzone 候选 | 层 | judge | d6-ghost 候选 | 层 | judge |
|---|---|---|---|---|---|---|
| w10r1 | f1 产出 | kept | HIT | f1 产出 | kept | miss(文本浅,没连到鬼弹跳后果) |
| w10r2 | **未产出** | - | miss | f1 产出 | kept | HIT |
| w10r3 | **未产出** | - | miss | f1 产出 | **DROP**(2/2) | miss |
| w11r1 | **未产出** | - | miss | f1 产出 | kept | HIT |
| w11r2 | **未产出** | - | miss | f1 产出 | kept | HIT |
| w11r3 | f1 产出 | **DROP**("parameter tuning suggestion, not a concrete defect / code correctly returns None") | miss | f1 产出 | kept | HIT |
| w12r1 | f1 产出 | kept | HIT | f1 产出 | kept | HIT |
| w12r2 | **f2 产出** | kept | HIT | f1 产出 | kept | HIT |
| w12r3 | **未产出**(f1+f2 双缺) | - | miss | f1 产出 | kept | HIT |
| w13r1 | **未产出** | - | miss | f1 产出 | kept | HIT |

**修正立项前提:真 flapper 是 d5-window-deadzone 单独一个**(产出 4/10、其中砍 1、judge 3/10;主导失败=finder 未产出 6/10)。**d6-ghost 不是 flapper**:10/10 产出(全部 f1)、W11 起 judge 7/7 连续命中,仅有的 2 次 miss 都在 W10(浅文本 ×1、verifier 砍 ×1)。W13 T7 所记"d5/d6 单run 双漏"与工件不符——w13r1 实际 d6 judge=HIT(d5 确实 miss:仅产出 docstring 措辞候选)。

**关键机理核实(零 LLM)**:d5 证据(ingest.py:22-23 调用方注释"serve-toss segments frequently end after only 2-4 samples")**每一轮都在 context pack 的 caller 节里**(build_review_input 确定性复现)——失败是注意力而非可见性:4 次产出的候选文本全部引用了该注释;未产出的 run 里 finder 报的是同 diff 的 off-by-one/docstring 措辞候选(注意力被同文件更浅的信号吸走)。现有 SYSTEM 清单 documented-unhandled 条目缺 dead-path 条目那样的**代入动作指令**("把实际值代入条件")。附:w11r3 的砍杀话术("tuning suggestion, not a concrete defect")是 dead-path/numeric 之外的**潜在哨兵第三族历史数据点**(1/10,未系统化,T7 继续观察;本周不动哨兵)。

**佐证**:回放 B×3(冻结 W12 finder 输出)d5/d6 层判定与对应 w12 run 完全一致——verifier 层在这两个 case 上无新增方差。

**T5 mini-立项(用户定两取舍,2026-07-13)**:机制=**清单代入指令**(SYSTEM documented-unhandled 条目补 call-site 代入动作,仿 dead-path 条目的"把实际值代入条件";零结构改动、零新增调用、golden 按引用不破);finder2 升温被否(f2@0.7 产出率 1/3 ≈ f1@0 的 3/10,数据不支持,且双变量无法归因)。

**T6 预写闸门(先写后跑,全量 V2×3 + holdout×1;基线=results_repeat_w12×3 配对参照 + w13r1 单run,不重跑基线——W12 与现 HEAD finder 代码相同)**:
1. **主闸门 d5-window-deadzone:候选层 3/3(f1/f2 任一产出)+ judge 层 ≥2/3**(容 1 次 verifier/judge 方差;砍杀史 1/10);
2. d6-ghost judge 3/3 不回退;
3. recall min ≥0.867(W12 地板);FP mean ≤0.3;noise mean ≤8.7;陷阱 d12+d13 kept mean ≤1.67;never_hit 不增(d10-cov/d11-origin 挂起豁免);
4. 原始 tokens_in ≤ W12 均值(1349k)+10%(清单一句话不应增加探索);真实 ¥ 入台账(T1 新列);
5. holdout 7/7、h6 陷阱 kept=0;
6. 哨兵回归:--sweep 对 W12 139 drop 仍恰 4 救+1 守卫(verifier 未动,应零漂移)。
内环切片(机制指标 only,成本门不设):d5×3 候选层产出 3/3 为过;d6/d12/d13 各×3 同批跑作误伤探针(d12/d13 findings 不升、d6 不回退)。

**T5 内环切片(d5/d6/d12/d13 ×3,results_w14_slice)**:**d5 候选层 3/3 ✅(全部 f1 直接产出,run2 f2 重复副本被正确 drop)、judge 3/3 ✅**——超闸门(≥2/3),产出文本全部引用 ingest.py:22-24 调用方注释,机制按设计起效。探针:d12+d13 kept 均值 1.33 ≤1.67(kept 为 json 健壮性类噪音,与新句无关);**d6 run1 judge=miss——非 finder 回归**(候选照常产出),系 verifier "Speculative: no evidence that the physics detector actually produces low-asr bounce events" 话术砍杀,与 w11r3 d5("tuning suggestion")、w10r3 d6 同族=**"speculative/generic 驳斥吃 documented-unhandled/missing-filter 真 bug"第三族证据第 3 例**;issue 门(config-disable/numeric)按设计不触发哨兵。T7 持续记账,本周不动哨兵;d6 闸门保持原样,若 T6 触发按 W13 G-full 先例做字面未达+归因判定。哨兵 sweep 预验:W12 139 drop 仍恰 4 救+1 守卫,verifier 零漂移。

**T6 验收(全量 V2×3 results_repeat_w14 + holdout×1,九门 6 过 3 字面未达)**:

| 指标 | W12 | **W14** |
|---|---|---|
| recall | 0.900 [.867–.933] | 0.900 [.833–.933] |
| precision* | 0.770 | 0.760 |
| F1 | 0.830 | 0.823 |
| FP | 0.0 [0–0] | 0.7 [0–2] |
| noise | 8.0 | **7.7** |
| oos | 5.3 | **2.7** |
| 陷阱 kept | 1.67 | **0.67** |
| never_hit | d10-cov, d11-origin | **仅 d11-origin(d10-cov 毕业 2/3)** |
| tokens_in | 1,349k | 1,369k(+1.5% ✅) |
| 真实计费 | - | **¥1.85/run(台账首录)** |

- 过:G2 d6 judge 3/3、G3c/3d/3e(noise/陷阱/never_hit)、G4 成本、G5 holdout **7/7 + h6 kept=0**、G6 哨兵零漂移(W12 139 drop 恰 4+1 不变;W14 145 新 drop 零误救)。
- **字面未达 ×3,逐条归因**:①G1 d5 候选 2/3(关键词初筛 3/3 系误报,run2 实未产出)、judge 1/3——run3 被"parameter-tuning observation, not a concrete defect / returns None gracefully"2/2 砍杀=**第三族第 4 例**;②G3a recall min 0.833——全部 run3:d5 砍杀 + d7-dead-flag 被实质性机理驳斥砍(声称 FREEZE 常量耦合不存在,非话术,哨兵按设计不触发)+ d7-gap finder 方差;③G3b FP 0.667——两条全在 run1:d3 `f.ball` 幻觉与 W13 G-full 一字不差(已知 finder 方差),另一条系**哨兵救活坏措辞 d7 候选(no-callers 主张)后被 judge 判 FP=哨兵代价面首次记账**(本轮哨兵净账:+3 HIT[d10-cov×2、d7×1] / −1 FP,净收益为正,d10-cov 因此毕业)。
- 机制本身:d5 候选层合并切片 **5/6(历史基线 4/10)**,产出文本全部引用调用方注释;无害面全绿。清单句是上游改动,三条未达项均在其作用域之外(verifier 层×2、finder 方差×1)。

**T7(哨兵第三族,4 例证据链闭合)**:w10r3-d6、w11r3-d5、W14切片r1-d6("Speculative: no evidence detector produces low-asr events")、W14全量r3-d5——共同形态=**驳斥话术 × 有文档证据的真 bug(documented-unhandled/missing-filter 类)**。W14 145 条 drop 中 31 条带驳斥话术但绝大多数在正确杀噪音——第三族哨兵必须沿用两族先例的紧 issue 门(caller-documented 条件类 issue),纯话术匹配会大量误救。另记:run3 d7 的实质性机理驳斥是**不同的族**(有具体反主张、非禁止话术),哨兵路线救不了,候选进 bench 台架。

**判定(用户批准,2026-07-13):有保留通过、不回滚。** 仿 W12 先例:机制达成采样层方向目标(候选 40%→83%),d5 记"未稳定化"不记达标;残余瓶颈换层至 verifier 第三族话术执行率。

**W15 候选**:①**哨兵第三族(头号,用户已预定向)**——驳斥话术 × caller-documented 真 bug,issue 门=documented-unhandled/missing-filter,纪律:先补 test_pure 逐模式正反例 + --sweep 对 W12/W14 全部已录 drop 验证(预期恰救 w11r3-d5/w14r3-d5 两例 + 不误救 31 条合法驳斥),再走配对回放主判(verifier-only 周,回放台架适用);②d7 实质性驳斥族观察(bench_verifier 补 case);③d5 候选层 run2 型缺产出(5/6→6/6)视第三族落地后 judge 层表现再定;d11-origin 维持挂起。

## W15(2026-07-13~):哨兵第三族 doc-condition-dismissed + 产品化搭车

**立项(W14 验收时用户预定向①)**:范围=哨兵第三族(verifier-only 周,配对回放主判,不跑全量×3);机制=`classify_drop` 增第三族——**reason 门**(4 例共同基元:"not a/rather than a concrete defect"×4、"parameter-tuning suggestion/observation"、"no evidence that"、"speculative robustness/future-proofing"、"handles gracefully/correctly returns")× **issue 门**(引用现存文档断言:comment/docstring/constant-comment + notes/states/documents that 形式——区分于"docstring 未写"类 missing-doc nit,后者动词是 does not specify/not documented,不命中);VERIFIER_SYSTEM 一字不动;重复守卫沿用。搭车:B1 打包+CI+LICENSE(已完成,a967793);glm 交叉重判(**阻塞:无 GLM_API_KEY,待用户提供**)。

**预写闸门**:
1. **G-sweep(四向,零成本内环)**:W12 dir 恰 4 救+1 守卫不变;W11 dir 恰 1 救(w11r3-d5);W14 dir 恰 1 救(r3-d5),31 条驳斥话术合法 drop 零误救;W14 切片 dir 恰 1 救(r1-d6)。任何计划外命中逐条人工裁决,合法驳斥被误救→收紧门,预算内迭代(沿 W13 两轮先例)。
2. **G-pure**:test_pure 第三族逐模式正反例先于实现落地,全绿;既有 4 例族测试零回归。
3. **G-bench**:既有 24/24 不破 + 新增 2 个冻结 kill-case(w14全量r3-d5、w14切片r1-d6)通过。
4. **G-replay(主判据,配对回放×3,source=results_repeat_w14)**:B(三族)vs A(两族)——d5-deadzone 在"有候选且被砍"的 run 被救活入 uncertain;A 命中的 bug B 无一丢失;FP 差=0;noise_B ≤ noise_A+2;precision_B ≥ precision_A−0.03。
5. **G-holdout(×1)**:7/7、h6 kept=0、第三族预期零触发(holdout 无已知 doc-condition 砍杀)。
预算:回放×3 ≈2M raw(真实 ~¥1.2)+ holdout ≈0.6M;内环全零成本。

**W15 实现与内环(2026-07-13,commit e7e4305)**:`classify_drop` 第三族 `doc-condition-dismissed` = reason 门(not a/rather than a concrete defect | parameter/threshold-tuning suggestion/observation | no evidence that | speculative robustness/future-proofing | handles/works…gracefully/correctly | correctly returns)× issue 门(comment/caller/constant/docstring + **非否定** notes/states/says/documents **that** | 带引号 comment 引用)。两轮 sweep 迭代:①v1 issue 门过松(任意文档引用),W12 误救 8——"Docstring states 'Native frames…'"类**格式自述 nit** 是主误救源 → 收紧为"断言动词+that";路径含点还暴露 `[^.]{0,60}` 句内启发被 `ingest.py` 截断的 bug → 改惰性 `.{0,60}?`;②v2 唯一残留误救 w14r3-d11"does not document that"=**否定形引用**(缺文档 nit 穿引用外衣)→ 动词前定宽负后顾 `(?<!not )(?<!n't )`,案例固化为负例测试。

**G-sweep 终局(六向,超出预写的四向)**:W12 恰 4+1 不变 ✅;W14 恰 r3-d5、31 条合法驳斥零误救 ✅;切片恰 r1-d6 ✅;W11 第三族恰 w11r3-d5 ✅(族二在 W11 数据上另中 w11r3-d10 cov 真 bug 砍杀——W11 从未入 sweep 验证集,系族二既有正确行为非本周改动);W13 零 ✅;**W10(追加负控)第三族命中 w10r3-d6(第 4 例)+ w10r3-d7(计划外,人工裁决=正确救活:被砍的是 d7-gap-connect 真埋点,话术"Design preference, not a concrete defect / correctly implements what it advertises"——第三族第 5 例,先前 T7 扫描未及 W10)**。G-pure:82 测试全绿(第三族 4 正例+5 反例:missing-doc nit/无引用 robustness/实质性文档反驳/否定形引用/重复守卫)。

**W15 验收(六门五过一保留)**:
- **G-pure ✅** 82 零 API 测试;**G-sweep ✅** 六向精确计数(见上);**G-bench ✅ 30/30**(含 2 新冻结 case:目标存活、同录合法 drop 全留死);**VERIFIER_SYSTEM 零改动 ✅**(verifier.py +31 行全在哨兵区)。
- **G-replay(主判据)✅**:配对回放×3(source=results_repeat_w14;注:台架 A 视图=哨兵全关,较预写的"A=两族"更严——族一/二已知代价被算进 B 侧,按 tag 归因拆解)。**run3=目标链路 live 验证:d5-deadzone 候选被 fresh verifier 再砍,B 第三族救活 → judge HIT(A=miss)**;run1 d5 verifier 自保(无需救)、run2 录制无候选(按构造豁免)。配对无回退(B⊇A ×3);均值 recall A .855→B .889、precision −0.009(容差 .03)✓。严格口径下两处 flag 均非第三族:run1 fp_B=1 是族一救坏措辞 d7 的 W14 已记账代价;run3 fp_A=1>fp_B=0 是 judge 对相同 unmatched 的分类方差(方向利 B)。**第三族边际:+1 真 bug / 0 FP / 0 噪音**。
- **G-holdout 有保留(6/7)**:h6 陷阱 kept=0 ✅、哨兵零触发 ✅(符合预写),但 h2-hud-line-cap 单点 miss。归因:finder 照常产出候选,verifier 2/2 砍,理由="6 行 HUD 约定**缺乏声明的真实后果**(对比 print/dbg 规则的'发布版要能一键静音')、无 crash/overlap/失败机理"——**VERIFIER_SYSTEM convention 条款的字面合规应用,不属三族禁止话术**('convention states' 不在三族 issue 门内,'no crash…identified' 不中任何 reason 门);W14 同套件同 prompt 7/7,单 run 翻转;哨兵只增不减按构造无法致 miss。**记录张力:未声明后果的 convention 违反该不该算命中,是 truth-set 设计问题非执行缺陷**(fixture CLAUDE.md 的 6 行规则确实没写后果)——W16 泛化周一并考虑,不做 mid-week 资产修改。
- 真实 ¥ 台账:回放 B×3 + bench + holdout 计 2.36M in(cache 折后)/432k out = **$0.50 ≈ ¥3.6**(judge 未 trace,B2 落地后并入)。

**判定(用户批准,2026-07-13):通过、不回滚。** 主判据干净,唯一保留项归因 W15 无关(沿 W13 G-full 先例);h2 truth-set 张力进 W16 议题。

**W16 候选**(路线图既定主题=真实 PR 抽查+泛化决策门,搭车 B2 judge-trace+tools 单测):①h2-line-cap 张力的 truth-set 处置(改 CLAUDE.md 声明后果 vs 接受 convention 类按规则字面执行);②glm 交叉重判(仍等 GLM_API_KEY);③d5 候选层 run2 型缺产出维持观察。

## W16(2026-07-14):glm 交叉重判 + 真实 PR 泛化门 + B2 搭车

**立项(用户定三取舍,2026-07-14)**:主攻=真实 PR 抽查(素材 pingpong_tracker,用户拍板 90af514/43fc78c/b489ae7 三 commit)+泛化决策门;glm 交叉重判进本轮(GLM_API_KEY 到位);搭车=B2(judge 入 trace + scores.json meta 自描述/truth_sha256 陈旧校验 + judge_one golden 测试 8 例,119 零 API 测试全绿)。插曲:.env 全局 `LLM_MODEL=GLM-5.2` 曾把 deepseek 主管线带偏(400 invalid model),已注释并改为 per-run 传参——教训:LLM_MODEL 是跨 provider 全局覆盖,只应按次设置。

**W16-A glm 交叉重判(判定:一致性极高,不引入仲裁机制)**:

- 方法:复制 results_repeat_w14/v2_run1..3(仅 result JSON,不含 scores)到 scratch,`LLM_PROVIDER=glm` 重判(judge_model=**GLM-5.2**,scores meta 可查证);对比脚本出逐 bug 翻转表+一致率。judge 协议(submit_scores tool-calling)在 glm 端零改动跑通——厂商无关结构化输出设计的首次跨厂实证。
- **结果:90/90 埋点命中判定 100% 一致,零翻转**;三 run 的 recall(.933/.933/.833)与 precision(.737/.794/.75)在双 judge 下逐位相同;unmatched findings 的 FP/noise 分类一致率 24/25(96%,唯一分歧=run3 一条 ds 判 noise、glm 判 FP,不影响任何埋点判定)。
- **解读(按预写门)**:系统性翻转(同 bug ≥2 run)=0 → 双 judge 仲裁机制不立项。"judge 与被测同模型 → self-preference 抬高分数"的质疑在**命中判定层面被证伪于本集**(跨家族 deepseek-v4-pro ↔ GLM-5.2 完全可复现);残余风险收窄为**共享盲区**(两个模型都判不出的命中形态)——该风险无法用交叉重判排除,属 truth-set 人工校准欠账(README 限制节保留该条)。成本 ~¥1(48 次 judge 调用)。

**W16-B 真实 PR 抽查(pingpong_tracker,主线;判定口径=机器侧预判、待作者复核,用户批准落盘)**:

| commit | 内容 | verifier | kept/dropped | 真实成本 |
|---|---|---|---|---|
| 90af514 | COR 标定 X1(82 行单文件) | ok | 1/4 | $0.046 |
| 43fc78c | 每球分段 vy-flip(300 行 3 文件,主文件 5400+ 行) | **degraded**(B pass 失败) | 5/6 | $0.259(含重试) |
| b489ae7 | UKF 修复链 F/G/H/I(93 行 2 文件) | ok | 5/5 | $0.173 |

- **finding 质量**:11 kept ≈ **8 真 / 2 真但低价值 / 1 待复核**。代表性真问题:b489ae7 跨文件过期建议(analyze_innovation_adaptive.py:131 仍推荐已被 H1 撤回的 F1,源码 903 行注释"H1…撤回 F1"核验属实——工具查证型跨文件一致性,eval 集没有这类形态);43fc78c `hit_display_suppress_left` 跨轨迹状态泄漏(medium,含 finder2 重复副本被 verifier 正确合并);90af514 tangent 回归静默跳过与 COR 回归行为不一致(344-349 无 else 分支,核验属实)。低价值两条=dev 测试包装器的 argv 防护(事实为真,语境价值低)。**编造率:抽检 2/2 事实断言为真,零编造**(d16 能力在真实仓库首证)。
- **verifier drop 质量**:15 条全部可辩护——正确识别 watchdog 语境 `os._exit` 合理性、正确引用项目约定拒风格 nit、正确指出"*= 0.35 非本 diff 改动"。降分布不降判。
- **哨兵首个分布外实证:零触发、零误救**——reason 门多次命中禁止话术("not a concrete defect"/"speculative robustness")但 issue 门全部正确拦截,合取设计按预期泛化;局限:新分布无已知真 bug 错杀,救活率不可测,只证无害面。
- **新失败模式(工程鲁棒性 ×3,评测有效性 0)**:①anchor finder **空响应致命**(43fc78c 首跑 step8 模型返回空内容,`on_text_answer="raise"` 直接崩,重试即过=偶发 provider 毛刺,但 anchor 无重试是真实脆弱点);②verifier `submit_verdicts` **JSON 截断**(11 候选×长理由超 max_tokens=4000,pass B 失败)——降级路径按设计工作,`verifier_status=degraded` 标注生效(P0-6 修复首次生产可见);③**成本分布**:大文件仓库单条最高 ~¥1.85(≈eval 均值 17 倍),cache 命中率仍 73-75%。
- 全轮实耗 ≈ ¥4.5(交叉判 ~¥1 + 抽查含失败重试 ~¥3.4),略超预估 <¥3,归因 43fc78c 体量与重试。

**门裁决(用户批准,2026-07-14):通过。** GLM 零翻转 + 新失败模式全为工程鲁棒性类(非评测有效性类)→ **不触发第二域 fixture 周**;W17 = 既定路线(d7 实质性驳斥族 + uncertain 通道方差 + B3 GitHub 闭环搭车),外加鲁棒性双修进搭车清单:anchor 空响应重试一次、verifier max_tokens 提额或候选分块。结果 JSON/MD/trace 三件套在 eval/results_w16_real/(gitignore 内,复核入口)。
