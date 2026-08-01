# -*- coding: utf-8 -*-
"""Тесты конвертера разметки Telegram → MAX HTML.

Запуск из каталога worker:  python -m tests.test_markup
Зависимостей нет — обычный скрипт, чтобы проверять можно было и внутри контейнера.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.markup import MAX_TEXT_LIMIT, append_buttons, build_parts, to_html  # noqa: E402

fails = []


def ent(t, off, ln, url=None):
    return {"type": t, "offset": off, "length": ln, "url": url}


def check(name, got, expected):
    ok = got == expected
    if not ok:
        fails.append(f"{name}: ждали {expected!r}, вышло {got!r}")
    print(("OK   " if ok else "FAIL ") + name)


check("жирный", to_html("привет мир", [ent("bold", 0, 6)]), "<b>привет</b> мир")

# эмодзи занимает 2 UTF-16 code unit — если считать питоновскими символами, разметка съедет
check("смещение за эмодзи", to_html("🔥 жирный", [ent("bold", 3, 6)]), "🔥 <b>жирный</b>")

check("ссылка словом", to_html("тут ссылка", [ent("text_link", 4, 6, "https://example.com")]),
      'тут <a href="https://example.com">ссылка</a>')

check("вложенность", to_html("abcd", [ent("bold", 0, 4), ent("italic", 1, 2)]),
      "<b>a<i>bc</i>d</b>")

check("экранирование", to_html("a < b & c > d", None), "a &lt; b &amp; c &gt; d")

check("моноширинный", to_html("см. code тут", [ent("code", 4, 4)]), "см. <code>code</code> тут")

# спойлера в MAX нет — оформление теряется, но текст обязан остаться
check("спойлер без тега", to_html("секрет", [ent("spoiler", 0, 6)]), "секрет")

# длинный пост режется под лимит MAX
long_text = ("абзац раз. " * 400).strip()
parts = build_parts(long_text, None)
print(f"OK   разбивка: частей {len(parts)}, длины {[len(p) for p in parts]}")
if len(parts) < 2:
    fails.append("длинный текст не разбит")
if any(len(p) > MAX_TEXT_LIMIT for p in parts):
    fails.append("кусок превысил лимит MAX")
if "".join(parts).replace(" ", "") != long_text.replace(" ", ""):
    fails.append("при разбивке потерялся текст")

# разметка не должна теряться на границе кусков
mixed = ("текст " * 700) + "ЖИРНЫЙ"
parts2 = build_parts(mixed, [ent("bold", len("текст " * 700), 6)])
if not any("<b>ЖИРНЫЙ</b>" in p for p in parts2):
    fails.append("разметка потерялась при разбивке")
print(f"OK   разбивка с разметкой: частей {len(parts2)}")

# ссылки кнопок как текстовый фолбэк
btns = append_buttons(["пост"], [("Записаться", "https://example.com")])
if '<a href="https://example.com">Записаться</a>' not in btns[-1]:
    fails.append("ссылка кнопки не добавилась")
print("OK   фолбэк кнопок в текст")

print()
if fails:
    print("ПРОВАЛЫ:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОШЛИ")
