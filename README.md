# Airfoil Simulation Portable CLI

面向 AI 受控调用的翼型仿真与优化 CLI 工作流，适用于 Windows、ANSYS Fluent 2025 R1 和 Python 3.12，别的版本没测试，我的电脑上也没有，谁测了发我看看😋。

希望各位CFD同道能测试反馈一下啊，不过估计也没人看:(。我也不确定在别的电脑环境上能用不能，反正我自己的电脑用的没问题，目前已经测试过的ai有Codex-Chatgpt，Opencode-Deepseek-V4-Flash。别用V2.0.0,有BUG

> English summary: an offline-first, policy-gated command-line workflow for airfoil meshing, Fluent simulation, optimization orchestration, resumable state management, and auditable AI interaction. The public interface is CLI-only. ANSYS Fluent and its license are not included.

## 项目定位

本项目把“用户用一句中文或英文描述翼型计算任务”转换成严格、可审阅、可确认、可追踪的 JobSpec 2.0，再由固定执行器调用翼型几何、结构化网格和 Fluent 求解流程。

它特别适合：

- 让 AI 学习如何通过固定 CLI 合同调用 CFD 流程；
- 在不允许 AI 生成任意 shell、Python、Fluent TUI 或配置覆盖的前提下执行任务；
- 对计划、确认、运行、恢复、取消和结果建立可审计状态链；
- 在本机或由管理员预配置的 SSH worker 上执行；
- 用便携 Python 和离线 wheelhouse 在 Windows 上完成首次安装。

本项目不是 Fluent 的替代品，也不是已经完成生产级科学认证的气动数据库。当前科学资格为 **`PROVISIONAL`**，不能把程序正常结束或 screening pass 描述为生产级 CFD 结论。

> `2.0.1rc1` 是 **Pre-release candidate**：用于公开审阅新的 AI 监督 C-grid 流程，不替代 `v2.0.0` 稳定基线，也不代表生产级 CFD 资格。V2.0.0版仅有三个固定的绘制模板，`2.0.1rc1`抛弃了固定的绘制流程，采用了灵活的绘制方式，并采用了多约束交叉监督，可能会有更优秀的网格质量。此外，将默认的压力—速度耦合算法设置为coupled，此前为SIMPLE。


## 主要功能

### 1. 一句话生成计划

`plan` 接受中文或英文任务描述，提取翼型 DAT、弦长、速度/Re/Mach、攻角、海拔、约束、优化目标、预算和执行位置。信息不足时只返回明确问题，不直接启动昂贵计算。

### 2. 一次人工确认

完整计划进入 `PLANNED` 后，必须调用一次 `confirm` 才会执行。AI 不能绕过确认，也不能在 JobSpec 中夹带命令、环境变量、SSH 凭据或未知字段。

### 3. 严格 JobSpec 2.0

公共请求采用白名单字段并拒绝未知键。主要约束包括：

- 尾缘闭合只允许 `auto`、`sharp`、`blunt`；
- 流动条件在速度、Reynolds 数和 Mach 数之间严格三选一；
- 网格模式固定为 `ai_supervised_cgrid`，单元数只允许作为 AI 软偏好；
- 每次任务拥有可配置的 `max_cells` 硬预算（默认 80,000）和最多候选数（默认 5）；
- 面积、逐截面局部厚度和升力比例门禁不能由 AI 降低；
- SSH 请求只允许引用管理员管理的 profile ID。

### 4. 网格、求解与优化编排

- 主路径为结构化全四边形 C-grid；
- O-grid 仅用于显式诊断，不作为静默 fallback；
- `confirm` 后停在 `MESH`，AI 按便携 Skill 设计、实测并帕累托比较最多 5 个候选；
- 不再自动运行 `distribution_repair_48k`，也不按单一最低 OQ 自动选网格；
- GCI 候选网格为 25,182 / 44,098 / 77,340；
- 网格门禁包含单元数、正交质量、偏斜率、边界层拓扑和边界覆盖；
- 优化按“可行性优先、最小化阻力”执行，并保留面积、局部厚度和升力约束。

### 5. 可恢复、可审计状态

状态链覆盖：

```text
NEEDS_INPUT -> PLANNED -> CONFIRMED -> PREFLIGHT -> MESH -> BASELINE
-> OPTIMIZATION -> VALIDATION -> GRID_QUALIFICATION -> REPORTING -> 终态
```

状态转移写入 SHA-256 串联的只追加日志，高频 heartbeat 只更新原子状态快照。运行支持查询、幂等取消、死进程识别和从安全 checkpoint 恢复；仍存活的后台引擎可按同一 run ID 重新挂接。结果中的执行状态、设计状态和证据状态相互独立。

### 6. 经验数据接口

CLI 提供受控的经验导入/导出和内容寻址存储能力。运行期 ledger、CAS、密钥和隔离区属于本机数据，不上传到本仓库，也不进入干净发布包。

## 系统要求

便携发布包包含 Python 3.12 运行时和离线 Python wheels，但仍需要：

- Windows x64；
- 本地安装 ANSYS Fluent 2025 R1，或可用的管理员配置 SSH worker；
- 有效的 ANSYS 许可证；
- 满足实际网格和求解规模的 CPU、内存和磁盘空间。

仓库和发布包均不包含 ANSYS Fluent、ANSYS 许可证、许可证服务器地址、SSH 私钥或第三方 AI 密钥。

## 推荐安装：下载便携版

不熟悉 GitHub 的用户应打开仓库右侧 **Releases**，下载类似下面名称的 ZIP：

```text
airfoil-simulation-portable-v2.0.0-windows-x64.zip
```

解压到不需要管理员权限的目录。不要直接在 ZIP 内运行，也不建议放在会实时同步或路径过长的目录。

首次使用双击：

```text
RUN_SELF_TEST.cmd
```

它会使用包内 Python 3.12 和离线 wheelhouse 建立本地 `.venv`，然后运行合同自检和回归测试。此过程不下载 Fluent，也不会启动正式 CFD 求解。

## 从源码使用

源码仓库保留了便携运行时和 wheelhouse，克隆后可直接执行上面的 `RUN_SELF_TEST.cmd`。开发者也可用系统 Python 3.12 建立虚拟环境并安装可编辑包：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pytest -q
```

## 最快使用示例

先做不会启动 Fluent 的 dry-run：

```powershell
.\RUN_WORKFLOW.cmd plan --text '用 examples/naca4418.dat，弦长 1 米，速度 30 m/s，攻角 2 度，海拔 0 米，局部厚度至少 95%，Cl 至少 99.8%，目标降阻 0.5%，最多 12 次求解，本地运行，dry-run'
```

程序会返回 JSON。后续按状态执行：

```powershell
# 仅在返回 NEEDS_INPUT 时回答 questions 中列出的字段
.\RUN_WORKFLOW.cmd answer --plan-id plan-... --values '{"flow":{"altitude_m":0}}'

# 或接受程序提出的所有可保存默认值
.\RUN_WORKFLOW.cmd answer --plan-id plan-... --values '{}' --use-proposed-defaults

# 审阅计划后只确认一次
.\RUN_WORKFLOW.cmd confirm --plan-id plan-...

# 非 dry-run 会停在 MESH；AI 读取任务、提交候选并显式验收
.\RUN_WORKFLOW.cmd mesh-brief --run-id run-...
.\RUN_WORKFLOW.cmd mesh-evaluate --run-id run-... --proposal candidate.json
.\RUN_WORKFLOW.cmd mesh-accept --run-id run-... --attempt-id attempt_001 --decision decision.json
.\RUN_WORKFLOW.cmd resume --run-id run-...

# 查询状态和结果
.\RUN_WORKFLOW.cmd status --run-id run-...
.\RUN_WORKFLOW.cmd result --run-id run-...

# 安全取消或恢复
.\RUN_WORKFLOW.cmd cancel --run-id run-...
.\RUN_WORKFLOW.cmd resume --run-id run-...
```

默认状态固定写入发布包根目录的 `.airfoil-workflow`，不依赖命令启动时的当前目录。也可以显式指定：

```powershell
.\RUN_WORKFLOW.cmd --workspace D:\airfoil-state status --run-id run-...
```

或设置环境变量 `AIRFOIL_WORKFLOW_STATE_ROOT`。

## 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 命令技术上完成；仍需检查科学证据状态 |
| `10` | 输入不完整，需要回答返回的 questions |
| `20` | 科学约束或证据门禁拒绝 |
| `30` | 执行失败 |
| `2` | 请求格式或安全合同错误 |

## 如何解释结果

请同时查看：

- `execution_status`：程序是否完成执行；
- `design_status`：候选设计是否满足工程约束；
- `evidence_status`：证据属于 `NOT_EVALUATED`、`PROVISIONAL`、拒绝还是生产合格。

`dry-run` 的证据状态固定为 `NOT_EVALUATED`。`SCREENING_PASS` 和 `PROVISIONAL` 都不等于生产合格。只有签名证据明确达到 `PRODUCTION_QUALIFIED` 时，才能如此报告。

## 已处理的安全与审计问题

v2.0.0 将程序收敛为 CLI-only，并处理了已知审计项：

- 删除浏览器 GUI、第三方 AI provider、旧 AI 页面和不可达的 GUI mesh-repair；
- 删除 NiceGUI/httpx 等 GUI 依赖和配置文件；
- 公共 JobSpec 不再接受会在网格路径失败的 `closure="both"`；
- `status`、`result`、`resume`、`cancel` 对 run ID 做严格唯一匹配；
- 状态根改为固定包根或显式 workspace，不再依赖当前工作目录；
- 子进程强制 UTF-8，避免 Windows GBK 日志乱码；
- 删除无消费者的配置死键；
- 将最小面积比、最小局部厚度比等约束接入真实候选几何门禁。

历史静态审计原文保留在 `docs/STATIC_AUDIT_REPORT_20260814_ZH.md`，其中旧文件行号仅用于追溯，不代表这些 GUI 文件仍然存在。

## 当前验证状态

- 源码树与干净便携发布副本：`164 passed, 7 subtests passed`；
- CLI 内置合同自检：`PASS`；
- NACA0012、NACA4418、NACA64-414 默认网格完成真实 Fluent 结构检查；
- NACA4418 的 25,182 / 44,098 / 77,340 三档网格完成结构检查；
- 科学资格仍为 `PROVISIONAL`：尚缺三档收敛 Cd/Cl GCI、冻结多翼型完整流场回归，以及至少一个非零形变候选通过全部生产级门禁的证据。

验证详情见 `docs/CURRENT_VERIFICATION_ZH.md` 和 `docs/SCIENTIFIC_QUALIFICATION_ZH.md`。

## 目录结构

```text
airfoil_simulation_portable/
├─ src/airfoil_workflow/   CLI、状态机、JobSpec、执行与内嵌计算引擎
├─ schemas/                严格数据结构定义
├─ policies/               固定安全与科学策略
├─ examples/               示例翼型 DAT
├─ tests/                  离线回归测试
├─ docs/                   使用、审计、验证和移植说明
├─ ai_contract/            AI 调用合同和修复提示资料
├─ worker/                 固定 SSH worker 入口资料
├─ runtime/                便携 Python 3.12
├─ wheelhouse/             离线依赖 wheels 与哈希清单
├─ RUN_WORKFLOW.cmd        公共 CLI 启动器
└─ RUN_SELF_TEST.cmd       首次安装和自检入口
```

`.venv`、`.airfoil-workflow`、`runs`、`work`、诊断日志、经验 ledger/CAS/密钥以及 Fluent transcript 均属于本机生成数据，已通过 `.gitignore` 排除。

## 进一步文档

- `README_FIRST_ZH.md`：便携包快速开始；
- `docs/ONE_SENTENCE_GUIDE_ZH.md`：完整一句话流程；
- `docs/WEAK_AI_RUNBOOK_ZH.md`：弱 AI 安全规则；
- `docs/PORTABILITY_AND_REMOTE_ZH.md`：便携和 SSH worker；
- `docs/SCIENTIFIC_QUALIFICATION_ZH.md`：科学晋级条件；
- `docs/CURRENT_VERIFICATION_ZH.md`：当前实测证据；
- `docs/EXPERIENCE_TRANSFER_ZH.md`：经验数据导入导出；
- `FINAL_DELIVERY_INDEX_ZH.md`：交付总览。

## 版权、第三方组件与免责声明

本仓库当前不是 MIT、Apache-2.0 或 GPL 等开放源代码许可项目。根目录 `LICENSE` 保留项目代码的所有权利；公开仓库表示任何人可以查看仓库和分享链接，不自动授予复制、修改、再发布或商业使用权。如需允许社区自由复用，应由版权所有者另行选择明确的开源许可证。

第三方 Python 包以 wheel 形式随便携包分发，其版权和许可证由各自作者所有；具体版本和哈希见构建发布中的 `SBOM.cdx.json` 与 `wheelhouse/WHEELHOUSE_MANIFEST.json`。

本项目不隶属于、未获 ANSYS 官方认可，也不包含 ANSYS Fluent 或许可证。CFD 结果可能受几何、网格、湍流模型、边界条件、收敛性和软件环境影响；在工程决策前必须由具备资质的人员独立复核。
