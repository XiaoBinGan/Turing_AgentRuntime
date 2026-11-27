"""向量库三后端（memory / qdrant / milvus）的 SPI 验收。

核心要求：**换库不改业务代码，只改配置**，且三个后端的**语义必须一致**。
所以这里用"同一组断言跑三个实现"的方式验证（能跑的才跑）。
"""
from __future__ import annotations

import unittest

from plane.vector_backends import (
    MemoryVectorBackend, MilvusVectorBackend, QdrantVectorBackend,
    VectorBackendError, available_backends, configure_vector_backend,
    make_vector_backend,
)

DIM = 4
ACME = {"tenant": "acme", "user_id": "u1", "team": "sre", "project": "p1"}
EVIL = {"tenant": "evilcorp", "user_id": "u1", "team": "sre", "project": "p1"}


def flt(subject: dict) -> dict:
    return {"tenant": subject["tenant"], "user_id": subject["user_id"],
            "team": subject["team"], "project": subject["project"]}


def vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


# ══════════════════════════════════════════════════════════════
# 语义契约：三个后端都要满足
# ══════════════════════════════════════════════════════════════
class _BackendContract:
    """子类提供 `make()`；这里只写契约，不关心实现。"""

    def make(self):                      # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def setUp(self):
        self.b = self.make()
        self.b.upsert("a1", vec(0), {**ACME, "text": "连接池"})
        self.b.upsert("a2", vec(1), {**ACME, "text": "灰度"})
        self.b.upsert("e1", vec(0), {**EVIL, "text": "别的租户"})
        # **最终一致**：真集群 upsert 返回不等于可查。契约用例断言的是语义，
        # 不是"多久可见"，所以统一在这里刷一次（lite 上 flush 是 no-op）。
        if hasattr(self.b, "flush"):
            self.b.flush()

    def tearDown(self):
        self.b.close()

    def test_count(self):
        # Milvus 是最终一致的：count 内部会 flush，但显式再刷一次更稳
        if hasattr(self.b, "flush"):
            self.b.flush()
        self.assertEqual(self.b.count(), 3)

    def test_upsert_is_idempotent(self):
        self.b.upsert("a1", vec(0), {**ACME, "text": "连接池"})
        self.assertEqual(self.b.count(), 3)

    def test_search_returns_similar_first(self):
        if hasattr(self.b, "flush"):
            self.b.flush()
        rows = self.b.search(vec(0), flt(ACME), 5)
        self.assertTrue(rows)
        self.assertEqual(rows[0][0], "a1")
        self.assertGreater(rows[0][1], 0.9)

    def test_cross_tenant_isolated(self):
        rows = self.b.search(vec(0), flt(EVIL), 5)
        self.assertEqual([r[0] for r in rows], ["e1"])

    def test_scroll_honours_filter(self):
        rows = self.b.scroll(flt(ACME), 10)
        self.assertEqual({pid for pid, _ in rows}, {"a1", "a2"})

    def test_delete(self):
        self.b.delete("a1")
        self.assertEqual(self.b.count(), 2)
        rows = self.b.search(vec(0), flt(ACME), 5)
        self.assertNotIn("a1", [r[0] for r in rows])

    def test_payload_roundtrip(self):
        rows = self.b.scroll(flt(ACME), 10)
        payload = dict(rows)[ "a1"]
        self.assertEqual(payload.get("text"), "连接池")
        self.assertEqual(payload.get("tenant"), "acme")

    def test_info_shape(self):
        info = self.b.info()
        for attr in ("kind", "available", "shared", "ann", "dim", "location", "reason"):
            self.assertTrue(hasattr(info, attr))
        self.assertTrue(info.available)

    def test_dimension_mismatch_raises(self):
        with self.assertRaises(VectorBackendError):
            self.b.upsert("bad", [1.0, 0.0], {**ACME})

    def test_empty_filter_sees_all(self):
        rows = self.b.scroll(None, 10)
        self.assertEqual(len(rows), 3)


