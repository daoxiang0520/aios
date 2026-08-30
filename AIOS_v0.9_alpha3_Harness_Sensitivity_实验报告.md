# AIOS v0.9-alpha.3 Harness Sensitivity 实验报告

实验日期：2026-08-31

模型：DeepSeek `deepseek-v4-flash`，temperature=0.1

设计：3 个 full/pre-task immutable Capsules × 3 个 Harness Profiles × 3 次重复，共 27 次真实任务执行。

## 实验约束

固定模型、任务、初始 workspace、Authority、Tool capability、Token budget、temperature、Sandbox 与 Verifier，只改变 `harness_profile`：

- H0 Structured：完整 task state、context composer、memory、structured evidence 与 completion scaffold；
- H1 Reduced：保留持久任务、working state、resource addressing、tools、environment 与基础 completion observation；
- H2 Minimal Open：只保留目标、workspace、通用工具、预算和小型持久状态。

所有变体从 Capsule 相同 `initial_state_hash` fork。报告不计算单一 reward、不选择 winner、不自动推广 Harness。

## 结果

### 硬结果

| 指标 | H0 Structured | H1 Reduced | H2 Minimal Open |
|---|---:|---:|---:|
| Runs | 9 | 9 | 9 |
| Completed | 0 | 0 | 0 |
| Verifier Pass | 0 | 0 | 0 |
| True Completion | 0 | 0 | 0 |
| Security Violations | 0 | 0 | 0 |

三个 Profile 的 27 次执行全部进入 `dead_letter`。因此本实验没有发现可用 Harness，也不能把成本最低的 Profile 称为更优。

### 跨任务中位数

| 指标 | H0 Structured | H1 Reduced | H2 Minimal Open |
|---|---:|---:|---:|
| Tokens | 185,464 | 86,949 | 53,654 |
| Model Calls | 18 | 18 | 10 |
| Tool Calls | 19 | 18 | 12 |
| Cycles | 3 | 3 | 2 |
| Wall Time | 703.9 s | 499.3 s | 416.9 s |
| Failure Recovery Rate | 0 | 0 | 0 |

相对 H0，H1 的跨任务中位 Tokens 下降约 53.1%，H2 下降约 71.1%；H2 的中位 Model Calls 下降约 44.4%，Wall Time 下降约 40.8%。这些数字只说明删减 Harness 会显著改变计算成本，不能说明任务能力提高。

### 各 Capsule 中位 Tokens

| Capsule / Task | H0 | H1 | H2 | 完成情况 |
|---|---:|---:|---:|---|
| Task 90：仓库复现与真实修复 | 185,464 | 107,765 | 23,009 | 三档均 0/3 |
| Task 89：数据集验证与分析 | 150,463 | 86,949 | 62,294 | 三档均 0/3 |
| Task 88：PDF + XLSX 厚度分析 | 331,656 | 33,266 | 53,654 | 三档均 0/3 |

三个任务上，H1/H2 的中位 Tokens 都低于 H0，证明 Harness effect 在成本维度可重复观察；但完成率始终没有变化。

### 总记录成本

| Profile | Tokens | Model Calls | Tool Calls | Cycles | 累计 Wall Time |
|---|---:|---:|---:|---:|---:|
| H0 | 1,755,978 | 150 | 166 | 28 | 1.786 h |
| H1 | 640,576 | 139 | 137 | 29 | 1.289 h |
| H2 | 379,437 | 91 | 111 | 24 | 1.067 h |
| 合计 | 2,775,991 | 380 | 414 | 81 | 4.141 h |

Tokens 为 AIOS 持久化的模型用量，不等同于供应商最终账单。

## 观察与限制

1. **Harness 是可测变量。** 三档在 Tokens、Latency、Calls 和 Cycles 上出现稳定差异，Phase 1 的基本实验假设成立。
2. **当前证据不支持 Harness minimization。** H1/H2 只是更省地失败，没有改善 Completion、Verifier 或 Failure Recovery。
3. **终止与证据语义是共同瓶颈。** 25/27 runs 产生非空 final output，其中存在明确“任务已完成”的文本，但 27/27 都未通过 Host Verifier。这表明模型的完成声明不能替代可验证终态。
4. **存在外部环境噪声。** 实验期间观察到 DeepSeek `RemoteDisconnected`、TLS `UNEXPECTED_EOF`。Task 88 的 H1 replicate 3 与 H2 replicate 3 最终结果缺少模型用量遥测（0 calls/0 tokens），相关 Profile 的部分聚合成本被向下偏置。
5. **语义质量不可比较。** 没有 Verifier-passed baseline/candidate，Pairwise Semantic Judge 返回 `insufficient_evidence`。
6. **不选择 winner。** `promotion_state=MEASUREMENT_ONLY`，没有 Candidate、Promotion 或生产配置变更。

## 结论

本轮证明：

> Harness choice 会显著改变模型组织计算的成本，但在当前三个复杂任务上，删减 scaffold 没有转化为正确完成。

因此下一步不应直接进入 Harness Self-Evolution，也不应根据 Token 最低自动选择 H2。更合理的是补充一组难度分层、至少包含可稳定完成控制任务的新 Capsules，并继续使用相同 H0/H1/H2 冻结协议。只有出现 `Verifier Pass / True Completion` 的差异后，才能研究 Minimum 或 Adaptive Sufficient Harness。

## 持久实验记录

- Task 90：`exp_f11ff23817b94154aa34483388eda926`（由 9 条已持久化 runs 恢复汇总）；
- Task 89：`exp_c4a505dd86f44a0f985c6f7ba00c91b9`；
- Task 88：`exp_c7e28e7a367343c8958c418a65f2a2a7`。

首次无效启动（缺少环境变量、只读 world 清理失败）及被人工中止的截断实验不计入上述 27 runs。
