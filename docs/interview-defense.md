# 面试答辩手册 — code-review-agent

> 用途：面向华为、字节等大厂 AI Agent 岗位面试的中文答辩材料，面试前速览。
> 回答姿态统一为：**承认问题曾存在 → 说明修复/取舍 → 给出代码或数据出处**。
> 所有数字均有出处（本机实测或 eval 台账），不含虚构实验数字或生产效果。
> 数据基线：2026-07-15 本机验证（Week 2 后 190 测试 / 96% 覆盖率），评测数字截至 W17。
> 措辞纪律：区分**已实现并本地验证** / **尚未在 Week 2 GitHub CI 验证** / **尚未做真实 provider 延迟基准**三档（见第八节）。

---

## 一、项目陈述

### 30 秒版

两阶段 code review agent：finder 双跑（temp 0 锚定 + temp 0.7 采样）召回候选缺陷 →
结构化去重 / 文件级 scope 过滤 → verifier 双 pass 独立证据复核（分歧进 uncertain 通道，
禁止话术砍杀被哨兵兜底）→ JSON/Markdown/PR 行内评论输出。Provider 无关（OpenAI 兼容，
DeepSeek/GLM 实测）。Week 2 起 finder/verifier 阶段内双线程并行 + 整轮 300s 软截止。
配套 16 diffs/30 埋点评测集 + holdout + LLM judge + n 次重复跑
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

**工程质量**：190 个零 API 测试（golden 测试锁请求序列和 trace 事件流，Week 2 增并发/
超时回归）、覆盖率 96%（门禁 85%）、mypy/ruff 干净、CI 矩阵 Linux 3.10–3.13 + Windows
（Week 1 已在私有仓库 master 实际运行通过；Week 2 改动尚未经 GitHub CI）、Docker 打包、
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

Week 2 起（`orchestration.py`）：finder 两跑与 verifier 两 pass 各自在阶段内用双线程
并行（`run_parallel_pair`，两阶段仍串联）；整轮共享一个 300s monotonic 软截止——每个
loop 步进前取一次时钟快照，剩余 ≤0 就不再发起新请求（trace 记 `deadline_exhausted`），
>0 则该请求 timeout = min(剩余预算, 120s)；trace 写入行级加锁，新增
`parallel_stage_started/finished` 事件。fatal/降级/fail-open 语义与串行版一致。

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

### 96% 覆盖率和 CI 的意义？

如实定位：覆盖率是**回归防护网的完备度指标**，不是正确性证明。96%（分支覆盖，
门禁 85%）的意义在于这个项目的核心资产是行为契约——golden 测试锁住的请求序列、
降级语义、哨兵分类，任何人（包括另一个 agent）动代码，破坏契约会立刻红。CI 矩阵
（Linux 3.10–3.13 + Windows 3.11）验证的是"干净克隆可复现"，lock-check job 验证
评测环境存证可安装，container-smoke 验证打包链路——Week 1 交付已在私有仓库 master
实际运行通过，Week 2 改动尚未经 GitHub CI。190 个测试全部零 API 调用，
CI 不需要任何 key——这是刻意的设计约束，评测（要花钱）和验证（免费）严格分层。

## 六、权衡与局限

### 成本、延迟、召回率、误报率之间的权衡

- **召回 vs 误报**：verifier 用 -0.033 recall（均值约 1 个埋点）换 precision
  0.40→0.83、noise -86%——按"误报烧信任"的产品约束这是划算的。分歧不二选一：
  uncertain 通道把边界条目保留但隔离呈现，把二元取舍变成三态。
- **成本 vs 召回**：finder 双跑原始 tokens_in +69.7%，看起来贵；W14 用缓存感知计价
  实测（cache 命中 90%，hit 价 1/120）真实计费 ¥1.72/全量轮，裁决"不重要"，
  争论永久关闭。教训：**成本决策必须用真实账单口径**，原始 token 数高估一个量级。
