import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

import db
from scoring import FIELDS, parse_elements, score_entry

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]  # например @malination (бот должен быть админом канала)
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
MSK = ZoneInfo("Europe/Moscow")

router = Router()


class Form(StatesGroup):
    answering = State()


class SqliteStorage(BaseStorage):
    """Хранит состояния диалогов в той же базе, чтобы они переживали перезапуск процесса."""

    @staticmethod
    def _k(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key, state=None):
        st = state.state if isinstance(state, State) else state
        db.kv_set("fsm_state:" + self._k(key), st)

    async def get_state(self, key):
        return db.kv_get("fsm_state:" + self._k(key))

    async def set_data(self, key, data):
        db.kv_set("fsm_data:" + self._k(key), json.dumps(data, ensure_ascii=False))

    async def get_data(self, key):
        v = db.kv_get("fsm_data:" + self._k(key))
        return json.loads(v) if v else {}

    async def close(self):
        pass


HINTS = {
    "int": "Целое число, например: 2",
    "float": "Число, например: 98.45",
    "elements": "Через запятую, например: 4Lz, 4F, 3A, 4T+3T",
    "yesno": "Да или Нет",
}


# ---------------------------------------------------------------- helpers
def is_admin(uid: int) -> bool:
    return uid in ADMINS


def steps_for(stage):
    side = json.loads(stage["side"])
    return list(FIELDS) + [(f"side{i}", q, "yesno") for i, q in enumerate(side)]


def parse_value(kind: str, text: str):
    text = text.strip()
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text.replace(",", "."))
    if kind == "elements":
        return parse_elements(text)
    if kind == "yesno":
        t = text.lower()
        if t in ("да", "yes", "+"):
            return "да"
        if t in ("нет", "no", "-"):
            return "нет"
        raise ValueError
    raise ValueError


def fmt_answers(steps, answers):
    lines = []
    for key, label, kind in steps:
        v = answers.get(key)
        if v is None:
            shown = "—"
        elif isinstance(v, list):
            shown = ", ".join(v)
        else:
            shown = str(v)
        lines.append(f"• {label} {shown}")
    return "\n".join(lines)


JUMP_TYPES = ["T", "S", "Lo", "F", "Lz", "A"]
BUILDER_RESET = {"el_done": [], "el_cur": [], "el_rot": None, "el_phase": "rot"}


