# USB Assistant 优化计划
**制定日期**: 2026-04-29  
**基准测试**: `test_report_2026-04-29.json`（API 100% / Judge 均分 4.8 / E2E 89%）

---

## 优先级与目标

| 优化项 | 目标指标 | 当前基准 |
|--------|---------|---------|
| P1-A Prompt Caching | TTFT ↓ 60%（SQL Agent 首轮 15s→6s） | 平均 45s/轮 |
| P1-B 回答截断修复 | 截断率 0%（当前约 30% 长回答有截断） | 完整性均分 4.8 |
| P2-A Tool Result Clearing | 数据使用维度 ≥ 4.7（当前 4.4） | Judge 数据使用 4.4 |
| P2-B 范围控制 | 相关性 ≥ 4.95（当前 4.9） | Judge 相关性 4.9 |
| P3   DuckDB 并发安全验证 | E2E 100%（当前 89%） | E2E 8/9 |

**不做**（当前体量不划算）：Intent + Schema Pruning 前置、Scratchpad 独立工具

---

## Phase 1 — 低风险高收益（目标 1 周内完成）

### P1-A：Prompt Caching

**问题**：每次请求都重新传输相同的静态 system prompt（角色指令 ~2KB），
造成不必要的 TTFT 延迟和 input token 费用。

**当前代码位置**：
- `backend/llm.py` : `_stream_claude()` L118、`run_sql_agent_loop()` L286
- `backend/prompts.py` : `build_system_prompt()` L63

**改动方案**：

把 `system` 字段从字符串改为数组，将静态部分（角色指令）和动态部分（商家数据、数据摘要）分层：

```python
# llm.py — _build_cached_system(system_prompt: str) -> list[dict]
# system_prompt 已经是 build_system_prompt() 拼好的字符串
# 我们按固定分隔符把它拆成静态层 + 动态层

def _build_cached_system(system_prompt: str) -> list[dict]:
    # 约定：build_system_prompt() 输出中，
    # "\n## About This Merchant" 之前的部分是静态的（角色 + 规则）
    # 之后是动态的（商家信息 + 数据摘要）
    split_marker = "\n## About This Merchant"
    if split_marker in system_prompt:
        static_part, dynamic_part = system_prompt.split(split_marker, 1)
        return [
            {
                "type": "text",
                "text": static_part.strip(),
                "cache_control": {"type": "ephemeral"},  # 缓存静态层
            },
            {
                "type": "text",
                "text": split_marker + dynamic_part,     # 动态层不缓存
            },
        ]
    # 如果没有动态内容，整体缓存
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
```

同时需要在 `headers` 里加 `"anthropic-beta": "prompt-caching-2024-07-31"`（目前 Prompt Caching 仍为 beta）。

**需要改的文件**：
- `backend/llm.py`：
  - `_stream_claude()`：把 `"system": system_prompt` 改为 `"system": _build_cached_system(system_prompt)`
  - `run_sql_agent_loop()` 里两处 `"system": system_prompt` 同样改
  - 所有 headers 加 `"anthropic-beta": "prompt-caching-2024-07-31"`

**风险控制**：
- 动态内容（`## About This Merchant`、`## 已上传的数据表`、`## 商家知识库`）绝不加 cache_control
- 加日志：记录每次请求的 `usage.cache_read_input_tokens` 和 `usage.cache_creation_input_tokens`，验证命中率

**验证方法**：连续发两条相同商家的消息，第二条的 `cache_read_input_tokens > 0` 即命中。

---

### P1-B：回答截断修复

**问题**：长回答（>2000字）被 token 上限截断，`analysis_card` 末尾表格丢失。
当前 `max_tokens=4096` 已经够，但 `prompts.py` 里的 `SYSTEM_PROMPT` 缺乏主动收尾机制，
导致模型在 token 用完前没有机会做结构化收尾。

**改动方案**：

在 `SYSTEM_PROMPT` 的 Analysis Card 规则里，已有这一行：
> "CRITICAL: The analysis card MUST be the very last thing..."

