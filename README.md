# Airfoil Simulation Portable CLI

面向 AI 受控调用的翼型仿真与优化 CLI 工作流，适用于 Windows、ANSYS Fluent 2025 R1 和 Python 3.12。

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

> `v2.0.2` 是当前正式软件版本，合并了 AI 监督 C-grid 网格流程、一次性原固定 C-grid 受审回退和已观测到的非网格缺陷修复。正式软件发布不等于生产级 CFD 认证；科学证据状态仍为 **`PROVISIONAL`**。

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
- 每次任务拥有可配置的 `max_cells` 硬预算（默认 80,000）和 `max_candidates`
  候选预算（默认 5，允许 1–20）；
- 面积、逐截面局部厚度和升力比例门禁不能由 AI 降低；
- SSH 请求只允许引用管理员管理的 profile ID。

### 4. 网格、求解与优化编排

- 主路径为结构化全四边形 C-grid；
- O-grid 仅用于显式诊断，不作为静默 fallback；
- 非 dry-run 的 `confirm` 后停在 `MESH`，AI 按
  `ai_contract/skills/optimize-airfoil-cgrid` 设计、实测并帕累托比较任务候选；
  `max_candidates` 默认 5，允许按任务配置为 1–20；
- 不再自动运行 `distribution_repair_48k`，也不按单一最低 OQ 自动选网格；若最多
  `max_candidates` 个 AI 候选全部不可验收，才开放一次保持原实现与原参数不变的
  48,458 单元 C-grid 显式回退；
- 独立 GCI 科学资格网格族为 25,182 / 44,098 / 77,340；它们不属于 `MESH`
  阶段的自动重试候选；
- 网格硬失败包括非正 Jacobian、负/零面积或体积、折叠/自交、错误连接、边界区错误、非纯四边形、Fluent 读入失败或超出任务单元上限；
- Fluent 最小正交质量必须严格大于 `0.01`，但这只是安全底线，不是“优质网格”的单指标结论；
- 质量报告分别覆盖前缘、上/下翼面边界层、尾缘、尾迹入口/核心/出口和远场，并报告 OQ、Skewness 分位数与坏单元占比、局部面积退化、壁面法向性、尾迹流向偏差和尺寸连续性；
- 高长宽比边界层单元不会仅因 AR 被拒绝；AI 必须结合拉伸方向、正交性、
  局部流动物理、目标 y+ 推导的首层高度和边界层总厚度判断；实际 y+ 只能在流场
  求解后反馈，不能在网格生成前宣称已经满足；
- 每个无网格硬失败的候选都必须完成 100 次一阶 Fluent 试算；带软警告的候选还必须
  由 AI 明确承认警告并给出可审计的帕累托验收理由；
- 翼型气动优化按“可行性优先、最小化阻力”执行，并保留面积、局部厚度和升力约束；
  这不是网格候选的单指标排名规则。

#### AI 监督 C-grid 的实际划分方法

1. **识别几何而不是依赖固定点号。** 从 DAT 坐标重新识别前缘、上下翼面与两个尾缘
   端点。真实钝尾缘原样保留；尖尾缘或尾缘错位仅建立避免零宽 C-cut 所需的最小数值
   间隙，不覆盖原 DAT。
2. **按区域与流动物理分配节点。** 前缘按曲率和驻点加密；上下翼面分别按曲率、压力
   梯度与潜在分离需求布点；尾缘和尾迹入口协同过渡；尾迹长轴沿主流/预期尾迹方向，
   横向保留剪切层分辨率；远场只保留边界独立性所需密度。
3. **联合设计边界层。** 根据 Re、湍流模型与目标 y+ 推导首层高度，再联合选择层数、
   增长率和总厚度。生成前的 y+ 只是估计，正式基线求解后的实际 y+ 才用于反馈修正。
4. **优先平滑方向与尺寸变化。** 检查壁面法向性、受保护法向层到 C-wrap 的过渡、
   尾缘最后一条表面边与第一条尾迹边的尺度衔接，以及尾迹入口到出口的渐进放松。
5. **有限、可解释地迭代。** 每个候选必须记录父候选、针对区域、缺陷假设、预期效果、
   完整参数、预测/实际单元数、生成器哈希、补丁和结果；运行级补丁只能修改白名单内的
   网格源码，不能触碰求解器或优化器。
6. **以帕累托集合验收。** 同时比较 OQ、skewness、非壁面 AR、尺寸连续性、局部分辨率、
   100 次试算稳定性和计算成本，不把全域 AR、最低 OQ 或单元数压成一个自动总分。

#### AI 网格审核规则

- **硬失败，不能签署：** 非有限坐标，折叠/自交，非正 Jacobian，负/零面积或体积，
  重复/错误连接，壁面或 C-cut/边界区错误，非纯四边形，Fluent 读入失败，实际单元数
  超过任务上限，或 Fluent 最小 OQ 小于等于 0.01。
