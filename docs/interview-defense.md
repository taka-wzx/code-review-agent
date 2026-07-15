# 面试答辩手册 — code-review-agent

> 用途：面向华为、字节等大厂 AI Agent 岗位面试的中文答辩材料，面试前速览。
> 回答姿态统一为：**承认问题曾存在 → 说明修复/取舍 → 给出代码或数据出处**。
> 所有数字均有出处（本机实测或 eval 台账），不含虚构实验数字或生产效果。
> 数据基线：2026-07-15 本机验证（178 测试 / 95% 覆盖率），评测数字截至 W17。

---

## 一、项目陈述

### 30 秒版

两阶段 code review agent：finder 双跑（temp 0 锚定 + temp 0.7 采样）召回候选缺陷 →
结构化去重 / 文件级 scope 过滤 → verifier 双 pass 独立证据复核（分歧进 uncertain 通道，
禁止话术砍杀被哨兵兜底）→ JSON/Markdown/PR 行内评论输出。Provider 无关（OpenAI 兼容，
DeepSeek/GLM 实测）。配套 16 diffs/30 埋点评测集 + holdout + LLM judge + n 次重复跑
方差归因 + 全链 JSONL trace。核心数字：verifier 把 precision 从 ~0.35 提到 ~0.83
（noise -86%），W12 后 recall 0.90、FP 三轮全 0；全量评测单轮真实计费 ¥1.72。

### 3 分钟版

**问题**：直接让 LLM 看 diff 挑毛病，召回不差但噪声淹没一切——我的 V0 基线
precision 只有 0.40，平均每轮 37 条噪声。代码审查的产品约束是**误报会烧掉审阅者的
信任**，所以这个项目的主线就是一件事：在不牺牲召回的前提下把噪声打下来，并且**每一步
改进都要有可复现的测量**。

**架构**（数据流四段）：① diff 解析后主动预取上下文——约定文档、改动文件全文、
import 追踪（flat 和 src/ 布局都解析）、调用方片段，预算封顶 28k 字符；② finder 在
agent loop 里带三个只读工具（read_file/search_repo/run_linter）自主查证，双跑并集
（锚定跑保证确定性语义，采样跑补召回，结构化去重）；③ verifier 独立二次复核，同一份
候选跑两个 pass（顺序倒转做确定性去相关），keep/drop 双票制——双 drop 才丢弃，分歧
保留并标 uncertain 附少数派理由；④ 正则哨兵扫 drop 理由，命中"prompt 明令禁止的
驳斥话术 × 受保护缺陷类别"合取的降级回 uncertain。输出带完整审计通道（dropped 条目
带 drop_reason，uncertain 单独成节），全链 JSONL trace。

**评测台架**（这个项目一半的工程量在这里）：16 diffs/30 埋点公开集从真实项目 bug
蒸馏，含陷阱用例专测误报、信息缺失用例专测编造；6 diffs/7 埋点 holdout 做验收回归门；
LLM judge 结构化裁决（GLM 交叉重判 90/90 一致收窄同模型偏置）；repeat_eval 做 n 次
重复跑 + per-bug 翻转表把运行方差和真 miss 拆开；replay_verifier 回放存盘的 finder
输出做配对 A/B，verifier 迭代成本降 60%。十几周里有回滚（W8 判据改写验收失败）、
有推翻自己（W7 复验证明"预取伤 recall"是单次噪声）、有负结果入档（W17 第三票被
证据否决）——验收纪律是预写门槛，不是事后找理由。

**工程质量**：178 个零 API 测试（golden 测试锁请求序列和 trace 事件流）、覆盖率 95%
（门禁 85%）、mypy/ruff 干净、CI 矩阵 Linux 3.10–3.13 + Windows、Docker 打包、
一键 `scripts/verify.py` 离线全验证。

**限制我先说**：单项目人工埋点评测、规模小、judge 同模型偏置只被部分收窄、哨兵正则
和特定模型措辞耦合、真实代码库泛化只有一次 3-commit 抽查——数字是工程迭代信号，
不是行业 SOTA 声明。

---

## 二、系统架构与数据流

