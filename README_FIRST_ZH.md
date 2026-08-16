# 翼型一句话仿真便携工作流

这是供 AI 学习和受控调用的纯 CLI 工作流。浏览器 GUI、第三方 AI 分析页以及旧 mesh-repair 页面均已删除；`src\airfoil_workflow` 是唯一运行入口和事实源。

交付概览见 `FINAL_DELIVERY_INDEX_ZH.md`。`v2.0.2` 是当前正式软件版本，状态见 `RELEASE_CANDIDATE_STATUS_ZH.md`；科学证据仍为 `PROVISIONAL`，不能据此宣称生产级 CFD 资格。

## 最快开始

1. 双击 `RUN_SELF_TEST.cmd`。它只使用包内 Python 3.12 与离线 wheelhouse，不联网。
2. 在任意目录执行绝对路径形式的 `RUN_WORKFLOW.cmd`，或先进入本目录执行：

   ```text
   RUN_WORKFLOW.cmd plan --text "用 examples/naca4418.dat，弦长1米，速度30m/s，攻角2度，海拔0米；局部厚度至少90%%，Cl至少99.8%%，目标降阻0.5%%，最多12次求解，本地运行"
   ```

3. 若结果是 `NEEDS_INPUT`，只回答 `questions` 中列出的字段；显示完整计划后执行一次 `confirm`。

默认状态固定写入本包根目录 `.airfoil-workflow`，不依赖当前工作目录。可用 `--workspace <目录>` 或 `AIRFOIL_WORKFLOW_STATE_ROOT` 显式覆盖。

完整流程见 `docs\ONE_SENTENCE_GUIDE_ZH.md`。弱 AI 必须遵守 `docs\WEAK_AI_RUNBOOK_ZH.md`。

## 不可绕过的政策

- 主网格是结构化全四边形 C-grid；非 dry-run 的 `confirm` 后正常计算停在 `MESH`，
  由 AI 使用 `ai_contract\skills\optimize-airfoil-cgrid` 生成和比较候选。
- `max_cells` 是每次任务可配置的硬预算（默认 80,000），`preferred_cells` 只是软偏好；最多候选默认 5、允许 1–20。
- OQ 必须严格大于 0.01，但这是安全底线而非优质结论；高长宽比壁面法向层按正交性、
  方向和区域物理综合评价。首层高度在生成前按目标 y+ 推导，实际 y+ 只能由求解结果反馈。
- 质量报告独立检查前缘、上下翼面边界层、尾缘、尾迹入口/核心/出口和远场，包含 OQ/Skewness 分位数、坏单元占比、局部面积退化、壁面法向性和尾迹流向偏差。
- 固定 48,458 网格不再作为正常自动重试；它的原实现和原参数被完整保留，仅在 AI
  候选名额耗尽且没有可验收候选时，通过 `mesh-fallback` 开放一次显式回退。回退仍
  必须通过所有硬门禁、100 次一阶试算和 AI 显式签署；失败后停在 `MESH`。
- 25,182 / 44,098 / 77,340 是独立 GCI 科学资格网格族，不是 `MESH` 阶段的自动重试候选。
- 面积和逐截面局部厚度均执行真实候选几何比例门禁；AI、推荐器和经验导入都不能降低面积、厚度、升力、网格或物理门槛。
- O-grid 只用于显式诊断，不是自动 fallback。

## 网格方法和审核要点

AI 从 DAT 坐标识别前缘、上下翼面和尖/钝/错位尾缘，按前缘曲率、边界层、尾缘、
C-wrap、尾迹入口/核心/出口和远场分别布点；首层高度由 Re、湍流模型和目标 y+ 推导，
并与层数、增长率和总厚度联合设计。候选不能只按最低 OQ 或 AR 排名，而要结合 OQ、
skewness、非壁面 AR、尺寸连续性、方向性、分区分辨率、试算稳定性和成本做帕累托比较。

任何非正 Jacobian、负/零面积或体积、折叠/自交、错误连接/边界、非纯四边形、Fluent
读入失败、超单元预算或最小 OQ 小于等于 0.01 都是硬失败。高 AR 的规则边界层单元
不会仅因 AR 被拒绝。完整流程、固定回退参数、已测翼型结果和当前仍需优化的
法向保护层至 C-wrap 过渡见 `README.md` 的“网格、求解与优化编排”章节。

## 当前科学资格

实现与离线回归通过不等于 CFD 生产资格。NACA0012/4418/64-414 默认网格、三档 GCI 网格和三弦长尺度已完成真实 Fluent 结构验证；但尚未取得三档收敛 Cd/Cl GCI，也没有非零形变候选通过厚度、面积、Cl、收敛、重复性和不确定性全部门禁，因此仍保持 `PROVISIONAL`。历史 63k 验证工件不再属于自动策略。详见 `docs\SCIENTIFIC_QUALIFICATION_ZH.md`。

## 便携边界

工作流、Python、依赖和相对路径是便携的；ANSYS Fluent 2025 R1、有效许可证和足够算力仍须存在于本地或 SSH worker。发布包不包含 Fluent、许可证、密钥或主机路径。
