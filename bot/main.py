import asyncio
from collections import defaultdict
from datetime import datetime
from html import escape
import logging
import re
import time
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .content import FAQ, PREPARATION, consent_text, summary
from .flow import Flow
from .store import Store

LOG = logging.getLogger("chaildy")
USER_LOCKS = defaultdict(asyncio.Lock)
LABELS = {"pending": "На рассмотрении", "confirmed": "Сеанс подтверждён", "declined": "Мастер не принял заявку",
          "cancelled": "Заявка отменена", "completed": "Сеанс завершён"}
ADMIN_HELP = (
    "<b>Управление заявками</b>\n/bookings - последние 20 активных заявок\n"
    "/view ID - анкета и фото\n"
    "/confirm ID ГГГГ-ММ-ДД ЧЧ:ММ МИНУТЫ - подтвердить / перенести сеанс\n"
    "Пример: /confirm a1b2c3d4e5 2026-10-20 15:00 120\n"
    "/decline ID - отклонить\n/cancel_booking ID - отменить\n/done ID - завершить\n\n"
    "Перед /confirm согласуй дату, стоимость и условия с клиентом. "
    "Длительность нужна для проверки пересечений."
)


def keyboard(buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action)] for label, action in buttons])


def booking_text(booking, cfg, admin=False):
    text = f"<b>Заявка #{booking['id']}</b>\n{LABELS[booking['status']]}\n"
    if booking["scheduled"]:
        dt = datetime.fromtimestamp(booking["scheduled"], ZoneInfo(cfg.timezone))
        text += f"\nДата: {dt:%d.%m.%Y, %H:%M} ({escape(cfg.timezone)})\n"
        text += f"Продолжительность: {booking['duration']} мин.\n"
    if admin:
        user = booking["user_id"]
        text += f'\n<a href="tg://user?id={user}">Написать клиенту</a> · ID {user}\n'
        username = booking["data"].get("username")
        if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            text += f"@{escape(username)}\n"
    text += "\n" + summary(booking["data"])
    if booking["status"] == "pending":
        text += "\n\nДата пока не подтверждена."
    return text