```text
diff（文件/--commit/--uncommitted/--pr）
  → context.py：build_context() 预取（约定文档+改动文件+import+调用方，cap 28k）
  → agent.py：finder run1(temp=0) + run2(temp=0.7)，各自在 agentloop 里按需调工具
  → findings.py：token-set Jaccard 双档去重 + 文件级 scope 过滤（diff 外→out_of_scope）
  → verifier.py：pass A + pass B（候选倒序），keep/drop 双票合并
      keep+keep→confirmed  drop+drop→dropped  分歧→uncertain+少数派理由
  → sentinels.py：rescue_forbidden_drops()——2/2 drop 且理由命中禁止话术→降级 uncertain
  → render.py/github_review.py：JSON / Markdown / PR 行内评论载荷（--post-dry-run）
  → tracelog.py：全程 JSONL 事件流（llm_response/tool/submit_rejected/verdicts + token 计量）
```

要点：finder/verifier 共用一个 loop 引擎（agentloop.py），差异只在 prompt、载荷 schema
和成败映射；结构化输出做成 `submit_review` tool call，schema 当函数参数，不依赖厂商
专有 JSON mode；护栏是 MAX_STEPS=10、坏载荷回填重试上限 2、同参数重复调用短路、
temp=0、120s 超时 + SDK 重试 2 次。

## 三、设计动机快答

### 为什么不是"一次 Prompt 调用"？

一次调用没有证据获取、没有独立复核、没有可审计的中间态。我有 ablation 数据：
`--no-context --no-verify` 就是单次调用形态的近似（V0），precision 0.40、每轮 36 条
噪声；加 verifier 后 precision 0.83、noise 降 86%。另外单次调用的输出不可控——
我把提交做成 tool call 过结构校验，非法载荷回填重试，这在单 prompt 里做不到。
diff 之外还有信息缺失问题：d16 用例（import 了仓库里不存在的模块）单次调用只能编造，
带工具的 agent 能查证后诚实报告"该模块不存在"。

### 为什么 Finder/Verifier 分离？

召回和精度的最优 prompt 姿态是冲突的：finder 要"宁可多报"，verifier 要"证据不足就砍"。
一个角色内让模型自我反思（Reflection）我试过等效形态——没有查证手段的复核只能猜
（W6 之前 d5 被错杀就是这么来的），所以 verifier 挂同款只读工具，规则是"依赖未见代码
的 finding 必须先查再判"。分离还带来独立失败域：verifier 挂了 fail-open 放行并标注
`verifier_status`，不会把 finder 的结果一起带走。

### 为什么需要双运行、去重、scope 和 sentinel？

四个机制对着四个实测的失败模式，不是堆砌：

- **finder 双跑**（W12）：per-bug 翻转表显示部分埋点是采样波动（hit 1/3、2/3），
  temp=0 单跑锁死了这部分召回。锚定跑+采样跑并集，recall 0.845→0.900。
- **去重**：双跑必然产生近重复。token-set Jaccard 双档（≥.40 任意行距 / ≥.25 且
  Δ行≤4，阈值实测自跨 run 对），残余靠 verifier 的 duplicate→DROP 兜底。
- **scope**（W12）：finder 会报 diff 之外的旧账。文件级 scope 把 diff 外发现降级
  out_of_scope，不验证不计 precision——协议层 FP 摇摆就此终结（FP 三轮全 0）。
- **sentinel**（W13–W17）：verifier 对边界 bug 的裁决有方差，会用 prompt 明令禁止的
  话术砍真 bug（累计 5 例历史错杀）。措辞改写救不了（W8 验收失败回滚），所以上
  编排层机制：正则匹配"禁止话术×受保护类别"合取，命中降级 uncertain 而非丢弃，
  失败方向安全（失配只会不救）。

## 四、工程可靠性

### 工具调用安全边界

工具全部静态只读，不执行被审代码（run_linter 是 pyflakes 静态分析）。read_file 做
`resolve()` 后 `is_relative_to` 逃逸检查（symlink 也被解析）；search_repo 用 `os.walk`
进目录前剪枝，不进 vcs/venv/缓存树；git/gh 子进程全部 list 形式无 shell 注入，
`--commit`/`--pr` 对 `-` 开头的参数注入有校验；每次 API 请求 120s 超时。

### 如何防止读取 .env 和私钥？

