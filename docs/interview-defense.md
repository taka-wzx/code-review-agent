# 面试答辩手册 — code-review-agent

> 用途：面试前速览。每条 = 面试官问法 → 追问链 → 建议回答。
> 回答姿态统一为：**承认问题曾存在 → 说明修复/取舍 → 给出代码或数据出处**。
> 这份文档本身产自一轮"模拟大厂面试官"的三方向深度审查（架构/评测方法论/工程化）。

## 30 秒项目陈述

两遍式 code review agent：finder 双跑（temp 0 锚定 + temp 0.7 采样）出候选 →
结构化去重/文件级 scope 过滤 → verifier 双 pass 独立复核（分歧进 uncertain 通道）→
JSON/Markdown 输出。Provider 无关（OpenAI 兼容接口）。配套 16 diffs/30 埋点评测集 +
holdout + LLM judge + n 次重复跑方差归因 + 全链 JSONL trace。核心数字：verifier 把
precision 从 ~0.35 提到 ~0.79（noise -86%），W14 实测全量单轮 ¥1.72。

---

## 一、架构与代码质量（字节风格：抓设计气味往死里追）

### Q1「verifier 里那 100 行正则哨兵，是不是把 eval 答案抄进了生产代码？」

**追问链**：换个仓库还成立吗？敢删吗？删了掉多少分？这和 if-else 写测试答案有何区别？

**建议回答**：
- 先承认攻击面：模式确实**逆向自 5 例历史真 bug 错杀的 drop_reason 措辞**，措辞级耦合是真实风险。
- 再给辩护结构（三点）：① 哨兵编码的是**规则不是用例**——每族都是"reason 用了 prompt 明令禁止的推理 × issue 属于该规则保护的类别"的合取，不是对某条 finding 的白名单；② 失败方向安全——正则失配只会"不救"，救活的也只进 uncertain 通道并带 `[sentinel:tag]` 标签可审计，不会伪造 keep；③ 验证不是只看命中——6 个结果目录全量 sweep **零误救**，负例（"does not document that"这类穿着引用外衣的 nit）冻结为单测。
- 最后主动交底：泛化性是**开放问题**，已写进 `src/code_review_agent/sentinels.py` 模块 docstring 的 KNOWN LIMITS 节，W16 真实 PR 抽查是裁决门；换 provider 前必须重跑 sweep。
- 出处：`src/code_review_agent/sentinels.py`（设计依据/验证方法/已知限制三节齐全）。

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
上排过优先级：eval 仓库规模下不是瓶颈（W11 成本裁决：输出 token 占账单 72.6%，检索不是
主要开销），所以只修了复杂度不对的部分，没做缓存——这是"按测量分配工程预算"的取舍。

### Q4「verifier 挂了 fail-open 放行所有噪声，用户怎么知道？」

**建议回答**：fail-open 的方向是深思过的——坏掉的 verifier 静默吃掉 finding 比放行噪声更
危险（漏报不可恢复，噪声可人工过滤）。原缺口是放行时用户无感知，已修：`verify_findings`
返回 `status`（ok/degraded/failed_open），JSON 输出带 `verifier_status` 字段，markdown 渲染
在 failed_open 时置顶警示条。单 pass 失败降级也同理标注。出处：`verifier.py::verify_findings`、
`render.py`、golden 测试三个 status 值全锁。

### Q5「finder 跑两遍、verifier 跑两遍，为什么串行？最坏延迟多少？」

**建议回答**：诚实答案是并行化在优先级上输给了正确性和评测工作——review 是离线批处理场景，
延迟不在关键路径；成本台账显示真实约束是 token 账单不是 wall-clock。已知上界：最坏
(10+10+2×6) 步 × 120s/步 + SDK 重试，确实没有整轮预算兜底，这是 P3 待办（和并行化一起，
因为都要动 agentloop 的 golden 契约）。能并行的点我清楚：finder1/2 独立、verifierA/B 独立，
threading 即可，改动面在 trace 事件序的确定性上。

---

## 二、评测方法论（百度风格：挑统计与实验设计）

### Q6「judge 和被测 agent 同一个模型，recall 数字可信吗？」

**建议回答**：不完全可信，且我在 README「限制」节公开写了这一点。self-preference 偏置 +
共享盲区（模型不懂的 bug，agent 漏、judge 也判不出漏）意味着指标上限被同一模型能力封顶。
缓解设计：judge 有结构化裁决 schema + 逐埋点命中标准（不是自由打分）、temp=0、校验重试。
计划中的修复：GLM 交叉重判（代码支持 `LLM_PROVIDER` 切换，阻塞在 key），目标是报跨 judge
一致率。**主动交底比被抓到强**：这些数字我定位为工程迭代信号，不是论文结论。

