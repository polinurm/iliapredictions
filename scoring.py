"""Подсчёт очков. Вся логика "насколько близко угадал" живёт здесь — правь пороги под себя."""
import re
from collections import Counter

# Лимиты контента по правилам: сколько прыжковых элементов и сколько из них каскадов.
# Меняй числа здесь: подсказки и проверки в боте подстроятся сами.
LIMITS = {
    "sp_content": {"elements": 3, "cascades": 1},
    "fs_content": {"elements": 6, "cascades": 2},
}

# Учитывать ли порядок элементов при подсчёте очков.
# False: порядок только запоминается и показывается, очки считаются по набору элементов.
# True: элемент засчитывается, только если стоит на своём месте.
ORDER_MATTERS = False

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
        pl, al = [canon(x) for x in pred], [canon(x) for x in actual]
        if ORDER_MATTERS:
            miss = sum(
                1 for i in range(max(len(pl), len(al)))
                if i >= len(pl) or i >= len(al) or pl[i] != al[i]
            )
            if miss == 0:
                return 1.0
            if miss == 1:
                return 0.75
            return 0.5 if miss <= len(al) / 2 else 0.25
        p, a = Counter(pl), Counter(al)
        if p == a:
            return 1.0
        if max(sum((a - p).values()), sum((p - a).values())) == 1:
            return 0.75
        if al and sum((a & p).values()) >= len(al) / 2:
            return 0.5
        return 0.25
    if kind == "yesno":
        return 1.0 if pred == actual else 0.25
    return 0.0


def score_entry(answers: dict, result: dict, side_count: int):
    """Возвращает (сумма очков, разбивка по полям)."""
    spec = list(FIELDS) + [(f"side{i}", "", "yesno") for i in range(side_count)]
    breakdown = {}
    for key, _label, kind in spec:
        breakdown[key] = score_field(key, kind, answers.get(key), result.get(key))
    return sum(breakdown.values()), breakdown