曾有真实缺口：search 侧有 SKIP_DIRS/后缀白名单，read 侧没有——模型可以合法读仓库内
`.env`，内容会进对话上下文和 trace 落盘。已修：`_refuse_read` 统一拦 SKIP_DIRS +
敏感文件黑名单（`.env*`/`*.pem`/`*.key`/`id_rsa*`/`credentials*`），选黑名单而非白名单
是因为被审仓库可以有任意语言后缀。有回归测试保证密钥值不出现在错误文案里
（tests/test_p0_fixes.py）。Docker 侧 `.dockerignore` 同样排除 `.env*` 和密钥文件。
自己仓库的 key 卫生：`git log --all -- .env` 为空，`.gitignore` 首行就是它，
`.env.example` 只有占位符。

### 如何处理工具失败、重复调用和模型不稳定？

- **工具失败**：返回可行动的 `Error:` 文本而非抛异常——"哪里错了+下一步试什么"
  （候选路径、"仓库里确实没有"），把失败变成模型可恢复的信息。
- **重复调用**：同参数重复调用直接短路返回提示；连续 3 次搜索 clean miss 注入
  nudge（"一次未命中已证不存在，缺失本身可报告"）——这是对着 trace 里实测的
  not-found 搜索变体链（每轮 ~12.7 条 3+ 连败链）做的，连败 calls 降 27%–46%。
- **模型不稳定**：坏 submit 载荷结构校验后回填问题重试（上限 2）；anchor finder
  空响应重试一次；finder2/verifier 每请求级 API 异常（超时/5xx）走降级语义
  （finder2 退单跑、verifier degraded/failed_open），不击穿整个 run；但账号级异常
  （AuthenticationError/RateLimitError）刻意穿透响亮失败——凭据问题不允许静默
  fail-open。verifier 双 pass 本身就是对裁决方差的机制性对冲。

## 五、评测方法论

### 如何设计离线评测、holdout 和黄金测试？

- **评测集**：从真实项目 bug 蒸馏 16 diffs/30 埋点，diff 由 pre/post 机械生成，
  `check_consistency.py` 保证 diff↔fixture↔truth 三方一致；故意混入 2 个零埋点陷阱
  用例（测误报）和 1 个信息缺失用例（测编造）。ground truth 每埋点附命中标准，
  judge 按标准裁决而非自由打分。
- **holdout**：6 diffs/7 埋点独立副本，纪律是"只在 prompt/判据验收时跑"——作为
  防主集过拟合的回归门。如实交代：它被跑过 15+ 次并据结果迭代，实际是第二开发集
  （详见 Q7）。
- **黄金测试**：FakeClient 锁定完整请求序列（消息/工具/催交/拒绝文案）和 trace
  事件流——不是测输出对不对，是测**行为没变**，src/ 迁移、哨兵外置这类重构全靠它
  兜底。判据是：任何 prompt/编排改动必须让 golden 测试红，否则改动没生效。
- **仪器先于迭代**：W7 先建 repeat_eval（方差归因）再做机制迭代；W13 建回放台架
  把 verifier 迭代从"全量重跑 1.55M token"降到"回放 0.34M"。方法论沉淀：n=1 验收
  门会被 verifier 方差假触发，归因必须是验收判定的一部分。

### 95% 覆盖率和 CI 的意义？

如实定位：覆盖率是**回归防护网的完备度指标**，不是正确性证明。95%（分支覆盖，
门禁 85%）的意义在于这个项目的核心资产是行为契约——golden 测试锁住的请求序列、
降级语义、哨兵分类，任何人（包括另一个 agent）动代码，破坏契约会立刻红。CI 矩阵
（Linux 3.10–3.13 + Windows 3.11）验证的是"干净克隆可复现"，lock-check job 验证
评测环境存证可安装，container-smoke 验证打包链路。178 个测试全部零 API 调用，
CI 不需要任何 key——这是刻意的设计约束，评测（要花钱）和验证（免费）严格分层。

## 六、权衡与局限

### 成本、延迟、召回率、误报率之间的权衡

- **召回 vs 误报**：verifier 用 -0.033 recall（均值约 1 个埋点）换 precision
  0.40→0.83、noise -86%——按"误报烧信任"的产品约束这是划算的。分歧不二选一：
  uncertain 通道把边界条目保留但隔离呈现，把二元取舍变成三态。
- **成本 vs 召回**：finder 双跑原始 tokens_in +69.7%，看起来贵；W14 用缓存感知计价
  实测（cache 命中 90%，hit 价 1/120）真实计费 ¥1.72/全量轮，裁决"不重要"，
  争论永久关闭。教训：**成本决策必须用真实账单口径**，原始 token 数高估一个量级。
