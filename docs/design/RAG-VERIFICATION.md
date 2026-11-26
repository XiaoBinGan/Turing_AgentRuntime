# RAG 真实后端验证

之前所有 RAG 测试都用**假 embedding**（哈希向量）。这份文档记录接**真实 2B
embedding + reranker 模型**后的实测结果。

后端：`Qwen3-VL-Embedding-2B`（2048 维）+ `Qwen3-VL-Reranker-2B`，
vLLM 0.11 部署在另一台 64 核机器上，经 Tailscale 访问（**28ms RTT**）。

> 下面的延迟都含这 28ms 网络往返。同机部署会低约 30ms。

## 1. Embedding 接入验证

`adapters/embeddings.py` 的 `OpenAICompatEmbeddings` 直连 vLLM 的
`POST /v1/embeddings`：

```
3 条 × 2048 维，172ms（含 RTT）
相似度（排障 vs 连接池耗尽）: 0.5725
相似度（排障 vs 今晚吃什么）:   0.3875
```

语义区分有效：相关文本 0.57，无关文本 0.39。

## 2. 端到端入库与检索

5 条运维文档入库 → 两次查询：

| 操作 | 耗时 |
|---|---|
| 入库 | 444ms（89ms/条，每次一条 embed 调用） |
| 查询「为什么 p99 阶梯式上涨」 | 86ms |
| 查询「发布要注意什么」 | 82ms |

检索结果正确：问 p99 时返回连接池耗尽的两条（0.586 / 0.580），
问发布时返回 DDL 禁令与灰度流程（0.506 / 0.458）。

## 3. 纯向量排序会错 —— 这是两阶段检索的理由

**设计了一个能暴露问题的语料**：混入"字面高度相似但语义无关"的干扰项。

```
查询：为什么 p99 阶梯式上涨

① 纯向量 top-4
   1. 0.658  p99 指标的计算方式是从延迟分布取第 99 百分位   ← 讲 p99，但不是根因
   2. 0.616  api p99 从 800ms 涨到 3s，连接池 max_connections=20 被打满  ← 真因
   3. 0.437  发布流程要求先过回归再灰度
   4. 0.435  日志盘占用超 85% 告警
```

**真因被排到第 2。** embedding 模型看到"p99"这个高频词就给了高分，
分不清"讲 p99 是什么"与"p99 为什么涨"。

```
② rerank 后（84ms）
   1. 0.931  api p99 从 800ms 涨到 3s，连接池 max_connections=20 被打满  ← 纠正了
   2. 0.926  日志盘占用超 85% 告警
   3. 0.915  发布流程要求先过回归再灰度
   4. 0.898  p99 指标的计算方式是从延迟分布取第 99 百分位
```

**rerank 把真因提到了第一。** 两阶段检索不是"锦上添花"，是**修正了单阶段的排序错误**。

## 4. 但 rerank 的绝对分数不可信

注意②的分数分布：`0.931 / 0.926 / 0.915 / 0.898` —— **全部挤在 0.9 以上，
最大差 0.033**。而实际相关性天差地别（第 1 条是真因，第 4 条只是定义）。

**所以：**

| 用法 | 是否可靠 |
|---|---|
| 按 rerank 分数**排序** | ✅ 可靠（相对顺序对） |
| 用 rerank 分数做**阈值过滤**（如 <0.5 丢弃） | ❌ 不可靠（全都 >0.89，一个都丢不掉） |
| 跨查询比较 rerank 分数 | ❌ 不可靠（不同 query 的分布不同） |

正确做法：**rerank 只用来定序，阈值用向量相似度或独立的相关性判断。**

## 5. 代价

| 阶段 | 耗时（含 28ms RTT） |
|---|---|
| 向量检索 top-4 | 85ms |
| rerank 4 条 | 84ms |
| **合计** | **169ms（几乎翻倍）** |

**两阶段是 2 倍延迟换排序正确性。** 是否值得取决于场景：

- 用户直接读结果（如对话补全）→ 值得，169ms 无感
- 高频内部调用 → 可以考虑只对 top-2 做 rerank，或只在高不确定性时做

## 6. 复现

```bash
# 需要能访问 vLLM embedding / reranker
EMB='http://HOST:18000/v1' RR='http://HOST:18001/v1/rerank'
.venv/bin/python - <<'PY'
from adapters.embeddings import OpenAICompatEmbeddings
from plane.vector_store import VectorProvider
from contracts import Subject, MemoryRecord, MemoryKind
emb = OpenAICompatEmbeddings(base_url=EMB, model='m', dim=2048)
vp = VectorProvider(embedder=emb, location=':memory:', dim=2048)
sub = Subject(tenant='acme', user_id='u')
vp.put(MemoryRecord(id='d0', kind=MemoryKind.SEMANTIC, text='...', subject=sub))
for rec, score in vp.search('查询', sub, k=4):
    print(score, rec.text)
PY
```

## 未做

- **没接到 `KnowledgeBase` 的检索路径上**：`KnowledgeBase` 用 BM25（`core/fts_index.py`），
  向量检索走 `VectorProvider`（memory plane）。两条路径**目前是分开的**，
  没有"BM25 + 向量 + rerank"三路融合。这是设计选择（Knowledge 面用全文、
  Memory 面用向量），但如果要做混合检索，需要一个融合层。
- **没测并发下的 embedding 吞吐**：vLLM 有 continuous batching，单条 89ms 不代表
  批量吞吐。要测需要并发压测脚本。
- **没测长文本**：`max_model_len` 是 8192，超长文档的截断行为未验证。
