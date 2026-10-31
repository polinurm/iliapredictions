"""Подсчёт очков. Вся логика "насколько близко угадал" живёт здесь — правь пороги под себя."""
import re

# Лимиты контента по правилам: сколько прыжковых элементов и сколько из них каскадов.
# Меняй числа здесь: подсказки и проверки в боте подстроятся сами.
LIMITS = {
    "sp_content": {"elements": 3, "cascades": 1},
    "fs_content": {"elements": 6, "cascades": 2},
}

# Очки за каждый элемент программы (оценивается каждый элемент из фактического контента):
#   стоит на своём месте в прогнозе  -> ELEMENT_EXACT
#   есть в прогнозе, но на другом месте -> ELEMENT_PRESENT
#   в прогнозе нет вообще            -> 0
# За весь пункт очки суммируются: в КП максимум 3, в ПП максимум 6.
ELEMENT_EXACT = 1.0
ELEMENT_PRESENT = 0.5
# Каскад, у которого угадан только первый прыжок (например, в факте 4Lz+3T, в прогнозе 4Lz+2T):
# половина очков, которые дал бы целый элемент (0.5 на своём месте, 0.25 на другом).
CASCADE_PARTIAL = 0.5

# Вес каждого пункта: очки за пункт умножаются на это число. Крути, чтобы поменять
# значимость пунктов (например, "total": 0.5 сделает сумму баллов вдвое менее важной).
WEIGHTS = {
    "place": 1.0, "sp": 1.0, "fs": 1.0, "total": 1.0,
    "sp_content": 1.0, "fs_content": 1.0,
    "side": 1.0,  # сайд-вопросы
}

# (ключ, подпись, тип)
FIELDS = [
    ("place", "Какое место займёт Илья?", "int"),
    ("sp", "Сколько баллов за КП?", "float"),
    ("fs", "Сколько баллов за ПП?", "float"),
    ("total", "Сколько баллов в сумме?", "float"),
    ("sp_content", "Какой контент в КП?", "elements"),
    ("fs_content", "Какой контент в ПП?", "elements"),
]

# Допуск по числовым полям: (порог для 1 балла, для 0.75, для 0.5). Дальше — 0.25.
# Точное попадание в баллы нереалистично, поэтому есть "коридоры".
TOLERANCE = {
    "place": (0, 1, 2),
    "sp": (1.0, 3.0, 6.0),
    "fs": (2.0, 6.0, 12.0),
    "total": (3.0, 8.0, 15.0),
}

_LOOKALIKE = str.maketrans("АТЕОСРКМНВ", "ATEOCPKMHB")  # кириллица -> латиница


_JUMP_NAMES = {"T": "T", "S": "S", "LO": "Lo", "F": "F", "LZ": "Lz", "A": "A"}


def canon(el: str) -> str:
    """'4LZ+3t' -> '4Lz+3T' (Lz и Lo пишутся с маленькой буквой, остальные заглавные)."""
    out = []
    for part in el.upper().translate(_LOOKALIKE).split("+"):
        part = part.strip()
        m = re.fullmatch(r"(\d)(LZ|LO|T|S|F|A)", part)
        out.append(m.group(1) + _JUMP_NAMES[m.group(2)] if m else part)
    return "+".join(out)


def parse_elements(text: str) -> list[str]:
    """'4lz, 4F, 3a + 3t' -> ['3A+3T', '4F', '4Lz']"""
    t = re.sub(r"\s*\+\s*", "+", text)
    tokens = [x for x in re.split(r"[,;\s]+", t) if x]
    if not tokens:
        raise ValueError("empty")
    return [canon(x) for x in tokens]  # порядок сохраняем как ввёл пользователь


def _first(el: str) -> str:
    return el.split("+")[0]


def score_elements(pred, actual) -> float:
    """Каждый фактический элемент: на своём месте 1, есть где-то ещё 0.5, нет 0.
    Каскад, у которого угадан только первый прыжок, даёт половину от этих очков."""
    pl = [canon(x) for x in pred]
    al = [canon(x) for x in actual]
    used = [False] * len(pl)
    got = [None] * len(al)
    # 1) точное совпадение на своём месте
    for i, e in enumerate(al):
        if i < len(pl) and pl[i] == e:
            got[i], used[i] = ELEMENT_EXACT, True
    # 2) тот же элемент, но на другом месте
    for i, e in enumerate(al):
        if got[i] is None:
            for j, p in enumerate(pl):
                if not used[j] and p == e:
                    got[i], used[j] = ELEMENT_PRESENT, True
                    break
    # 3) каскад с верным первым прыжком: половина очков
    for i, e in enumerate(al):
        if got[i] is None and "+" in e:
            if i < len(pl) and not used[i] and "+" in pl[i] and _first(pl[i]) == _first(e):
                got[i], used[i] = CASCADE_PARTIAL * ELEMENT_EXACT, True
                continue
            for j, p in enumerate(pl):
                if not used[j] and "+" in p and _first(p) == _first(e):
                    got[i], used[j] = CASCADE_PARTIAL * ELEMENT_PRESENT, True
                    break
    return sum(g or 0.0 for g in got)


def score_field(key: str, kind: str, pred, actual) -> float:
    if pred is None:
        return 0.0
    if actual is None:
        return 0.0
    if kind in ("int", "float"):
        t1, t2, t3 = TOLERANCE[key]
        diff = abs(float(pred) - float(actual))
        if diff <= t1:
            return 1.0
        if diff <= t2:
            return 0.75
        if diff <= t3:
            return 0.5
        return 0.25
    if kind == "elements":
        return score_elements(pred, actual)
    if kind == "yesno":
        return 1.0 if pred == actual else 0.25
    return 0.0


def score_entry(answers: dict, result: dict, side_count: int):
    """Возвращает (сумма очков, разбивка по полям)."""
    spec = list(FIELDS) + [(f"side{i}", "", "yesno") for i in range(side_count)]
    breakdown = {}
    for key, _label, kind in spec:
        w = WEIGHTS.get(key, WEIGHTS["side"] if key.startswith("side") else 1.0)
        breakdown[key] = w * score_field(key, kind, answers.get(key), result.get(key))
    return sum(breakdown.values()), breakdown


def exact_hits(answers: dict, result: dict, side_count: int) -> int:
    """Число точных попаданий за старт (для разрешения ничьих в таблице):
    пункты с полным баллом + элементы программы, стоящие ровно на своём месте."""
    spec = list(FIELDS) + [(f"side{i}", "", "yesno") for i in range(side_count)]
    n = 0
    for key, _label, kind in spec:
        pred, actual = answers.get(key), result.get(key)
        if pred is None or actual is None:
            continue
        if kind == "elements":
            pl, al = [canon(x) for x in pred], [canon(x) for x in actual]
            n += sum(1 for i, e in enumerate(al) if i < len(pl) and pl[i] == e)
        elif score_field(key, kind, pred, actual) == 1.0:
            n += 1
    return n