- **延迟**：诚实答案是并行化优先级输给了正确性——review 是离线批处理，延迟不在
  关键路径；成本台账显示真实约束是 token 账单不是 wall-clock。最坏上界
  (10+10+2×6) 步 × 120s/步，无整轮预算兜底，这是已知待办。
- **输出 token 占真实账单 72.6%**：今后的成本杠杆在压输出（简洁 submit schema），
  不在检索侧优化。

### 当前项目最明显的局限

按杀伤力排序：① **泛化未证**——所有指标来自单项目人工埋点集，分布外证据只有
W16 一次 3-commit 抽查；② **哨兵正则措辞级耦合**——逆向自单一模型族的 drop 话术，
换 provider 必须重跑 sweep，这是把评测观察固化进生产代码的边界案例（辩护见 Q1）；
③ **judge 同模型偏置**——交叉重判收窄了但共享盲区无法排除，人工校准只有 9 埋点；
④ **holdout 已污染**（第二开发集）；⑤ n=3 无显著性检验。

### 如果进入真实生产环境，下一步如何扩展？

- **接入层**：GitHub App/webhook 替代手动 CLI，PR open/sync 自动触发，live post
  行内评论（载荷构建和 dry-run 已就绪，差远程仓库和权限模型）
- **正确性**：开放世界 truth 维护（把真实 PR 中的集外真发现回填评测集）、
  按 provider 重跑哨兵 sweep 的自动化门、人工反馈闭环（accept/reject 信号回流）
- **性能**：finder1/2 与 verifier A/B 并行化（threading 即可，改动面在 trace 事件序
  确定性）、整轮延迟预算兜底、大仓库增量上下文缓存
- **多语言**：run_linter/import 追踪目前 Python 特化，工具接口本身语言无关，
  按语言插件化 linter 和 import 解析器
- **运维**：trace 已是 JSONL，接监控面板和成本告警是搭车工作；按 repo 的
  rate limit 与队列；敏感信息双向扫描（读入侧已有黑名单，输出侧待加）

---

## 七、高频面试问题（17 问）

### Q1「verifier 里那些正则哨兵，是不是把 eval 答案抄进了生产代码？」

**追问链**：换个仓库还成立吗？敢删吗？删了掉多少分？这和 if-else 写测试答案有何区别？

**建议回答**：

- 先承认攻击面：模式确实**逆向自 5 例历史真 bug 错杀的 drop_reason 措辞**，措辞级耦合是真实风险。
- 再给辩护结构（三点）：① 哨兵编码的是**规则不是用例**——每族都是"reason 用了 prompt 明令禁止的推理 × issue 属于该规则保护的类别"的合取，不是对某条 finding 的白名单；② 失败方向安全——正则失配只会"不救"，救活的也只进 uncertain 通道并带 `[sentinel:tag]` 标签可审计，不会伪造 keep；③ 验证不是只看命中——历史结果目录全量 sweep 零误救，负例（"does not document that"这类穿着引用外衣的 nit）冻结为单测。
- 最后主动交底：泛化性是**开放问题**，已写进 `src/code_review_agent/sentinels.py` 模块 docstring 的 KNOWN LIMITS 节；W16 真实 PR 抽查中哨兵零触发零误救（reason 门命中、issue 门正确拦截），是合取设计的首个分布外实证；换 provider 前必须重跑 sweep。

### Q2「read_file 能不能读到 .env？路径逃逸怎么防？」

**建议回答**：逃逸检查一直是对的（`resolve()` 后 `is_relative_to`，symlink 也被解析）。
但曾有真实缺口：search 侧有 SKIP_DIRS/后缀白名单，read 侧没有——模型可以合法读仓库内
`.env`/`.git/config`，且内容会进对话上下文和 trace 落盘。已修：`_refuse_read` 统一拦
SKIP_DIRS + 敏感文件黑名单（`.env*`/`*.pem`/`*.key`/`id_rsa*`/`credentials*`），选黑名单
而非后缀白名单是因为被审仓库可以有任意语言后缀。有回归测试保证密钥值不出现在错误文案里
（`tests/test_p0_fixes.py::TestReadFileGuards`）。git/gh 子进程一直是 list 形式无 shell 注入，
但 `--commit`/`--pr` 以 `-` 开头的参数注入已补校验。

### Q3「search_repo 在带 .venv 的仓库上跑一次遍历多少文件？」

