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
import texts as T
from scoring import FIELDS, LIMITS, canon, parse_elements, score_entry

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


def hint_for(kind: str, key: str = "") -> str:
    if key == "place":
        return T.HINT_PLACE
    return {"int": T.HINT_INT, "float": T.HINT_FLOAT, "yesno": T.HINT_YESNO,
            "elements": T.HINT_ELEMENTS_TEXT}[kind]


def limits_for(key: str):
    """(макс. элементов, макс. каскадов) для пункта с контентом."""
    lim = LIMITS.get(key)
    return (lim["elements"], lim["cascades"]) if lim else (99, 99)


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
            shown = T.SKIPPED_MARK
        elif isinstance(v, list):
            shown = " → ".join(v)
        else:
            shown = str(v)
        lines.append(f"• {label} {shown}")
    return "\n".join(lines)


JUMP_TYPES = ["T", "S", "Lo", "F", "Lz", "A"]
BUILDER_RESET = {"el_done": [], "el_cur": [], "el_rot": None, "el_phase": "rot"}


def can_cascade(cur: list, cas_used: int, n_cas: int) -> bool:
    """Можно ли добавить к текущему элементу ещё один прыжок (каскад до 3 прыжков)."""
    return len(cur) < 3 and (len(cur) >= 2 or cas_used < n_cas)


def render_builder(d: dict, header: str, key: str = ""):
    """Пошаговый ввод: обороты -> тип прыжка -> каскад? -> ещё элемент / готово.
    Порядок элементов = порядок ввода, лимиты берутся из scoring.LIMITS."""
    done = d.get("el_done", [])
    cur = d.get("el_cur", [])
    rot = d.get("el_rot")
    phase = d.get("el_phase", "rot")
    n_el, n_cas = limits_for(key)
    limited = key in LIMITS
    cas_used = sum(1 for e in done if "+" in e)
    full = len(done) >= n_el
    btn = InlineKeyboardButton

    lines = [header]
    rows = []
    if limited:
        lines.append(T.rules(T.PART.get(key, ""), n_el, n_cas))
    lines.append(T.ORDER_HINT)
    if done:
        lines.append("")
        lines.append(T.LIST_TITLE)
        lines += [T.LIST_ITEM.format(i=i, el=e) for i, e in enumerate(done, 1)]
    if limited:
        lines.append("")
        if phase != "menu":
            lines.append(T.NOW_ENTERING.format(k=len(done) + 1, n=n_el))
        if not full:
            lines.append(T.LEFT.format(left=n_el - len(done), cas_left=n_cas - cas_used, cas=n_cas))
    lines.append("")

    if phase == "rot":
        lines.append(T.ASK_ROT_CASC.format(cur="+".join(cur)) if cur else T.ASK_ROT)
        lines.append(T.HINT_ELEMENTS_TEXT)
        rows.append([btn(text=str(n), callback_data=f"rot:{n}") for n in (1, 2, 3, 4, 5)])
        if cur:
            rows.append([btn(text=T.BTN_RESET, callback_data="elreset")])
        elif done:
            rows.append([btn(text=T.BTN_BACK_LIST, callback_data="elreset")])
    elif phase == "type":
        lines.append(T.ASK_TYPE.format(start="+".join(cur + [str(rot)])))
        types = [btn(text=t, callback_data=f"typ:{t}") for t in JUMP_TYPES]
        rows += [types[:3], types[3:]]
        rows.append([btn(text=T.BTN_RESET, callback_data="elreset")])
    elif phase == "next":
        if can_cascade(cur, cas_used, n_cas):
            lines.append(T.ASK_CASCADE.format(cur="+".join(cur)))
            rows.append([btn(text=T.BTN_CASCADE, callback_data="casc")])
        else:
            lines.append(T.NO_CASCADE_LEFT.format(cur="+".join(cur)))
        rows.append([btn(text=T.BTN_ELEMENT_DONE, callback_data="elend")])
        rows.append([btn(text=T.BTN_RESET, callback_data="elreset")])
    else:  # menu
        if full:
            lines.append(T.ALL_DONE.format(n=len(done)))
        else:
            lines.append(T.ASK_MENU)
            rows.append([btn(text=T.BTN_MORE, callback_data="elmore")])
        rows.append(
            [
                btn(text=T.BTN_UNDO, callback_data="eldel"),
                btn(text=T.BTN_DONE, callback_data="eldone"),
            ]
        )
    rows.append([btn(text=T.BTN_SKIP_ALL, callback_data="skip")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def kb(kind: str, key: str = ""):
    rows = []
    if kind == "yesno":
        rows.append(
            [
                InlineKeyboardButton(text=T.BTN_YES, callback_data="yn:да"),
                InlineKeyboardButton(text=T.BTN_NO, callback_data="yn:нет"),
            ]
        )
    if key == "place":
        rows.append([InlineKeyboardButton(text=str(n), callback_data=f"pl:{n}") for n in range(1, 7)])
    rows.append([InlineKeyboardButton(text=T.BTN_SKIP, callback_data="skip")])
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
                await bot.send_message(u["user_id"], T.SWEEP_DM)
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


async def remind_check(bot: Bot):
    """За час до дедлайна напоминает всем активным участникам (один раз на старт)."""
    now = time.time()
    for s in db.all_stages():
        left = s["deadline"] - now
        if s["scored"] or left <= 0 or left > 3600:
            continue
        flag = f"reminded:{s['id']}"
        if db.kv_get(flag):
            continue
        db.kv_set(flag, "1")  # ставим заранее, чтобы не отправить дважды
        have = {p["user_id"] for p in db.stage_predictions(s["id"])}
        mins = max(1, int(left // 60))
        until = f"{fmt_deadline(s['deadline'])} (осталось ~{mins} мин)"
        sent = 0
        for u in db.active_users():
            if u["user_id"] in have:
                text = T.REMIND_HAVE.format(name=s["name"], until=until)
                label = T.REMIND_BTN_HAVE
            else:
                text = T.REMIND_NEW.format(name=s["name"], until=until)
                label = T.REMIND_BTN_NEW
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"pick:{s['id']}")]]
            )
            try:
                await bot.send_message(u["user_id"], text, reply_markup=markup)
                sent += 1
            except Exception:  # noqa: BLE001  (пользователь заблокировал бота и т.п.)
                pass
            await asyncio.sleep(0.05)
        for a in ADMINS:
            try:
                await bot.send_message(a, f"Напоминание по «{s['name']}» отправлено: {sent} из {len(db.active_users())}")
            except Exception:  # noqa: BLE001
                pass


