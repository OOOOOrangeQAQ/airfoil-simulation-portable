# 最终交付索引

已发布稳定基线包仍为：`dist\airfoil_simulation_portable_release_final`。当前 `2.0.1rc1` 只允许作为 GitHub Pre-release 或本地校验包共享；资格完成前不得替换稳定基线包。

## 当前结论

- 工程实现：非网格修复完成，网格修复待合并。
- 回归：源码树与候选发布验证副本均为 `173 passed, 7 subtests passed`；CLI 内置 contract self-test 为 `PASS`。
- 一句话 dry-run：计划、一次确认、幂等状态机、严格 run_id 结果绑定与经验账本均已闭环。
- 交互面：仅保留 CLI；GUI、第三方 AI 页面和旧 GUI mesh-repair 已删除。
- 真实 Fluent 网格：NACA0012、NACA4418、NACA64-414 默认网格，以及 NACA4418 的 25,182/44,098/77,340 三档均完成结构检查。
- 63,040 工件：是早期审计验证遗留，不再属于自动策略；当前自动修复保持 48,458 单元并优化分布/平滑。
- 科学状态：`PROVISIONAL`。尚缺三档收敛 Cd/Cl GCI、冻结多翼型完整流场回归，以及至少一个非零形变候选的生产级全部门禁证据。

## 从哪里开始

1. 阅读 `README_FIRST_ZH.md`。
2. 首次使用运行 `RUN_SELF_TEST.cmd`。
3. 按 `docs\ONE_SENTENCE_GUIDE_ZH.md` 使用 `RUN_WORKFLOW.cmd` 调用公共命令。
4. 弱 AI 必须遵守 `docs\WEAK_AI_RUNBOOK_ZH.md`；不得把 `PROVISIONAL` 称为成功。

详细实测数值见 `docs\CURRENT_VERIFICATION_ZH.md`，科学晋级条件见 `docs\SCIENTIFIC_QUALIFICATION_ZH.md`。
63k 来源及新旧目录全部大网格清单见 `docs\MESH_COMPUTE_INVENTORY_ZH.md`。
