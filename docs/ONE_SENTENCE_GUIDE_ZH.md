# 一句话仿真：弱 AI 安全操作手册

## 核心规则

弱 AI 只能调用本文列出的命令。它不能运行 shell、编辑代码、调用 Fluent TUI、使用旧引擎的 `--set PATH=JSON`，也不能自行放宽厚度、面积、升力、网格和物理门禁。

一句话首先产生计划，不会立即启动昂贵计算。缺项时程序返回退出码 `10` 及精确问题；字段完整时返回 `PLANNED`。用户检查计划后只执行一次 `confirm`。

## 最短流程

```powershell
RUN_WORKFLOW.cmd plan --text '优化 "D:\input\naca4418.dat"，弦长 1 m，流速 32.5 m/s，攻角 2 度，海拔 0 m，局部厚度至少 90%，升力至少 99.8%，降阻至少 0.5%，最多 12 次求解，本地'
RUN_WORKFLOW.cmd answer --plan-id plan-... --values '{"flow":{"altitude_m":0}}'
RUN_WORKFLOW.cmd confirm --plan-id plan-...
RUN_WORKFLOW.cmd mesh-brief --run-id run-...
RUN_WORKFLOW.cmd mesh-evaluate --run-id run-... --proposal candidate.json
RUN_WORKFLOW.cmd mesh-accept --run-id run-... --attempt-id attempt_001 --decision decision.json
RUN_WORKFLOW.cmd resume --run-id run-...
RUN_WORKFLOW.cmd status --id run-...
RUN_WORKFLOW.cmd result --run-id run-...
```

可在任意命令前添加 `--workspace <目录>`。未指定时固定使用发布包根目录 `.airfoil-workflow`，不会随 CWD 漂移。也可通过 `AIRFOIL_WORKFLOW_STATE_ROOT` 设置默认状态根。

CLI 使用严格 JobSpec 2.0。网格契约为 `mode=ai_supervised_cgrid`、可空的 `preferred_cells`、任务级 `max_cells` 和 `max_candidates`。旧的 48,458/80,000 请求仍可读取，但只转换为软偏好和默认预算。

测试流程应在一句话加入 `dry-run`，或在 JobSpec 设 `execution.dry_run=true`。dry-run 只编译并检查固定执行计划，不启动 Fluent。

## 缺项和个人默认

关键字段是：DAT、弦长、流速/Re/Mach 三选一、攻角、海拔、局部厚度比例、Cl 比例、降阻目标、最大求解评估数、执行位置。SSH 还必须给管理员配置的 `profile_id`。

当输入 Re 时，系统用海拔、物理弦长及 ISA-1976/Sutherland 空气性质反解速度；输入 Mach 时按该海拔声速反解速度。可选温度会在该海拔标准压力下参与密度、黏度和声速计算。原始输入、推导速度、Re、Mach 和大气参数都会写入 resolved config。

一句话中的单元数是软偏好；系统把任务 `max_cells` 提升到至少覆盖该偏好。候选仍必须逐个通过任务级硬预算、结构检查、Fluent 读入和 100 次一阶试算，工作流不会自行加密或选择候选。

接受所有推荐默认值：

```powershell
airfoil-workflow answer --plan-id plan-... --values '{}' --use-proposed-defaults
```

只有海拔、局部厚度、Cl、降阻目标、预算和本地/SSH 选择可以通过 `--save-defaults` 写入代码外个人默认。文件路径、SSH 地址、密钥、命令和网格门槛永远不能成为学习默认。

## 状态、恢复和退出码

状态链为 `NEEDS_INPUT → PLANNED → CONFIRMED → PREFLIGHT → MESH → BASELINE → OPTIMIZATION → VALIDATION → GRID_QUALIFICATION → REPORTING → 终态`。事件写入带 SHA-256 链的只追加 `events.jsonl`，最新快照采用原子替换。

- `0`：命令技术上完成。结果是否科学合格必须继续查看 `design_status` 和 `evidence_status`。
- `10`：缺少输入，读取 `questions` 后调用 `answer`。
- `11`：正常停在 `MESH` 等待 AI 候选或显式验收。
- `20`：科学或约束拒绝。
- `30`：执行失败。
- `2`：请求/安全契约错误。

```powershell
airfoil-workflow cancel --run-id run-...
airfoil-workflow resume --run-id run-...
```

`cancel`、`confirm` 和已完成运行的恢复具有幂等性。恢复只能从安全 checkpoint 继续；科学拒绝不能恢复为成功，必须创建新计划。

## SSH worker

用户 JobSpec 只含管理员分配的 SSH profile ID，不含主机、用户名、密钥、密码或命令。远端固定入口为：

```powershell
python -m airfoil_workflow worker-run --request job\request.json --run-root job\run
```

该隐藏入口只接受经过哈希校验、`execution.kind=local` 的严格 JobSpec。远端包还携带已验收 case、网格摘要、AI 决策和仅限网格白名单的补丁证据；worker 逐文件校验哈希并重建网格检查点，不在远端回退到固定网格。

## 结果解释

`execution_status`、`design_status` 和 `evidence_status` 相互独立。`SCREENING_PASS` 或 `PROVISIONAL` 不等于生产合格；只有签名证据明确达到 `PRODUCTION_QUALIFIED` 才能如此报告。dry-run 的证据状态固定为 `NOT_EVALUATED`。