async def reminder_watcher(bot: Bot):
    while True:
        try:
            await remind_check(bot)
        except Exception:  # noqa: BLE001
            logging.exception("remind failed")
        await asyncio.sleep(60)


async def gate(bot: Bot, user):
    """Возвращает текст ошибки, если участвовать нельзя, иначе None."""
    u = db.get_user(user.id)
    if not u:
        return T.NEED_START
    if u["eliminated"]:
        return T.ELIMINATED
    if await is_subscribed(bot, user.id) is False:
        db.set_eliminated(user.id)
        return T.UNSUB_NOW.format(channel=CHANNEL)
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
    header = T.STEP_HEADER.format(i=i + 1, n=len(steps), label=label)
    if kind == "elements":
        text, markup = render_builder(d, header, key)
        await msg.answer(text, reply_markup=markup)
        return
    await msg.answer(f"{header}\n{hint_for(kind, key)}", reply_markup=kb(kind, key))


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
            await msg.answer(T.TOO_LATE)
            return
        db.save_prediction(d["user_id"], stage["id"], d["answers"])
        await msg.answer(
            T.SAVED.format(name=stage["name"], answers=fmt_answers(steps, d["answers"]),
                           deadline=fmt_deadline(stage["deadline"]))
        )
    else:
        db.save_result(stage["id"], d["answers"])
        await msg.answer(f"Результаты сохранены ✅ Теперь /score {stage['id']}")


# ---------------------------------------------------------------- user commands
@router.message(Command("start"))
async def cmd_start(m: Message):
    sub = await is_subscribed(m.bot, m.from_user.id)
    if sub is False:
        await m.answer(T.START_NEED_SUB.format(channel=CHANNEL))
        return
    existing = db.get_user(m.from_user.id)
    if existing and existing["eliminated"]:
        await m.answer(T.ELIMINATED)
        return
    db.upsert_user(m.from_user)
    await m.answer(T.WELCOME)


