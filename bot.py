import asyncio
import logging
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, InputMediaPhoto

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в файле .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("tiktok_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MAX_VIDEO_BYTES = 50 * 1024 * 1024  # лимит Bot API на отправку видео
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # лимит Bot API на отправку фото
MAX_CAROUSEL_BYTES = 100 * 1024 * 1024  # потолок памяти на одну карусель
MAX_PHOTOS = 35
ALBUM_SIZE = 10  # sendMediaGroup принимает от 2 до 10 элементов
MAX_CONCURRENT_JOBS = 3
UPLOAD_TIMEOUT = 300  # по умолчанию aiogram ждёт запрос только 60 секунд
RECONNECT_DELAY = 5

# Файл держится в памяти и при скачивании, и при отправке, поэтому слот занят на обе стадии.
job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

TIKTOK_URL_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:https?://)?(?:(?:www|vm|vt|t|m)\.)?tiktok\.com/[A-Za-z0-9_\-/?&=@.%~+#]+",
    re.IGNORECASE
)
SPLIT_SCHEME_RE = re.compile(r"\bh\s+ttps?://", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
SSSTIK_REFERER = {"Referer": "https://ssstik.io/"}


class MediaTooLarge(Exception):
    """Файл превышает лимит, который Telegram разрешает ботам."""


def find_tiktok_url(text: str) -> str | None:
    match = TIKTOK_URL_RE.search(SPLIT_SCHEME_RE.sub("https://", text))
    if not match:
        return None
    url = match.group(0).rstrip(".?&#")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_tiktok_url(message: types.Message) -> str | None:
    """Ищет ссылку в тексте или подписи, затем в ссылках, спрятанных под текстом."""
    url = find_tiktok_url(message.text or message.caption or "")
    if url:
        return url
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "text_link" and entity.url:
            url = find_tiktok_url(entity.url)
            if url:
                return url
    return None


def is_video(data: bytes) -> bool:
    """MP4-контейнер; аудио (M4A) и фото (HEIC) используют тот же формат, но не подходят."""
    return data[4:8] == b"ftyp" and data[8:12] not in (b"M4A ", b"M4B ", b"heic", b"heix", b"mif1", b"msf1")


def is_image(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


async def fetch_media(session: aiohttp.ClientSession, url: str, headers: dict | None = None) -> bytes | None:
    """Скачивает файл потоком и прерывается, как только он превысил лимит."""
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                log.warning("Файл %s: HTTP %s", url, resp.status)
                return None
            if resp.content_length and resp.content_length > MAX_VIDEO_BYTES:
                raise MediaTooLarge(url)
            buffer = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buffer += chunk
                if len(buffer) > MAX_VIDEO_BYTES:
                    raise MediaTooLarge(url)
            return bytes(buffer)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Не удалось скачать %s: %r", url, e)
        return None


async def collect_media(session: aiohttp.ClientSession, urls: list[str], title: str, headers: dict | None = None):
    """Скачивает ссылки и определяет тип по содержимому: фото собирает в карусель, видео возвращает сразу."""
    images: list[bytes] = []
    total = 0
    missing = max(0, len(urls) - MAX_PHOTOS)

    for url in urls[:MAX_PHOTOS]:
        data = await fetch_media(session, url, headers)
        if data is None:
            missing += 1
        elif is_video(data) and not images:
            return {"type": "video", "data": data, "title": title}
        elif not is_image(data):
            log.warning("Файл %s не похож на фото или видео, пропускаю", url)
        elif len(data) > MAX_PHOTO_BYTES or total + len(data) > MAX_CAROUSEL_BYTES:
            log.warning("Фото %s не проходит по размеру (%s байт)", url, len(data))
            missing += 1
        else:
            images.append(data)
            total += len(data)

    if images:
        return {"type": "images", "items": images, "title": title, "missing": missing}
    return None


@dataclass
class Link:
    href: str
    classes: str
    text: str = ""

    @property
    def is_music(self) -> bool:
        return (
            "music" in self.classes
            or "mp3" in self.text.lower()
            or self.href.split("?")[0].lower().endswith(".mp3")
        )

    @property
    def is_without_watermark(self) -> bool:
        return "without_watermark" in self.classes or "without watermark" in self.text.lower()


class SsstikPage(HTMLParser):
    """Достаёт из ответа ssstik описание и ссылки, не завися от порядка атрибутов."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._link: Link | None = None

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class") or ""
        if tag == "a":
            href = attrs.get("href") or ""
            self._link = Link(href, classes) if href.startswith(("http://", "https://")) else None
            if self._link:
                self.links.append(self._link)
        elif tag == "p" and "maintext" in classes.split():
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        elif tag == "p":
            self._in_title = False

    def handle_data(self, data):
        if self._link:
            self._link.text += data
        if self._in_title:
            self._title_parts.append(data)


async def download_from_ssstik(session: aiohttp.ClientSession, url: str):
    """Качает видео или галерею фото через ssstik.io."""
    try:
        async with session.get("https://ssstik.io/en") as resp:
            if resp.status != 200:
                log.warning("ssstik: главная страница ответила HTTP %s", resp.status)
                return None
            html = await resp.text()

        token_match = re.search(r's_tt\s*=\s*[\x22\x27]([^\x22\x27]+)', html)
        if not token_match:
            log.warning("ssstik: на странице не найден токен s_tt")
        token = token_match.group(1) if token_match else ""

        post_headers = {
            "HX-Request": "true",
            "HX-Trigger": "_gcaptcha_pt",
            "HX-Target": "target",
            "HX-Current-URL": "https://ssstik.io/en",
            "Referer": "https://ssstik.io/en",
        }
        data = {"id": url, "locale": "en", "tt": token}

        async with session.post("https://ssstik.io/abc?url=dl", headers=post_headers, data=data) as resp:
            if resp.status != 200:
                log.warning("ssstik: запрос на скачивание ответил HTTP %s", resp.status)
                return None
            post_html = await resp.text()

        page = SsstikPage()
        page.feed(post_html)

        main_link = next((l for l in page.links if l.is_without_watermark and not l.is_music), None)
        if main_link:
            video = await fetch_media(session, main_link.href, SSSTIK_REFERER)
            if video and is_video(video):
                return {"type": "video", "data": video, "title": page.title}
            log.warning("ssstik: ссылка «без водяного знака» вернула не видео: %s", main_link.href)

        # У карусели и видео ссылки на одном CDN, поэтому разбираемся по содержимому файлов.
        candidates = [
            l.href for l in page.links
            if "tikcdn.io/" in l.href and not l.is_music and l is not main_link
        ]
        media = await collect_media(session, candidates, page.title, SSSTIK_REFERER)
        if not media:
            log.warning("ssstik: в ответе нет подходящих ссылок для %s (%s байт)", url, len(post_html))
        return media

    except MediaTooLarge:
        raise
    except Exception as e:
        log.warning("ssstik: ошибка при обработке %s: %r", url, e)

    return None


async def download_from_tikwm(session: aiohttp.ClientSession, url: str):
    """Фоллбек через tikwm.com, если основной парсер не ответил."""
    try:
        async with session.post("https://www.tikwm.com/api/", data={"url": url}) as resp:
            if resp.status != 200:
                log.warning("tikwm: API ответил HTTP %s", resp.status)
                return None
            data = await resp.json(content_type=None)

        if data.get("code") != 0:
            log.warning("tikwm: ошибка API для %s: %s", url, data.get("msg"))
            return None

        media_data = data.get("data") or {}
        title = media_data.get("title") or ""
        images = media_data.get("images")

        if images and isinstance(images, list):
            media = await collect_media(session, images, title)
            if media:
                return media

        video_url = media_data.get("play")
        if video_url:
            video = await fetch_media(session, video_url)
            if video and is_video(video):
                return {"type": "video", "data": video, "title": title}
            log.warning("tikwm: ссылка на видео вернула не видео: %s", video_url)

    except MediaTooLarge:
        raise
    except Exception as e:
        log.warning("tikwm: ошибка при обработке %s: %r", url, e)

    return None


async def download_tiktok(url: str):
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        media = await download_from_ssstik(session, url)
        if media:
            return media
        log.info("ssstik не справился с %s, пробую tikwm", url)
        return await download_from_tikwm(session, url)


async def send(message: types.Message, method):
    """Отправляет файл с увеличенным таймаутом и один раз переживает ограничение частоты."""
    try:
        return await message.bot(method, request_timeout=UPLOAD_TIMEOUT)
    except TelegramRetryAfter as e:
        log.warning("Telegram просит подождать %s с", e.retry_after)
        await asyncio.sleep(e.retry_after + 1)
        return await message.bot(method, request_timeout=UPLOAD_TIMEOUT)


async def send_photos(message: types.Message, images: list[bytes], caption: str | None):
    for start in range(0, len(images), ALBUM_SIZE):
        chunk = images[start:start + ALBUM_SIZE]
        chunk_caption = caption if start == 0 else None
        files = [
            BufferedInputFile(img, filename=f"photo_{start + i}.jpg")
            for i, img in enumerate(chunk)
        ]
        if len(files) == 1:  # альбом из одного элемента Telegram отклоняет
            await send(message, message.answer_photo(photo=files[0], caption=chunk_caption))
        else:
            group = [
                InputMediaPhoto(media=file, caption=chunk_caption if i == 0 else None)
                for i, file in enumerate(files)
            ]
            await send(message, message.answer_media_group(media=group))


async def send_media(message: types.Message, media: dict):
    caption = media["title"][:1000] if media["title"] else None

    if media["type"] == "video":
        await send(message, message.answer_video(
            video=BufferedInputFile(media["data"], filename="tiktok.mp4"),
            caption=caption
        ))
    elif media["type"] == "images":
        await send_photos(message, media["items"], caption)
        if media.get("missing"):
            await message.answer(f"Не удалось скачать фото: {media['missing']} шт.")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Кидай ссылку на TikTok — скачаю видео или фото-пост без водяных знаков."
    )


@dp.message(F.text | F.caption)
async def handle_link(message: types.Message):
    url = extract_tiktok_url(message)
    if not url:
        # В группах молчим: там бот видит и обычную переписку.
        if message.chat.type == ChatType.PRIVATE and message.text and not message.text.startswith("/"):
            await message.answer("Не похоже на ссылку на TikTok.")
        return

    status_msg = await message.answer("Качаю, секунду...")

    try:
        async with job_slots:
            await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_video")
            media = await download_tiktok(url)

            if not media:
                await status_msg.edit_text(
                    "Не получилось скачать. Возможно, видео приватное или удалено, "
                    "либо сервис скачивания сейчас недоступен. Попробуй позже."
                )
                return

            await send_media(message, media)

    except MediaTooLarge:
        await edit_quietly(
            status_msg,
            "Файл больше 50 МБ. Телеграм не разрешает ботам отправлять такие тяжелые видео."
        )
    except Exception:
        log.exception("Ошибка при обработке %s", url)
        await edit_quietly(status_msg, "Что-то пошло не так при отправке. Попробуй еще раз.")
    else:
        # Файл уже у пользователя: неудача с удалением статуса не должна выглядеть как ошибка.
        try:
            await status_msg.delete()
        except Exception as e:
            log.warning("Не удалось удалить статус: %r", e)


async def edit_quietly(status_msg: types.Message, text: str):
    try:
        await status_msg.edit_text(text)
    except Exception as e:
        log.warning("Не удалось обновить статус: %r", e)


async def wait_for_telegram():
    """Дожидается связи при старте. Дальше переподключением занимается сам aiogram."""
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.me()
            return
        except TelegramNetworkError as e:
            log.warning("Нет связи с Telegram: %s. Повтор через %s с...", e, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


async def main():
    log.info("Запуск бота...")
    try:
        await wait_for_telegram()
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        log.info("Бот остановлен, сессия закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
