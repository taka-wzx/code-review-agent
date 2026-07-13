# code-review-agent

最小 code review agent loop（W0 里程碑）。喂一段 unified diff → Claude 审查（可用 `read_file` 工具自己读仓库文件补上下文）→ 输出结构化 JSON review。

## 运行

Provider 无关：走 OpenAI 兼容接口，`LLM_PROVIDER` 切换 `deepseek`（默认）/ `glm`。

```powershell
# 0. 建项目独立虚拟环境 + 装依赖（只做一次）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 1. 设 provider + key
$env:LLM_PROVIDER = "deepseek"          # 或 "glm"
$env:DEEPSEEK_API_KEY = "sk-..."        # glm 则设 $env:GLM_API_KEY

# 2. 跑内置样例（sample.diff 里埋了几个真实 bug，看它能不能抓到）
.venv\Scripts\python.exe agent.py sample.diff

# 3. 审查真实改动（W7 git 集成,不再需要手动导 diff）
.venv\Scripts\python.exe agent.py --commit HEAD --repo e:\shiyan\pingpong_tracker      # 某个 commit
.venv\Scripts\python.exe agent.py --uncommitted --repo e:\shiyan\pingpong_tracker      # 工作区未提交改动
.venv\Scripts\python.exe agent.py --pr 42 --repo path\to\repo --format md --out pr.md  # GitHub PR(需 gh,先 checkout PR 分支)

# 输出格式：默认 JSON；--format md 出可直接贴 PR 的 markdown(--out 落盘后
# 用 gh pr comment N --body-file pr.md 发布)
```

## 结构

- **agent loop**（agentloop.py，finder/verifier 共用引擎）：`run_submit_loop()` — 调 API → 有 `tool_calls` 就执行、把 `role:"tool"` 结果回填 → 循环直到模型提交校验合法的 submit 载荷（末步撤探索工具催交）；agent.py `run_review()` / verifier.py `_verify_pass()` 只留 prompt、载荷校验和成败映射
- **provider**（llm.py）：`make_client()` — `LLM_PROVIDER` 切 deepseek/glm，OpenAI 兼容端点（自 agent.py 抽出）
- **测试**（tests/，零 API 调用）：FakeClient golden 测试锁定请求序列（消息/工具/催交与拒绝文案）和 trace 事件流——行为保持重构的安全网；外加校验/合并/指标纯函数单测。跑法：`python -m unittest discover -s tests`
- **工具**（tools.py，finder/verifier 共用）：`read_file`（路径逃逸检查、大文件按 `start_line` 续读、文件不存在时返回候选路径/"仓库里确实没有"——错误信息可恢复）+ `search_repo`（字面量全仓 grep,追 import/查符号是否存在）+ `submit_review`
- **结构化输出**：把「提交结果」做成 `submit_review` 工具、schema 当函数参数——不依赖任何厂商专有的 JSON 模式，跨 DeepSeek/GLM 通用；payload 过 `validate_review` 结构校验，非法则把问题回填重试（W6）
- **护栏**：`MAX_STEPS=10`、坏 submit 上限 2、同参数重复工具调用短路、`temperature=0`、每次请求 120s 超时（SDK 自动重试两次）、工具失败回可恢复的 `Error:` 文本而不是崩
- **trace**（tracelog.py）：JSONL 事件流（llm_response/tool/submit_rejected/review/verdicts），`agent.py --trace PATH` 手动开，`run_eval.py` 自动写 `<results>/traces/<name>.jsonl`（W6）

## 评测（W1 人工基线 + W2 自动打分 + 扩集）

```powershell
.venv\Scripts\python.exe run_eval.py [--no-context] [--results-dir DIR] [--only d1_sign,...]  # 跑评测集
.venv\Scripts\python.exe judge.py [--results-dir DIR] [--truth PATH]                          # LLM-judge 打分
.venv\Scripts\python.exe eval\check_consistency.py [eval eval\holdout]                        # 校验评测资产一致性
.venv\Scripts\python.exe repeat_eval.py --runs 3                          # W7:每版 n 次重复跑+方差聚合
.venv\Scripts\python.exe cost_report.py eval\results_repeat\v2_run1       # W7:token/成本报表(从 trace)
```

