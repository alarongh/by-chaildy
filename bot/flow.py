import hashlib
from html import escape
import time
import uuid
from dataclasses import dataclass, field

from .content import CONSENT_VERSION, consent_text, summary

FIELDS = ("name", "idea", "placement", "size", "availability", "budget", "refs")
PROMPTS = {
    "name": "1 / 5 · Как к тебе обращаться?\nДостаточно имени, до 60 символов.",
    "idea": "2 / 5 · Какая у тебя идея?\nОпиши сюжет, настроение, стиль или цвет. Можно пока без точного эскиза.",
    "placement": "3 / 5 · Где хочешь тату?\nНапример: левое предплечье. Для перекрытия укажи это здесь.",
    "size": "4 / 5 · Какой примерный размер?\nВ сантиметрах, «с ладонь» или «помоги определиться».",
    "availability": "5 / 5 · Когда тебе удобно?\nДни и время, например: будни после 18:00. Это пожелание, а не бронь.",
    "budget": "По желанию · Есть ориентир по бюджету?\nМожно написать диапазон или пропустить. Итоговую стоимость назовёт мастер.",
    "refs": "По желанию · Пришли до 3 фото референсов.\nТолько рисунки и примеры тату. Без интимных фото, документов и медицинских данных. Когда закончишь, нажми «Готово / пропустить».",
}


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


class Flow:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg

    def begin(self, user):
        if not self.cfg.can_collect_for(user):
            return Reply("Запись скоро откроется. Мастер завершает настройку контактов и документов. Пока анкету заполнить нельзя.")
        if self.store.active(user):
            return Reply("У тебя уже есть активная заявка. Посмотреть её: /my. Если нужна новая, сначала отмени текущую.")
        session = self.store.session(user)
        if session:
            return self.prompt(session)
        return self.prompt(self.store.save_session(user, "consent", {}, uuid.uuid4().hex[:8]))

    def prompt(self, session):
        step, data = session["step"], session["data"]
        def btn(label, action):
            return (label, f"f:{session['nonce']}:{step}:{action}")
        if step == "consent":
            return Reply(escape(consent_text(self.cfg)), [btn("Согласен / согласна", "accept"), btn("Не согласен / не согласна", "refuse")])
        if step == "age":
            return Reply("Запись здесь доступна с 18 лет. Тебе уже исполнилось 18?", [btn("Да, мне 18+", "adult"), btn("Мне нет 18", "minor")])
        if step == "review":
            return Reply("<b>Проверь заявку</b>\n\n"+summary(data)+"\n\nМастер обсудит детали и подтвердит время. Отправка анкеты не бронирует сеанс.",
                         [btn("Отправить мастеру", "submit")]+[btn("Изменить: "+label, key) for key, label in
                         [("name", "имя"), ("idea", "идею"), ("placement", "место"), ("size", "размер"),
                          ("availability", "дни"), ("budget", "бюджет"), ("refs", "фото")]]+[btn("Удалить черновик", "abort")])
        buttons = [btn("Готово / пропустить", "next")] if step == "refs" else ([btn("Пропустить", "skip")] if step == "budget" else [])
        buttons += [btn("Назад", "back"), btn("Удалить черновик", "abort")]
        return Reply(PROMPTS[step], buttons)

    def action(self, user, callback, username=None):
        if not self.cfg.can_collect_for(user):
            return Reply("Анкеты временно недоступны. Данные можно удалить командой /delete.")
        session = self.store.session(user)
        parts = callback.split(":")
        if not session or len(parts) != 4 or parts[1:3] != [session["nonce"], session["step"]]:
            return Reply("Эта кнопка уже устарела. Открой текущий шаг: /book.")
        action, step, data = parts[3], session["step"], session["data"]
        if action in ("abort", "refuse", "minor"):
            self.store.drop_session(user)
            return Reply("Черновик удалён. Запись не создана." if action != "minor" else "В этом боте запись только с 18 лет. Черновик удалён.")
        if step == "consent" and action == "accept":
            text = consent_text(self.cfg)
            data["consent"] = {"version": CONSENT_VERSION, "accepted_at": time.time(), "text": text,
                               "sha256": hashlib.sha256(text.encode()).hexdigest()}
            data["username"] = username
            data["test"] = self.cfg.test_mode
            step = "age"
        elif step == "age" and action == "adult":
            data["adult"] = True
            step = "name"
        elif step == "review" and action == "submit":
            b = self.store.submit(user, self.cfg.admins)
            return Reply(f"Заявка <b>#{b['id']}</b> сохранена. Мастер получит её и свяжется с тобой здесь.\n\nДата ещё не подтверждена. Статус и отмена: /my.")
        elif step == "review" and action in FIELDS:
            data["editing"] = True
            if action == "refs":
                data["refs"] = []
            step = action
        elif action == "back" and step in FIELDS:
            if data.get("editing"):
                data.pop("editing", None)
                step = "review"
            else:
                step = FIELDS[max(0, FIELDS.index(step)-1)]
        elif (step == "budget" and action == "skip") or (step == "refs" and action == "next"):
            if step == "budget":
                data["budget"] = "Не указан"
            step = self.advance(step, data)
        else:
            return Reply("Открой текущий шаг: /book.")
        return self.prompt(self.store.save_session(user, step, data))

    def advance(self, step, data):
        if data.pop("editing", False):
            return "review"
        return FIELDS[FIELDS.index(step)+1] if step != "refs" else "review"

    def answer(self, user, text=None, photo=None):
        if not self.cfg.can_collect_for(user):
            return Reply("Анкеты временно недоступны. Данные можно удалить командой /delete.")
        session = self.store.session(user)
        if not session:
            return Reply("Чтобы начать запись, нажми /book. Вопросы мастеру: /contact.")
        step, data = session["step"], session["data"]
        if step in ("consent", "age", "review"):
            return self.prompt(session)
        if step == "refs":
            if not photo:
                return Reply("Пришли фото или нажми «Готово / пропустить».", self.prompt(session).buttons)
            refs = data.setdefault("refs", [])
            if len(refs) >= 3:
                return Reply("Уже добавлено 3 фото. Нажми «Готово / пропустить».", self.prompt(session).buttons)
            if photo not in refs:
                refs.append(photo)
            session = self.store.save_session(user, step, data)
            return Reply(f"Добавлено фото: {len(refs)} / 3.", self.prompt(session).buttons)
        if not text or not text.strip():
            return Reply("Ответь текстом, пожалуйста.", self.prompt(session).buttons)
        text = text.strip()
        limit = 60 if step == "name" else (900 if step == "idea" else 240)
        if len(text) > limit:
            return Reply(f"Нужно чуть короче: до {limit} символов.", self.prompt(session).buttons)
        data[step] = text
        return self.prompt(self.store.save_session(user, self.advance(step, data), data))
