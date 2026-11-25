"""信封 schema 迁移。

**为什么需要它**：不认识的 `schema_version` 直接拒绝是对的（fail-closed，不猜），
但那意味着任何格式变更都让**存量数据全部作废**。企业环境里一次升级丢掉
所有会话与检查点，等于不能升级。

迁移表把"拒绝"变成"能升就升，升不了才拒"。
"""
from __future__ import annotations

import unittest

import core.store as store_mod
from core.store import (
    STORE_SCHEMA_VERSION, StoreSchemaMismatch, migrate_envelope,
    register_migration,
)


def envelope(version: int, **extra) -> dict:
    return {"schema_version": version, "ctx_id": "ctx_x", "saved_at": 1.0,
            "payload": "{}", **extra}


class MigrationBase(unittest.TestCase):
    def setUp(self):
        self._saved = dict(store_mod._MIGRATIONS)
        store_mod._MIGRATIONS.clear()

    def tearDown(self):
        store_mod._MIGRATIONS.clear()
        store_mod._MIGRATIONS.update(self._saved)


class TestNoMigrationRefuses(MigrationBase):
    def test_unknown_version_refused(self):
        """没有迁移函数 → 拒绝，不猜旧格式怎么读。"""
        with self.assertRaises(StoreSchemaMismatch) as caught:
            migrate_envelope(envelope(0), "test")
        self.assertIn("没有到", str(caught.exception))

    def test_current_version_passes_through(self):
        env = envelope(STORE_SCHEMA_VERSION)
        self.assertEqual(migrate_envelope(env, "t")["schema_version"],
                         STORE_SCHEMA_VERSION)

    def test_future_version_refused(self):
        """比本进程新的版本必须拒 —— 旧代码读新数据是猜。"""
        with self.assertRaises(StoreSchemaMismatch) as caught:
            migrate_envelope(envelope(STORE_SCHEMA_VERSION + 5), "test")
        self.assertIn("更新", str(caught.exception))

    def test_non_int_version_refused(self):
        for bad in ("1", None, True, 1.5):
            with self.subTest(v=bad):
                with self.assertRaises(StoreSchemaMismatch):
                    migrate_envelope({"schema_version": bad}, "t")


class TestMigrationWorks(MigrationBase):
    def test_single_step(self):
        register_migration(0, lambda env: {**env, "schema_version": 1,
                                           "added": True})
        env = migrate_envelope(envelope(0), "t")
        self.assertEqual(env["schema_version"], STORE_SCHEMA_VERSION)
        self.assertTrue(env["added"])

    def test_chain_is_stepwise(self):
        """多级迁移必须**逐级**走，不是一步跳到底。

        逐级的意义：加一级不用改已有逻辑。一步到位则每加一级都要重写。
        """
        seen: list[int] = []

        def m1(env):
            seen.append(1)
            return {**env, "schema_version": 1}

        def m2(env):
            seen.append(2)
            return {**env, "schema_version": 2}

        register_migration(0, m1)
        register_migration(1, m2)
        saved_version = store_mod.STORE_SCHEMA_VERSION
        try:
            store_mod.STORE_SCHEMA_VERSION = 2
            env = migrate_envelope(envelope(0), "t")
            self.assertEqual(env["schema_version"], 2)
            self.assertEqual(seen, [1, 2], "没有逐级迁移")
        finally:
            store_mod.STORE_SCHEMA_VERSION = saved_version

    def test_wrong_migration_detected(self):
        """迁移函数忘了升版本 → 明确报错，而不是死循环或静默通过。"""
        register_migration(0, lambda env: env)
        with self.assertRaises(StoreSchemaMismatch) as caught:
            migrate_envelope(envelope(0), "t")
        self.assertIn("迁移写错了", str(caught.exception))

    def test_original_not_mutated(self):
        """迁移不能就地改调用方传进来的 dict。"""
        register_migration(0, lambda env: {**env, "schema_version": 1})
        original = envelope(0)
        migrate_envelope(original, "t")
        self.assertEqual(original["schema_version"], 0)


class TestLoadPathUsesMigration(MigrationBase):
    """`_validate_envelope` 必须走迁移，而不是在版本不等时直接拒。"""

    def test_decode_envelope_migrates(self):
        """`_decode_envelope` 是读盘的第一道关，它必须走迁移。"""
        import json
        from core.store import _decode_envelope
        register_migration(0, lambda env: {**env, "schema_version": 1})
        raw = json.dumps(envelope(0))
        env = _decode_envelope(raw, "f.json")
        self.assertEqual(env["schema_version"], STORE_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