- **评测集**：16 diffs / 30 埋点，源自 pingpong 项目**真实 bug 蒸馏**（dt 感知门、单位/量纲、
  死旗标、降采样过滤、COR 标定……），含 2 个**无埋点陷阱用例**（干净代码/纯重构）专测误报、
  1 个**信息缺失用例**（d16：import 仓库中不存在的模块）专测"编造 vs 诚实报告"
- ground truth 在 `eval/truth.json`（每个埋点附**命中标准**）；diff 由 pre/post 机械生成，
  `check_consistency.py` 保证 diff↔fixture↔truth 三方一致
- judge 裁决经结构化校验 + 自动重试；校准：埋点判定 9/9 与人工一致（W2，见 cases.md）

## 上下文检索（W3）

`context.py`：解析 diff → 主动预取约定文档（CLAUDE.md）、改动文件全文、函数调用方片段（纯符号级检索，无向量库，预算 cap 28k chars）。`--no-context` 做 ablation（V0=agent 被动按需 read_file，V1=主动预取+按需工具）。

| 版本（n=30，W6 重跑） | recall | precision | FP | noise |
|---|---|---|---|---|
| V0 被动工具 | **0.900 (27/30)** | 0.419 | 0 | 36 |
| V1 +主动检索 | 0.800 (24/30) | 0.348 | 0 | 43 |
| V2 +verifier | 0.800 (24/30) | **0.793** | 0 | **6** |

- **V2 主线不变**：`verifier.py` 独立二次复核（keep/drop 每条 finding，temperature=0，fail-open，
  被丢条目带 `drop_reason` 落盘可审计）仍是 precision 引擎（0.35→0.79，noise -86%），
  陷阱用例 d12 findings 归零
- **W7 复验裁决（n=3/版）**："V1 预取伤 recall"**不成立**——v0 与 v1 recall 均值完全相同
  （0.844），当时的 0.90→0.80 是拿 v0 的幸运上沿比了 v1 的普通一轮；且 v1 区间更窄
  （[0.833–0.867] vs [0.800–0.900]），预取反而**稳定** recall。上表单次数字已被
  W7 节的 mean [min–max] 表取代
- F1 视角（W7 n=3 均值）：V0 0.545 → V1 0.531 → **V2 0.819**

## W6：工具层 + 可靠性补齐（对照 agent 路线文章逐项）

- finder 输出 schema 校验+重试（此前 submit_review payload 不校验直接用）
- `search_repo` 工具——agent 可按需追 import/查符号（对着 d7 屡漏的架构缺口）
- verifier 挂同款只读工具，"依赖未见代码的 finding 必须先查再判"（对着 d5 错杀:
  没有查证手段的 Reflection 只能猜）
- trace 落盘 + finder temperature=0 + 重复调用短路（对着 d7 run 方差不可归因）
- 新用例 d16 测"信息缺失时编造 vs 诚实报告"（评估文章:"错误失败"最危险）
- 30 埋点三版 ablation 已全量重跑（2026-07-06）：d16 三版全中、零编造✔；d7 仍漏✘
  （search_repo 在手但预取模式下不去用）；verifier 新错杀 d1 空列表除零
  （"search 证实无调用方→投机"）——完整拆账见 eval/cases.md「W6 全量重跑」节

## W7：测量仪器 + 外围功能（2026-07-09）

**仪器**（先于一切 prompt/功能迭代——W6 重跑留下的两个开放问题都需要它裁决）：

- `repeat_eval.py`：每版 n 次重复跑（可断点续跑,scores.json 存在即跳过），聚合
  recall/precision/F1/FP/noise 的 **mean [min–max]**,并输出 **per-bug 翻转表**
  （hit x/n 的埋点=方差问题,0/n=真 miss）——"V1 伤 recall"是否成立由它裁决
- **held-out 集** `eval/holdout/`：6 diffs / 7 埋点 + 1 陷阱（轴混用、约定违反 ×2、
  追加变覆盖、单位 1000x、可变默认参数、重复时间戳除零；h6 纯重构零埋点），
  独立 fixture 副本,与主集零共享。**纪律：held-out 只在 prompt/判据迭代验收时跑,
  平时不跑不看**——主集过拟合的保险丝
- `cost_report.py`：从 trace 聚合 finder/verifier 的 llm 调用数/工具调用数/token,
  `--price-in/--price-out`（每百万 token）可选出成本列；judge 无 trace 不计入

**外围功能**（不碰 recall/precision 指标）：

