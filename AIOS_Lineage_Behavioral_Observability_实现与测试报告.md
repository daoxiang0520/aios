# Lineage Behavioral Observability — 实现与测试报告

## 范围

在现有 v0.9-alpha.6 上补充 Stage 1 的事实观察，保留单轮 Lineage Decision。
不增加 SkillNeedDetector、Manager、推荐规则、认知合同或自动 Adoption。
`AUTHOR_COMPONENT_CANDIDATE` 仍由模型自己选择；`CONTINUE` 仍然合法。

新增路径：

```text
任务的 checkpoint / lineage_bound 关联 Cycle
→ 有界 plan_created + action_result 字段投影
→ 每任务 behavior_digest + 跨任务 cross_task_patterns
→ 单轮 Lineage Decision
→ 若选择 AUTHOR：选定任务的 digest 一并进入原有创作阶段
```

## 提供什么

- `tool_sequence`：已观察到 action_result 的工具调用顺序，保留 Trace ID；计划但未执行的动作不计入。
- `action_families`：有限的操作语法归类，例如 resource_read、file_write、file_edit、network_fetch、python_exec、file_search、shell_exec；无法匹配计划则为 unknown。
- `repeated_patterns`：同一个 Cycle 中连续的 2–4 项 family 序列，允许重叠计数，附出现次数及真实 Trace 示例。
- `failure_families`：结果明确失败时，按已有异常类归组；无异常类则为 tool_failure；exit code 单独计数。
- `reused_observation_requests`：缓存命中的请求数量。调用请求不等于重复解析/执行，不虚构物理执行次数。
- `artifact_paths_written`：成功 primitive write/edit 的工作区相对路径；不声称一定是新建，不声称已经发布到生产。Bash 中隐含的写文件不猜测。
- `resource_types_touched`：显式尝试访问路径的后缀；不证明成功/完整读取，不读文件正文，不从 Bash 自由文本推断资源类型。
- `cross_task_patterns`：至少两个任务观察到的相同 family 形状，包含任务状态、各自次数与 Trace 示例。这里的“至少两个”只是跨任务重复的定义，不是触发进化的阈值。

## 事实边界

1. 归类依据显式调用语法，不推断“因登录墙失败”“应改用某工具”等语义。
2. curl/wget、read URL、inline Python 中显式 requests.get/urllib.request.urlopen 可归为 network_fetch；Python 文件内容不读取，复杂 shell 仍是 shell_exec。
3. 一个内联函数调用签名不证明其分支真的执行或远端请求成功；政策说明明确保留这一区分。
4. 相同 family 形状不证明两个任务目的相同，更不证明 Skill 化有益。
5. 统计包括所有关联的已记录 Cycle/Attempt，而非仅最终一次结果。因此可能与最终 result.metrics 不同。
6. 无 Trace 明确标记 history_available=false；不是“确认没有发生行为”。
7. 不输出 should_create_skill、recommended_skill 或 bad_behavior 标签；不修改 Runtime/Verifier/权限。

## 大小与来源

- 每次最多 20 个最近绑定任务；调用方请求更大值也会按上限取样并标注请求值。
- 每任务最多 400 条 plan/result Trace，按 ID 从旧到新取样；不读取 context_composed 等大上下文。
- SQL 只提取需要字段，排除 stdout/stderr 全文、写文件 content、model reason 和源码正文。
- 计划中的 command 最多 4096 字符，仅本地用于语法归类，不加入 digest；截断命令不做精细归类。
- 每任务最多 24 项序列、4 个重复模式、8 个写入路径、12 种资源类型；截断数量显式保留。
- 每任务摘要最多 1200 字符；全部 digest 共用 24000 字符上限，每任务最多 3000；跨任务模式最多 8 个、总计 8000 字符。
- 以上是新增行为投影的预算，不宣称整个 Lineage Prompt（含已有 Component Set/历史等）具备同样总上限。
- 若超大错误类别表等仍超出预算，会置为 null 并列入 projection_omitted_fields；null 不是零次。
- Stage 1 和 Author 阶段出站文本沿用凭据/Host 用户目录脱敏。
- 事实随新 Evolution Run 的 diagnosis 保存；不重写 Run 73 或任何旧 Run，不额外生成 Trace。

`behavior:task:N` 是任务投影标识，`trace:N` 指向已有审计记录。当前并未开放
inspect_behavior / inspect_trace_slice 模型工具；按需多轮调查留作后续独立实验变量。

## 真实记录只读检查

本轮没有调用 DeepSeek，没有执行 lineage-run，没有生成、采用或 Benchmark 新候选。
仅从本地 Task 108–112 的既有记录构造投影：

| Task | plan/result 记录 | 动作结果 | 部分观察 |
|---|---:|---:|---|
| 108 | 48 | 27 | network_fetch 10；Python 6；已记录失败 3 |
| 109 | 86 | 62 | resource_read 53；缓存命中请求 18 |
| 110 | 1 | 0 | 有计划记录，没有动作结果 |
| 111 | 6 | 3 | resource_read 2；file_write 1 |
| 112 | 40 | 23 | network_fetch 5；resource_read 4；已记录失败 1 |

这五个样本没有命中 400 条取样上限，也没有未配对 action_result。
跨任务发现了重复读取、连续网络获取等操作形状；这些只是事实，不能据此宣称“应生成 Skill”。

## 验证

新增测试 `tests/test_lineage_behavior.py` 覆盖：

- 未执行计划不计入、重复 checkpoint 不重复计数；
- 任务间隔离、跨 Cycle 不拼造连续模式、计划错配保留 unknown；
- 成功流程也参与跨任务模式，Trace 引用和计数可追溯；
- 不把注释/字符串中的 requests.get 当成调用，不猜脚本正文；
- 失败写入不记为产物、Host 路径不泄露、cache hit 与失败分别计数；
- SQL 排除巨大输出/源码，Trace 与投影预算有明确截断；
- task_limit 和摘要预算、重复观察不写入 Trace/Checkpoint；
- 即使出现重复模式，模型仍可 CONTINUE，不产生 Candidate 或子谱系。

既有 Author 集成测试同时验证选定任务的 behavior_digest 进入创作请求。

验证结果：

- 行为投影专项最终 13/13 通过（含全量运行之后新增的超大错误类别表预算测试）。
- 谱系 / Author 专项 21/21 通过。
- 全量运行 272 项：267 通过、2 失败、3 跳过。两项失败仍为此前的 v0.5 网络能力断言：
  test_enabled_network_is_available_and_uses_unrestricted_docker_bridge 和
  test_network_task_stops_before_model_and_memory；Docker operational health 不可用时，
  当前 Runtime 返回 blocked_capability 而旧断言期望 available/needs_authority。三项跳过均需真实 Docker。
- 本轮未修改这些网络策略或断言，不把完整套件报告为全绿；git diff --check 无空白错误。

这只证明行为证据能正确、有界地送入现有决策路径；没有真实模型重跑，不能据此声称
Need Detection 或 Skill Authoring 概率已经提高。