**建议回答**：曾经确实是 `sorted(rglob("*"))` 全遍历后再过滤——.venv 的两千多个文件每次
search 都白走。已改 `os.walk` 进目录前剪枝（`dirnames[:] = ...`），结果再排序保持输出序
不变（golden 测试锁行为）。进一步的优化（会话级文件内容缓存、ripgrep 子进程）在成本台账
上排过优先级：eval 仓库规模下不是瓶颈（W14 成本裁决：输出 token 占账单 72.6%，检索不是
主要开销），所以只修了复杂度不对的部分，没做缓存——这是"按测量分配工程预算"的取舍。

### Q4「verifier 挂了 fail-open 放行所有噪声，用户怎么知道？」

**建议回答**：fail-open 的方向是深思过的——坏掉的 verifier 静默吃掉 finding 比放行噪声更
危险（漏报不可恢复，噪声可人工过滤）。原缺口是放行时用户无感知，已修：`verify_findings`
返回 `status`（ok/degraded/failed_open），JSON 输出带 `verifier_status` 字段，markdown 渲染
在 failed_open 时置顶警示条。单 pass 失败降级也同理标注。补充边界（W17）：fail-open 只给
基础设施类异常；AuthenticationError/RateLimitError 刻意穿透崩掉——凭据/配额问题必须响亮
失败，不允许静默降级。出处：`verifier.py::verify_findings`、golden 测试三个 status 值全锁。

### Q5「finder 跑两遍、verifier 跑两遍，为什么串行？最坏延迟多少？」

**建议回答**：诚实答案是并行化在优先级上输给了正确性和评测工作——review 是离线批处理场景，
延迟不在关键路径；成本台账显示真实约束是 token 账单不是 wall-clock。已知上界：最坏
(10+10+2×6) 步 × 120s/步 + SDK 重试，确实没有整轮预算兜底，这是待办（和并行化一起，
因为都要动 agentloop 的 golden 契约）。能并行的点我清楚：finder1/2 独立、verifierA/B 独立，
threading 即可，改动面在 trace 事件序的确定性上。

### Q6「judge 和被测 agent 同一个模型，recall 数字可信吗？」

**建议回答**：这是我在 README 限制节公开声明的第一风险，且已经做了实测收窄：W16 用
GLM-5.2 独立重判 W14 全部 3 个 run——90/90 埋点命中判定 100% 一致、零翻转，FP/noise
分类一致率 96%，judge 协议（tool-calling 结构化裁决 + 逐埋点命中标准 + temp=0 + 校验
重试）在另一家 provider 上零改动跑通。所以双 judge 仲裁机制不立项。残余风险是**共享
盲区**——两个模型都判不出的命中形态，交叉重判排除不了，要靠人工校准抽样兜底，而人工
校准只有 W2 的 9 埋点（这是欠账）。这些数字我定位为工程迭代信号，不是论文结论。

### Q7「holdout 跑了 15 次还叫 holdout 吗？」

**建议回答**：不叫。它从 W8 用到 W17，每次验收都看结果做归因，实际上已经是第二开发集——
README 限制节原话就是这么写的。辩护点只有一个：它的**用途**是回归门（防止改动砍伤已有
能力），不是泛化证明；真正的泛化证据是 W16 的真实 PR 抽查（从未被任何迭代看过的分布）：
11 kept ≈ 8 真 / 2 低价值 / 1 待复核，15 drop 全可辩护，抽检零编造。如果重来，我会锁一个
"只揭一次"的封存集 + 用轮换的新样本做每周验收。

### Q8「n=3、30 个埋点，凭什么说 V2 优于 V1？p 值呢？」

**建议回答**：n=3 的 mean[min–max] 确实只够做方差归因（哪个埋点在翻转），不够做显著性
断言。已加固：聚合现在输出 stdev + 种子化 bootstrap 95% CI，并且关键改进是**换单位**——
对 ~30 个埋点重采样的 bug 级 CI（W14 v2 recall CI [0.811, 0.978]）比对 3 个 run 重采样有
统计意义得多，因为采样不确定性主要来自评测集而不是 run 数。V2 vs V1 的 F1 差（0.53→0.82）
远超这个区间宽度，结论稳；V0 vs V1 那种 0.01 级差异我在 W7 就复判为"不成立"，方向一致。
出处：`repeat_eval.py::bootstrap_ci/bug_level_recall_ci` + `tests/test_repeat_stats.py`。