- **git 集成**：`agent.py --commit [SHA] | --uncommitted | --pr N`,不再手动导 diff;
  `--commit` 非 HEAD 时警告工作区不一致（read_file 看的是当前树）
- **PR comment 输出**：`--format md` 渲染按严重度排序的 markdown,dropped findings
  收进 `<details>` 审计块；`--out FILE` + `gh pr comment N --body-file FILE` 发布

**n=3 重复跑结果（30 埋点,2026-07-09,本项目的权威指标表）**：

| 版本(n=3) | recall | precision | F1 | FP | noise |
|---|---|---|---|---|---|
| V0 被动工具 | 0.844 [0.800–0.900] | 0.403 [0.391–0.417] | 0.545 | 1.0 [0–2] | 36.7 [34–39] |
| V1 +主动检索 | 0.844 [0.833–0.867] | 0.388 [0.353–0.424] | 0.531 | 0.3 [0–1] | 39.7 [37–44] |
| V2 +verifier | 0.811 [0.800–0.833] | **0.833 [0.727–0.920]** | **0.819 [0.776–0.856]** | 0.3 [0–1] | **4.7 [2–9]** |

- **V1 伪回归关闭**（见上）；**verifier 的 recall 代价收敛到 -0.033**（均值约 1 个埋点,
  远小于 W5 单次的 -0.107 印象）
- **翻转表把方差和真 miss 拆开了**：全版本 0/3 的真 miss 只有 d11-origin-fit、
  d7-dead-flag-path（+v2 下 d7-gap-connect、d10-cov-asymmetry 被 verifier 系统性砍掉）
  ——这四个才是架构迭代的靶子,其余波动是运行方差,不值得针对性修
- **verifier 自身也有方差**：v2 noise 区间 [2–9],严格度 run-to-run 不稳——held-out
  验收时的观察项
- 成本（v2 全集单轮,from cost_report）：约 47 万 token 入 / 7.7 万 token 出,
  finder:verifier ≈ 6:4

## W8：判据尝试 + 预取追 import + linter 工具（2026-07-09,每项过 held-out 验收）

held-out 集首次实战,三项处置:

1. **verifier 投机判据改写——验收失败,已回滚**。新判据("投机=约定/注释/调用方无证据")救有文档
   证据的慢性 bug,却处决无文档触发的崩溃类 bug(h5 除零被砍,recall 6/7→5/7)。后续验收还揭示
   更深一层:基线措辞的 verifier 对同一条 bug 也会随机砍(drop 理由甚至编造 docstring 内容)——
   **verifier 对边界 bug 的裁决方差是底层问题,措辞不是**。按预写规则回滚,不二次迭代。
2. **context.py 预取追 import——通过**。改动文件的 in-project import 直接进 pack
   (cap 5k×4 个);无法解析的项目内 import 输出显式 note(喂 d16 型检测);stdlib 静默。
3. **run_linter 工具(pyflakes,静态不执行)——通过,held-out 满分**(recall 7/7、FP 0、陷阱 0)。

**终测(V2×3)如实结论:主集指标方差内持平**(F1 0.803 [0.763–0.840] vs W7 0.819 [0.776–0.856]),
净收益在能力面。d7-dead-flag 仍 0/3 但性质改变:预取生效后 finder 首次报出旗标问题(1/3 run)、
被 verifier 砍——可见性已解决,识别+复核是剩余瓶颈。方法论沉淀:**n=1 验收门会被 verifier 方差
假触发,归因(drop_reason + pack 对比)必须是验收判定的一部分**。

## W9：双复核 + 分歧→uncertain(2026-07-09,治 verifier 边界裁决方差)

W8 证明措辞救不了裁决方差 → W9 上**编排层机制,判据 prompt 一字不动**:verifier 跑两个
独立 pass(B 倒序呈现 findings 做确定性去相关),合并规则 keep+keep→confirmed、
drop+drop→dropped、**分歧→保留标 uncertain+附少数派理由**(两 pass 的分歧本身就是
边界探测器,不靠模型自报)。drop 需 2/2 票:单 pass 误砍率 p → 双 pass p²。
单 pass 失败降级单复核,双失败仍 fail-open。`--format md` 把 uncertain 单独成节呈现。

