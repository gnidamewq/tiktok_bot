import asyncio
import logging
import os
import re
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramNetworkError
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

TIKTOK_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|vm\.|vt\.|t\.)?tiktok\.com/[a-zA-Z0-9_\-/?&=@.%]+",
    re.IGNORECASE
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


async def download_from_ssstik(session: aiohttp.ClientSession, url: str):
    """Качает видео или галерею фото через ssstik.io."""
    try:
        async with session.get("https://ssstik.io/en") as resp:
            if resp.status != 200:
                return None
            html = await resp.text()

        token_match = re.search(r's_tt\s*=\s*[\x22\x27]([^\x22\x27]+)', html)
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
                return None
            post_html = await resp.text()

        title = ""
        desc_match = re.search(r'<p[^>]*class=[\x22\x27]maintext[\x22\x27][^>]*>(.*?)</p>', post_html, re.DOTALL)
        if desc_match:
            title = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()

        # Ищем кнопку скачивания видео без водяного знака
        video_url = None
        v_match = re.search(
            r'<a[^>]+href=[\x22\x27](https://tikcdn\.io/ssstik/[^\x22\x27]+)[\x22\x27][^>]*class=[\x22\x27][^\x22\x27]*without_watermark[^\x22\x27]*[\x22\x27]',
            post_html
        )
        if v_match:
            video_url = v_match.group(1)
        else:
            for m in re.finditer(r'<a[^>]+href=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</a>', post_html, re.DOTALL):
                href, text = m.group(1), m.group(2)
                if "Without watermark" in text and href.startswith("http"):
                    video_url = href
                    break

        if not video_url:
            tikcdn_match = re.search(r'href=[\x22\x27](https://tikcdn\.io/ssstik/[^\x22\x27]+)[\x22\x27]', post_html)
            if tikcdn_match:
                video_url = tikcdn_match.group(1)

        if video_url:
            async with session.get(video_url, headers={"Referer": "https://ssstik.io/"}) as v_resp:
                if v_resp.status == 200:
                    video_bytes = await v_resp.read()
                    return {"type": "video", "data": video_bytes, "title": title}

        # Если видео нет, проверяем фото-карусель
        photo_urls = re.findall(
            r'<a[^>]+href=[\x22\x27](https://tikcdn\.io/ssstik/[^\x22\x27]+)[\x22\x27][^>]*class=[\x22\x27](?!.*(?:without_watermark|music)).*?[\x22\x27]',
            post_html
        )
        if photo_urls:
            images = []
            for p_url in photo_urls[:10]:
                try:
                    async with session.get(p_url, headers={"Referer": "https://ssstik.io/"}) as img_resp:
                        if img_resp.status == 200:
                            images.append(await img_resp.read())
                except Exception as e:
                    log.warning(f"Не удалось скачать фото {p_url}: {e}")
            if images:
                return {"type": "images", "items": images, "title": title}

    except Exception as e:
        log.error(f"Ошибка парсинга ssstik: {e}")

    return None


async def download_from_tikwm(session: aiohttp.ClientSession, url: str):
    """Фоллбек через tikwm.com, если основной парсер не ответил."""
    try:
        async with session.post("https://www.tikwm.com/api/", data={"url": url}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        if data.get("code") != 0:
            return None

        media_data = data.get("data", {})
        title = media_data.get("title", "")
        images = media_data.get("images")

        if images and isinstance(images, list):
            downloaded = []
            for img_url in images[:10]:
                async with session.get(img_url) as r:
                    if r.status == 200:
                        downloaded.append(await r.read())
            if downloaded:
                return {"type": "images", "items": downloaded, "title": title}

        video_url = media_data.get("play")
        if video_url:
            async with session.get(video_url) as r:
                if r.status == 200:
                    return {"type": "video", "data": await r.read(), "title": title}
    except Exception as e:
        log.debug(f"Фоллбек tikwm вернул ошибку: {e}")

    return None


async def download_tiktok(url: str):
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        media = await download_from_ssstik(session, url)
        if media:
            return media
        return await download_from_tikwm(session, url)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Кидай ссылку на TikTok — скачаю видео или фото-пост без водяных знаков."
    )


@dp.message(F.text | F.caption)
async def handle_link(message: types.Message):
    raw_text = message.text or message.caption or ""
    text = re.sub(r'h\s+ttps?://', 'https://', raw_text)

    match = TIKTOK_URL_RE.search(text)
    if not match:
        if message.text and not message.text.startswith("/"):
            await message.answer("Не похоже на ссылку на TikTok.")
        return

    url = match.group(0)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    status_msg = await message.answer("Качаю, секунду...")

    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_video")
        media = await download_tiktok(url)

        if not media:
            await status_msg.edit_text(
                "Не получилось скачать. Проверь ссылку: возможно, видео приватное или удалено."
            )
            return

        caption = media["title"][:1000] if media["title"] else None

        if media["type"] == "video":
            video_bytes = media["data"]
            if len(video_bytes) > 50 * 1024 * 1024:
                await status_msg.edit_text(
                    "Файл больше 50 МБ. Телеграм не разрешает ботам отправлять такие тяжелые видео."
                )
                return

            await message.answer_video(
                video=BufferedInputFile(video_bytes, filename="tiktok.mp4"),
                caption=caption
            )

        elif media["type"] == "images":
            group = [
                InputMediaPhoto(
                    media=BufferedInputFile(img, filename=f"photo_{i}.jpg"),
                    caption=caption if i == 0 else None
                )
                for i, img in enumerate(media["items"])
            ]
            await message.answer_media_group(media=group)

        await status_msg.delete()

    except Exception as e:
        log.error(f"Ошибка при обработке {url}: {e}", exc_info=True)
        try:
            await status_msg.edit_text("Что-то пошло не так при отправке. Попробуй еще раз.")
        except Exception:
            pass


async def main():
    log.info("Запуск бота...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        while True:
            try:
                await dp.start_polling(bot)
            except (TelegramNetworkError, aiohttp.ClientError) as e:
                log.warning(f"Сетевой сбой: {e}. Переподключение через 5 секунд...")
                await asyncio.sleep(5)
    finally:
        await bot.session.close()
        log.info("Бот остановлен, сессия закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