### Q9「deepseek-v4-pro 是别名，W3 和 W15 面对的是同一个模型吗？」

**建议回答**：无法保证，这是跨代对比的真实混淆变量，README 限制节已声明。止血措施：
trace 现在每 run 记录 meta(provider/model)，`LLM_MODEL` env 可锁定快照 id；每周版本对比
以同周内 A/B（replay_verifier 的配对回放）为准——同周内模型漂移风险小得多，跨代趋势线
只当叙事参考。

### Q10「agent 找到你没埋的真 bug，会被判成什么？」

**建议回答**：会被判 FP/noise——封闭世界假设，precision 因此是有偏低估，且方向性惩罚
"多找对"。这是已声明的限制。缓解手段是人工抽检被判 noise 的条目（W2 做过一轮，扩集后
没重做，这是欠账）。彻底修复要么开放世界判定（judge 允许"集外真 bug"类别，但那又放大
judge 偏置），要么周期性把集外真发现回填进 truth——我倾向后者，W16 真实 PR 抽查天然
提供这类样本。

### Q11「你的包装到别人环境里会发生什么？」

**建议回答**：现在是 src/ 布局 + 单一 `code_review_agent` 包，只暴露一个顶层名。曾经的
确是 py-modules 平铺（`tools`/`llm`/`context` 这种通用名直接进 site-packages），是我做完
打包后补的课——`pip install -e .` + golden 测试做安全网完成迁移，`crag` 和
`python -m code_review_agent` 双入口，CI 里有装包冒烟，另有 Dockerfile（slim 基底、
非 root 用户、.dockerignore 排除密钥与 VCS 元数据）等 CI 容器冒烟验证。

### Q12「依赖怎么锁的？CI 和你本地跑的是同一份环境吗？」

**建议回答**：三层——pyproject 抽象约束（openai>=1.40,<3 封上界防 3.x 破坏性升级）、
requirements.lock 精确锁定（评测环境的存证）、CI 双通道验证（矩阵装 `-e ".[dev]"` 跑
Linux 3.10–3.13 + Windows 3.11；lock-check job 专门验证 lockfile 可安装）。本地和 CI
跑的是**同一个入口** `scripts/verify.py`：ruff → coverage 单测（85% 门禁）→ mypy →
双 CLI 冒烟 → 评测资产一致性，本地过 = CI 过（差异只剩平台）。如实声明：CI 配置齐了
但还没建远程 GitHub 仓库，从未在 GitHub 上真正跑过。

### Q13「.env 在你仓库里，key 泄漏了吗？」

**建议回答**：给证据链——`git log --all -- .env` 为空（从未被任何 commit 触碰）、
`.gitignore` 第一行就是它、全部 revision grep 无真实 key。属于 gitignore 先行做对了的
情况。但操作卫生上：key 明文落盘且目录被展示过，按"已暴露"处理去控制台轮换了，并加了
`.env.example` 占位模板。如果真提交过：rotate 立即 + filter-repo/BFG 重写历史 + force
push，`git rm --cached` 不够（历史仍在）。

### Q14「测试测了什么？覆盖率多少？」

**建议回答**：178 个零 API 测试、分支覆盖率 95%（门禁 85%，2026-07-15 本机实测）。
三层——golden 测试用 FakeClient 锁**请求序列和 trace 事件流**（行为保持重构的安全网，
src/ 迁移和哨兵外置都靠它兜底）、纯函数单测（校验/合并/去重/指标/哨兵分类含冻结负例）、
回归测试（P0 安全修复、src-layout import 解析、CLI 参数路径、工具协议、provider 构建）。
已知缺口主动交代：judge 的 LLM 回路有 golden 测试但 repeat_eval 的编排逻辑无测（统计
函数有测）；`__main__.py` 和 agent.py 的部分 live 异常分支未覆盖（91%），是 mock 成本
最高的路径。

### Q15「为什么不用 embeddings/RAG 做上下文检索？」

**建议回答**：按需求选工具——review 的检索目标是**确定性可解释**的：改动文件的
import 定义、函数调用方、约定文档，符号级字符串检索 + 结构化预算就够，且零基础设施、
零索引延迟、结果可在 trace 里逐行审计。向量检索解决的是"语义相似但字面不同"的召回，
在 diff→定义/调用方这种强结构关系上不占优，还引入嵌入模型这个新的不确定源。数据支撑：
预取 + 按需工具的组合在评测上没有输给纯工具基线（W7 复验 recall 均值相同且区间更窄），
成本台账也显示检索不是账单主项（输出 token 占 72.6%）。如果目标变成"全仓找相似历史
bug"，我会重新评估向量方案。