- **延迟**：Week 1 及之前无整轮兜底，最坏上界 (10+10+2×6) 步 × 120s/步。Week 2 落地
  阶段内并行（finder 两跑、verifier 两 pass 各双线程）+ 整轮 300s monotonic 软截止：
  截止后不发新请求，单请求 timeout 封顶 min(剩余, 120s)。每阶段耗时从两 lane 之和变成
  两 lane 取 max，理论加速上界 ~2x/阶段——这是结构性推断 + 离线测试验证，真实 provider
  的 p50/p95 尚未实测。
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
- **性能**：阶段内并行与整轮软截止已落地（Week 2，本地验证）；下一步是真实 provider
  延迟基准（p50/p95、stage latency、超时/429/降级率，见 Q28）、把 SDK 重试预算纳入
  剩余时间、大仓库增量上下文缓存
- **多语言**：run_linter/import 追踪目前 Python 特化，工具接口本身语言无关，
  按语言插件化 linter 和 import 解析器
- **运维**：trace 已是 JSONL，接监控面板和成本告警是搭车工作；按 repo 的
  rate limit 与队列；敏感信息双向扫描（读入侧已有黑名单，输出侧待加）

---

## 七、高频面试问题（28 问）

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

### Q5「finder 跑两遍、verifier 跑两遍，延迟怎么控？」

**建议回答**：Week 1 时的诚实答案是"并行化优先级输给了正确性，无整轮兜底"（最坏
(10+10+2×6) 步 × 120s/步）。Week 2 已落地：finder 两跑、verifier 两 pass 各自阶段内
双线程并行（两阶段仍串联，因为 verifier 的输入依赖 finder 的去重并集），整轮共享
300s monotonic 软截止——截止后不再发起新请求，单请求 timeout 封顶 min(剩余, 120s)，
原有 fatal/降级/fail-open 语义不变，golden 测试把并行 patch 成串行继续锁协议。边界
如实说：软截止不强杀在途请求（同步 SDK，见 Q21），加速比是结构性推断（阶段耗时从
两 lane 之和变 max），真实 provider 的 p50/p95 尚未实测（见 Q27/Q28）。深入追问的
展开在 Q18–Q28。

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
非 root 用户、.dockerignore 排除密钥与 VCS 元数据）——容器冒烟已随 Week 1 master CI
在私有 GitHub 仓库实际运行通过（本机无 Docker，本地构建仍未验证）。

### Q12「依赖怎么锁的？CI 和你本地跑的是同一份环境吗？」

**建议回答**：三层——pyproject 抽象约束（openai>=1.40,<3 封上界防 3.x 破坏性升级）、
requirements.lock 精确锁定（评测环境的存证）、CI 双通道验证（矩阵装 `-e ".[dev]"` 跑
Linux 3.10–3.13 + Windows 3.11；lock-check job 专门验证 lockfile 可安装）。本地和 CI
跑的是**同一个入口** `scripts/verify.py`：ruff → coverage 单测（85% 门禁）→ mypy →
双 CLI 冒烟 → 评测资产一致性，本地过 = CI 过（差异只剩平台）。如实声明：私有 GitHub
仓库已建，Week 1 交付的 master CI 已实际运行通过；Week 2 改动目前只在本地任务分支，
尚未经 GitHub CI 验证。

### Q13「.env 在你仓库里，key 泄漏了吗？」

**建议回答**：给证据链——`git log --all -- .env` 为空（从未被任何 commit 触碰）、
`.gitignore` 第一行就是它、全部 revision grep 无真实 key。属于 gitignore 先行做对了的
情况。但操作卫生上：key 明文落盘且目录被展示过，按"已暴露"处理去控制台轮换了，并加了
`.env.example` 占位模板。如果真提交过：rotate 立即 + filter-repo/BFG 重写历史 + force
push，`git rm --cached` 不够（历史仍在）。

### Q14「测试测了什么？覆盖率多少？」

