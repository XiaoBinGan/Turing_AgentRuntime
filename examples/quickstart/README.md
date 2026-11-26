# examples/quickstart —— 从零写自己的第一个 Agent

三个能直接跑的示例，**只用标准库**，不需要 API Key 就能跑通前两个：

```bash
.venv/bin/python examples/quickstart/01_minimal.py    # 一个工具 + 一个假 LLM + 一次 Loop.run
.venv/bin/python examples/quickstart/02_real_llm.py   # 换成 OpenAI 兼容端点（需 BASE_URL/API_KEY/MODEL）
.venv/bin/python examples/quickstart/03_approval.py   # T2 高风险工具的审批与三种续跑方式
```

完整讲解（真实输出、概念速查表、上手障碍）：[`docs/design/QUICKSTART.md`](../../docs/design/QUICKSTART.md)

三个例子里 Runtime 侧一个字都没改 —— 换的只有「工具定义」和「LLM 实现」。
