import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Message, Update, User

from bot.main import USER_LOCKS, build_router, deliver_outbox
from bot.store import Store
from bot.flow import Flow
from test_flow import CONFIG, click, complete


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.counter = 100
        self.fail = False
        self.paused = None
        self.release = None

    async def close(self):
        pass

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        api = method.__api_method__
        if api == "getMe":
            return User(id=123456, is_bot=True, first_name="Test", username="chaildy_test_bot")
        if api == "answerCallbackQuery" or api == "deleteMessage":
            return True
        if self.fail:
            raise TelegramNetworkError(method=method, message="simulated offline")
        if self.paused:
            self.paused.set()
            await self.release.wait()
        self.counter += 1
        return Message(message_id=self.counter, date=datetime.now(timezone.utc),
                       chat={"id":int(method.chat_id),"type":"private"}, text=getattr(method,"text",None))


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        USER_LOCKS.clear()
        self.store = Store(":memory:")
        self.flow = Flow(self.store, CONFIG)
        self.session = FakeSession()
        # An intentionally fake token, no real network traffic.
        self.bot = Bot("123456:"+"a"*35, session=self.session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.store, CONFIG))
        self.counter = 1

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.db.close()
        USER_LOCKS.clear()

    async def message(self, text, user=1, chat_type="private"):
        self.counter += 1
        content = {"message_id":self.counter,"date":datetime.now(timezone.utc),
                   "chat":{"id":user,"type":chat_type},
                   "from":{"id":user,"is_bot":False,"first_name":"Example"},"text":text}
        if text.startswith("/"):
            content["entities"] = [{"type":"bot_command","offset":0,"length":len(text.split()[0])}]
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id":self.counter,"message":content}))

    async def callback(self, data, user=1):
        self.counter += 1
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id":self.counter,"callback_query":{
            "id":str(self.counter),"from":{"id":user,"is_bot":False,"first_name":"Example"},
            "chat_instance":"test","data":data,"message":{"message_id":self.counter,"date":datetime.now(timezone.utc),
            "chat":{"id":user,"type":"private"},"from":{"id":123456,"is_bot":True,"first_name":"Bot"},"text":"menu"}}}))

    def texts(self):
        return [call.text for call in self.session.calls if call.__api_method__ == "sendMessage"]

    async def test_router_full_booking_flow(self):
        await self.message("/book")
        for action in ("accept","adult"):
            s = self.store.session(1)
            await self.callback(f"f:{s['nonce']}:{s['step']}:{action}")
        for answer in ("Аня","Ветка","Предплечье","8 см","В выходные"):
            await self.message(answer)
        for action in ("skip","next","submit"):
            s = self.store.session(1)
            await self.callback(f"f:{s['nonce']}:{s['step']}:{action}")
        self.assertEqual(self.store.active(1)["status"], "pending")
        self.assertTrue(any("сохранена" in t for t in self.texts()))

    async def test_unauthorized_admin_cannot_read_or_confirm(self):
        booking = complete(self.flow, self.store)
        await self.message("/view "+booking["id"], 2)
        await self.message("/confirm "+booking["id"]+" 2026-12-12 15:00 120", 2)
        self.assertEqual(self.store.active(1)["status"], "pending")
        self.assertFalse(any("Аня" in t for t in self.texts()))
        self.assertTrue(all("доступна мастеру" in t for t in self.texts()))

    async def test_private_data_commands_ignored_in_groups(self):
        booking = complete(self.flow, self.store)
        await self.message("/view "+booking["id"], 99, "group")
        await self.message("/my", 1, "group")
        self.assertEqual(self.texts(), [])

    async def test_another_client_cannot_cancel_booking(self):
        booking = complete(self.flow, self.store)
        await self.callback("cancel_yes:"+booking["id"], 2)
        self.assertEqual(self.store.active(1)["status"], "pending")

    async def test_admin_confirm_and_client_cancellation(self):
        booking = complete(self.flow, self.store)
        dt = datetime.now()+timedelta(days=7)
        await self.message(f"/confirm {booking['id']} {dt:%Y-%m-%d} 15:00 120", 99)
        self.assertEqual(self.store.active(1)["status"], "confirmed")
        await self.callback("cancel_yes:"+booking["id"])
        self.assertEqual(self.store.booking(booking["id"])["status"], "cancelled")

    async def test_master_notification_queue_and_photo_view(self):
        complete(self.flow, self.store, send=False)
        click(self.flow, self.store, 1, "refs")
        self.flow.answer(1, photo="fake-photo-file-id")
        click(self.flow, self.store, 1, "next")
        click(self.flow, self.store, 1, "submit")
        await deliver_outbox(self.bot, self.store, CONFIG)
        self.assertEqual(len(self.store.due()), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0], 1)
        booking = self.store.active(1)
        await self.message("/view "+booking["id"], 99)
        photos = [call for call in self.session.calls if call.__api_method__=="sendPhoto"]
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].photo, "fake-photo-file-id")

    async def test_network_failure_is_durable_and_retries(self):
        complete(self.flow, self.store)
        self.session.fail = True
        await deliver_outbox(self.bot, self.store, CONFIG)
        event = self.store.db.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(event["attempts"], 1)
        self.assertEqual(len(self.store.due()), 0)
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET due=0")
        self.session.fail = False
        await deliver_outbox(self.bot, self.store, CONFIG)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    async def test_data_deletion_removes_database_and_telegram_copies(self):
        complete(self.flow, self.store)
        await deliver_outbox(self.bot, self.store, CONFIG)
        await self.message("/delete")
        self.assertIsNotNone(self.store.active(1))
        await self.callback("erase:yes")
        self.assertIsNone(self.store.active(1))
        deletions = [call for call in self.session.calls if call.__api_method__ == "deleteMessage"]
        self.assertEqual(len(deletions), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0], 0)

    async def test_deletion_cannot_race_notification_delivery(self):
        complete(self.flow, self.store)
        self.session.paused = asyncio.Event()
        self.session.release = asyncio.Event()
        worker = asyncio.create_task(deliver_outbox(self.bot, self.store, CONFIG))
        await self.session.paused.wait()
        deleting = asyncio.create_task(self.callback("erase:yes"))
        await asyncio.sleep(0)
        self.assertFalse(deleting.done())
        self.session.paused = None
        self.session.release.set()
        await worker
        await deleting
        self.assertIsNone(self.store.active(1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0], 0)
        self.assertTrue(any(call.__api_method__=="deleteMessage" for call in self.session.calls))


if __name__ == "__main__":
    unittest.main()
