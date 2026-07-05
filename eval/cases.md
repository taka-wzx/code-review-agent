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