def build_router(store, cfg):
    router = Router()
    flow = Flow(store, cfg)
    # Serialize each private chat so album photos and repeated buttons cannot race.
    locks = USER_LOCKS

    async def send(message, reply):
        await message.answer(reply.text, reply_markup=keyboard(reply.buttons))

    def admin(message):
        return bool(message.from_user and message.from_user.id in cfg.admins and message.chat.type == "private")

    @router.message(Command("id"))
    async def identity(message: Message):
        if message.chat.type == "private":
            await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(CommandStart())
    async def start(message: Message):
        if message.chat.type != "private":
            return
        await message.answer("<b>by:Chaildy</b>\n\nДавай начнём с твоей идеи. Здесь можно оставить короткую заявку на тату и получить подтверждение от мастера.\n\n/book - записаться\n/my - статус заявки\n/faq - частые вопросы\n/prepare - перед сеансом\n/contact - связь с мастером\n/privacy - обработка данных\n/delete - удалить данные", reply_markup=keyboard([("Оставить заявку", "book"), ("Моя заявка", "my"), ("Вопросы и ответы", "faq")]))
        if admin(message):
            await message.answer(ADMIN_HELP)

    @router.message(Command("book"))
    async def book(message: Message):
        if message.chat.type != "private":
            return
        async with locks[message.from_user.id]:
            await send(message, flow.begin(message.from_user.id))

    @router.message(Command("privacy"))
    async def privacy(message: Message):
        if message.chat.type == "private":
            text = consent_text(cfg)
            if not cfg.can_collect:
                text = "Документ в подготовке. Сбор анкет выключен.\n\n" + text
            await message.answer(escape(text))

    @router.message(Command("faq", "prepare", "contact", "help"))
    async def info(message: Message):
        if message.chat.type != "private":
            return
        command = message.text.split()[0].split("@")[0]
        if command == "/faq":
            text = FAQ
        elif command == "/prepare":
            text = PREPARATION
        elif command == "/contact":
            text = f"Мастер: @{cfg.master}" if re.fullmatch(r"[A-Za-z0-9_]{5,32}", cfg.master) else "Контакт мастера ещё не настроен."
            if cfg.city:
                text += f"\nГород: {cfg.city}"
        else:
            text = "Начать: /book · Заявка: /my · Удаление: /delete · Вопросы: /faq · Контакт: /contact"
        await message.answer(escape(text))

    async def show_my(message):
        booking = store.active(message.from_user.id)
        if not booking:
            await message.answer("Активной заявки пока нет. Начать: /book.")
            return
        await message.answer(booking_text(booking, cfg), reply_markup=keyboard([("Отменить заявку", f"cancel:{booking['id']}")]))

    @router.message(Command("my"))
    async def my(message: Message):
        if message.chat.type == "private":
            await show_my(message)

    @router.message(Command("delete"))
    async def deletion(message: Message):
        if message.chat.type == "private":
            await message.answer("Удалить все данные из базы бота и отозвать согласие? Активная запись также будет отменена. Бот попробует удалить свои сообщения с анкетой у мастера; старые сообщения и ручные копии могут потребовать отдельного обращения к оператору.", reply_markup=keyboard([("Да, удалить и отменить запись", "erase:yes"), ("Оставить данные", "erase:no")]))

    @router.message(Command("cancel"))
    async def cancel_draft(message: Message):
        if message.chat.type == "private":
            async with locks[message.from_user.id]:
                store.drop_session(message.from_user.id)
            await message.answer("Черновик удалён. Отмена отправленной заявки: /my.")

    @router.message(Command("admin", "bookings", "view", "confirm", "decline", "cancel_booking", "done"))
    async def manage(message: Message):
        if not admin(message):
            if message.chat.type == "private":
                await message.answer("Эта команда доступна мастеру.")
            return
        parts = message.text.split()
        command = parts[0].split("@")[0]
        if command == "/admin":
            await message.answer(ADMIN_HELP)
            return
        if command == "/bookings":
            bookings = store.list_bookings()
            await message.answer("\n".join(f"<code>{b['id']}</code> · {LABELS[b['status']]} · {escape(b['data']['name'])}" for b in bookings) or "Активных заявок нет.")
            return
        if len(parts) < 2:
            await message.answer(ADMIN_HELP)
            return
        booking = store.booking(parts[1])
        if not booking:
            await message.answer("Заявка не найдена.")
            return
        if command == "/view":
            async with locks[booking["user_id"]]:
                booking = store.booking(booking["id"])
                if not booking:
                    await message.answer("Заявка уже удалена.")
                    return
                result = await message.answer(booking_text(booking, cfg, True))
                store.track_message(booking["user_id"], result.chat.id, result.message_id, booking["id"])
                for photo in booking["data"].get("refs", []):
                    result = await message.answer_photo(photo)
                    store.track_message(booking["user_id"], result.chat.id, result.message_id, booking["id"])
            return
        try:
            if command == "/confirm":
                if len(parts) != 5:
                    raise ValueError("Формат: /confirm ID ГГГГ-ММ-ДД ЧЧ:ММ МИНУТЫ")
                dt = datetime.strptime(parts[2]+" "+parts[3], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(cfg.timezone))
                async with locks[booking["user_id"]]:
                    store.transition(booking["id"], "confirmed", cfg.admins, dt.timestamp(), int(parts[4]))
            else:
                status = {"/decline": "declined", "/cancel_booking": "cancelled", "/done": "completed"}[command]
                async with locks[booking["user_id"]]:
                    store.transition(booking["id"], status, cfg.admins)
        except ValueError as error:
            await message.answer(escape(str(error)))
            return
        await message.answer("Изменение сохранено. Уведомления поставлены в очередь.")

    @router.callback_query()
    async def callback(query: CallbackQuery):
        if not query.message or query.message.chat.type != "private":
            await query.answer()
            return
        user = query.from_user.id
        await query.answer()
        async with locks[user]:
            data = query.data or ""
            if data == "book":
                await send(query.message, flow.begin(user))
            elif data == "faq":
                await query.message.answer(escape(FAQ))
            elif data == "my":
                booking = store.active(user)
                await query.message.answer(booking_text(booking, cfg) if booking else "Активной заявки нет. /book", reply_markup=keyboard([("Отменить заявку", f"cancel:{booking['id']}")]) if booking else None)
            elif data == "erase:no":
                await query.message.answer("Данные оставлены.")
            elif data == "erase:yes":
                active = store.active(user)
                copies = store.erase(user)
                await erase_copies(query.bot, copies, cfg)
                if active:
                    for recipient in cfg.admins:
                        try:
                            await query.bot.send_message(recipient, f"Заявка #{active['id']} отменена: клиент отозвал согласие и удалил данные. Удали ручные копии анкеты, если сохранял их отдельно.")
                        except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter):
                            LOG.warning("Could not deliver deletion notice")
                await query.message.answer("Данные удалены из базы бота, согласие отозвано, активная запись отменена. Для удаления ручных копий обратись к оператору. Историю своего чата можно удалить в Telegram.")
            elif data.startswith("cancel:"):
                booking = store.booking(data.split(":", 1)[1])
                if not booking or booking["user_id"] != user or booking["status"] not in ("pending", "confirmed"):
                    await query.message.answer("Активная заявка не найдена.")
                else:
                    await query.message.answer("Точно отменить заявку и согласованный сеанс?", reply_markup=keyboard([("Да, отменить", f"cancel_yes:{booking['id']}"), ("Оставить заявку", "my")]))
            elif data.startswith("cancel_yes:"):
                booking = store.booking(data.split(":", 1)[1])
                if booking and booking["user_id"] == user:
                    try:
                        store.transition(booking["id"], "cancelled", cfg.admins)
                        await query.message.answer("Заявка отменена. Мастер получит уведомление. Удалить данные: /delete.")
                    except ValueError:
                        await query.message.answer("Заявка уже закрыта.")
            elif data.startswith("f:"):
                await send(query.message, flow.action(user, data, query.from_user.username))

    @router.message()
    async def answer(message: Message):
        if message.chat.type != "private" or not message.from_user:
            return
        user = message.from_user.id
        async with locks[user]:
            reply = flow.answer(user, message.text, message.photo[-1].file_id if message.photo else None)
            await send(message, reply)

    return router


