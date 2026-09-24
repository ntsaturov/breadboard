#!/usr/bin/env python3
"""Публикует пост блога в Telegram-канал.

Использование:
    python3 .github/scripts/telegram_post.py _posts/info/2026-09-24-foreword.md [...]

Настройки берутся из переменных окружения или из файла .env в корне репозитория:
    TELEGRAM_BOT_TOKEN   токен бота (бот должен быть админом канала)
    TELEGRAM_CHANNEL_ID  @username канала или числовой id (-100...)
    SITE_URL             адрес сайта, по умолчанию берётся из CNAME
    WAIT_FOR_PAGE        сколько секунд ждать, пока страница появится на сайте (0 — не ждать)
    DRY_RUN=1            только напечатать сообщение, ничего не отправлять
    YES=1                не спрашивать подтверждение перед отправкой

В front matter поста можно указать `telegram: false`, чтобы не отправлять его.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

MESSAGE_LIMIT = 4096
ROOT = Path(__file__).resolve().parents[2]


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def site_url():
    url = os.environ.get("SITE_URL")
    if not url:
        url = "https://" + (ROOT / "CNAME").read_text().strip()
    return url.rstrip("/")


def read_post(path):
    text = Path(path).read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def post_url(path, meta, base):
    # Повторяет permalink из _config.yml: /:categories/:title/
    slug = meta.get("slug") or re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(path).stem)
    categories = meta.get("categories") or []
    if isinstance(categories, str):
        categories = categories.split()
    parts = [str(c).lower() for c in categories] + [slug]
    return f"{base}/{'/'.join(parts)}/"


# --- Markdown -> Telegram HTML ---------------------------------------------

def absolute(url, base):
    if url.startswith("/"):
        return base + url
    return url


def inline(text, base):
    """Строчная разметка: код, ссылки, картинки, жирный, курсив."""
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)",
        lambda m: keep(f'🖼 <a href="{html.escape(absolute(m.group(2), base))}">'
                       f'{html.escape(m.group(1) or "картинка")}</a>'),
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)",
        lambda m: keep(f'<a href="{html.escape(absolute(m.group(2), base))}">'
                       f"{inline(m.group(1), base)}</a>"),
        text,
    )
    text = re.sub(r"<(https?://[^>]+)>", lambda m: keep(f'<a href="{html.escape(m.group(1))}">'
                                                        f"{html.escape(m.group(1))}</a>"), text)

    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)

    while "\x00" in text:
        text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def to_telegram_html(markdown, base):
    """Возвращает список блоков (абзацев) в формате Telegram HTML."""
    markdown = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", "", markdown, flags=re.S)  # Liquid
    lines = markdown.splitlines()
    blocks, paragraph = [], []
    i = 0

    def flush():
        if paragraph:
            blocks.append(inline(" ".join(s.strip() for s in paragraph), base))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            fence, lang = stripped[:3], stripped[3:].strip()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            blocks.append(f"<pre><code{cls}>{html.escape(chr(10).join(code))}</code></pre>")
        elif not stripped or re.match(r"^\{:.*\}$", stripped):  # пустая строка / атрибуты kramdown
            flush()
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush()
            blocks.append("— — —")
        elif m := re.match(r"^#{1,6}\s+(.*?)\s*#*$", stripped):
            flush()
            blocks.append(f"<b>{inline(m.group(1), base)}</b>")
        elif stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(f"<blockquote>{inline(' '.join(quote), base)}</blockquote>")
            continue
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            flush()
            items = []
            while i < len(lines) and lines[i].strip():
                if m := re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i]):
                    indent = "    " * (len(m.group(1).expandtabs(4)) // 2)
                    marker = "•" if m.group(2) in "-*+" else m.group(2)
                    items.append(f"{indent}{marker} {inline(m.group(3), base)}")
                elif items:  # продолжение пункта на следующей строке
                    items[-1] += " " + inline(lines[i].strip(), base)
                i += 1
            blocks.append("\n".join(items))
            continue
        else:
            paragraph.append(line)
        i += 1

    flush()
    return [b for b in blocks if b.strip()]


def build_message(path, meta, body, base):
    url = post_url(path, meta, base)
    title = f"<b>{html.escape(str(meta.get('title', '')))}</b>"

    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split()
    hashtags = " ".join("#" + re.sub(r"\W+", "_", str(t)) for t in tags)

    footer_full = f'🔗 <a href="{url}">Читать в блоге</a>'
    footer_cut = f'…\n\n🔗 <a href="{url}">Читать полностью в блоге</a>'
    tail = f"\n\n{hashtags}" if hashtags else ""

    blocks = to_telegram_html(body, base)
    full = "\n\n".join([title, *blocks, footer_full]) + tail
    if len(full) <= MESSAGE_LIMIT:
        return full

    # Не влезает — берём столько абзацев, сколько поместится, и ведём в блог.
    kept = [title]
    budget = MESSAGE_LIMIT - len(footer_cut) - len(tail) - 2
    for block in blocks:
        if len("\n\n".join(kept + [block])) > budget:
            break
        kept.append(block)
    return "\n\n".join(kept) + "\n\n" + footer_cut + tail


# --- Telegram / сеть -------------------------------------------------------

def wait_for_page(url, timeout):
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                if resp.status == 200:
                    return
        except urllib.error.URLError:
            pass
        if time.time() > deadline:
            sys.exit(f"Страница {url} так и не открылась за {timeout} с — пост не отправлен")
        print(f"Жду публикации {url} ...")
        time.sleep(20)


def send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {
        "chat_id": os.environ["TELEGRAM_CHANNEL_ID"],
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.load(resp)
    except urllib.error.HTTPError as err:
        sys.exit(f"Telegram ответил ошибкой: {err.read().decode()}")


def main(paths):
    if not paths:
        sys.exit(__doc__)
    load_env()
    base = site_url()
    dry_run = os.environ.get("DRY_RUN") == "1"
    confirm = os.environ.get("YES") != "1"
    wait = int(os.environ.get("WAIT_FOR_PAGE", "0"))

    if not dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHANNEL_ID")):
        sys.exit("Заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в .env")

    for path in paths:
        meta, body = read_post(path)
        if meta.get("telegram") is False or meta.get("published") is False:
            print(f"Пропускаю {path}: отключено во front matter")
            continue

        text = build_message(path, meta, body, base)
        print(f"----- {path} ({len(text)} символов)\n{text}\n")
        if dry_run:
            continue
        if confirm and input(f"Отправить в {os.environ['TELEGRAM_CHANNEL_ID']}? [y/N] ").strip().lower() not in ("y", "д"):
            print("Не отправлено")
            continue

        if wait:
            wait_for_page(post_url(path, meta, base), wait)
        send(text)
        print(f"Отправлено: {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