def render_builder(d: dict, header: str):
    """Пошаговый ввод элемента: обороты -> тип прыжка -> каскад? -> ещё элемент / готово."""
    done = d.get("el_done", [])
    cur = d.get("el_cur", [])
    rot = d.get("el_rot")
    phase = d.get("el_phase", "rot")
    btn = InlineKeyboardButton
    lines = [header]
    rows = []
    if done:
        lines.append("Уже добавлено: " + ", ".join(done))

    if phase == "rot":
        if cur:
            lines.append(f"Каскад: {'+'.join(cur)}+… Введите количество оборотов следующего прыжка.")
        else:
            lines.append(f"Элемент {len(done) + 1}. Введите количество оборотов.")
        lines.append("(Можно сразу написать текстом, например: 4Lz+3T, 3A)")
        rows.append([btn(text=str(n), callback_data=f"rot:{n}") for n in (1, 2, 3, 4)])
        if cur:
            rows.append([btn(text="✖️ Сбросить элемент", callback_data="elreset")])
        elif done:
            rows.append([btn(text="← К списку", callback_data="elreset")])
    elif phase == "type":
        start = "+".join(cur + [str(rot)])
        lines.append(f"{start}… Введите тип прыжка (A — аксель).")
        types = [btn(text=t, callback_data=f"typ:{t}") for t in JUMP_TYPES]
        rows += [types[:3], types[3:]]
        rows.append([btn(text="✖️ Сбросить элемент", callback_data="elreset")])
    elif phase == "next":
        lines.append(f"Сейчас: {'+'.join(cur)}")
        lines.append("Каскад? Нажмите «+ Каскад», чтобы добавить ещё один прыжок "
                     "(обороты и тип), или «Элемент готов».")
        rows.append([btn(text="➕ Каскад (+ прыжок)", callback_data="casc")])
        rows.append([btn(text="Элемент готов ✔️", callback_data="elend")])
        rows.append([btn(text="✖️ Сбросить элемент", callback_data="elreset")])
    else:  # menu
        lines.append("Добавить ещё элемент или закончить?")
        rows.append([btn(text="➕ Ещё элемент", callback_data="elmore")])
        rows.append(
            [
                btn(text="↩️ Убрать последний", callback_data="eldel"),
                btn(text="Готово ✔️", callback_data="eldone"),
            ]
        )
    rows.append([btn(text="Пропустить весь пункт", callback_data="skip")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def kb(kind: str, key: str = ""):
    rows = []
    if kind == "yesno":
        rows.append(
            [
                InlineKeyboardButton(text="Да", callback_data="yn:да"),
                InlineKeyboardButton(text="Нет", callback_data="yn:нет"),
            ]
        )
    if key == "place":
        rows.append([InlineKeyboardButton(text=str(n), callback_data=f"pl:{n}") for n in range(1, 7)])
    rows.append([InlineKeyboardButton(text="Пропустить", callback_data="skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_deadline(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M МСК")


async def is_subscribed(bot: Bot, user_id: int):
    """True / False / None (не удалось проверить — не наказываем)."""
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
    except Exception as e:  # noqa: BLE001
        logging.warning("get_chat_member failed for %s: %s", user_id, e)
        return None
    if m.status in ("member", "administrator", "creator"):
        return True
    if m.status == "restricted" and getattr(m, "is_member", False):
        return True
    return False


async def sweep(bot: Bot):
    """Проверяет подписку у всех живых участников и выбивает отписавшихся."""
    for u in db.active_users():
        if await is_subscribed(bot, u["user_id"]) is False:
            db.set_eliminated(u["user_id"])
            try:
                await bot.send_message(
                    u["user_id"],
                    "Ты отписался от канала, поэтому выбываешь из конкурса 😔 "
                    "Прогнозы можно присылать и дальше, но в зачёт они не идут.",
                )
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0.1)


async def sub_watcher(bot: Bot):
    while True:
        try:
            await sweep(bot)
        except Exception:  # noqa: BLE001
            logging.exception("sweep failed")
        await asyncio.sleep(6 * 3600)


async def gate(bot: Bot, user):
    """Возвращает текст ошибки, если участвовать нельзя, иначе None."""
    u = db.get_user(user.id)
    if not u:
        return "Сначала нажми /start и подпишись на канал."
    if u["eliminated"]:
        return "Ты выбыл из конкурса из-за отписки от канала 😔"
    if await is_subscribed(bot, user.id) is False:
        db.set_eliminated(user.id)
        return f"Ты отписался от {CHANNEL}, поэтому выбываешь из конкурса 😔"
    return None


# ---------------------------------------------------------------- flow
async def start_flow(msg: Message, state: FSMContext, mode: str, stage_id: int, user_id: int):
    await state.set_state(Form.answering)
    await state.update_data(mode=mode, stage_id=stage_id, idx=0, answers={}, user_id=user_id, **BUILDER_RESET)
    await ask(msg, state)


async def ask(msg: Message, state: FSMContext):
    d = await state.get_data()
    stage = db.get_stage(d["stage_id"])
    steps = steps_for(stage)
    i = d["idx"]
    if i >= len(steps):
        await finish(msg, state, stage, steps, d)
        return
    key, label, kind = steps[i]
    header = f"({i + 1}/{len(steps)}) {label}"
    if kind == "elements":
        text, markup = render_builder(d, header)
        await msg.answer(text, reply_markup=markup)
        return
    hint = "Нажми кнопку или напиши число" if key == "place" else HINTS[kind]
    await msg.answer(f"{header}\n{hint}", reply_markup=kb(kind, key))


async def record(msg: Message, state: FSMContext, value):
    d = await state.get_data()
    stage = db.get_stage(d["stage_id"])
    steps = steps_for(stage)
    answers = d["answers"]
    if value is not None:
        answers[steps[d["idx"]][0]] = value
    await state.update_data(answers=answers, idx=d["idx"] + 1, **BUILDER_RESET)
    await ask(msg, state)


async def finish(msg: Message, state: FSMContext, stage, steps, d):
    await state.clear()
    if d["mode"] == "predict":
        if time.time() >= stage["deadline"]:
            await msg.answer("Приём прогнозов уже закрыт, не успел(а) 🥲")
            return
        db.save_prediction(d["user_id"], stage["id"], d["answers"])
        await msg.answer(
            f"Прогноз на «{stage['name']}» сохранён ✅\n\n{fmt_answers(steps, d['answers'])}\n\n"
            f"Можно переделать до {fmt_deadline(stage['deadline'])} — просто снова /predict"
        )
    else:
        db.save_result(stage["id"], d["answers"])
        await msg.answer(f"Результаты сохранены ✅ Теперь /score {stage['id']}")


# ---------------------------------------------------------------- user commands
@router.message(Command("start"))
async def cmd_start(m: Message):
    sub = await is_subscribed(m.bot, m.from_user.id)
    if sub is False:
        await m.answer(f"Чтобы участвовать, подпишись на {CHANNEL} и снова нажми /start 💙")
        return
    existing = db.get_user(m.from_user.id)
    if existing and existing["eliminated"]:
        await m.answer("Ты выбыл из конкурса из-за отписки от канала 😔")
        return
    db.upsert_user(m.from_user)
    await m.answer(
        "Привет! Угадываем, как Илья откатает старт 🎿⛸\n\n"
        "Перед стартом присылаешь прогноз: место, баллы за КП/ПП/сумму и контент. "
        "Можно пропускать пункты.\n\n"
        "Очки за каждый пункт: 1 — всё верно, 0.75 — почти, 0.5 — примерно, "
        "0.25 — мимо, 0 — не ответил(а). Очки копятся весь сезон, "
        "топ-3 получат автограф. Отписка от канала = вылет из конкурса.\n\n"
        "/predict — сделать прогноз\n/my — мои очки\n/top — таблица лидеров"
    )


@router.message(Command("predict"))
async def cmd_predict(m: Message, state: FSMContext):
    err = await gate(m.bot, m.from_user)
    if err:
        await m.answer(err)
        return
    stages = db.open_stages(time.time())
    if not stages:
        await m.answer("Сейчас нет открытых стартов. Следи за каналом 👀")
        return
    rows = [
        [InlineKeyboardButton(text=f"{s['name']} (до {fmt_deadline(s['deadline'])})", callback_data=f"pick:{s['id']}")]
        for s in stages
    ]
    await m.answer("На какой старт прогноз?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("pick:"))
async def on_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    err = await gate(cb.bot, cb.from_user)
    if err:
        await cb.message.answer(err)
        return
    stage = db.get_stage(int(cb.data.split(":")[1]))
    if not stage or time.time() >= stage["deadline"]:
        await cb.message.answer("Приём прогнозов на этот старт закрыт.")
        return
    await start_flow(cb.message, state, "predict", stage["id"], cb.from_user.id)


@router.callback_query(Form.answering, F.data == "skip")
async def on_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup()
    except Exception:  # noqa: BLE001
        pass
    await record(cb.message, state, None)


@router.callback_query(Form.answering, F.data.startswith("yn:"))
async def on_yesno(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup()
    except Exception:  # noqa: BLE001
        pass
    await record(cb.message, state, cb.data.split(":")[1])


async def current_step(state: FSMContext):
    d = await state.get_data()
    steps = steps_for(db.get_stage(d["stage_id"]))
    return d, (steps[d["idx"]] if d["idx"] < len(steps) else None)


@router.callback_query(Form.answering, F.data.regexp(r"^(rot:|typ:|casc$|elend$|elmore$|eldel$|elreset$)"))
async def on_builder(cb: CallbackQuery, state: FSMContext):
    d, step = await current_step(state)
    if not step or step[2] != "elements":
        await cb.answer("Этот вопрос уже закрыт")
        return
    data = cb.data
    phase = d.get("el_phase", "rot")
    done, cur, rot = d.get("el_done", []), d.get("el_cur", []), d.get("el_rot")

    if data.startswith("rot:") and phase == "rot":
        rot, phase = int(data[4:]), "type"
    elif data.startswith("typ:") and phase == "type":
        cur.append(f"{rot}{data[4:]}")
        rot, phase = None, "next"
    elif data == "casc" and phase == "next":
        phase = "rot"
    elif data == "elend" and phase == "next":
        done.append("+".join(cur))
        cur, phase = [], "menu"
    elif data == "elmore" and phase == "menu":
        phase = "rot"
    elif data == "eldel" and phase == "menu" and done:
        done.pop()
        phase = "menu" if done else "rot"
    elif data == "elreset":
        cur, rot = [], None
        phase = "menu" if done else "rot"
    else:
        await cb.answer()
        return

    await state.update_data(el_phase=phase, el_done=done, el_cur=cur, el_rot=rot)
    d = await state.get_data()
    steps = steps_for(db.get_stage(d["stage_id"]))
    text, markup = render_builder(d, f"({d['idx'] + 1}/{len(steps)}) {step[1]}")
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(Form.answering, F.data == "eldone")
async def on_elements_done(cb: CallbackQuery, state: FSMContext):
    d, step = await current_step(state)
    if not step or step[2] != "elements":
        await cb.answer("Этот вопрос уже закрыт")
        return
    sel = d.get("el_done", [])
    if not sel:
        await cb.answer("Добавьте хотя бы один элемент или нажмите «Пропустить»", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup()
    except Exception:  # noqa: BLE001
        pass
    await record(cb.message, state, parse_elements(",".join(sel)))


@router.callback_query(Form.answering, F.data.startswith("pl:"))
async def on_place(cb: CallbackQuery, state: FSMContext):
    _d, step = await current_step(state)
    if not step or step[0] != "place":
        await cb.answer("Этот вопрос уже закрыт")
        return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup()
    except Exception:  # noqa: BLE001
        pass
    await record(cb.message, state, int(cb.data[3:]))


@router.message(Form.answering, F.text, ~F.text.startswith("/"))
async def on_text(m: Message, state: FSMContext):
    d = await state.get_data()
    steps = steps_for(db.get_stage(d["stage_id"]))
    kind = steps[d["idx"]][2]
    try:
        value = parse_value(kind, m.text)
    except ValueError:
        await m.answer(f"Не понял формат 🤔\n{HINTS[kind]}")
        return
    await record(m, state, value)


@router.message(Command("my"))
async def cmd_my(m: Message):
    rows = db.user_scores(m.from_user.id)
    u = db.get_user(m.from_user.id)
    if not rows:
        await m.answer("Пока нет прогнозов. Жми /predict")
        return
    total = sum(r["score"] for r in rows if r["scored"])
    lines = [f"• {r['name']}: {r['score']:.2f}" if r["scored"] else f"• {r['name']}: ждёт результатов" for r in rows]
    status = "\n\n⚠️ Ты выбыл из конкурса (отписка)." if u and u["eliminated"] else ""
    await m.answer("Твои очки:\n" + "\n".join(lines) + f"\n\nИтого: {total:.2f}{status}")


@router.message(Command("top"))
async def cmd_top(m: Message):
    rows = db.leaderboard(10)
    if not rows:
        await m.answer("Таблица пока пуста.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        who = f"@{r['username']}" if r["username"] else r["name"]
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {who} — {r['total']:.2f}")
    await m.answer("Таблица лидеров:\n" + "\n".join(lines))


# ---------------------------------------------------------------- admin commands
@router.message(Command("newstage"))
async def cmd_newstage(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        name, dl = [x.strip() for x in command.args.split("|")]
        ts = int(datetime.strptime(dl, "%Y-%m-%d %H:%M").replace(tzinfo=MSK).timestamp())
    except Exception:  # noqa: BLE001
        await m.answer("Формат: /newstage Skate America КП | 2026-10-30 20:00 (время МСК, дедлайн прогнозов)")
        return
    sid = db.create_stage(name, ts)
    await m.answer(f"Старт создан, id={sid}. Дедлайн: {fmt_deadline(ts)}\nСайд-вопрос: /addside {sid} Прыгнет ли ...?")


@router.message(Command("addside"))
async def cmd_addside(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        sid, text = command.args.split(" ", 1)
        n = db.add_side(int(sid), text.strip())
    except Exception:  # noqa: BLE001
        await m.answer("Формат: /addside <id> Текст вопроса (ответ да/нет)")
        return
    await m.answer(f"Добавлено. Сайд-вопросов на старте: {n}")


@router.message(Command("stages"))
async def cmd_stages(m: Message):
    if not is_admin(m.from_user.id):
        return
    rows = db.all_stages()
    await m.answer(
        "\n".join(
            f"{s['id']}. {s['name']} — до {fmt_deadline(s['deadline'])}, "
            f"{'подсчитан' if s['scored'] else ('есть результат' if s['result'] else 'без результата')}"
            for s in rows
        )
        or "Стартов нет."
    )


@router.message(Command("result"))
async def cmd_result(m: Message, command: CommandObject, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    stage = db.get_stage(int(command.args)) if command.args and command.args.strip().isdigit() else None
    if not stage:
        await m.answer("Формат: /result <id старта>")
        return
    await m.answer(f"Вводим фактические результаты для «{stage['name']}»")
    await start_flow(m, state, "result", stage["id"], m.from_user.id)


@router.message(Command("score"))
async def cmd_score(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    stage = db.get_stage(int(command.args)) if command.args and command.args.strip().isdigit() else None
    if not stage or not stage["result"]:
        await m.answer("Формат: /score <id>. Сначала внесите результаты через /result <id>.")
        return
    await sweep(m.bot)  # выбиваем отписавшихся перед подсчётом
    result = json.loads(stage["result"])
    side_count = len(json.loads(stage["side"]))
    preds = db.stage_predictions(stage["id"])
    for p in preds:
        total, _ = score_entry(json.loads(p["answers"]), result, side_count)
        db.set_score(p["user_id"], stage["id"], total)
    db.mark_scored(stage["id"])
    top = db.stage_top(stage["id"], 5)
    lines = [f"{i + 1}. {('@' + r['username']) if r['username'] else r['name']} — {r['score']:.2f}" for i, r in enumerate(top)]
    await m.answer(f"Подсчитано прогнозов: {len(preds)}\nТоп за старт:\n" + "\n".join(lines) + "\n\nОбщая таблица: /top")


# ---------------------------------------------------------------- main
async def run_once(bot: Bot, dp: Dispatcher, duration: int):
    """Режим для GitHub Actions: слушаем Telegram `duration` секунд и выходим."""
    end = time.time() + duration
    offset = int(db.kv_get("offset") or 0)
    while (left := end - time.time()) > 2:
        updates = await bot.get_updates(
            offset=offset or None,
            timeout=min(25, int(left)),
            allowed_updates=dp.resolve_used_update_types(),
        )
        for u in updates:
            try:
                await dp.feed_update(bot, u)
            except Exception:  # noqa: BLE001
                logging.exception("update %s failed", u.update_id)
            offset = u.update_id + 1
            db.kv_set("offset", str(offset))
    # проверка подписок не чаще раза в 6 часов
    if time.time() - float(db.kv_get("last_sweep") or 0) > 6 * 3600:
        await sweep(bot)
        db.kv_set("last_sweep", str(time.time()))


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init()
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=SqliteStorage())
    dp.include_router(router)
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "once":
            duration = int(sys.argv[2]) if len(sys.argv) > 2 else 270
            await run_once(bot, dp, duration)
        else:  # обычный постоянный режим (VPS, Railway и т.п.)
            asyncio.create_task(sub_watcher(bot))
            await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
