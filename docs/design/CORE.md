# CORE 模块设计：会话·门禁·路由·记忆

> 来源：设计文档 v1.7 拆分（原 §4.1-§4.4，内容原样迁移；章节保留原编号以维持外部引用）。
> 职责边界：接口契约见 `docs/SPEC.md`；为什么这么设计见 `docs/DECISIONS.md`；本档只讲机制（HOW）。

### 4.1 会话管理器

**v1 状态机（睡眠默认 + 热键唤醒，D22）——壳与主循环一体，五个状态**：

```mermaid
stateDiagram-v2
    direction LR
    Standby: 睡眠 · 闭眼瞌睡（零转写，等热键）
    Listening: 监听 · 雷达眼
    Thinking: 思考 · 瞳孔转圈
    Speaking: 说话 · 嘴动
    TaskRunning: 编码态 · 工作版式

    [*] --> Standby: 启动（默认即睡）
    Standby --> Listening: 热键唤醒（v1.1 + 语音"小饼"）
    Listening --> Standby: 超时8s / 拜拜 / 语义结束
    Listening --> Thinking: 门禁通过 · 闲聊意图
    Listening --> TaskRunning: 门禁通过 · 编码意图
    Thinking --> Speaking: 首 token 就绪
    Speaking --> Listening: 播报完成 / 被打断
    TaskRunning --> Speaking: 任务完成 · 摘要播报
    TaskRunning --> Listening: 任务转后台
```

- **任务完成自动唤醒**：睡眠中后台任务完成 → 睤眼播报 → 8s 无新输入回睡（"管家主动汇报"）；
- **唤醒方式**：v1 = 热键（ctl，可配置键位）；v1.1 = + 语音唤醒词（openWakeWord + P5 补丁）；**常听模式已删除**（D22：误触家人对话/心理不安/任务误触发三重风险）；
- **睡眠 = 零转写**：浏览器麦克风保持授权打开（页面常驻），但音频不进任何处理管线——不切段、不转写、不存（逻辑零转写；"物理静音"= 待机时停 track，留实测后定）；
- 两个**不改变状态**的静默情形（图上不画）：门禁未通过（drop/灰区）→ 留在监听、零输出；member 编码请求 → 拒绝话术走 Speaking；
- **结束信号**（优先级从高到低）：口头"拜拜/好了"（路由专项规则）→ LLM 回复尾随 `[end]` 标志 → 8s 超时；
- **打断**：Speaking 态下 VAD 检测人声 → 立即停 TTS → 回 Listening（OLV 原生能力）。

### 4.2 声纹门禁与权限档案

```mermaid
sequenceDiagram
    autonumber
    participant VAD as VAD
    participant GATE as 门禁(wafer-core)
    participant LED as render
    participant ASRx as ASR
    participant LLM as 后续管线

    Note over GATE: 注册期（scripts/enroll.py，离线）
    U->>GATE: 15-30s 干净样本 ×3
    GATE->>GATE: 均值嵌入 → voiceprints/<name>.bin
    GATE->>GATE: speakers.yaml 增加档案（role/priority）

    Note over GATE: 运行期（每声段）
    VAD->>GATE: 声段（PCM）
    GATE->>GATE: embed + 与全部注册声纹算余弦
    alt score ≥ T_pass 且说话人优先级最高
        GATE->>ASRx: 放行（带 speaker_id 标签）
        ASRx->>LLM: 转写 + speaker 上下文
    else T_gray ≤ score < T_pass
        GATE->>LED: 灰区：丢弃 + LED "？"
    else score < T_gray / 未注册
        GATE->>GATE: 静默丢弃（不触发 ASR）
    end
    GATE-->>GATE: 审计日志 gate.decision
```

- 多人同段：取**注册优先级**（priority 小者优先）中得分最高者；均不过 → 按灰区/丢弃处理（§12#5）；
- 阈值：`T_pass`/`T_gray` 配置化，初值 0.62/0.55（CAM++ 余弦，**待家庭实测标定**）；离线评估用合成测试集（§6.2）；
- 热键兜底：全局热键临时 `gate.enabled=false`（感冒/变声）；
- 权限档案：`speakers.yaml`（schema 见 §5.3）；`role: admin` = chat+coding+opencode，`role: member` = 仅 chat。

### 4.3 意图路由器与护栏

**术语与符号（本节读图须知）**：

