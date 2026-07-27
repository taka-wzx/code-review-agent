# Phase 9H auth-004 失败分析与运行时加固 v1

Phase 9H 是纯离线工程加固，不是新 Pilot，也不是 auth-004 重跑。auth-004 的五个
attempt-1 headline 永久保持 `failed`；不得换 PR、修改五项分母、创建诊断 attempt 来覆盖
headline，或把 fake/synthetic 通过写成真实模型成功。

冻结合同见
[`plans/phase9h-auth004-failure-hardening-v1.md`](plans/phase9h-auth004-failure-hardening-v1.md)，
机器可读分析见
[`../phase9g_solo_run/phase9h-failure-analysis.json`](../phase9g_solo_run/phase9h-failure-analysis.json)。

## 证据结论

已提交的脱敏 revision 2 报告确认：5 个 selected PR、5 个 headline、0 completed、5 failed，
稳定类别都是 `provider_or_pipeline_RuntimeError`；累计 59 logical calls、59 HTTP attempts、
135,781 input Tokens、47,301 output Tokens、113,920 cached-input Tokens、费用 1,727,156
micro-CNY。Finding、人工 Review、诊断 attempt 和成功重跑均为 0。

现有脱敏 receipt 没有 finish reason、响应形状、tool-call、submit、空响应、step cap 或异常边界
的细粒度字段。因此不能确认是 provider、SDK response schema、agent loop、Finder/Verifier
step cap、空 read-tool root、输出截断或本地终止中的哪一条。所有这些根因当前均为
`unknown`；调用数、Token 数、耗时或日志片段都不能代替直接证据。

能确定的外围事实只有：SDK retries 冻结为 0；不存在 headline replacement、diagnostic rerun
或成功重跑；聚合的实际及预留预算都低于授权总上限。这些事实仍不足以排除局部 step/token/
deadline 或 provider response 兼容性问题。

永久结论边界保持：

```text
evidence_type=single_participant_exploratory
business_claim_allowed=false
quality_claim_allowed=false
formal_quality_status=incomplete
model_quality_status=not_measured
```

## 安全遥测

未来独立授权的运行只允许记录固定枚举、布尔值和非负整数：pipeline stage、稳定失败码、
finish-reason 类别、response-shape 类别、是否存在 tool call、submit/empty/step 计数、是否达到
输出上限、usage 是否已知、provider exception 固定类型以及是否完成脱敏。

严禁保存 Prompt、diff、响应正文、tool arguments/results、异常消息、身份、Key、locator 或主机
路径。未知供应商字段映射为固定 `other`/`unknown`，不保留原值。

## 离线回归边界

兼容性回归全部使用 fakes/synthetic，覆盖正常 tool call、空响应、连续空响应、text-only、
malformed tool call、output exhaustion、`finish_reason=length`、Finder step cap、Verifier bad
submits、空工具根 search/read、五个失败 headline 不可覆盖、diagnostic 不改变 headline、零 SDK
重试、预算不回滚、raw content 不落盘和 synthetic 不能打开 real gate。

这些测试只能证明离线协议和防护逻辑，不能证明 GLM 服务端、真实模型质量或 Business/Formal
Quality 成功。

## Phase 9I v2 建议

当前不建议创建可执行的真实 Phase 9I-Solo-Run v2：auth-004 根因仍未解决。完成离线兼容矩阵
且稳定失败码能关闭 provider/pipeline 歧义后，才值得另行提交提案。

提案必须使用新的 authorization ID、runtime hash 和 canonical authorization SHA-256；采用新的
确定性 cohort，形成新 denominator，绝不能替代或回填 auth-004。五个新目标的建议硬上限为
80 logical calls、80 HTTP attempts、1,500,000 input Tokens、163,840 output Tokens、
15,000,000 micro-CNY，SDK retries 继续为 0，并携带完整安全遥测字段。该数值只是离线规划，
不是调用、付费或数据采集授权。

## 验证

```powershell
python -m unittest -v tests.test_phase9g_solo_run
python phase9g_solo_run.py validate-synthetic
python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
python -m ruff check .
python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py
python scripts/verify.py
python -m pip check
git diff --check
```

所有命令均不得读取或枚举 `eval/**`，也不得调用模型、GitHub evidence API 或任何付费服务。