**验收与终测**:held-out ×2 全过——在 base→w8a→w8b 三轮里翻来翻去的 h5 除零,
两轮双票 confirmed,两轮指标完全一致。主集 V2×3:recall 0.778→**0.856**(区间
[.833–.867] 三代最窄)、unstable 埋点 3→2→**1**、d5/d6 从 0/3 回收(d6 满 3/3);
代价 precision 0.83→0.74(uncertain 边界条目存活,已在呈现层隔离),F1 持平 0.795。
剩余 0/3(d7×2、d10、d11)全是 **finder 没报**,uncertain 救不了"没报"——瓶颈正式换层。

## W10：finder 类别清单 + verifier 证据规则(2026-07-10,对着 4 个 0/3 的两层瓶颈)

立项前提经原始 run JSON 核查后修正:四个 0/3 里只有 d7-dead-flag、d11-origin 是 finder
真没报;d7-gap、d10-cov 是 finder 间歇报出、被双复核 2/2 砍("投机建议"类理由)。两个改动:

1. **finder 清单**(agent.py SYSTEM):三类常漏缺陷的主动检查提示——死路径(把旗标实际值
   代入 guard 条件)、文档写明却未处理的输入、数值/统计缺陷。首句 guardrail:清单是待验证
   假设,报出须指名具体代码路径+失败机制。措辞类别化零 eval 专名。
2. **verifier 证据规则**(VERIFIER_SYSTEM,判据本体一字不动):文档写明的输入条件是"未处理"
   类 finding 的支持证据而非"有意设计";指名了量+机理的慢性数值 finding 不算泛泛建议,
   怀疑≠反驳;死路径可达性按 repo 内实际定义判,"假想的未来调用方"不是反驳,反向排除
   "新增函数暂无调用方≠死代码"。节首优先级声明(与 DROP 列表冲突时以本节为准)。

两项各经一次记录在案的措辞迭代(Stage A/B 靶向验收 + holdout 把关全程,过程见 cases.md)。

**终测(V2×3)**:recall 0.856 持平(区间变宽 [.800–.900])、precision 0.743→0.777、
F1 0.795→**0.811**、noise 8.3→**5.3**、陷阱均值 3.0→**1.0**、never_hit **4→2**
(d7-gap 历史首次 2/3,d7-dead-flag 历史首次存活 1/3;剩 d10-cov、d11-origin)。
代价:成本 +26.6%(870k in/轮)、unstable 1→4(其中 2 个是毕业爬升型)。预写门槛 5 过
4 未达,未达项归因全指向方差/协议缺陷/成本——判**有保留通过**。

## W11：成本优先——兔子洞刹车 + 计量固化 + 预算纪律(2026-07-10)

离线归因先行(W9/W10 全量 trace 对比,零 LLM 成本):W10 的 +183k/轮中 finder 占 70%
(更多轮次+更深会话重放);**not-found 搜索变体链是长期洼地非 W10 新增**(两代都 ~12.7
条 3+连败链/run,样本:draw_line→.draw_line→def draw_line→cv2→class.*Frame)。

1. **T1 连败刹车**(tools.py ToolSession,prompt 零改动):search_repo 连续 3 次 clean miss
   起在结果尾注入 nudge("一次未命中已证不存在,缺失本身可报告,勿再试变体");regex 用法
   错误类 miss 不计数;命中或其他工具成功清零。trace 补显式 miss/miss_streak 字段。
2. **T2 计量固化**(cost_report.py):repeat 根聚合、`--baseline` 组件级对比、连败链计数。
3. **T3 预算纪律**:机制类改动 tokens_in 预算 ≤+10%/W(全量口径),超出需当轮回收或明示
   豁免;每轮 W 终测跑 `cost_report --baseline` 入档。**成本门槛只在全量口径设定,靶向
   切片只看机制指标**(本轮教训:-10% 门在 3-diff 切片 σ±16% 下统计不可达成,且被
   dead-flag 探索率 1/3→3/3 的收益推高读数——成本与收益在小切片纠缠)。

验收:d16 探针 2/2 无误伤、holdout 全绿(h6 陷阱 0)、trio 行为无回退(dead-flag finder 层
1/3→**3/3**);连败链 calls 三组件一致 **-27%~-46%**;切片 tokens_in +3.3%(σ内,见上)。
T1 保留,全量成本效果留待 W12 重新基线化校验。

## W12:finder 双跑并集 + 文件级 scope 口径(2026-07-11~12)

