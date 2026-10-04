import json
from pathlib import Path
import sqlite3
import time
import uuid


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS sessions(
                user_id INTEGER PRIMARY KEY, nonce TEXT NOT NULL, step TEXT NOT NULL,
                data TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bookings(
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, scheduled REAL,
                duration INTEGER NOT NULL DEFAULT 120, version INTEGER NOT NULL DEFAULT 1);
            CREATE UNIQUE INDEX IF NOT EXISTS active_booking ON bookings(user_id)
                WHERE status IN ('pending','confirmed');
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
                recipient INTEGER NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL,
                due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(booking_id,recipient,kind,version));
            CREATE TABLE IF NOT EXISTS sent_messages(
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                booking_id TEXT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
                PRIMARY KEY(chat_id,message_id));
        """)

    def session(self, user):
        row = self.db.execute("SELECT * FROM sessions WHERE user_id=?", (user,)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def save_session(self, user, step, data, nonce=None):
        old = self.session(user)
        nonce = nonce or (old["nonce"] if old else uuid.uuid4().hex[:8])
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                            (user, nonce, step, json.dumps(data, ensure_ascii=False), time.time()))
        return self.session(user)

    def drop_session(self, user):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user,))

    def booking(self, booking_id):
        row = self.db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def active(self, user):
        row = self.db.execute("SELECT id FROM bookings WHERE user_id=? AND status IN ('pending','confirmed')",
                              (user,)).fetchone()
        return self.booking(row["id"]) if row else None

    def list_bookings(self, user=None, limit=20):
        sql = "SELECT id FROM bookings WHERE status IN ('pending','confirmed')"
        args = []
        if user is not None:
            sql += " AND user_id=?"
            args.append(user)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(limit)
        return [self.booking(r["id"]) for r in self.db.execute(sql, args)]

    def submit(self, user, admins):
        session = self.session(user)
        if not session or session["step"] != "review" or not session["data"].get("consent"):
            raise ValueError("No consented form to submit")
        if not session["data"].get("adult") or any(not session["data"].get(key) for key in
                                                  ("name", "idea", "placement", "size", "availability")):
            raise ValueError("Form is incomplete")
        current = self.active(user)
        if current:
            return current
        booking_id = uuid.uuid4().hex[:10]
        with self.db:
            self.db.execute("INSERT INTO bookings(id,user_id,data,status,created) VALUES(?,?,?,?,?)",
                            (booking_id, user, json.dumps(session["data"], ensure_ascii=False), "pending", time.time()))
            for admin in admins:
                self._enqueue(booking_id, admin, "new", 1, time.time())
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user,))
        return self.booking(booking_id)

    def _enqueue(self, booking_id, recipient, kind, version, due):
        self.db.execute("INSERT OR IGNORE INTO outbox(booking_id,recipient,kind,version,due) VALUES(?,?,?,?,?)",
                        (booking_id, recipient, kind, version, due))

    def transition(self, booking_id, status, admins, scheduled=None, duration=120):
        old = self.booking(booking_id)
        if not old or old["status"] not in ("pending", "confirmed"):
            raise ValueError("Заявка уже закрыта или не найдена.")
        if status not in ("confirmed", "declined", "cancelled", "completed"):
            raise ValueError("Неизвестный статус.")
        if status == "confirmed":
            if scheduled is None or scheduled <= time.time() or not 30 <= duration <= 720:
                raise ValueError("Нужна будущая дата и длительность от 30 до 720 минут.")
            clash = self.db.execute(
                "SELECT id FROM bookings WHERE status='confirmed' AND id<>? "
                "AND scheduled<? AND scheduled+duration*60>?",
                (booking_id, scheduled+duration*60, scheduled)).fetchone()
            if clash:
                raise ValueError(f"Время пересекается с записью {clash['id']}.")
        if status == "completed" and old["status"] != "confirmed":
            raise ValueError("Сначала подтвердите сеанс.")
        version = old["version"] + 1
        with self.db:
            self.db.execute("UPDATE bookings SET status=?,scheduled=?,duration=?,version=? WHERE id=?",
                            (status, scheduled if status == "confirmed" else old["scheduled"], duration, version, booking_id))
            self.db.execute("DELETE FROM outbox WHERE booking_id=?", (booking_id,))
            self._enqueue(booking_id, old["user_id"], "status", version, time.time())
            for admin in admins:
                self._enqueue(booking_id, admin, "admin_status", version, time.time())
            if status == "confirmed":
                for hours in (24, 2):
                    due = scheduled - hours * 3600
                    if due > time.time():
                        self._enqueue(booking_id, old["user_id"], f"reminder{hours}", version, due)
        return self.booking(booking_id)

    def due(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM outbox WHERE due<=? ORDER BY id LIMIT 30", (time.time(),))]

    def retry(self, event_id, attempts):
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=?,due=? WHERE id=?",
                            (attempts, time.time()+min(3600, 30 * 2**min(attempts, 7)), event_id))

    def delivered(self, event_id):
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (event_id,))

    def track_message(self, user, chat, message, booking_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sent_messages VALUES(?,?,?,?)", (user, chat, message, booking_id))

    def erase(self, user):
        copies = [dict(r) for r in self.db.execute("SELECT * FROM sent_messages WHERE user_id=?", (user,))]
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user,))
            self.db.execute("DELETE FROM bookings WHERE user_id=?", (user,))
            self.db.execute("DELETE FROM sent_messages WHERE user_id=?", (user,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return copies

    def expire_drafts(self):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE updated<?", (time.time()-7*86400,))

    def expired_bookings(self, retention_days):
        deadline = time.time() - retention_days*86400
        return [dict(row) for row in self.db.execute("SELECT id,user_id FROM bookings WHERE COALESCE(scheduled,created)<?", (deadline,))]

    def erase_booking(self, booking_id):
        copies = [dict(r) for r in self.db.execute("SELECT * FROM sent_messages WHERE booking_id=?", (booking_id,))]
        with self.db:
            self.db.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return copies