### Q16「被审仓库里有恶意内容怎么办？比如文件里写'忽略之前的指令'？」

**建议回答**：读入侧是真实攻击面——工具结果会拼进对话。现有缓解是结构性的：工具全部
只读（prompt injection 拿不到写/执行能力）、敏感文件黑名单挡住最有价值的目标（key）、
submit 载荷过结构校验（注入无法直接伪造裁决字段）、全链 trace 让注入痕迹可审计。
如实交代：没有专门的注入检测层，改动文件内容原文进 prompt，恶意注释理论上可以影响
finder 的报告倾向——但影响上限是"报错东西/漏报"，被 verifier 的独立复核和人工终审
兜住一部分。生产化清单里这是必做项（内容消毒 + 注入探针用例进评测集）。

### Q17「verifier 双 pass 为什么是倒序呈现，而不是换个模型？」

**建议回答**：目标是去相关，手段按成本排序。顺序反转是**零成本、确定性**的去相关——
LLM 对列表位置敏感（首因/近因效应），同一模型换呈现顺序就能让边界裁决的错误不完全
重叠，W9 实测分歧通道把 recall 从 0.778 拉回 0.856。换模型去相关更强，但引入第二家
的成本、延迟、措辞分布差异（哨兵正则要重验），而 W16 交叉重判显示两家模型在这个
任务上判定高度一致（90/90），异构收益可能有限。drop 需 2/2 票的结构下，单 pass
误砍率 p 变 p²，这个数学不依赖 pass 之间完全独立，部分去相关就有收益。

---

## 八、事实边界：已验证 / 待 CI 验证 / 计划

面试中被问到任何能力，先落到这三档之一：

**已实现并本机验证（2026-07-15，Windows 11 / Python 3.13）**：

- 178 个零 API 测试全过、分支覆盖率 95%（门禁 85%）、ruff/mypy 干净
- `python -m code_review_agent --help` 与 `crag --help` 双入口冒烟通过
- eval（16 diffs/30 埋点）与 holdout（6 diffs/7 埋点）资产一致性校验通过
- src-layout import 解析（含回归测试：flat/src 布局预取、缺失模块 note、外部静默）
- 全部评测数字（截至 W17）产自真实 run 并有 trace/结果目录存证

**已实现但只由 CI 待验证（尚无远程仓库，CI 从未实际运行）**：

- GitHub Actions 矩阵（Linux 3.10–3.13 + Windows 3.11）——本机只验证了 3.13
- Docker 镜像构建与容器冒烟（本机无 Docker，构建从未执行过）
- lockfile 安装校验 job
- live PR post（`--post`）——载荷构建与 dry-run 有测试，真实发帖待 GitHub 仓库

**后续计划（未实现，别说成已有）**：

- 公共 GitHub 仓库发布（待密钥审计）、CI badge
- finder/verifier 并行化与整轮延迟预算
- 开放世界 truth 回填、人工校准扩样、注入探针用例
- 多语言 linter/import 插件化、GitHub App 自动触发

## 九、一页速记（数字）

| 项 | 值 | 出处 |
| --- | --- | --- |
| 评测集 | 16 diffs / 30 埋点 + holdout 6/7 + 3 陷阱 | eval/truth.json |
| V0→V2 precision | 0.40 → 0.83（noise -86%） | README W7 表（n=3） |
| W12 终测 | recall 0.900、F1 0.830、FP 三轮全 0 | README W12 节 |
| bug 级 recall CI | [0.811, 0.978]（W14 v2） | repeat_eval |
| 哨兵战绩 | 5/5 历史错杀命中、sweep 零误救、分布外零误触 | sentinels.py + W16 |
| judge 交叉重判 | GLM 独立重判 90/90 一致、分类一致率 96% | cases.md W16 |
| 成本 | ¥1.72/全量轮，¥0.11/review，输出占 72.6% | W14 台账 |
| 测试 | **178 个零 API，覆盖率 95%（门禁 85%）** | 2026-07-15 本机实测 |
| CI | Linux 3.10–3.13 + Win 3.11 + Docker smoke，**已配置未远程运行** | ci.yml |
