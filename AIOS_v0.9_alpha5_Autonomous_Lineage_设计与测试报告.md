# AIOS v0.9-alpha.5 Autonomous Lineage 设计与测试报告

## 目标

本版不扩大 mutation surface，而是让已存在的 Harness Candidate 能够成为可持续运行的
实验子谱系。Agent 可以自主选择 `CONTINUE / ADOPT_CANDIDATE / FORK_MUTATION / RETURN`，
Host 只维护不可篡改的谱系历史、权限边界与持久状态，不判断哪条谱系“更优”。

## 已实现

- 新增持久化 `evolution_lineages`、`lineage_events`、`task_lineage_bindings`；
- 根谱系 `lin_root` 是创建时生产 Harness 的快照，不是生产激活指针；
- 子谱系继承父谱系全部 Harness 设置，并且每次自主 fork 只允许一个既有白名单 mutation；
- Candidate 可被 Agent 采用到子谱系，但 Candidate 状态和生产 `active_harness` 均不被改写；
- `RETURN` 只允许返回当前谱系的祖先，防止伪造无继承关系的状态跳转；
- 独立 `experimental_lineage_head` 记录 Agent 当前选择的实验后代；它不等于生产默认版本；
- 普通 Durable Task 可通过 `--lineage` 绑定谱系，Runtime 在所有 continuation cycle 中持续使用该谱系；
- Task Result、Context、Trace 和 Web UI 均显示谱系 ID、代际和父谱系；
- Web UI Composer 提供 `Production / Unbound`、当前实验 Head、具体谱系三种提交目标；
- 每次 Agent 谱系决策写入 `evolution_runs`，并明确记录
  `production_activated=false`、`host_fitness_judgment=false`；
- Lineage Experience 显式交付 mutation contract 与逐动作可用性；没有既有 Candidate 时，
  `FORK_MUTATION` 仍明确标记为可执行；
- Free/Verified completion mode 与 Lineage 是两个正交维度，UI 分别显示。

## 自主边界

Alpha.5 只开放可执行的 Harness Lineage：

- `prompt_append`
- `max_actions_per_cycle`
- `memory_context_characters`
- `harness_profile`

以下仍由 Host/Root of Trust 持有，不能通过 Lineage 修改：Sandbox、Authority、Storage/Audit、
Security Kernel、外部评估器、凭据和生产激活。Runtime/Skill Candidate 暂时只能作为研究产物，
不能伪装成已经可执行的 Lineage。

## 使用

```powershell
python -m aios --config config.json evolution lineage-list
python -m aios --config config.json evolution lineage-run lin_root
# 后续省略 lineage_id 时，从 experimental_lineage_head 继续
python -m aios --config config.json evolution lineage-run
python -m aios --config config.json evolution lineage-show <lineage_id>
python -m aios --config config.json task submit "真实任务" --lineage current
python -m aios --config config.json run
```

`lineage-run` 调用当前配置模型，由模型自主决定一次谱系动作。使用 mock provider 时确定性地执行
`CONTINUE`。任何子谱系都不会自动替换生产 Harness。

Web UI 默认选择 `Production / Unbound`，用于正常对照测试；选择 `Current experimental` 或
具体 `lin_xxx` 后，绑定关系在任务创建时写入，并在 Runtime 执行前固定下来。

## 测试门禁

新增测试覆盖：

1. 根谱系幂等与快照语义；
2. 父子继承且生产版本不变；
3. Candidate 仅被子谱系采用；
4. 返回祖先合法、返回 sibling 非法；
5. Agent 自主 fork 同时保留 Candidate provenance；
6. mock Agent 的确定性 continue；
7. 绑定任务实际获得谱系 Harness Context；
8. `lineage_bound` Trace 与 Task Result provenance；
9. CLI submit/show 谱系投影；
10. Root-of-Trust 字段不可进入谱系。

最终完整 `unittest discover`：250 项通过，0 失败；Web UI、Free Runtime、Autonomous
Lineage、mutation affordance 与本地密钥加载均纳入全量回归。

后续 mutation-affordance 与本地 `api.key` 加载修复专项测试：12 项通过。真实 Run 51 已收到
Task 97 与完整 mutation contract，但 DeepSeek 在 `thinking=disabled` 下仍选择 `CONTINUE`，并给出
“no mutations exist”的事实不一致理由。Host 保留该结果作为 Reasoner failure，未替模型改写为
`FORK_MUTATION`，因此没有产生虚假的自主进化成绩。

## 当前结论

本版实现的是“变化获得持续存在的子谱系”，不是“生产自动升级”。系统现在允许 Agent 在有边界、
可回退、可审计的实验世界中决定自己下一代如何继续；哪条谱系成为长期运行路径，由 Agent 后续
行动和真实世界后果体现，而非 Host 预设 Selector。