**重新基线化先行**(T0,改代码前从 `git archive` 冻结副本跑 V2×3):W11 全量
tokens_in 870k→**795k(-8.6%)**、连败链 calls **-83%**——W11 悬置的成本裁决兑现,切片门
"失败"确系门槛设计失误。质量面 F1 持平 0.811;**d5-window/d7-dead-flag 恶化为 0/3**,
采样层缺口坐实。

1. **双跑并集**(agent.py/findings.py):run1 保持 temp=0 锚点(失败语义不变),run2 temp=0.7
   采样、失败 fail-open 降级;并集结构化去重(issue 文本 token-set Jaccard 双档:≥.40 任意
   行距 / ≥.25 且 Δ行≤4,阈值实测自 w10 跨 run 对;单向合并,锚点措辞永不动),残余重复靠
   verifier duplicate→DROP 兜底;run2 新增项带 `origin:"finder2"` 溯源。
2. **文件级 scope**(findings.py/judge.py):并集后、验证前,diff 外发现降级
   `out_of_scope_findings`——不验证、不计 precision(judge 侧 `n_out_of_scope` 单列);
   verifier prompt 一字不动。**W12 起 precision 换分母,与前代不直接可比。**
3. **成本豁免**:全额双跑,实测 +69.7%(795k→1349k in,预估包络顶格),cases.md 明示豁免;
   连败链在 temp=0.7 下维持低位(刹车依然有效)。

**终测(V2×3)**:recall 0.845→**0.900 [.867–.933]**、F1 0.811→**0.830**(区间五代最窄)、
**FP 三轮全 0**、noise 8.0、陷阱 1.0、never_hit 4→**2**(d5 毕业 2/3、dead-flag 历史首次
judge 层存活;剩 d10-cov、d11-origin);oos 5.3 条/轮。holdout **7/7 满分**、h6 陷阱 kept=0、
classify_bounce 伪影如预期落 oos——三代协议 FP 摇摆终结。预写闸门 **7/7 全过,判定通过**。
切片归因:dead-flag/cov finder 层 0/3→**3/3**(双跑达成立项目标),但 verifier 2/2 砍、
drop 理由逐字复现证据规则明令禁止的话术——"报了被砍"回归为 W13 头号候选。

## W13:verifier 回放台架 + 砍杀台架 + 规则触发率哨兵(2026-07-12)

**评测成本结构性修复(本周真正的杠杆)**:result JSON 的 findings+dropped_findings 拼回即
verifier 完整候选列表,build_context 确定性可重建 finder 输入——**verifier 改动回放存盘的
finder 输出,不重跑 finder(省 ~60%),且配对比较(A/B 看相同候选)把 finder 方差从对比剔除**。
分层金字塔:单测(0)→ `--sweep` 哨兵内环(0,亚秒)→ 砍杀台架(~0.34M)→ 配对回放×3
(~2.0M,主判据)→ 全量×1+holdout(仅验收)。迭代单元从 1.55M(全量×1)降到 0/0.34M。

1. **回放台架**(replay_verifier.py):单次 live 执行产 B 视图(哨兵开),A 视图(HEAD)由
   rescue 标记降回派生——一次执行两变体,配对由构造保证;candidate_findings 持久化(agent.py)
   让今后回放拿到精确候选顺序;`--sweep` 是零 LLM 的哨兵设计内环。
2. **规则触发率哨兵**(verifier.py `rescue_forbidden_drops`,纯函数,post-merge/degraded 两分支):
   2/2 drop 且 drop_reason 命中禁止话术模式→降级 uncertain(非砍掉)。模式为规则原文的合取:
   模态/scaffolding 式 future-work 话术 × **具名常量=布尔的 config 禁用**issue(靠 issue 门把
   纯 no-callers 合法 drop 挡在外);generic/speculative 驳斥 × **声明不变量丢失**的 numeric issue。
   两次预算内迭代收敛(G-sweep 对 139 条已录 drop 恰 4 救 + 1 守卫);**VERIFIER_SYSTEM 一字未动**。
3. **砍杀台架**(bench_verifier.py + eval/bench_verifier.json):10 个 W12 冻结 case(dead-flag×3、
   cov×2 + 5 负控)live 复跑,确定性断言无 LLM judge。
4. **prompt-cache 计量**(T1):trace 记 DeepSeek cache_hit/miss,cost_report 加 cache% 列——
   全量实测 **90% 命中**,原始 tokens_in 高估真实计费约一个量级(W14 成本回收基线)。

