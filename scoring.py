"""Подсчёт очков. Вся логика "насколько близко угадал" живёт здесь — правь пороги под себя."""
import re

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


def parse_elements(text: str) -> list[str]:
    """'4Lz, 4F, 3a + 3t' -> ['3A+3T', '4F', '4LZ']"""
    t = text.upper().translate(_LOOKALIKE)
    t = re.sub(r"\s*\+\s*", "+", t)
    tokens = [x for x in re.split(r"[,;\s]+", t) if x]
    if not tokens:
        raise ValueError("empty")
    return sorted(set(tokens))


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
        p, a = set(pred), set(actual)
        if p == a:
            return 1.0
        if max(len(a - p), len(p - a)) == 1:
            return 0.75
        if a and len(a & p) >= len(a) / 2:
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