**建议回答**：190 个零 API 测试、分支覆盖率 96%（门禁 85%，2026-07-15 本机实测）。
三层——golden 测试用 FakeClient 锁**请求序列和 trace 事件流**（行为保持重构的安全网，
src/ 迁移、哨兵外置、Week 2 并行化都靠它兜底：并行编排被 patch 成串行执行，协议语义
不许变）、纯函数单测（校验/合并/去重/指标/哨兵分类含冻结负例）、回归测试（P0 安全修复、
src-layout import 解析、CLI 参数路径、工具协议、provider 构建，Week 2 新增并发/超时
回归 12 个：barrier 验证 lane 重叠、截止后零请求、截止降级语义、并发 trace 完整性）。
已知缺口主动交代：judge 的 LLM 回路有 golden 测试但 repeat_eval 的编排逻辑无测（统计
函数有测）；`__main__.py` 和 agent.py 的部分 live 异常分支仍未覆盖，是 mock 成本
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

### Q18「并行为什么用线程，不用 asyncio 或多进程？」（Week 2）

**建议回答**：瓶颈是网络 I/O 等待不是 CPU——两条 lane 的绝大部分时间在等 provider
响应，请求期间 GIL 释放，threading 不受 GIL 约束。asyncio 语义上更优雅，但要把整条
调用链改成 async（换 AsyncOpenAI、工具的文件/子进程操作、agentloop 全链）或在线程池里
包同步调用——前者改动面横穿全部 golden 契约，后者本质还是线程只是多一层事件循环。
多进程为隔离付出序列化和启动成本，还要跨进程共享 trace 文件句柄，而两条 lane 本来就
只共享只读输入（review_input 字符串、client），线程共享内存正合适。并发度恒为 2，
`ThreadPoolExecutor(max_workers=2)` 一个 with 块管完生命周期（`orchestration.py::
run_parallel_pair`），agentloop 除注入 deadline 检查外一行不用为并发而改。

### Q19「为什么只阶段内并行，不四路同时跑？」（Week 2）

**建议回答**：数据依赖决定的——verifier 的输入是两个 finder 输出经去重并集 + scope
过滤后的候选列表，finder 没跑完就没有 verifier 的输入，四路同跑在数据流上不成立。
语义上也有依赖：anchor 失败是致命的，若 verifier 已经开跑而 anchor 失败，烧掉的是
真金 token。所以形状是"两段串联、段内并行"：每段耗时从两 lane 之和变成两 lane 取
max，理论加速上界 ~2x/阶段，这已是该数据流下可得的全部并行度。

### Q20「为什么叫 soft deadline，不是 hard timeout？」（Week 2）

**建议回答**：到期时它只保证"不再发起新请求"，不保证"在途请求立刻死"。实现是协作式
的：每个 loop 步进前取一次 monotonic 时钟快照（单次快照同时做 go/no-go 判定和请求
cap，避免读两次钟的边界竞态，`agentloop.py` 有注释），剩余 ≤0 → 返回
`reason="deadline"` 不再调 API；>0 → 该请求 timeout = min(剩余, 120s)。所以整轮
wall-clock 可能略超 300s——最后一个在途请求（加 SDK 重试）会跑完——但超出量被单请求
timeout 封顶。文档措辞刻意不写"硬实时超时"或"精确 300s 停"，那不是这个实现能提供的
保证。

### Q21「同步 SDK 的在途请求为什么不能安全强杀？」（Week 2）

**建议回答**：Python 线程没有安全的外部终止原语——`Thread` 没有 cancel，强行手段
（ctypes 注异常、daemon 线程弃养）会把 httpx 连接池、锁和文件句柄留在中间态，破坏
共享 client 的后续复用，换来的只是提前几十秒返回。要真正可取消，要么 asyncio（在
await 点 cancel），要么进程级 kill——都在 Q18 的取舍里被否决。soft deadline +
请求级 timeout 封顶是同步栈里最诚实的方案：新请求不发、在途请求限时，越界有上界。

### Q22「deadline、单请求 timeout、SDK retry 三者什么关系？」（Week 2）