- **分区软审核：** 分别报告前缘、上/下翼面边界层、尾缘、C-wrap、尾迹入口/核心/出口
  与远场的 OQ、skewness 分位数及坏单元比例、最差位置与连片范围、相邻面积比、局部
  面积退化、壁面法向偏差、尾迹流向偏差和分辨率。平均值不得掩盖局部坏带。
- **长宽比按方向解释：** 壁面切向拉长、法向很薄且保持正交的边界层单元可具有很高
  AR；远场或横跨尾迹方向的异常拉伸则必须调查。低 AR 不能为剪切、扭曲或折叠免责。
- **计算可用性门禁：** 结构检查无硬失败后，以任务真实流动和湍流设置执行 100 次一阶
  Fluent 试算，每 10 次记录残差和力；非有限值、发散/停滞、负体积、连接错误、Fluent
  异常或未完成试算均拒绝。通过只证明“可正常计算”，不证明收敛、精度或网格无关性。
- **显式验收：** 任何软警告必须由 AI 逐区解释，并说明为何候选未被其他可验收候选
  帕累托支配。工作流不会替 AI 自动挑选警告网格。

#### 固定网格回退与 GCI 的区别

固定回退通过 `mesh-fallback --run-id ...` 显式触发，只有当 AI 候选预算耗尽且没有任何
可验收 AI 候选时才开放一次。它使用正式包内未修改的原 C-grid 生成器和固定参数
`214/10/58/190`（翼面侧/bridge/径向层/尾迹列），预测 48,458 单元；运行级 AI 补丁
不会传入。回退仍须满足任务 `max_cells`、全部硬门禁、100 次试算和 AI 显式验收；失败
后停留在 `MESH`，不会继续旧 `distribution_repair_48k` 梯子。

25,182 / 44,098 / 77,340 三套网格只在接受生产网格之后用于 GCI 科学资格检查。它们
既不是 AI 候选，也不是上述失败回退，更不会在 MESH 阶段自动选择。

#### 已测试翼型与结果评估

| 翼型/用途 | 单元数 | Fluent 最小 OQ | 最大 skewness | 评估 |
|---|---:|---:|---:|---|
| NACA0012，尖尾缘 AI 验收候选 | 53,576 | 0.234686 | 0.694598 | 全四边形、100 次 SST 试算通过；带 OQ 软警告显式验收 |
| NACA2418，B2 第三候选 | 52,692 | 0.171416 | 0.890339 | 前两个候选后继续定向探索；100 次试算通过并以帕累托理由验收 |
| NACA0018，快速几何集 | 48,210 | 0.332063 | 0.630403 | PASS |
| NACA2414，快速几何集 | 48,210 | 0.274251 | 0.819729 | 可计算，OQ 低于软目标 |
| NACA63-412，尖尾缘截断 | 53,972 | 0.301393 | 0.803455 | PASS；在 x/c=0.995 建立 0.000605c 钝口 |
| NACA6413，快速几何集 | 48,210 | 0.135369 | 0.910266 | 可计算；法向保护层至 C-wrap 过渡仍是热点 |
| NACA6412，错位尾缘截断 | 53,204 | 0.099125 | 0.935044 | 可计算但软警告明显；同一过渡带仍需方法优化 |

上表快速几何集均经 Fluent 2025 R1 真实读入，为纯四边形，未发现负/零体积、退化、
连接或边界区错误；但未运行 100 次试算，不能当作正式可验收生产网格。另有
NACA0012、NACA4418、NACA64-414 的旧固定网格结构检查，以及 NACA4418 三档 GCI
结构检查。NACA6413/6412 的多候选对比表明，仅增加单元或降低 AR 会交换指标而不会
消除保护层至 C-wrap 的低 OQ 带，因此新方法仍需实现有界 C1 过渡；固定回退只提供
可靠恢复路径，不替代这项方法改进。

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
airfoil-simulation-portable-v2.0.2-windows-x64.zip
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
.\RUN_WORKFLOW.cmd plan --text '用 examples/naca4418.dat，弦长 1 米，速度 30 m/s，攻角 2 度，海拔 0 米，局部厚度至少 90%，Cl 至少 99.8%，目标降阻 0.5%，最多 12 次求解，本地运行，dry-run'
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
# 仅当 mesh-brief 返回 fallback.status=AVAILABLE 时运行一次
.\RUN_WORKFLOW.cmd mesh-fallback --run-id run-...
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

- 源码树与干净便携发布副本：`196 passed, 7 subtests passed`；
- CLI 内置合同自检：`PASS`；
- NACA0012 尖尾缘和 NACA2418 钝尾缘/B2 完成 AI 候选、真实 Fluent 结构检查与 100 次一阶试算；
- NACA2418 在前两个候选后继续探索第三个针对性候选，没有按单一 OQ 自动回选默认网格；
- NACA0012、NACA4418、NACA64-414 默认网格完成真实 Fluent 结构检查；
- NACA4418 的 25,182 / 44,098 / 77,340 三档网格完成结构检查；
- 真实 Fluent 2025 R1 启动/退出验证未留下 Fluent、Cortex 或 MPI 测试进程；
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
├─ ai_contract/            AI 调用合同及可移植 C-grid 网格 Skill
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