但缺少对**正文**的收尾约束。在 Response Style 章节加一条：

```
## Response Length
- Keep responses focused. For data analysis: lead with findings, follow with top 3 actions, close.
- If your answer has multiple sections, limit to 4 sections maximum. Each section max 200 words.
- When you have covered all key points, stop. Do not pad with summaries of what you just said.
```

同时在 SQL Agent 的 final round（`is_last=True`）加一条 user 级别的收尾提示注入：
```python
# run_sql_agent_loop() 进入 final round 前
messages.append({
    "role": "user",
    "content": "[系统提示] 请基于以上查询结果给出最终分析。回答请控制在 800 字以内，使用 analysis_card 作为最后元素。"
})
```

**需要改的文件**：
- `backend/prompts.py`：在 `SYSTEM_PROMPT` 加 `## Response Length` 章节
- `backend/llm.py`：`run_sql_agent_loop()` 进入 `is_last` 前注入收尾提示

**风险控制**：字数软约束（800字）不是硬截断，模型可以适当超出，但会倾向于收尾。

---

## Phase 2 — 中等改动（目标 2 周内完成）

### P2-A：Tool Result Clearing（多轮数据引用质量）

**问题**：SQL Agent 多轮后，messages 历史里堆积了大量中间 SQL 片段和错误重试的
tool_result，导致第3轮时模型"淹没在噪声里"，不再深度引用具体数字（数据使用 4.4 < 4.8）。

**当前代码位置**：`backend/llm.py` `run_sql_agent_loop()` L379

```python
messages.append({"role": "user", "content": tool_results})
```

**改动方案**：

在每轮工具调用后，检查结果是否为错误，如果是错误则将这一轮的 assistant（tool_use）和
user（tool_result）从 messages 里清除，只保留成功的查询：

```python
# run_sql_agent_loop() 里，在 messages.append({"role": "user", ...}) 之后

# 判断本轮所有工具调用是否都成功
all_success = all(
    not r["content"].startswith("工具执行错误")
    for r in tool_results
)

if not all_success:
    # 移除刚才追加的 assistant 块（失败轮）
    messages.pop()  # 移除 tool_results user 块（还没加）
    # 重新只加错误摘要，不加完整失败结果
    messages.pop()  # 移除失败的 assistant content_blocks
    messages.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": ..., 
                     "content": "上一条SQL有误，请修正后重试"}]
    })
else:
    messages.append({"role": "user", "content": tool_results})
```

另外，对成功的 tool_result，如果结果超过 100 行，裁剪为摘要（前 20 行 + 统计摘要），
避免大量原始数据占满 context：

```python
def _summarize_result(result_text: str, max_rows: int = 20) -> str:
    lines = result_text.strip().split("\n")
    if len(lines) <= max_rows + 2:  # +2 for header
        return result_text
    header = lines[:2]
    sample = lines[2:max_rows + 2]
    return "\n".join(header + sample) + f"\n... (共 {len(lines)-2} 行，已截断显示前 {max_rows} 行)"
```

**需要改的文件**：`backend/llm.py`

**风险控制**：
- 改之前先加详细日志（记录每轮 messages 长度、清除前后 token 估算）
- 错误轮清除逻辑单独封装成 `_clean_failed_tool_rounds(messages)` 函数，方便单独测试
- OpenAI Agent 路径（`_sql_agent_openai`）同步做同样改动

---

### P2-B：范围控制

**问题**：AI 偶尔超出问题范围延伸（Judge 相关性 4.9，偶有"用户问概述，AI 给了促销建议"）。

**改动方案**：

在 `SYSTEM_PROMPT` 的 Response Style 章节加范围约束，用**正面示例**而非禁令：

