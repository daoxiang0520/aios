# AIOS v0.9-alpha.4 Free Runtime 设计与测试报告

## 目标

将 Verifier 从生产与演化在线闭环移出，而不是替换成更自由的裁判。在线系统只保存世界事实、
Agent 声明与真实执行后果；研究者需要评价时，通过独立、只读的 shadow measurement 执行。

```text
Online:
Agent → Action → World Consequence → Stop / Yield / Continue

Offline:
Recorded Result → Shadow Verifier → Research Measurement
```

## 保留的不变量

- History：Task、Checkpoint、Trace 与世界修改历史保留；
- Reversibility：Sandbox 事务提交、Capsule/Fork/Rollback 机制保留；
- Reality：Tool Result 来自真实执行，Agent 不能伪造 Host 观察；
- Boundary：Authority、SecurityKernel、Sandbox、资源上限和 Root of Trust 不开放。

Free Runtime 只移除正确性裁判，不移除环境边界。

## 状态语义

| 状态 | 含义 | 是否表示真实完成 |
|---|---|---|
| `stopped` | Agent 声明当前目标已经达到并停止 | 否 |
| `yielded` | Agent 未声明完成，但本轮主动让出 | 否 |
| `abandoned` | 执行/协议故障耗尽，系统停止投入 | 否 |
| `completed` | 仅保留给 verified 兼容模式 | 是，由旧 Verifier 判定 |

Free 模式中 `success=null`、`host_observed_completion=null`，避免删除 Verifier 后把其语义偷渡到
Completion Arbiter。

## Research Instrument

```powershell
python -m aios --config config.json result shadow-verify 85
```

该命令输出 `measurement_mode=offline_shadow` 与 `affects_task_state=false`。它不更新 Task status、
不添加 Checkpoint/Trace/Memory，也不修改工作区，因此 Agent 的在线学习链看不到该测量结果。

## 兼容与启用

```json
{
  "runtime": {
    "completion_mode": "free"
  }
}
```

合法值为 `free` 和 `verified`。旧配置缺省为 `verified`；当前 `config.json` 和
`config.example.json` 显式采用 `free`。

## 测试范围

新增回归覆盖：

1. 旧配置默认 verified；非法模式拒绝；
2. Agent stop 不调用在线 Verifier，不写成功 Memory；
3. yield 不产生完成判断；
4. 存在未恢复 Tool failure 时，Agent 仍可停止，Host 不进行正确性拒绝；
5. 协议失败耗尽进入 abandoned，不进入 dead letter；
6. shadow verification 前后 Task、Checkpoint、Trace 完全不变；
7. verified 既有完整测试继续保持原语义。

最终验证：`python -m unittest discover -s tests` 共运行 **236 项**，全部通过；
`python -m compileall -q src tests` 通过，`git diff --check` 无内容错误（仅 Windows
工作区的 CRLF 提示）。

## UI 模式区分

- 顶部常驻 Runtime policy 徽标：蓝色 `FREE LOOP`、绿色 `VERIFIED LOOP`；
- `Normal / Inspect` 明确标为显示模式，不改变 Runtime policy；
- 任务详情从任务自身 Evidence 推断历史运行模式，不用当前配置覆盖历史事实；
- Free 状态显示为 `agent stopped / agent yielded / runtime abandoned`，与
  `verified complete / verified degraded / verified rejected` 分离；
- Chat 顶部展示 completion semantics，Evidence 页展示完整结构化投影；
- 桌面三栏和 390px 窄屏均通过真实浏览器目视验收，窄屏仍保留 Runtime policy 徽标。
