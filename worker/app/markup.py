"""Перенос форматирования Telegram → MAX.

Telegram отдаёт текст плоским, а разметку — отдельным списком entities со
смещениями в **UTF-16 code units** (поэтому любой эмодзи или редкий иероглиф
сдвигает позиции, если считать питоновскими символами). MAX своих entities не
принимает, но понимает HTML при `format: "html"` — проверено на живом API
2026-08-01: b→strong, i→emphasized, u→underline, s→strikethrough, a→link,
code/pre→monospaced.

Здесь entities разворачиваются в HTML и, если пост не влезает в лимит MAX
(4000 символов на поле text), режутся на части по границе абзаца.
"""
from __future__ import annotations

from typing import Any, Iterable

MAX_TEXT_LIMIT = 4000
# запас под теги: режем исходник мельче лимита, HTML всегда длиннее плоского текста
PLAIN_CHUNK = 2800

# entity Telegram -> (открывающий, закрывающий) тег MAX-HTML
SIMPLE_TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
}
# спойлер и цитата у MAX не выражаются тегом — текст переносим без обёртки,
# custom_emoji тоже: сам символ уже лежит в тексте


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _raw(text: str) -> bytes:
    """UTF-16LE представление: в его code units Telegram считает смещения."""
    return text.encode("utf-16-le")


def _u_len(text: str) -> int:
    return len(_raw(text)) // 2


def _u_slice(raw: bytes, a: int, b: int) -> str:
    """Кусок [a, b) в code units. Срез делаем по БАЙТАМ и декодируем целиком —
    иначе суррогатная пара эмодзи распадается на два битых полусимвола."""
    return raw[a * 2:b * 2].decode("utf-16-le", errors="replace")


def _tags_for(ent: Any) -> tuple[str, str] | None:
    etype = getattr(ent, "type", None) or (ent.get("type") if isinstance(ent, dict) else None)
    if etype in SIMPLE_TAGS:
        return SIMPLE_TAGS[etype]
    if etype == "text_link":
        url = getattr(ent, "url", None) or (ent.get("url") if isinstance(ent, dict) else None)
        if url:
            return f'<a href="{_escape(url)}">', "</a>"
    return None


def _span(ent: Any) -> tuple[int, int]:
    off = getattr(ent, "offset", None)
    ln = getattr(ent, "length", None)
    if off is None and isinstance(ent, dict):
        off, ln = ent.get("offset"), ent.get("length")
    return int(off or 0), int(ln or 0)


def to_html(text: str, entities: Iterable[Any] | None) -> str:
    """Собирает HTML для MAX из плоского текста и entities Telegram."""
    raw = _raw(text)
    total = len(raw) // 2
    marked = []
    for e in entities or []:
        tags = _tags_for(e)
        if not tags:
            continue
        off, ln = _span(e)
        if ln <= 0:
            continue
        marked.append((off, min(off + ln, total), tags))
    if not marked:
        return _escape(text)
    # порядок вскрытия: раньше начинается — раньше открывается; при равном старте
    # длинный охватывает короткий
    marked.sort(key=lambda x: (x[0], -(x[1] - x[0])))

    # текст режем только на границах разметки, между ними — цельным куском
    points = sorted({0, total} | {m[0] for m in marked} | {m[1] for m in marked})
    out: list[str] = []
    stack: list[tuple[int, int, tuple[str, str]]] = []
    for idx, pos in enumerate(points):
        active = [m for m in marked if m[0] <= pos < m[1]]
        while stack and stack[-1] not in active:  # закрываем в обратном порядке открытия
            out.append(stack.pop()[2][1])
        for m in active:
            if m not in stack:
                out.append(m[2][0])
                stack.append(m)
        nxt = points[idx + 1] if idx + 1 < len(points) else total
        if nxt > pos:
            out.append(_escape(_u_slice(raw, pos, nxt)))
    while stack:
        out.append(stack.pop()[2][1])
    return "".join(out)


def _slice_entities(entities: Iterable[Any] | None, start: int, end: int) -> list[dict[str, Any]]:
    """Оставляет entities, попавшие в [start, end), со смещением к началу куска."""
    res = []
    for e in entities or []:
        off, ln = _span(e)
        s, t = max(off, start), min(off + ln, end)
        if t <= s:
            continue
        etype = getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else None)
        url = getattr(e, "url", None) or (e.get("url") if isinstance(e, dict) else None)
        res.append({"type": etype, "offset": s - start, "length": t - s, "url": url})
    return res


def append_buttons(parts: list[str], buttons: list[tuple[str, str]]) -> list[str]:
    """Дописывает ссылки inline-кнопок в конец поста.

    Кнопок как элемента у MAX для канала нет, а ссылка из них — часто самое ценное
    в посте (запись на курс, оплата). Молча терять её нельзя.
    """
    links = [f'<a href="{_escape(u)}">{_escape(t)}</a>' for t, u in buttons if u]
    if not links:
        return parts
    tail = "\n\n" + "\n".join(links)
    if parts and len(parts[-1]) + len(tail) <= MAX_TEXT_LIMIT:
        parts[-1] += tail
        return parts
    return parts + [tail.strip()]


def _cut_points(raw: bytes, limit: int) -> list[int]:
    """Границы кусков в code units: по возможности на переносе строки, иначе на пробеле."""
    total = len(raw) // 2
    points, start = [], 0
    while total - start > limit:
        window = _u_slice(raw, start, start + limit)
        cut = None
        for sep in ("\n", " "):
            idx = window.rfind(sep)
            if idx > limit // 2:
                cut = _u_len(window[:idx + 1])
                break
        cut = cut or limit
        # не рассекать суррогатную пару: сдвигаемся на один unit вперёд
        if 0xD800 <= int.from_bytes(raw[(start + cut) * 2:(start + cut) * 2 + 2], "little") <= 0xDBFF:
            cut += 1
        points.append(start + cut)
        start += cut
    return points


def build_parts(text: str, entities: Iterable[Any] | None) -> list[str]:
    """Готовые HTML-куски под лимит MAX. Пустой текст → пустой список."""
    if not text:
        return []
    raw = _raw(text)
    total = len(raw) // 2
    bounds = [0] + _cut_points(raw, PLAIN_CHUNK) + [total]
    parts = []
    for a, b in zip(bounds, bounds[1:]):
        chunk = _u_slice(raw, a, b)
        html = to_html(chunk, _slice_entities(entities, a, b))
        if len(html) > MAX_TEXT_LIMIT:
            # аварийный случай: разметки столько, что теги раздули кусок — режем плоско
            html = _escape(chunk)[:MAX_TEXT_LIMIT]
        parts.append(html)
    return parts
