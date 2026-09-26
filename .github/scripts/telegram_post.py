#!/usr/bin/env python3
"""Публикует пост блога в Telegram-канал.

Использование:
    python3 .github/scripts/telegram_post.py _posts/info/2026-09-24-foreword.md [...]

Настройки берутся из переменных окружения или из файла .env в корне репозитория:
    TELEGRAM_BOT_TOKEN   токен бота (бот должен быть админом канала)
    TELEGRAM_CHANNEL_ID  @username канала или числовой id (-100...)
    SITE_URL             адрес сайта, по умолчанию берётся из CNAME
    WAIT_FOR_PAGE        сколько секунд ждать, пока страница появится на сайте (0 — не ждать)
    TELEGRAM_PREVIEW_ID  твой личный chat id — куда слать превью
    DRY_RUN=1            только напечатать сообщение, ничего не отправлять
    PREVIEW=1            отправить превью себе в личку вместо канала
    YES=1                не спрашивать подтверждение перед отправкой

В front matter поста можно указать `telegram: false`, чтобы не отправлять его.
`telegram_emoji` — эмодзи перед заголовком (по умолчанию 📝, пустая строка — без эмодзи).
`telegram_image` — обложка поста (иначе берётся header.teaser / header.image).
Картинки из текста статьи уходят альбомом перед постом (тогда обложка не нужна);
`telegram_album: false` — оставить их в тексте ссылками.
`telegram_text` — свой текст для канала вместо статьи (Markdown); альбома тогда нет,
картинка — telegram_image, кнопка — «Читать полностью в блоге».
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
DEFAULT_EMOJI = "📝"
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)(\{:[^}\n]*\})?")
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


def cover_url(meta, base):
    """Обложка: telegram_image, иначе картинка из header (как в Minimal Mistakes)."""
    header = meta.get("header") or {}
    image = meta.get("telegram_image") or header.get("teaser") or header.get("image")
    return absolute(image, base) if image else None


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
    markdown = re.sub(r"\{:[^}\n]*\}", "", markdown)  # атрибуты kramdown, например {: width="300"}
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


def build_message(path, meta, body, base, teaser=False):
    """Возвращает (текст сообщения, кнопка со ссылкой на пост)."""
    url = post_url(path, meta, base)
    if teaser:
        blocks = to_telegram_html(meta["telegram_text"], base)
        header = build_header(meta)
        return "\n\n".join([header, *blocks]), {"text": "📖 Читать полностью в блоге", "url": url}

    header = build_header(meta)
    blocks = to_telegram_html(body, base)
    full = "\n\n".join([header, *blocks])
    if len(full) <= MESSAGE_LIMIT:
        return full, {"text": "📖 Читать в блоге", "url": url}

    # Не влезает — берём столько абзацев, сколько поместится, и ведём в блог.
    kept = [header]
    budget = MESSAGE_LIMIT - 3
    for block in blocks:
        if len("\n\n".join(kept + [block])) > budget:
            break
        kept.append(block)
    return "\n\n".join(kept) + " …", {"text": "📖 Читать полностью в блоге", "url": url}


def build_header(meta):

    emoji = meta.get("telegram_emoji", DEFAULT_EMOJI)
    title = html.escape(str(meta.get("title", "")))
    return f"{emoji} <b>{title}</b>" if emoji else f"<b>{title}</b>"


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


def api(method, payload, files=None):
    """Вызов Bot API: JSON, а если есть файлы — multipart/form-data."""
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in payload.items():
            value = value if isinstance(value, str) else json.dumps(value)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                         f"{value}\r\n".encode())
        for key, path in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                         f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                         + path.read_bytes() + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    else:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers),
                                    timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        sys.exit(f"Telegram ответил ошибкой: {err.read().decode()}")


def send_album(chat_id, images, base):
    """Картинки из статьи альбомом (до 10 штук в альбоме). Локальные файлы
    загружаются с диска, поэтому альбом работает и до публикации сайта."""
    for start in range(0, len(images), 10):
        media, files = [], {}
        for i, (alt, src) in enumerate(images[start:start + 10]):
            local = ROOT / src.lstrip("/")
            if src.startswith("/") and local.exists():
                files[f"photo{i}"] = local
                media.append({"type": "photo", "media": f"attach://photo{i}"})
            else:
                media.append({"type": "photo", "media": absolute(src, base)})
        if len(media) == 1:
            if files:
                api("sendPhoto", {"chat_id": chat_id}, {"photo": files["photo0"]})
            else:
                api("sendPhoto", {"chat_id": chat_id, "photo": media[0]["media"]})
        else:
            api("sendMediaGroup", {"chat_id": chat_id, "media": media}, files)


def send(chat_id, text, button, image=None, base=""):
    markup = {"inline_keyboard": [[button]]}
    if image and len(text) <= CAPTION_LIMIT:
        # Короткий пост — одно сообщение: фото с подписью (файл берём с диска, если он есть).
        local = ROOT / image.removeprefix(base).lstrip("/")
        payload = {"chat_id": chat_id, "caption": text, "parse_mode": "HTML", "reply_markup": markup}
        if local.is_file():
            api("sendPhoto", payload, {"photo": local})
        else:
            api("sendPhoto", {**payload, "photo": image})
        return

    # Длинный пост — обложка как большое превью ссылки над текстом: у такого
    # сообщения нет лимита подписи в 1024 символа.
    preview = ({"url": image, "prefer_large_media": True, "show_above_text": True}
               if image else {"is_disabled": True})
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": preview,
        "reply_markup": markup,
    })


def main(paths):
    if not paths:
        sys.exit(__doc__)
    load_env()
    base = site_url()
    dry_run = os.environ.get("DRY_RUN") == "1"
    preview = os.environ.get("PREVIEW") == "1"
    confirm = os.environ.get("YES") != "1" and not preview
    wait = 0 if preview else int(os.environ.get("WAIT_FOR_PAGE", "0"))
    chat_var = "TELEGRAM_PREVIEW_ID" if preview else "TELEGRAM_CHANNEL_ID"
    chat_id = os.environ.get(chat_var)

    if not dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN") and chat_id):
        sys.exit(f"Заполни TELEGRAM_BOT_TOKEN и {chat_var} в .env")

    for path in paths:
        meta, body = read_post(path)
        if meta.get("telegram") is False or meta.get("published") is False:
            print(f"Пропускаю {path}: отключено во front matter")
            continue

        # Картинки статьи уходят альбомом перед текстом, а из текста убираются.
        teaser = bool(meta.get("telegram_text"))
        album = IMAGE_RE.findall(body) if meta.get("telegram_album", not teaser) else []
        album = [(alt, src) for alt, src, _ in album]
        body = IMAGE_RE.sub("", body) if album else body
        text, button = build_message(path, meta, body, base, teaser)
        image = None if album else cover_url(meta, base)
        print(f"----- {path} ({len(text)} символов)\n"
              f"{f'[альбом: {len(album)} фото]' + chr(10) if album else ''}"
              f"{f'[обложка: {image}]' + chr(10) if image else ''}"
              f"{text}\n[{button['text']}] → {button['url']}\n")
        if dry_run:
            continue
        if confirm and input(f"Отправить в {chat_id}? [y/N] ").strip().lower() not in ("y", "д"):
            print("Не отправлено")
            continue

        if wait:
            wait_for_page(button["url"], wait)
        if album:
            send_album(chat_id, album, base)
        send(chat_id, text, button, image, base)
        print(f"Отправлено в {chat_id}: {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
