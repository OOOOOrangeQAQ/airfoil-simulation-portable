# 网格计算清单与 AI 候选预算

## 当前策略

工作流正常路径不再自动生成固定 48,458 单元网格，也不再运行
`distribution_repair_48k` 重试梯子。原 48,458 参数和生成器不作修改，只在 AI
候选全部不可验收后开放一次显式 `mesh-fallback`，并执行相同审核。`preferred_cells` 只是 AI 的成本参考；
每次任务的 `max_cells` 才是硬上限，默认 80,000 且可配置。确认任务后必须停在
`WAITING_FOR_AI_MESH`，没有 AI 验收检查点时不得进入基线求解。

## 已有历史工件

| 单元数 | 用途 | 当前状态 |
|---:|---|---|
| 25,182 | GCI coarse 结构检查 | 保留，qualification-only |
| 44,098 | GCI medium 结构检查 | 保留，qualification-only |
| 48,458 | 原固定方法、代表翼型及尺度检查 | 非运行默认；仅作 AI 全失败后的显式一次性回退 |
| 63,040 | 早期 NACA4418 审计验证 | 已退出自动策略 |
| 77,340 | GCI fine 结构检查 | 保留，qualification-only |

63,040 工件是在停止核验该修复档之前已经启动的旧审计任务；后续没有再次运行，
也不会被 CLI、worker、经验推荐器或 AI 候选流程自动调用。

旧 handoff 和旧 GUI 目录中的 81k、99k、113,936 单元记录同样仅是只读历史证据，
不包含在干净发布包内，也不构成当前任务上限。

## 当前预算与安全行为

- 新 JobSpec 使用 `mode=ai_supervised_cgrid`、可空的 `preferred_cells`、正整数
  `max_cells` 和 `1..20` 的 `max_candidates`；默认候选预算为 5。
- 旧 `{target_cells: 48458, max_cells: 80000}` 仍可读取，但只转换为软偏好和
  本次任务预算。
- schema、候选预测、实际网格、Fluent 检查、本地/SSH worker 和验收检查点共同
  执行任务级上限；超过上限的候选硬失败。
- AI 可以在运行隔离副本中修改网格白名单文件，以增加缺失的区域控制；修改求解器、
  优化器或主工作流文件会被哈希检查拒绝，补丁不会自动回写主源码。
- 任何软警告候选都必须通过实际物理条件下的 100 次一阶 Fluent 试算，并由 AI
  给出区域物理与帕累托理由；工作流不会按最高 OQ 自动选择。
- `mesh-fallback` 仅在 `fallback.status=AVAILABLE` 时可调用；它使用 canonical 引擎、
  忽略运行级补丁、不消耗新的 AI 候选名额、不可重复，并且必须再次显式 `mesh-accept`。