```
## Scope Discipline
Answer exactly what was asked. If you have a related insight the merchant didn't ask for,
add it in ONE sentence at the end: "如需了解[X]，可以继续询问。" — do not expand it.

Example (correct):
  User: "哪个门店销售额更高？"
  You: "Grand Indonesia 门店更高，本月收入 2.8M IDR，比 Senopati 高 40%。[analysis_card]"
  ✗ Wrong: 在回答后继续展开"如何利用这个优势"的3个策略

Example (correct):
  User: "帮我做个整体概述"  
  You: 给出概述（总收入、门店对比、Top商品），结尾一句"如需深入分析某个方向，告诉我"
  ✗ Wrong: 概述里顺带设计了一个促销方案
```

**需要改的文件**：`backend/prompts.py`

**风险控制**：改完后先通过 test_agent.py 的场景 A 第1轮（"帮我做个整体概述"）验证，
看 Judge 是否给出"未超出范围"的评价。

---

## Phase 3 — 验证与清理（改动完成后）

### 对比测试方案

每个 Phase 完成后运行完整测试，与基准对比：

```bash
# 基准（已归档）
# test_report_2026-04-29.json

# P1 完成后
python -m backend.test_agent --report test_report_p1.json

# P2 完成后  
python -m backend.test_agent --report test_report_p2.json
```

**对比维度**：

| 维度 | 基准 | P1 目标 | P2 目标 |
|------|------|---------|---------|
| SQL Agent 首轮延迟（s） | ~45 | ≤ 25 | ≤ 25 |
| Judge 综合均分 | 4.8 | 4.8（不退步） | ≥ 4.9 |
| Judge 数据使用 | 4.4 | 4.4（不退步） | ≥ 4.7 |
| Judge 相关性 | 4.9 | ≥ 4.9 | ≥ 4.95 |
| 截断次数（8轮中） | 3轮 | 0轮 | 0轮 |
| API 通过率 | 100% | 100% | 100% |
| E2E 通过率 | 89% | 89% | ≥ 89% |

**延迟如何测量**：
test_agent.py 的 `Round.latency_ms` 已经记录每轮耗时，
对比 `test_report_p1.json` 和基准的 `latency_ms` 均值即可。

**截断如何检测**：
在 test_agent.py 里加一个截断检测函数：

```python
def _has_truncation(text: str) -> bool:
    # analysis_card 存在但没有 closing tag
    if "<analysis_card>" in text and "</analysis_card>" not in text:
        return True
    # 文字突然结束（无标点收尾）
    if text and text[-1] not in "。.！!？?」）)」…":
        return True
    return False
```

每轮 Round 记录 `truncated: bool`，报告里输出截断率。

### 回滚方案

每个 Phase 改动前：
1. `git commit`（或备份改动的文件）
2. 保留旧版本 `prompts.py` 为 `prompts_v1.py`（方便快速切回）
3. 改动上线后观察 1 天，如果 Judge 均分下降 > 0.2，立即回滚

---

## 执行顺序总结

```
Week 1
  Day 1-2 : P1-A Prompt Caching（改 llm.py，加日志验证命中率）
  Day 3   : P1-B 截断修复（改 prompts.py + llm.py final round 注入）
  Day 4   : 运行 test_agent.py 对比测试，确认 P1 指标达标
  Day 5   : P2-B 范围控制（改 prompts.py，风险低，先改）

Week 2
  Day 1-3 : P2-A Tool Result Clearing（最复杂，单独分支开发 + 充分日志）
  Day 4   : 运行完整对比测试
  Day 5   : Code review + 上线，归档 test_report_p2.json
```

---

## 附：test_agent.py 需要同步增强的内容

配合优化验证，测试脚本需要新增：

1. **截断检测**：每轮记录 `truncated` 字段，报告输出截断率
2. **延迟分层统计**：区分"首 token 延迟（TTFT）"和"总延迟"，验证 Caching 效果
3. **Cache 命中率**：从 `/status` 或新增 `/metrics` 端点读取 cache 命中统计
4. **回归对比模式**：`--compare baseline.json` 参数，直接输出与基准的 diff 表格