**建议回答**：三层从外到内——review 级 deadline（300s，一个 review 一份，构建上下文前
起算）→ 请求级 timeout（每次 `create` 传入 min(剩余预算, 120s)）→ SDK retry（client
构造时 `max_retries=2`，SDK 内部）。已知交互如实说：传入的 timeout 是 per-attempt
的，SDK 重试的每个 attempt 各自享有该 timeout，所以临近截止的一次请求最坏可以
3 个 attempt + backoff，越过 deadline 的量可能数倍于当时的剩余预算。这是软截止已
声明的越界来源之一；改进方向是把重试预算也纳入剩余时间（比如临近截止时把
per-request 的 max_retries 降为 0），列在待办。loop 层面则简单：该请求返回后，下一次
步进检查立即以 `deadline` 出局。

### Q23「并行化会增加总 token 成本吗？」（Week 2）

**建议回答**：计划内不增加——并行改变的是时间排布不是调用图，请求数与内容仍是
anchor + finder2 + verifier A/B（+ 校验重试），正常路径成本与串行版完全一致。有一个
真实的行为差异要交代：finder2 现在随 anchor 并行提前启动，anchor 快速致命失败时
finder2 可能已经发出请求（串行版里 anchor 失败后 finder2 根本不启动）——golden 测试
固化了这个差异（anchor 失败用例的请求数 2→3）。另一个未量化的点：两条 lane 同时
首发，provider 前缀缓存（DeepSeek cache 命中 90% 是串行版实测）谁先建缓存不再确定，
命中率变化未实测，列在真实基准待测项里。

### Q24「为什么说并行可能增加 rate-limit 风险？」（Week 2）

**建议回答**：总请求数不变，但瞬时并发从 1 变 2——同一时刻账号在 provider 侧有两个
在途请求，RPM/并发型限流更容易触碰。语义上有兜底：RateLimitError 刻意穿透（不静默
降级），真触发时响亮失败，不会污染评测结果。但真实 provider 上 429 率是否上升未实测，
这正是不把"并行更快"写成已验证结论的原因之一（见 Q28 基准计划）。

### Q25「anchor fatal 和 verifier fail-open 语义在并行下怎么保持？」（Week 2）

**建议回答**：关键机制是**异常先捕获、join 后按固定优先级重放**。`run_parallel_pair`
把每条 lane 的异常包进 `CallOutcome` 而不是当场抛；join 之后 `run_review` 先检查
anchor 的 error 先 raise——所以无论两条 lane 谁先失败，anchor 失败都优先致命；finder2
的 error 再按与旧 `except` 链等价的 isinstance 序列处理（RuntimeError→按协议失败降级、
Auth/RateLimit→穿透、其他 OpenAIError→按请求失败降级、未知→raise）。verifier 侧
`_verify_pass` 本来就把基础设施异常吞成 None（降级/fail-open 在返回值层裁决），只有
Auth/RateLimit 以异常冒出，join 后 A 先 B 后 raise，与旧串行"A 先跑"的暴露序一致。
测试证据双保险：golden 测试把 `_run_pair` patch 成串行执行锁协议语义，week2 测试再用
真线程验证并发本身（含 anchor 截止致命、finder2 截止降级、verifier 双截止 fail-open）。

### Q26「trace 并发交错会影响统计和审计吗？」（Week 2）

**建议回答**：分两层。行完整性：`Trace.event()` 在锁内整行写 + flush，JSONL 每行原子，
并发写不会交错损坏（week2 测试多线程压写验证）。事件顺序：不同 lane 的事件按真实时间
交错，不再有"finder 事件全排在 finder2 之前"的隐含序——但消费端全部不依赖顺序：
`cost_report` / `repeat_eval` / `bench_verifier` 都按事件自带的 `kind` 字段无序聚合
token，组件归因用每条事件自带的 `component` 字段，交错无影响。审计能力反而增强：
新增 `parallel_stage_started/finished`（含阶段耗时与各 lane 异常类型）和
`deadline_exhausted`（含组件与已完成步数），阶段时序首次变得可直接审计。

### Q27「离线测试证明了什么？还有什么没证明？」（Week 2）

