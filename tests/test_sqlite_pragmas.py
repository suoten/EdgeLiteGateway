"""SQLite PRAGMA 辅助函数测试 - WAL/busy_timeout/synchronous 配置

覆盖 storage/sqlite_pragmas.py：
- apply_standard_pragmas: 5 项 PRAGMA 配置
  （foreign_keys/journal_mode/busy_timeout/synchronous/ignore_check_constraints）
- apply_standard_pragmas: 幂等性
- check_and_convert_to_wal: 不存在的文件/已 WAL/非 WAL 转换
"""

from __future__ import annotations

import sqlite3

from edgelite.storage.sqlite_pragmas import apply_standard_pragmas, check_and_convert_to_wal


class TestApplyStandardPragmas:
    def test_sets_journal_mode_wal(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        try:
            apply_standard_pragmas(conn)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_sets_busy_timeout(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        try:
            apply_standard_pragmas(conn)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout")
            timeout = cur.fetchone()[0]
            assert timeout == 5000
        finally:
            conn.close()

    def test_sets_synchronous_normal(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        try:
            apply_standard_pragmas(conn)
            cur = conn.cursor()
            cur.execute("PRAGMA synchronous")
            sync = cur.fetchone()[0]
            assert sync == 1  # NORMAL = 1
        finally:
            conn.close()

    def test_sets_foreign_keys_on(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        try:
            apply_standard_pragmas(conn)
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys")
            fk = cur.fetchone()[0]
            assert fk == 1  # ON = 1
        finally:
            conn.close()

    def test_idempotent(self, tmp_path):
        """重复调用不应产生副作用"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        try:
            apply_standard_pragmas(conn)
            apply_standard_pragmas(conn)  # 再次调用
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode")
            assert cur.fetchone()[0].lower() == "wal"
        finally:
            conn.close()


class TestCheckAndConvertToWal:
    def test_nonexistent_file_noop(self, tmp_path):
        """文件不存在时应直接返回，不抛异常"""
        db_path = str(tmp_path / "nonexistent.db")
        # 不应抛异常
        check_and_convert_to_wal(db_path)

    def test_already_wal_no_change(self, tmp_path):
        """已经是 WAL 模式时无需转换"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()

        check_and_convert_to_wal(db_path)

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        conn.close()
        assert mode.lower() == "wal"

    def test_converts_delete_to_wal(self, tmp_path):
        """DELETE 模式应被转换为 WAL"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        conn.close()

        check_and_convert_to_wal(db_path)

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        conn.close()
        assert mode.lower() == "wal"
