"""agents/__init__.py — **示例 Agent 的实现，不是 Runtime 的一部分。**

目录名本身就是架构声明：

    plane/   与领域无关的管道（Memory / Knowledge / Tool / Quality / Governance）
    agents/  会随客户变化的判断（这一段话属于哪类记忆、算几分重要、什么算同一事实）

`plane/memory.py` 只认 `contracts.Extractor` 这个 SPI：它知道「记忆有三类、管道有五段、
保留矩阵怎么淘汰、同一条事实怎么覆写」，但不知道「这句话是偏好还是报错」。
后者是领域知识 —— 换成英文客户、法务 Agent、代码 Agent，判断完全不同，
而 Memory Plane 的存储、重排、反思、巩固、遗忘一行都不用改。

所以：**新增业务领域 → 在 agents/ 下加一个 Extractor，别改 plane/。**

本目录的运维域内容（Runtime 侧的工具 / 技能 / Agent 三张表都是空的，由这里填）：

    fake_backend.py   内存假集群（不连真网络），演示数据与可变状态都在这里
    tools.py          8 个工具 + 诚实的 side_effect / risk_tier（T0×4 / T2×2 / T3×2）
    skills.py         3 个多 Tool 编排技能（索引常驻、规程按需）
    registry.py       3 个 AgentSpec（sre / release / capacity）+ build_router()
    tools.yaml        给 toolconfig.load_tools() 读的配置面，与 tools.py 一一对应

记忆与措辞这一半（Runtime 只给 SPI，内容全在这里）：

    extractor.py      记忆抽取器 OpsExtractor：哪类记忆、几分重要、什么算同一事实
    prompts.py        Prompt 人格 / 硬性约束段 / 段 header / token 档位
    demo_extractor.py、demo_template.py
                      重构前的实现，**保留为对比基线**（tests/test_agent_extractor.py
                      会拿 OpsExtractor 与它们对拍）

`__init__` 刻意**不 eager import** 上面这些模块：`import agents` 应当永远很轻，
否则「只想拿一个 Extractor」的调用方会被迫加载整张工具表与假后端。
"""
