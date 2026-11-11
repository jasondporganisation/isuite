import sqlite3
import unittest

from isuite.config import load_config
from isuite.db import connect


class DbTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def _agent(self, telegram_id):
        cur = self.conn.execute(
            "INSERT INTO agents(telegram_id, name, status) VALUES (?, 'A', 'team')",
            (telegram_id,),
        )
        return cur.lastrowid

    def _contact(self, agent_id, phone):
        self.conn.execute(
            "INSERT INTO contacts(agent_id, name, phone, captured_on) "
            "VALUES (?, 'C', ?, '2026-11-10')",
            (agent_id, phone),
        )

    def test_connect_creates_all_tables(self):
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"agents", "invites", "contacts", "activity", "pending", "jobs"} <= names)

    def test_old_database_without_paused_on_gets_the_column(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE "
                        "NOT NULL, name TEXT, status TEXT NOT NULL, joined_at TEXT, left_at TEXT,"
                        " trial_ends_on TEXT, roadshow_on TEXT)")
            old.execute("INSERT INTO agents(telegram_id, status) VALUES (5, 'paused')")
            old.commit()
            old.close()
            conn = connect(path)
            try:
                row = conn.execute("SELECT paused_on FROM agents").fetchone()
                self.assertIsNone(row["paused_on"])
                connect(path).close()  # a second start does not try to add it again
            finally:
                conn.close()

    def test_foreign_keys_on(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_contact_phone_unique_per_agent(self):
        a1, a2 = self._agent(1), self._agent(2)
        self._contact(a1, "6591234567")
        with self.assertRaises(sqlite3.IntegrityError):
            self._contact(a1, "6591234567")
        self._contact(a2, "6591234567")

    def test_load_config_parses_admins(self):
        self.assertEqual(load_config({"ISUITE_ADMINS": "1, 22"}).admin_ids, {1, 22})
        self.assertEqual(load_config({}).admin_ids, set())

    def test_bad_admin_id_names_the_variable_not_the_value(self):
        with self.assertRaises(ValueError) as ctx:
            load_config({"ISUITE_ADMINS": "1, secret-abc"})
        self.assertIn("ISUITE_ADMINS", str(ctx.exception))
        self.assertNotIn("secret-abc", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