async def erase_copies(bot, copies, cfg):
    failed_chats = set()
    for copy in copies:
        try:
            await bot.delete_message(copy["chat_id"], copy["message_id"])
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter):
            failed_chats.add(copy["chat_id"])
    for chat in failed_chats:
        if chat in cfg.admins:
            try:
                await bot.send_message(chat, "Клиент отозвал согласие. Часть старых сообщений с его анкетой Telegram не разрешил удалить автоматически. Удали доступные ручные копии; проверь переписку и уведомление об отмене.")
            except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter):
                LOG.warning("Could not deliver privacy cleanup notice")


async def deliver_outbox(bot, store, cfg):
    for event in store.due():
        booking = store.booking(event["booking_id"])
        if not booking:
            store.delivered(event["id"])
            continue
        async with USER_LOCKS[booking["user_id"]]:
            await deliver_event(bot, store, cfg, event)


async def deliver_event(bot, store, cfg, event):
    booking = store.booking(event["booking_id"])
    if not booking or booking["version"] != event["version"]:
        store.delivered(event["id"])
        return
    if event["kind"].startswith("reminder") and (booking["status"] != "confirmed" or booking["scheduled"] <= time.time()):
        store.delivered(event["id"])
        return
    is_admin = event["kind"] in ("new", "admin_status")
    text = booking_text(booking, cfg, is_admin)
    if event["kind"].startswith("reminder"):
        text = "<b>Напоминание о сеансе</b>\n\n" + text + "\n\nЕсли планы изменились: /my. Подготовка: /prepare."
    try:
        result = await bot.send_message(event["recipient"], text)
        store.track_message(booking["user_id"], result.chat.id, result.message_id, booking["id"])
        store.delivered(event["id"])
    except TelegramRetryAfter as error:
        store.retry(event["id"], event["attempts"]+1)
        await asyncio.sleep(min(error.retry_after, 5))
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError):
        # Keep durable queue, no personal data or token in logs.
        store.retry(event["id"], event["attempts"]+1)
        LOG.warning("Notification delivery deferred")

async def maintenance(bot, store, cfg):
    last_cleanup = 0
    while True:
        try:
            await deliver_outbox(bot, store, cfg)
            if time.time()-last_cleanup > 3600:
                store.expire_drafts()
                for expired in store.expired_bookings(cfg.retention_days):
                    async with USER_LOCKS[expired["user_id"]]:
                        # A reschedule can happen while a previous cleanup awaits Telegram.
                        booking = store.booking(expired["id"])
                        if booking and (booking["scheduled"] or booking["created"]) < time.time()-cfg.retention_days*86400:
                            await erase_copies(bot, store.erase_booking(expired["id"]), cfg)
                last_cleanup = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.error("Maintenance failed; retrying without logging personal data")
        await asyncio.sleep(5)


async def run():
    cfg = Settings.from_env()
    if not cfg.token:
        raise SystemExit("BOT_TOKEN is empty. Copy .env.example to .env and set the token locally.")
    store = Store(cfg.db_path)
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(store, cfg))
    task = None
    try:
        me = await bot.get_me()
        if not cfg.can_collect:
            LOG.warning("Form collection disabled until operator details, ADMIN_IDS and PRIVACY_READY are configured")
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in
                                  [("start", "Главное меню"), ("book", "Заявка на тату"), ("my", "Статус и отмена"),
                                   ("faq", "Частые вопросы"), ("contact", "Связь с мастером"),
                                   ("prepare", "Перед сеансом"), ("privacy", "Обработка данных"), ("delete", "Удалить данные"), ("id", "Мой Telegram ID")]])
        # Preserve pending Telegram updates; do not silently reset an existing webhook.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise SystemExit("An existing webhook is configured. Remove it deliberately before using polling.")
        LOG.info("Bot @%s ready", me.username)
        task = asyncio.create_task(maintenance(bot, store, cfg))
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"], close_bot_session=False)
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        store.db.close()
        await bot.session.close()


if __name__ == "__main__":
    # aiogram's update logs include user IDs; keep them disabled in production.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    LOG.setLevel(logging.INFO)
    asyncio.run(run())
