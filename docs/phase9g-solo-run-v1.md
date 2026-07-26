# Phase 9G-Solo-Run v1 操作手册

本阶段是单一真人、五个真实已合并 PR、仅 shadow 的探索性运行。它不是原始
Business Pilot，也不是 Formal Quality。以下值永久固定：

```text
evidence_type=single_participant_exploratory
business_claim_allowed=false
quality_claim_allowed=false
formal_quality_status=incomplete
```

冻结合同见 [`plans/phase9g-solo-run-v1.md`](plans/phase9g-solo-run-v1.md)。任何命令均不得
读取或枚举 `eval/**`、`eval/holdout/**`。

## 当前状态

- 源提交：`a79b77e9e7e3792dd46cea4d6415c18ddcc54bb4`；
- 选择窗口：`[2026-01-01T00:00:00Z, 2026-07-26T00:00:00Z)`；
- 选择规则：对完整候选 ledger 的不透明 PR ID 计算冻结 rank，取最低五项；
- GitHub 证据 API、评论、Check、publish、部署均关闭；
- `auth-002` 只允许本地 manifest、选样及选中 diff 的读取/哈希；
- 已从 8 个候选中确定性选择 5 个 PR；公共收据为
  `phase9g_solo_run/selection-receipt.json`；
- 2 个 selected diff 的敏感模式扫描命中，选择没有替换，对应 headline 保持阻断；
- 用户已确认 Key 文件预检通过；executor 仍须在付费前执行独立的即时预检；
- auth-003 已获批准，Solo-only 执行器正在通过提交、hash binding 与离线门禁，尚未发出
  真实模型请求。

## 1. 建立私有授权输入

私有授权目录必须在 Git worktree 外，且必须是一个尚不存在的新目录。稳定 ID 仅通过当前
PowerShell 进程的环境变量传入；命令结果只显示固定哈希，不显示 ID。

```powershell
$Repo = (Get-Location).Path
$PrivateInput = Join-Path $env:TEMP 'phase9g-solo-run-v1-auth-002'
$env:PHASE9G_SOLO_PARTICIPANT_ID = '<已批准的参与者稳定 ID>'
$env:PHASE9G_SOLO_REPOSITORY_ID = '<已批准的不透明仓库 ID>'
$env:PHASE9G_SOLO_APPROVER_ID = '<已批准的人类审批人稳定 ID>'
python phase9g_solo_run.py initialize-auth-002 `
  --repo-root $Repo `
  --output-root $PrivateInput
```

应得到 `authorization_sha256=365ba325...bcc89` 和
`runtime_config_sha256=cee0b676...60da`。任何不匹配都 fail closed。不要把私有目录、稳定
ID 或授权文件加入 Git。

## 2. 物化候选、选择和选中 diff

证据目录和公共收据都禁止覆盖。失败后保留原目录和失败信息，修复时使用新的诊断目录，
不得把成功重跑改成 headline。

```powershell
$Evidence = Join-Path $env:TEMP 'phase9g-solo-run-v1-evidence-001'
$Receipt = Join-Path $Repo 'phase9g_solo_run\selection-receipt.json'
python phase9g_solo_run.py materialize-selection `
  --repo-root $Repo `
  --private-root $Evidence `
  --public-receipt $Receipt `
  --authorization (Join-Path $PrivateInput 'authorization.json') `
  --runtime-config (Join-Path $PrivateInput 'runtime-config.json') `
  --generated-at '<当前 UTC，格式 YYYY-MM-DDTHH:MM:SSZ>'
python phase9g_solo_run.py validate-public-receipt --receipt $Receipt
```

工具只用 `origin/master` 的 first-parent 本地元数据建立完整候选 ledger。选择冻结后才打开五个
selected diff；未选中的 diff 不会读取。潜在 secret 命中不会换样，而会增加
`selected_diff_secret_scan_hit` 阻断原因。

## 3. 凭证预检

Key 必须位于仓库外文件中。不得把 Key 直接放入命令行、JSON、Git、日志或普通
`LLM_API_KEY` 环境变量。预检不联网，也不打印 Key、路径、长度、前后缀或异常内容。

```powershell
$env:LLM_PROVIDER = 'glm'
$env:LLM_MODEL = 'glm-5.2'
$env:GLM_API_KEY_FILE = '<仓库外 Key 文件>'
python phase9g_solo_run.py preflight-credential `
  --authorization (Join-Path $PrivateInput 'authorization.json') `
  --runtime-config (Join-Path $PrivateInput 'runtime-config.json') `
  --repo-root $Repo
```

成功预检仍返回 `paid_call_gate=false`，不会触发 smoke request。

## 4. auth-003 已批准，仍需机器门禁

用户已批准标准 GLM-5.2 API 的 auth-003：

```text
temperature_profile=0.01/0.70/0.01/0.01
max_logical_calls=96
max_http_attempts=96
sdk_max_retries=0
max_input_tokens=1750000
max_output_tokens=200000
max_cost_microcny=20000000
price_cny_per_million=8 input / 28 output / 2 cached input
```

方案 A 将两个敏感扫描命中登记为零调用失败，只允许剩余三个 PR 运行。授权仍不会直接
打开网络：executor 必须先提交并绑定 SHA-256，完整离线门禁必须通过，auth-003 和 tariff
必须在仓库外物化，Key 文件必须重新预检。

授权物化后可使用以下只读验证命令：

```powershell
python phase9g_solo_run.py validate-auth-003-attestation `
  --attestation phase9g_solo_run/auth-003-attestation.json
python phase9g_solo_run.py preflight-auth-003 `
  --repo-root $Repo `
  --evidence-root $Evidence `
  --public-attestation phase9g_solo_run/auth-003-attestation.json
```

`execute-auth-003-headlines` 是唯一可能发出付费请求的命令。不得在 attestation、offline
validation、凭证预检或任一 hash binding 缺失时调用。

## 5. 离线验收

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

本阶段的公共结果只能写成“single-participant exploratory observation”和“model quality not
measured”。不得生成或暗示 Precision/Recall/F1、Bootstrap CI、多人采用率、生产力提升、
Business Pilot 成功或 Formal Quality 完成。