| 术语 | 含义 |
|---|---|
| **S（评分）** | 对一句话的"编码意图"打分：按下方规则表累加的纯数字 |
| **S_direct = 2.5** | 直达阈值：S ≥ 2.5 → 判定"确定要干活"，跳过 9B 直送 opencode |
| **S_gray = 1.0** | 模糊阈值：S_gray ≤ S < S_direct → **模糊带**，交给 9B 判断（它手里有转交工具） |
| **模糊带** | [S_gray, S_direct) 分数区间。⚠️ 与**声纹灰区**（§4.2）是两个概念：模糊带的请求**会被继续处理**（9B 接手），声纹灰区的声音**被直接丢弃**（只是眨个"？"）——两个"中间地带"，两种命运 |
| **admin / member** | 说话人权限角色（speakers.yaml，§4.2）：admin = 主人全权限；member = 家人仅聊天 |
| **闸N** | 护栏编号（需求 §6.2 共六条，§4.6 有落点对照）：闸1 工具白名单 / 闸2 意图路由（本节）/ 闸3 边界提示词 / 闸4 结果只读回流 / 闸5 身份门禁与权限（5b = 桥接器二次校验）/ 闸6 异常降级 |
| **盖章** | WaferAgent 在工具调用上附加已验证的 speaker 身份——LLM 看不见也改不了（§4.6） |
| **审计日志** | 每次路由/工具决策的结构化记录（ts·speaker·decision·score·prompt_sha256·task_id，§5.2）——调试数据 + 将来的升级训练集 |
| **TASK_DIRECT 等** | 路由结果的枚举值（§3.6 代码）：CHAT 纯闲聊 / LLM 模糊交 9B / TASK_DIRECT 直达 opencode / TASK_DENIED member 被拒 / SESSION_END 会话结束 / TASK_AMEND 修正当前任务 / TASK_CANCEL 取消当前任务（后两者仅在活跃任务时触发，D15）/ TASK_RESET 重置工作会话（D16） |

**评分怎么算——三句话实测**（对照下方规则表）：

| 你说的话 | 命中 | S | 去向 |
|---|---|---|---|
| "帮我修一下登录报错" | 修（动词）+ 报错（宾语）+ 帮我（语境）+ 组合加成 | 1+1+0.5+1 = **3.5** | ≥ 2.5 → 直达 opencode |
| "我昨天看了本讲 git 的书" | git（宾语） | **1.0** | 模糊带 → 9B 判断（大概率聊书不转交） |
| "今天好累啊" | 无命中 | **0** | 纯闲聊 |
| "派活，把刚才那个问题弄一下" | 触发前缀"派活"（句首） | 免评分 | **快车道直达**；"刚才那个"由组装器消解（§4.6） |

```mermaid
flowchart TD
    IN["声段（已过门禁，带 speaker_id）"] --> ASR["ASR 转写"]
    ASR --> PRE{"触发前缀命中？<br/>（句首锚定：派活 / 开工）"}
    PRE -->|"是 · 快车道（D14）"| PM{"权限：admin？"}
    PRE -->|"否"| RT{"路由评分"}
    RT -->|"S ≥ S_direct · 置信编码"| PM
    RT -->|"模糊带（S_gray ≤ S < S_direct）"| LLM["本地 9B + 闸3 边界提示词"]
    RT -->|"S < S_gray · 闲聊"| LLM
    PM -->|member| REJ["确定性拒绝<br/>（拒绝由代码决定，9B 仅措辞）<br/>LED 冒汗表情"]
    PM -->|admin| CMP["任务组装器：受限改写 + 上下文提取<br/>（模糊带路径无需——WaferAgent 调工具时天然组装；详见 §4.6）"]
    CMP --> BR["桥接器（盖章 speaker）"]
    LLM -->|"调用唯一工具"| TL{"闸1 工具名 ==<br/>task_to_opencode？"}
    TL -->|否| BLOCK["拦截 + 审计（不应发生）"]
    TL -->|是| PM2{"闸5b 桥接器二次校验<br/>speaker == admin？"}
    PM2 -->|否| REJ2["拒绝 + 审计"]
    PM2 -->|是| BR
    LLM -->|"纯文本 + [emotion:x]"| OUT["TTS + 情绪标签 → render"]
    BR --> RES["闸4 只读回流：摘要 → TTS + LED"]
    AUD["审计日志：每次路由/工具调用<br/>ts·speaker·decision·score·prompt_sha256·task_id"]
```

**路由评分规则**（`config/router.yaml`，全部确定性）：

| 类别 | 示例 | 分值 |
|---|---|---|
| 动作动词 | 写/改/修/跑/执行/部署/编译/重构/提交/安装 | +1.0 |
| 编码宾语 | 代码/文件/函数/接口/报错/bug/测试/脚本/项目/git | +1.0 |
| opencode 语境 | 帮我/让它/交给/任务 | +0.5 |
| 强信号组合 | 动词+宾语同时命中 | +1.0 加成 |