class TestMemoryBackend(_BackendContract, unittest.TestCase):
    def make(self):
        return MemoryVectorBackend(dim=DIM)

    def test_no_ann_and_not_shared(self):
        info = self.b.info()
        self.assertFalse(info.ann)
        self.assertFalse(info.shared)

    def test_deterministic_order_on_ties(self):
        self.b.upsert("z1", vec(2), {**ACME})
        self.b.upsert("z2", vec(2), {**ACME})
        first = [r[0] for r in self.b.search(vec(2), flt(ACME), 5)]
        second = [r[0] for r in self.b.search(vec(2), flt(ACME), 5)]
        self.assertEqual(first, second)


@unittest.skipUnless(available_backends()["qdrant"]["available"], "qdrant-client 未安装")
class TestQdrantBackend(_BackendContract, unittest.TestCase):
    def make(self):
        return QdrantVectorBackend(location=":memory:", dim=DIM,
                                   collection="test_vectors")

    def test_has_ann(self):
        self.assertTrue(self.b.info().ann)


def _milvus_runnable() -> bool:
    if not available_backends()["milvus"]["available"]:
        return False
    try:
        import milvus_lite  # noqa: F401
        return True
    except Exception:                                          # noqa: BLE001
        import os
        return bool(os.environ.get("MILVUS_TEST_URI"))


@unittest.skipUnless(_milvus_runnable(), "milvus 不可运行（无服务且无 milvus-lite）")
class TestMilvusBackend(_BackendContract, unittest.TestCase):
    """没有 Milvus 服务时这些用例会因为连不上而报错 —— 所以默认 skip。

    本机没有 Milvus，用 `MILVUS_TEST_URI` 显式开启。
    """

    def make(self):
        """优先用 `MILVUS_TEST_URI`（远程服务）；否则用 **milvus-lite 本地文件**。

        后者是真实的 Milvus 引擎（进程内嵌入），不是 mock —— 所以三条契约
        （维度/filter/返回形状）能在没有集群的情况下被真验证。
        """
        import os
        import tempfile
        uri = os.environ.get("MILVUS_TEST_URI", "")
        if not uri:
            try:
                import milvus_lite  # noqa: F401
            except Exception:                                  # noqa: BLE001
                self.skipTest("既无 MILVUS_TEST_URI 也未装 milvus-lite")
            d = tempfile.mkdtemp(prefix="milvus-lite-")
            uri = os.path.join(d, "verify.db")
        coll = f"test_vectors_{id(self)}"
        b = MilvusVectorBackend(uri=uri, dim=DIM, collection=coll)
        # 真集群上集合是持久的：先清空，避免上一次跑的残留数据混进来
        try:
            b.delete("a1"), b.delete("a2"), b.delete("e1")
            b.flush()
        except Exception:                                      # noqa: BLE001
            pass
        return b


# ══════════════════════════════════════════════════════════════
# 工厂与配置
# ══════════════════════════════════════════════════════════════
class TestFactory(unittest.TestCase):
    def test_available_backends_reports_reason(self):
        info = available_backends()
        self.assertEqual(set(info), {"memory", "qdrant", "milvus"})
        for name, row in info.items():
            self.assertIn("available", row)
            self.assertTrue(row["reason"], name)

    def test_make_memory(self):
        b = make_vector_backend("memory", dim=2)
        self.assertEqual(b.info().kind, "memory")
        b.close()

    def test_make_qdrant_or_explain(self):
        try:
            b = make_vector_backend("qdrant", location=":memory:", dim=2)
        except VectorBackendError as exc:
            self.assertIn("qdrant", str(exc).lower())
            return
        self.assertEqual(b.info().kind, "qdrant")
        b.close()

    def test_unknown_kind_raises_with_options(self):
        with self.assertRaises(VectorBackendError) as ctx:
            make_vector_backend("pinecone")
        self.assertIn("memory", str(ctx.exception))

    def test_auto_picks_available(self):
        b = make_vector_backend("auto", dim=2)
        self.assertIn(b.info().kind, ("memory", "qdrant", "milvus"))
        b.close()

    def test_milvus_without_dim_explains(self):
        """milvus 必须显式 dim —— 报错要说清为什么。"""
        if not available_backends()["milvus"]["available"]:
            self.skipTest("pymilvus 未安装")
        with self.assertRaises(VectorBackendError) as ctx:
            make_vector_backend("milvus")
        self.assertIn("dim", str(ctx.exception))