**建议回答**：证明了（FakeClient/barrier，零 API，190 测试 96% 覆盖率本机全绿）：
两条 lane 真实重叠（barrier 要求双 lane 同时到达才放行）、截止后零新请求、anchor 截止
致命 / finder2 截止降级 / verifier 截止 fail-open、全部 stage 共享同一个 deadline、
并发 trace 行完整、请求协议与输出 shape 与串行版一致（golden）。没证明的如实列：
真实 provider 下的 p50/p95 加速比（当前只是结构性推断）、单请求 timeout 在真实网络栈
的行为、429 率变化、DeepSeek 前缀缓存命中率变化、长 review 的实际截止表现。另外
Week 2 改动尚未在 GitHub CI 上运行（Week 1 master CI 通过的是并行化之前的代码），
本地 `scripts/verify.py --eval-assets` 全绿是当前唯一的验证证据。

### Q28「如果做真实 provider 基准，测什么？」（Week 2）

**建议回答**：配对对比（并行 vs 串行同一评测集）测五组：① 整轮与每 stage 的 p50/p95
wall-clock 和 lane 重叠率（trace 的 parallel_stage 事件直接可算）；② 单请求 timeout
触发率与 `deadline_exhausted` 触发率；③ 429/5xx 率——验证 Q24 的并发限流假设；
④ 降级率——finder2 失败率、verifier degraded/failed_open 率是否因并发上升；⑤ 前缀
缓存命中率（trace 已带 cache_hit/miss 字段，`cost_report` 直接出）与真实计费。再加一组
质量回归：并行版跑 `repeat_eval` 对比 recall/precision 无漂移——理论上请求内容不变，
但缓存与限流重试路径的变化可能间接影响输出。

---

## 八、事实边界：已验证 / 待 CI 验证 / 计划

面试中被问到任何能力，先落到这四档之一：

**已实现并本机验证（2026-07-15，Windows 11 / Python 3.13 独立 venv）**：

- 190 个零 API 测试全过、分支覆盖率 96%（门禁 85%）、ruff/mypy（14 源文件）干净
- Week 2 并行编排 + 软截止：lane 重叠、截止后零新请求、截止降级/fail-open 语义、
  并发 trace 完整性——全部由离线（FakeClient/barrier）测试验证
- `python -m code_review_agent --help` 与 `crag --help` 双入口冒烟通过
- eval（16 diffs/30 埋点）与 holdout（6 diffs/7 埋点）资产一致性校验通过
- src-layout import 解析（含回归测试：flat/src 布局预取、缺失模块 note、外部静默）
- 全部评测数字（截至 W17）产自真实 run 并有 trace/结果目录存证

**已在 GitHub 上验证（私有仓库，Week 1）**：

- 私有 GitHub 仓库已建，v0.1.0 Release 已发布（未公开）
- Week 1 交付合入 master 后 CI 已实际运行并通过：Actions 矩阵
  （Linux 3.10–3.13 + Windows 3.11）、Docker 容器冒烟、lockfile 安装校验 job

**已实现但尚未在 Week 2 GitHub CI 验证**：

- Week 2 全部改动（并行编排、软截止、线程安全 trace 及其测试）——只在本地任务分支，
  本地 `scripts/verify.py --eval-assets` 全绿；Week 1 master CI 通过的是并行化之前的代码
- live PR post（`--post`）——载荷构建与 dry-run 有测试，真实发帖未做过
- Docker 本地构建（本机无 Docker；CI 侧容器冒烟已通过）

**后续计划（未实现/未测量，别说成已有）**：

- 真实 provider 延迟基准：p50/p95、stage latency、超时率、429 率、降级率、
  缓存命中变化（见 Q28）——"并行更快"目前是结构性推断不是实测数字
- 把 SDK 重试预算纳入剩余时间（临近截止降 max_retries）
- 公开仓库发布（待密钥审计）、CI badge
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
| 延迟预算 | 300s 软截止 + 单请求 min(剩余, 120s)，阶段内双线程并行 | orchestration.py（Week 2，离线验证） |
| 测试 | **190 个零 API，覆盖率 96%（门禁 85%）** | 2026-07-15 本机实测 |
| CI | Linux 3.10–3.13 + Win 3.11 + Docker smoke，**Week 1 master 已运行通过；Week 2 改动未经 CI** | ci.yml + 私有仓库 Actions |
