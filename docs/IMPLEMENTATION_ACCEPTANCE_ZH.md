# 实施验收清单

## 自动化完成项

- [x] 唯一 CLI/worker 引擎入口；GUI 与页面依赖已删除
- [x] JobSpec v2、未知字段拒绝、死键删除与命令注入测试
- [x] AI 监督 C-grid brief、候选 schema、隔离源码、有限候选与帕累托验收
- [x] 可配置任务级 `max_cells`（默认 80,000）与旧 JobSpec 软偏好兼容
- [x] closure、物理弦长、拓扑 BL tag、边界覆盖和统一质量策略
- [x] SST/材料/边界/参考量设置后读回
- [x] 事件日志、锁、heartbeat、checkpoint、幂等恢复与取消
- [x] append-only ledger、CAS、确定性推荐器
- [x] Ed25519 经验导出、篡改校验、quarantine 导入和去重
- [x] 嵌入 Python 3.12、离线 wheelhouse、SSH Windows worker
- [x] allowlist 发布器、引擎哈希、SBOM、LICENSE/NOTICE
- [x] CLI 审计修复覆盖 run_id、CWD、UTF-8、面积与局部厚度硬门禁
- [x] 源码树完整测试通过（192 项，另有 7 个子测试）
- [x] `v2.0.1` 便携发布副本完成 Skill、引擎清单和完整测试
- [x] AI 监督网格修复已合并并生成正式稳定软件包
- [x] 分位数/坏单元占比、上下 BL、wake core、局部退化和方向性诊断已接入质量报告

## 本轮真实 Fluent 网格证据

- [x] NACA0012 / NACA4418 / NACA64-414 默认 48,458 单元网格均为全四边形且无硬失败
- [x] 0.5 / 1 / 2 m 弦长坐标线性缩放、面积平方缩放及 Fluent 无量纲质量一致
- [x] 25,182 / 44,098 / 77,340 三档均经真实 Fluent 读网格和质量硬门禁
- [x] 固定 48,458 自动重试、63,040 自动候选和单指标自动择优均已移除
- [x] NACA0012 尖尾缘与 NACA2418/B2 均完成真实 Fluent 检查和 100 次一阶试算
- [x] NACA2418 在前两个候选后继续实际探索第三个针对性候选，并以帕累托理由验收

## 必须由真实 Fluent 证据完成的资格项

- [ ] 厚/薄、尖/近尖/钝尾缘的更大冻结翼型集完整求解回归
- [ ] 25,182/44,098/77,340 三网格完成收敛 CFD 解并取得可信 Cd/Cl GCI
- [ ] 至少一个非零形变候选通过全部几何、Cl、网格、收敛和重复性门禁
- [ ] 干净 Windows 无系统 Python、断网、中文/空格路径最小 Fluent smoke
- [ ] 可用 SSH 测试机上的网络中断与同 run ID 恢复

未勾选的资格项使科学状态保持 `PROVISIONAL`，不会阻止软件自检、dry-run 或继续采集证据。