### Q7「holdout 跑了 15 次还叫 holdout 吗？」

**建议回答**：不叫。它从 W8 用到 W15，每次验收都看结果做归因，实际上已经是第二开发集——
README 限制节原话就是这么写的。辩护点只有一个：它的**用途**是回归门（防止改动砍伤已有能力），
不是泛化证明；真正的泛化证据要等 W16 真实 PR 抽查（从未被任何迭代看过的分布）。如果重来，
我会锁一个"只揭一次"的封存集 + 用轮换的新样本做每周验收。

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

---

## 三、工程化（华为风格：抠可安装、可复现、可校验）

### Q11「你的包装到别人环境里会发生什么？」

**建议回答**：现在是 src/ 布局 + 单一 `code_review_agent` 包，只暴露一个顶层名。曾经的
确是 py-modules 平铺（`tools`/`llm`/`context` 这种通用名直接进 site-packages），是我做完
打包后补的课——`pip install -e .` + golden 测试做安全网完成迁移，`crag` 和
`python -m code_review_agent` 双入口，CI 里有装包冒烟。

### Q12「依赖怎么锁的？CI 和你本地跑的是同一份环境吗？」

**建议回答**：三层——pyproject 抽象约束（openai>=1.40,<3 封上界防 3.x 破坏性升级）、
requirements.lock 精确锁定（pip freeze，评测环境的存证）、CI 双通道验证（矩阵装 -e .
测 3.10/3.11/3.12 × ubuntu+windows；lock-check job 专门验证 lockfile 可安装）。CI 还有
ruff lint + coverage 报告（先量化后设门槛）+ eval 资产一致性校验。

### Q13「.env 在你仓库里，key 泄漏了吗？」

**建议回答**：给证据链——`git log --all -- .env` 为空（从未被任何 commit 触碰）、
`.gitignore` 第一行就是它、全部 revision grep 无真实 key。属于 gitignore 先行做对了的
情况。但操作卫生上：key 明文落盘且目录被展示过，按"已暴露"处理去控制台轮换了，并加了
`.env.example` 占位模板。如果真提交过：rotate 立即 + filter-repo/BFG 重写历史 + force
push，`git rm --cached` 不够（历史仍在）。

### Q14「测试测了什么？覆盖率多少？」

**建议回答**：三层零 API 测试——golden 测试用 FakeClient 锁**请求序列和 trace 事件流**
（行为保持重构的安全网，src/ 迁移和哨兵外置都靠它兜底）、纯函数单测（校验/合并/指标/
哨兵分类含冻结负例）、eval 资产三方一致性校验（diff↔fixture↔truth）。P0 修复各有回归
测试。已知缺口主动交代：judge 的 LLM 回路和 repeat_eval 的编排逻辑无测（后者的统计函数
现已有测）；coverage 在 CI 里是报告模式，还没设门槛。

---

## 四、可以主动输出的亮点（别等问）

1. **错误信息可恢复设计**：工具失败返回"哪里错了 + 下一步试什么"（候选路径、"仓库里
   确实没有"），把失败变成模型可行动的信息——对着失败模式手册逐项做的。
2. **结构化输出的厂商无关解法**：submit 做成 tool call、schema 当参数，不依赖任何
   provider 专有 JSON mode，DeepSeek/GLM 通用。
3. **分歧即不确定性**：verifier 双 pass 措辞零改动、仅顺序反转去相关，pass 间分歧直接
   变 uncertain 标签——不让 prompt 自报置信度。
4. **成本纪律**：每周真实计费入台账，W14 用实测（¥1.72/轮、输出占 72.6%）关闭了"双跑
   太贵"的争论——用测量代替直觉做工程决策。
5. **诚实的限制文档**：README 限制节把 judge 同模型、holdout 污染、n=3、模型漂移全部
   白纸黑字——评测数字定位成工程日志，不冒充科学结论。

## 五、一页速记（数字）

| 项 | 值 | 出处 |
| --- | --- | --- |
| 评测集 | 16 diffs / 30 埋点 + holdout 6/7 + 3 陷阱 | eval/truth.json |
| V0→V2 precision | 0.35 → 0.79（noise -86%） | README W6/W7 表 |
| V2 F1（n=3） | 0.819，bug 级 recall CI [0.811, 0.978]（W14） | repeat_eval |
| 哨兵战绩 | 5/5 历史错杀命中、6 目录 sweep 零误救 | sentinels.py |
| 成本 | ¥1.72/全量轮，¥0.11/review，输出占 72.6% | W14 台账 |
| 测试 | 111 个零 API（golden+纯函数+资产一致性） | tests/ |