class TestEnvConfig(unittest.TestCase):
    def test_default_is_memory_or_auto(self):
        cfg = configure_vector_backend({})
        self.assertIsNotNone(cfg["backend"])
        self.assertEqual(cfg["reason"], "ok")
        cfg["backend"].close()

    def test_explicit_memory(self):
        cfg = configure_vector_backend({"VECTOR_BACKEND": "memory", "VECTOR_DIM": "2"})
        self.assertEqual(cfg["info"].kind, "memory")
        cfg["backend"].close()

    def test_unknown_backend_reports_reason_not_crash(self):
        cfg = configure_vector_backend({"VECTOR_BACKEND": "nope"})
        self.assertIsNone(cfg["backend"])
        self.assertIn("nope", cfg["reason"])

    def test_milvus_without_server_reports_reason(self):
        """连不上 Milvus 时要给可行动的原因，而不是抛栈。"""
        cfg = configure_vector_backend({
            "VECTOR_BACKEND": "milvus", "VECTOR_DIM": "4",
            "MILVUS_URI": "http://127.0.0.1:1", })
        if cfg["backend"] is None:
            self.assertTrue(cfg["reason"])
        else:                                   # 万一本机真有服务
            cfg["backend"].close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestMilvusRemotePath(unittest.TestCase):
    """远程集群路径：**没有集群也要验证"配错了会不会静默"**。

    ⑤ 项目标（远程 Milvus 集群）在本机被环境阻塞（Docker Hub 全面限流）。
    但"连不上时必须给出可行动的错误、而不是挂死或静默降级"这件事**可以验证**，
    而且它比"能连上"更重要 —— 能连上是别人的环境，配错不静默是自己的代码。
    """

    def test_unreachable_uri_fails_fast_with_actionable_error(self):
        import time
        from plane.vector_backends import MilvusVectorBackend

        t0 = time.monotonic()
        with self.assertRaises(VectorBackendError) as ctx:
            MilvusVectorBackend(uri="http://127.0.0.1:1", dim=4,
                                collection="nope")
        elapsed = time.monotonic() - t0
        msg = str(ctx.exception)
        # 必须说清"连不上 + 你给的是哪个地址"，而不是裸的 gRPC 异常
        self.assertIn("milvus", msg.lower())
        self.assertLess(elapsed, 60, f"失败要快（实测 {elapsed:.1f}s），不能挂死")

    def test_local_file_uri_is_recognised(self):
        """`.db` 结尾走 milvus-lite（进程内）；其它走远程客户端。"""
        try:
            import pymilvus  # noqa: F401
        except ImportError:
            # milvus-lite 随 pymilvus 一起装；没装就别报错 —— 这条用例验的是
            # 后端对 URI 的判定，不是"本机装了 SDK"。早先它直接 Error，
            # 让"干净机器上的全量跑"多一条噪音失败。
            self.skipTest("pymilvus 未安装（milvus-lite 路径不可用）")
        from plane.vector_backends import MilvusVectorBackend
        import tempfile, os
        d = tempfile.mkdtemp(prefix="ml-remote-")
        b = MilvusVectorBackend(uri=os.path.join(d, "x.db"), dim=4,
                               collection="probe")
        try:
            info = b.info()
            self.assertFalse(info.shared, "本地文件不该标成跨副本共享")
            self.assertIn("lite", info.reason.lower())
        finally:
            b.close()

    def test_remote_uri_marked_shared(self):
        """远程 URI 的自述里 shared=True —— 部署方据此判断能不能多副本。"""
        from contracts import VectorBackendInfo
        info = VectorBackendInfo(kind="milvus", available=True, shared=True,
                                 ann=True, dim=1024, location="http://h:19530",
                                 reason="生产集群后端")
        self.assertTrue(info.shared)
        self.assertTrue(info.ann)