- 阈值：`S_direct=2.5`（含组合加成即达），`S_gray=1.0`；
- 专项规则：口头结束词（"拜拜/好了"）→ 会话结束，不进 LLM；
- 专项规则：**触发前缀快车道**（句首锚定，默认 `["派活", "开工"]`，`router.yaml` 可配）→ 无视评分直达 TASK_DIRECT（权限校验照过）——给用户 opt-in 的 100% 确定性，自然语言瀑布照常兜底，两者并存（D14）；
- 专项规则：**唤醒词分流（D48，2026-10-25）**：待机 KWS 命中「小饼干活」→ 唤醒并**强制任务路由**（剥离唤醒词，跳过评分）；同句带任务则直接提交，只唤醒则回"好，说吧"并置 `pending_work`（下句直达派活，即使无动词）；「小饼小饼」命中 → 正常唤醒，不动路由。词表两行在同一 `keywords.txt`，`wake.word{keyword}` 记录命中词；
- 专项规则：**任务修正 / 取消**（仅当有活跃任务时生效，D15）：修正线索（不对/不是/说错了/等等）→ TASK_AMEND；取消线索（取消/算了/停）→ TASK_CANCEL；无修正线索但新话直达评分且任务在跑 → TTS 询问"停掉重来还是排队"（v1 单任务策略）；
- 专项规则：**会话重置**（D16）：重置线索（"新开会话/重头搞"）→ TASK_RESET——关闭工作会话，下个任务全新开始（详见 §4.6 会话策略）；
- 每条规则、阈值均可在 CI 中用 fixture 断言（§6.4）。

**设计依据（两级瀑布，业界同构）**：路由不是纯关键词——**模糊带交给 9B 判断**（它持有唯一工具），关键词只处理高置信直达。不让 9B 全程路由的理由：①延迟账——9B 一次路由决策 0.5~1.5s，吃光每轮 ≤1.5s 的预算，规则评分 <1ms；②可测性——规则是纯函数可 CI 断言，LLM 判断随采样漂移。错误代价不对称：误报（闲聊当编码）靠组合阈值压制，漏报（编码进闲聊）无害——模糊带 9B + 唯一工具双重兜底。与 Home Assistant 语音管线（模板意图优先 + LLM 兜底）同构，属本地助手场景主流方案。

**升级路径**：审计日志即训练数据积累器——若误路由多，依次升级：扩词表（改 YAML）→ 用日志训小分类器替换规则层 → 陪聊档由云端大模型路由；只换第一级，架构不动。

### 4.4 记忆管理

> **现状（v1.5 审计）**：本节设计仍是 v1.2/M7 的计划，**生产链路未接线**——`MemoryManager`/`SpeakerMemory` 已实现（M2/T2.5，`core/memory/`），但 `pipeline._chat_turn` 每轮只发 `[system, user]`（无历史注入，VE2E-A2 锁定测试）；滚动摘要/persona 模板/`config/memory/` 目录/清理脚本均未创建。多轮记忆整体归 v1.2 陪聊档（D30 用户裁决顺延）。下文为设计原文，保留作为 M7 实施依据。

**OLV 自带什么 / 我们补什么**：OLV 自带人设卡（随每次请求发送）与聊天记录持久化（可续聊）；v1 的长期记忆模块官方已暂时移除，且无按人记忆、无滚动摘要——本节补齐这三样。业界方案光谱：滑动窗口（丢旧信息）→ **滚动摘要（本方案：旧轮压缩 + 保留近窗）** → 向量记忆 RAG（Mem0/Letta，强但重，留 P2）。

**每轮发生什么（从你说的一句话开始读）**：

```mermaid
flowchart LR
    A["你说一句话"] --> B["组装 prompt<br/>（LLM 无状态 · 每轮重发全量）"]
    P["① 人设卡"] --> B
    F["② 说话人档案"] --> B
    S["③ 滚动摘要"] --> B
    H["④ 最近几轮历史"] --> B
    B --> C["本地 9B"] --> D["回复 → 喇叭"]
    D --> E["本轮对话追加进历史"]
    E --> G{"轮数 ≥8 或<br/>token 超限？"}
    G -->|"否 · 直接进下一轮"| A
    G -->|"是 · 每 8 轮一次"| SUM["9B 压缩旧轮 → 更新摘要<br/>历史截短（防漂移：放水露锚）"]
    SUM --> A
```

**四份材料的存储位置**（图外信息）：

| 材料 | 位置 | 说明 |
|---|---|---|
| 人设卡 | `config/persona/persona.md.tmpl` | 模板渲染（`{{robot_name}}` 等变量） |
| 说话人档案 | `config/memory/<speaker>.md` | 事实/偏好/提醒，与声纹身份对应 |
| 滚动摘要 | `data/summaries/<session>.md` | 每 8 轮重建 |
| 全量历史 | `data/sessions/<date>.jsonl` | 完整留档，**永不进 prompt**（仅审计/回看） |

- 注入预算：人设卡 ≤1.5K + 档案 ≤0.5K + 摘要 ≤0.5K；
- **人设漂移与摘要的关系**：LLM 无状态，人设卡在**每次请求**的上下文里（客户端每轮重发全量），不存在"中途掉出"；漂移主因是长历史**稀释**了系统提示的影响力。每 8 轮的滚动摘要把历史截短 → 人设卡相对权重回升——防漂移靠"放水露锚"，不是"重新抛锚"。若实测仍漂移，进阶手段是在历史中段插一条系统提醒消息（按实测决定，不预先设计）；
- 清理：`scripts/cleanup_history.py --before <date>`（§12#7）；
- 摘要生成失败 → 退化为"最近 N 轮原文截断"，不阻塞对话。