**验收(五道闸门)**:G-sweep 4/139 ✅、G-bench 24/24 ✅、**G-replay 主判据 ✅**(配对回放×3:
B recall 0.889→**0.922**、F1 0.822→**0.840**,每轮恰 1 救且命中 A 漏的真 bug,零 FP、noise
持平、precision 反升)、G-holdout ✅(7/7、哨兵零触发)、G-full 有保留通过(两条字面未达项
归因均为 W13 无关的单run finder 方差,哨兵零 LLM 调用不可能是肇因)。**d10-cov-asymmetry
(三代 0/x)首次被机制救活;dead-flag judge 层 1/3→3/3。判定:通过,不回滚。**

## W14:双跑成本裁决(关闭)+ d5 清单代入指令(2026-07-13)

**T0 计量裁决先行(预写阈值 ¥2/全量轮)**:cost_report 增缓存感知计价(`--price-hit`,
deepseek-v4-pro hit 价=miss 的 **1/120**)。实测全量单轮真实计费 **¥1.72 ≤ ¥2 → W12 双跑
+69.7% 豁免转永久关闭**,回收机制不做。结构性发现:**输出占真实账单 72.6%**——今后成本
杠杆在 tokens_out。预算纪律改双口径(tokens_in 漂移哨 + 真实 ¥ 台账)。

**归因先行修正立项前提**(10 个 live run 分层表):真 flapper 是 **d5-deadzone 单独一个**
(产出 4/10,主导失败=finder 未产出 6/10,而证据每轮都在 context pack——注意力非可见性);
d6 自 W11 起 7/7 稳定。机制=SYSTEM 清单 documented-unhandled 条目补 **call-site 代入指令**
(把调用方文档化的输入区间代入 guard/早退)。

**验收(九门 6 过 3 字面未达,判定:有保留通过、不回滚)**:d5 候选层合并切片 5/6(基线
40%)但全量 judge 1/3——run3 被"parameter-tuning observation"话术砍杀=**第三族第 4 例**
(w10r3-d6/w11r3-d5/切片r1-d6/全量r3-d5,共同形态=驳斥话术×caller-documented 真 bug);
recall min 0.833 与 FP 0.667 归因均为预存方差(`f.ball` 幻觉复现、d7 dead-path 摇摆)与
哨兵救活坏措辞候选的代价面(净账 +3HIT/−1FP,**d10-cov 首次从 never_hit 毕业 2/3**)。
无害面全绿:陷阱 0.67、noise 7.7、oos 2.7、holdout 7/7、成本 +1.5%(¥1.85/run 台账首录)。
**W15 头号候选:哨兵第三族**(紧 issue 门=documented-unhandled/missing-filter;31 条合法
驳斥 drop 是误救红线)。

## 限制

- 不跑测试(read_file/search_repo/run_linter 均为静态/只读)
- **成本已按真实计费裁决(W14 关闭)**:全量单轮实测 **¥1.72**(deepseek-v4-pro,hit 价=miss
  的 1/120、cache 命中 90%),单次 review 均值 ¥0.11——W12 双跑 +69.7%(原始 tokens_in)豁免
  转为"实测不重要"的永久结论,回收机制不做。**输出占真实账单 72.6%**,今后成本杠杆在
  tokens_out。预算纪律双口径:原始 tokens_in ≤+10%/W 仍作膨胀漂移哨(对 cache 不敏感),
  每周终测跑 `cost_report --price-in/--price-hit/--price-out` 真实 ¥ 入台账
- **d5-deadzone 未稳定化**(W14 判定):候选层 5/6(基线 40%)但判定链卡 verifier 第三族
  砍杀;d6 自 W11 起稳定(W14 3/3)非 flapper——W13 所记"d5/d6 双漏"经归因表修正
- d10-cov W14 从 never_hit 毕业(judge 2/3,哨兵救活生效);d11-origin-fit 领域难档五代 0/x
  ——维持挂起
- **哨兵第三族已系统化**(4 例:w10r3-d6/w11r3-d5/W14切片r1-d6/W14全量r3-d5,驳斥话术×
  caller-documented 真 bug)——W15 头号候选,须沿两族先例紧 issue 门(W14 全量 31 条合法
  驳斥 drop 是误救红线);d7 型实质性机理驳斥是另一族,哨兵路线不适用
- uncertain 通道容量与 oos 波动(2–9 条/run)仍是 precision 方差主源;oos 列自 W12 起单列