@router.message(Command("predict"))
async def cmd_predict(m: Message, state: FSMContext):
    err = await gate(m.bot, m.from_user)
    if err:
        await m.answer(err)
        return
    stages = db.open_stages(time.time())
    if not stages:
        await m.answer(T.NO_OPEN_STAGES)
        return
    rows = [
        [InlineKeyboardButton(
            text=T.STAGE_BTN.format(name=s["name"], deadline=fmt_deadline(s["deadline"])),
            callback_data=f"pick:{s['id']}")]
        for s in stages
    ]
    await m.answer(T.PICK_STAGE, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("pick:"))
async def on_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    err = await gate(cb.bot, cb.from_user)
    if err:
        await cb.message.answer(err)
        return
    stage = db.get_stage(int(cb.data.split(":")[1]))
    if not stage or time.time() >= stage["deadline"]:
        await cb.message.answer(T.STAGE_CLOSED)
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
        await cb.answer(T.CB_CLOSED)
        return
    data = cb.data
    key = step[0]
    n_el, n_cas = limits_for(key)
    phase = d.get("el_phase", "rot")
    done, cur, rot = d.get("el_done", []), d.get("el_cur", []), d.get("el_rot")
    cas_used = sum(1 for e in done if "+" in e)

    if data.startswith("rot:") and phase == "rot":
        rot, phase = int(data[4:]), "type"
    elif data.startswith("typ:") and phase == "type":
        cur.append(f"{rot}{data[4:]}")
        rot, phase = None, "next"
    elif data == "casc" and phase == "next" and can_cascade(cur, cas_used, n_cas):
        phase = "rot"
    elif data == "elend" and phase == "next":
        done.append("+".join(cur))
        cur, phase = [], "menu"
    elif data == "elmore" and phase == "menu" and len(done) < n_el:
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
    text, markup = render_builder(
        d, T.STEP_HEADER.format(i=d["idx"] + 1, n=len(steps), label=step[1]), key)
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(Form.answering, F.data == "eldone")
async def on_elements_done(cb: CallbackQuery, state: FSMContext):
    d, step = await current_step(state)
    if not step or step[2] != "elements":
        await cb.answer(T.CB_CLOSED)
        return
    sel = d.get("el_done", [])
    if not sel:
        await cb.answer(T.NEED_ONE_ELEMENT, show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup()
    except Exception:  # noqa: BLE001
        pass
    await record(cb.message, state, [canon(x) for x in sel])  # порядок как ввёл пользователь


@router.callback_query(Form.answering, F.data.startswith("pl:"))
async def on_place(cb: CallbackQuery, state: FSMContext):
    _d, step = await current_step(state)
    if not step or step[0] != "place":
        await cb.answer(T.CB_CLOSED)
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
    key, kind = steps[d["idx"]][0], steps[d["idx"]][2]
    try:
        value = parse_value(kind, m.text)
    except ValueError:
        await m.answer(T.BAD_FORMAT.format(hint=hint_for(kind, key)))
        return
    if kind == "elements" and key in LIMITS:
        n_el, n_cas = limits_for(key)
        part = T.PART.get(key, "")
        if len(value) > n_el:
            await m.answer(T.TOO_MANY_ELEMENTS.format(part=part, n=n_el))
            return
        if sum(1 for e in value if "+" in e) > n_cas:
            await m.answer(T.TOO_MANY_CASCADES.format(part=part, n=n_cas))
            return
    await record(m, state, value)


@router.message(Command("my"))
async def cmd_my(m: Message):
    rows = db.user_scores(m.from_user.id)
    u = db.get_user(m.from_user.id)
    if not rows:
        await m.answer(T.MY_EMPTY)
        return
    total = sum(r["score"] for r in rows if r["scored"])
    lines = [
        T.MY_LINE_SCORED.format(name=r["name"], score=r["score"]) if r["scored"]
        else T.MY_LINE_PENDING.format(name=r["name"])
        for r in rows
    ]
    status = T.MY_ELIMINATED if u and u["eliminated"] else ""
    await m.answer(T.MY.format(lines="\n".join(lines), total=total, status=status))


@router.message(Command("top"))
async def cmd_top(m: Message):
    rows = db.leaderboard(10)
    if not rows:
        await m.answer(T.TOP_EMPTY)
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        who = f"@{r['username']}" if r["username"] else r["name"]
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {who} — {r['total']:.2f}")
    await m.answer(T.TOP.format(lines="\n".join(lines)))


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
        try:
            await remind_check(bot)
        except Exception:  # noqa: BLE001
            logging.exception("remind failed")
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
            asyncio.create_task(reminder_watcher(bot))
            await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
