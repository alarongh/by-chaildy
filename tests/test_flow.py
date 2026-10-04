from dataclasses import replace
import hashlib
import sqlite3
import tempfile
import time
import unittest

from bot.config import Settings
from bot.content import CONSENT_VERSION, summary
from bot.flow import Flow
from bot.store import Store

CONFIG = Settings(admins=(99,), operator="Тестовый оператор", address="Тестовый адрес",
                  privacy_contact="test@example.invalid", privacy_ready=True)


def click(flow, store, user, action):
    session = store.session(user)
    return flow.action(user, f"f:{session['nonce']}:{session['step']}:{action}", "example_user")


def complete(flow, store, user=1, send=True):
    flow.begin(user)
    click(flow, store, user, "accept")
    click(flow, store, user, "adult")
    for answer in ("Аня", "Веточка, чёрная", "Левое предплечье", "8 см", "Выходные"):
        flow.answer(user, answer)
    click(flow, store, user, "skip")
    click(flow, store, user, "next")
    if send:
        click(flow, store, user, "submit")
        return store.active(user)
    return store.session(user)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = Flow(self.store, CONFIG)

    def tearDown(self):
        self.store.db.close()

    def test_no_form_collection_before_operator_setup(self):
        flow = Flow(self.store, Settings())
        self.assertIn("скоро", flow.begin(1).text)
        self.assertIsNone(self.store.session(1))
        for cfg in (replace(CONFIG, admins=()), replace(CONFIG, operator=""), replace(CONFIG, privacy_contact="")):
            self.assertFalse(cfg.can_collect)

    def test_consultation_form_without_address_and_dual_admin_delivery(self):
        cfg = Settings(admins=(1888165622, 1092851573), operator="by:Chaildy",
                       privacy_contact="@spider_013", master="spider_013", privacy_ready=True)
        self.assertTrue(cfg.can_collect_for(1))
        flow = Flow(self.store, cfg)
        consent = flow.begin(1).text
        self.assertIn("консультацию", consent)
        self.assertIn("@spider_013", consent)
        self.assertNotIn("[адрес", consent)
        booking = complete(flow, self.store)
        self.assertFalse(booking["data"]["test"])
        self.assertEqual({e['recipient'] for e in self.store.due()}, {1888165622, 1092851573})

    def test_consent_required_and_input_not_saved_before_accept(self):
        self.flow.begin(1)
        self.flow.answer(1, "Имя, без согласия")
        self.assertEqual(self.store.session(1)["data"], {})
        with self.assertRaises(ValueError):
            self.store.submit(1, CONFIG.admins)

    def test_test_mode_allows_only_admin_and_marks_booking(self):
        cfg = Settings(admins=(99,), test_mode=True)
        flow = Flow(self.store, cfg)
        self.assertFalse(cfg.can_collect)
        self.assertIn("скоро", flow.begin(1).text)
        self.assertIsNone(self.store.session(1))
        self.assertIn("Тест анкеты", flow.begin(99).text)
        booking = complete(flow, self.store, user=99)
        self.assertTrue(booking["data"]["test"])
        self.assertIn("тестовый текст", booking["data"]["consent"]["text"])

    def test_test_mode_blocks_existing_public_drafts(self):
        complete(self.flow, self.store, user=1, send=False)
        cfg = replace(CONFIG, test_mode=True)
        flow = Flow(self.store, cfg)
        self.assertIn("недоступны", click(flow, self.store, 1, "submit").text)
        self.assertIn("недоступны", flow.answer(1, "Ответ").text)
        self.assertIsNone(self.store.active(1))

    def test_refused_consent_and_underage_remove_draft(self):
        self.flow.begin(1)
        click(self.flow, self.store, 1, "refuse")
        self.assertIsNone(self.store.session(1))
        self.flow.begin(1)
        click(self.flow, self.store, 1, "accept")
        click(self.flow, self.store, 1, "minor")
        self.assertIsNone(self.store.session(1))

    def test_minimal_form_submission_contains_consent_evidence(self):
        booking = complete(self.flow, self.store)
        self.assertEqual(booking["status"], "pending")
        self.assertIsNone(booking["scheduled"])
        self.assertIsNone(self.store.session(1))
        consent = booking["data"]["consent"]
        self.assertEqual(consent["version"], CONSENT_VERSION)
        self.assertEqual(consent["sha256"], hashlib.sha256(consent["text"].encode()).hexdigest())
        self.assertGreater(consent["accepted_at"], 0)
        self.assertEqual(len(self.store.due()), 1)

    def test_no_duplicate_active_booking(self):
        complete(self.flow, self.store)
        self.flow.begin(1)
        self.assertIsNone(self.store.session(1))
        self.assertEqual(len(self.store.list_bookings()), 1)

    def test_stale_callback_cannot_skip_consent(self):
        self.flow.begin(1)
        reply = self.flow.action(1, "f:incorrect:review:submit")
        self.assertIn("устарела", reply.text)
        self.assertEqual(self.store.session(1)["step"], "consent")

    def test_edit_returns_to_summary_and_html_is_escaped(self):
        complete(self.flow, self.store, send=False)
        click(self.flow, self.store, 1, "idea")
        reply = self.flow.answer(1, "<script>alert('x')</script>")
        self.assertEqual(self.store.session(1)["step"], "review")
        self.assertIn("&lt;script&gt;", reply.text)
        self.assertNotIn("<script>", reply.text)

    def test_name_length_is_limited_without_advancing(self):
        self.flow.begin(1)
        click(self.flow, self.store, 1, "accept")
        click(self.flow, self.store, 1, "adult")
        self.flow.answer(1, "a"*61)
        self.assertEqual(self.store.session(1)["step"], "name")

    def test_photo_cap_and_references_can_be_replaced(self):
        complete(self.flow, self.store, send=False)
        click(self.flow, self.store, 1, "refs")
        for index in range(5):
            self.flow.answer(1, photo=f"file{index}")
        self.assertEqual(len(self.store.session(1)["data"]["refs"]), 3)
        click(self.flow, self.store, 1, "next")
        click(self.flow, self.store, 1, "refs")
        self.assertEqual(self.store.session(1)["data"]["refs"], [])

    def test_draft_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder+"/bookings.sqlite3"
            store = Store(path)
            flow = Flow(store, CONFIG)
            flow.begin(1)
            click(flow, store, 1, "accept")
            store.db.close()
            restarted = Store(path)
            self.assertEqual(restarted.session(1)["step"], "age")
            self.assertIn("18", Flow(restarted, CONFIG).begin(1).text)
            restarted.db.close()

    def test_incomplete_or_underage_review_cannot_submit(self):
        session = complete(self.flow, self.store, send=False)
        session["data"].pop("adult")
        self.store.save_session(1, "review", session["data"])
        with self.assertRaises(ValueError):
            self.store.submit(1, CONFIG.admins)

    def test_confirm_reschedule_and_reminder_versions(self):
        booking = complete(self.flow, self.store)
        stamp = time.time()+3*86400
        self.store.transition(booking["id"], "confirmed", CONFIG.admins, stamp, 120)
        rows = [dict(r) for r in self.store.db.execute("SELECT * FROM outbox")]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r["kind"].startswith("reminder") for r in rows), 2)
        newer = self.store.transition(booking["id"], "confirmed", CONFIG.admins, stamp+86400, 180)
        self.assertEqual(newer["version"], 3)
        self.assertTrue(all(r["version"] == 3 for r in self.store.db.execute("SELECT version FROM outbox")))

    def test_interval_conflict_not_just_same_start(self):
        a = complete(self.flow, self.store, 1)
        b = complete(self.flow, self.store, 2)
        start = time.time()+3*86400
        self.store.transition(a["id"], "confirmed", CONFIG.admins, start, 180)
        with self.assertRaises(ValueError):
            self.store.transition(b["id"], "confirmed", CONFIG.admins, start+3600, 120)
        self.store.transition(b["id"], "confirmed", CONFIG.admins, start+3*3600, 120)

    def test_past_date_and_invalid_duration_rejected(self):
        a = complete(self.flow, self.store)
        for stamp, duration in [(time.time()-10, 120), (time.time()+86400, 10), (time.time()+86400, 900)]:
            with self.assertRaises(ValueError):
                self.store.transition(a["id"], "confirmed", CONFIG.admins, stamp, duration)

    def test_cancellation_clears_reminders_and_allows_new_booking(self):
        a = complete(self.flow, self.store)
        self.store.transition(a["id"], "confirmed", CONFIG.admins, time.time()+3*86400)
        self.store.transition(a["id"], "cancelled", CONFIG.admins)
        self.assertIsNone(self.store.active(1))
        self.assertFalse(any(r["kind"].startswith("reminder") for r in self.store.db.execute("SELECT kind FROM outbox")))
        self.assertNotEqual(complete(self.flow, self.store)["id"], a["id"])

    def test_delete_cascades_outbox_and_copy_records(self):
        a = complete(self.flow, self.store)
        self.store.track_message(1, 99, 111, a["id"])
        copies = self.store.erase(1)
        self.assertEqual(copies[0]["message_id"], 111)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0], 0)

    def test_retention_removes_old_booking_without_erasing_new_draft(self):
        a = complete(self.flow, self.store)
        self.store.transition(a["id"], "cancelled", CONFIG.admins)
        with self.store.db:
            self.store.db.execute("UPDATE bookings SET created=? WHERE id=?", (time.time()-91*86400, a["id"]))
        self.flow.begin(1)
        expired = self.store.expired_bookings(90)
        self.assertEqual([e["id"] for e in expired], [a["id"]])
        self.store.erase_booking(a["id"])
        self.assertIsNotNone(self.store.session(1))

    def test_draft_retention(self):
        self.flow.begin(1)
        with self.store.db:
            self.store.db.execute("UPDATE sessions SET updated=?", (time.time()-8*86400,))
        self.store.expire_drafts()
        self.assertIsNone(self.store.session(1))

    def test_disabled_collection_blocks_old_session(self):
        complete(self.flow, self.store, send=False)
        disabled = Flow(self.store, replace(CONFIG, privacy_ready=False))
        click(disabled, self.store, 1, "submit")
        self.assertIsNone(self.store.active(1))

    def test_maximum_form_fits_telegram_message(self):
        data = {key:"x"*(60 if key=="name" else 900 if key=="idea" else 240)
                for key in ("name","idea","placement","size","availability","budget")}
        self.assertLess(len(summary(data))+500, 4096)


if __name__ == "__main__":
    unittest.main()
