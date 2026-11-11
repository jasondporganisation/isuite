import unittest
from datetime import date, datetime, timedelta, timezone

from isuite.config import Config
from isuite.contacts import LeadDraft, add_or_merge
from isuite.db import connect
from isuite.roster import (
    create_invite, redeem_invite, get_agent, mark_left, rejoin, is_admin, has_team_extras, list_agents,
    purge_paused, delete_date, PAUSED_KEEP_DAYS,
)

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_invite_redeem_creates_team_agent(self):
        code = create_invite(self.conn, 111, NOW)
        self.assertEqual(len(code), 8)
        agent = redeem_invite(self.conn, code, 222, "Amy", TODAY)
        self.assertEqual(agent.telegram_id, 222)
        self.assertEqual(agent.name, "Amy")
        self.assertEqual(agent.status, "team")
        self.assertEqual(agent.joined_at, "2026-11-10")
        self.assertTrue(has_team_extras(agent))
        self.assertEqual(get_agent(self.conn, 222), agent)
        row = self.conn.execute("SELECT created_by, used_by FROM invites WHERE code=?", (code,)).fetchone()
        self.assertEqual(row["created_by"], 111)
        self.assertEqual(row["used_by"], agent.id)

    def test_invite_single_use(self):
        code = create_invite(self.conn, 111, NOW)
        self.assertIsNotNone(redeem_invite(self.conn, code, 222, "Amy", TODAY))
        self.assertIsNone(redeem_invite(self.conn, code, 333, "Bob", TODAY))
        self.assertIsNone(get_agent(self.conn, 333))

    def test_code_claimed_between_check_and_update_returns_none(self):
        code = create_invite(self.conn, 111, NOW)
        winner = redeem_invite(self.conn, create_invite(self.conn, 111, NOW), 222, "Amy", TODAY)
        real = self.conn

        class RacingConn:
            """Lets another redeemer claim the code right after the used_by check."""
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                result = real.execute(sql, *args)
                if sql.startswith("SELECT used_by"):
                    real.execute("UPDATE invites SET used_by=? WHERE code=?", (winner.id, code))
                    real.commit()
                return result

        self.assertIsNone(redeem_invite(RacingConn(), code, 333, "Bob", TODAY))
        self.assertIsNone(get_agent(self.conn, 333))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 1)
        used = self.conn.execute("SELECT used_by FROM invites WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(used, winner.id)

    def test_unknown_code_returns_none(self):
        self.assertIsNone(redeem_invite(self.conn, "ZZZZZZZZ", 222, "Amy", TODAY))
        self.assertIsNone(get_agent(self.conn, 222))

    def test_redeem_by_existing_agent_is_idempotent(self):
        first = redeem_invite(self.conn, create_invite(self.conn, 111, NOW), 222, "Amy", TODAY)
        code2 = create_invite(self.conn, 111, NOW)
        again = redeem_invite(self.conn, code2, 222, "Other", TODAY)
        self.assertEqual(again, first)
        count = self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_mark_left_pauses_at_once_and_starts_the_30_day_export_window(self):
        agent = redeem_invite(self.conn, create_invite(self.conn, 111, NOW), 222, "Amy", TODAY)
        left, changed = mark_left(self.conn, agent.id, TODAY)
        self.assertTrue(changed)
        self.assertEqual(left.status, "paused")
        self.assertEqual(left.left_at, "2026-11-10")
        self.assertEqual(left.paused_on, "2026-11-10")
        self.assertIsNone(left.trial_ends_on)
        self.assertEqual(delete_date(left), date(2026, 12, 11))  # last kept day is 10 Dec
        self.assertFalse(has_team_extras(left))
        self.assertEqual(get_agent(self.conn, 222), left)

    def test_mark_left_is_a_no_op_unless_team(self):
        agent = redeem_invite(self.conn, create_invite(self.conn, 111, NOW), 222, "Amy", TODAY)
        first, _ = mark_left(self.conn, agent.id, TODAY)
        again, changed = mark_left(self.conn, agent.id, TODAY + timedelta(days=30))
        self.assertFalse(changed)
        self.assertEqual(again, first)
        self.conn.execute("UPDATE agents SET status='paused', paused_on='2026-11-01' WHERE id=?",
                          (agent.id,))
        paused, changed = mark_left(self.conn, agent.id, TODAY)
        self.assertFalse(changed)
        self.assertEqual(paused.status, "paused")

    def test_rejoin_restores_team_and_clears_dates(self):
        agent = redeem_invite(self.conn, create_invite(self.conn, 111, NOW), 222, "Amy", TODAY)
        mark_left(self.conn, agent.id, TODAY)
        back, changed = rejoin(self.conn, agent.id)
        self.assertTrue(changed)
        self.assertEqual((back.status, back.left_at, back.trial_ends_on), ("team", None, None))
        again, changed = rejoin(self.conn, agent.id)
        self.assertFalse(changed)
        self.assertEqual(again, back)
        self.conn.execute(
            "UPDATE agents SET status='paused', left_at='2026-01-01', trial_ends_on='2026-04-01'")
        back, changed = rejoin(self.conn, agent.id)
        self.assertEqual((changed, back.status, back.left_at, back.trial_ends_on),
                         (True, "team", None, None))

    def test_is_admin(self):
        cfg = Config("t", "k", ":memory:", {111})
        self.assertTrue(is_admin(cfg, 111))
        self.assertFalse(is_admin(cfg, 222))

    def test_list_agents_filters_by_status(self):
        for tg, status in ((1, "team"), (2, "trial"), (3, "paused"), (4, "left")):
            self.conn.execute("INSERT INTO agents(telegram_id, name, status) VALUES (?,?,?)",
                              (tg, f"A{tg}", status))
        self.assertEqual([a.telegram_id for a in list_agents(self.conn, ("team", "trial"))], [1, 2])
        self.assertEqual([a.telegram_id for a in list_agents(self.conn, ("left",))], [4])
        self.assertEqual(list_agents(self.conn, ()), [])


class PurgePausedTests(unittest.TestCase):
    ENDED = date(2026, 10, 1)

    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def agent(self, tg, status, ends=None):
        cur = self.conn.execute(
            "INSERT INTO agents(telegram_id, name, status, trial_ends_on, paused_on)"
            " VALUES (?,?,?,?,?)",
            (tg, f"A{tg}", status, ends.isoformat() if ends else None,
             ends.isoformat() if ends and status == "paused" else None))
        self.conn.commit()
        aid = cur.lastrowid
        cid, _ = add_or_merge(self.conn, aid, LeadDraft("Tan", f"9{tg:07d}", "yes"),
                              "text", self.ENDED, NOW)
        self.conn.execute("INSERT INTO pending VALUES (?,?,?)", (aid, '{"leads": []}', NOW.isoformat()))
        self.conn.commit()
        return aid

    def rows(self, aid):
        q = lambda t: self.conn.execute(f"SELECT COUNT(*) FROM {t} WHERE agent_id=?", (aid,)).fetchone()[0]
        return q("contacts"), q("activity"), q("pending")

    def test_constant_is_30_days(self):
        self.assertEqual(PAUSED_KEEP_DAYS, 30)

    def test_exactly_30_days_after_trial_end_keeps_everything(self):
        aid = self.agent(1, "paused", self.ENDED)
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=30)), 0)
        self.assertEqual(self.rows(aid), (1, 1, 1))

    def test_31_days_after_deletes_data_but_keeps_the_agent_row(self):
        aid = self.agent(1, "paused", self.ENDED)
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=31)), 1)
        self.assertEqual(self.rows(aid), (0, 0, 0))
        self.assertEqual(get_agent(self.conn, 1).status, "paused")
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=32)), 0)

    def test_roadshow_activity_without_a_contact_goes_too(self):
        aid = self.agent(1, "paused", self.ENDED)
        self.conn.execute("INSERT INTO activity (agent_id, contact_id, kind, at) VALUES (?,NULL,'roadshow','x')", (aid,))
        self.conn.commit()
        purge_paused(self.conn, self.ENDED + timedelta(days=40))
        self.assertEqual(self.rows(aid), (0, 0, 0))

    def test_team_and_trial_agents_with_old_dates_are_untouched(self):
        team = self.agent(1, "team", self.ENDED)
        trial = self.agent(2, "trial", self.ENDED)
        left = self.agent(3, "left", self.ENDED)
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=400)), 0)
        for aid in (team, trial, left):
            self.assertEqual(self.rows(aid), (1, 1, 1))

    def test_paused_without_a_paused_on_date_is_untouched(self):
        aid = self.agent(1, "paused", None)
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=400)), 0)
        self.assertEqual(self.rows(aid), (1, 1, 1))

    def test_other_agents_data_is_untouched(self):
        gone = self.agent(1, "paused", self.ENDED)
        other = self.agent(2, "paused", self.ENDED + timedelta(days=20))
        mine = self.agent(3, "team")
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=31)), 1)
        self.assertEqual(self.rows(gone), (0, 0, 0))
        self.assertEqual(self.rows(other), (1, 1, 1))
        self.assertEqual(self.rows(mine), (1, 1, 1))

    def test_rejoin_clears_paused_on(self):
        aid = self.agent(1, "paused", self.ENDED)
        rejoin(self.conn, aid)
        self.assertIsNone(self.conn.execute("SELECT paused_on FROM agents WHERE id=?",
                                            (aid,)).fetchone()[0])

    def test_rejoined_agent_keeps_everything(self):
        aid = self.agent(1, "paused", self.ENDED)
        rejoin(self.conn, aid)
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=90)), 0)
        self.assertEqual(self.rows(aid), (1, 1, 1))

    def test_unreadable_paused_on_is_left_alone(self):
        aid = self.agent(1, "paused", self.ENDED)
        self.conn.execute("UPDATE agents SET paused_on='garbage'")
        self.conn.commit()
        self.assertEqual(purge_paused(self.conn, self.ENDED + timedelta(days=90)), 0)
        self.assertEqual(self.rows(aid), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
