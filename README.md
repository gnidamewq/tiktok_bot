# TikTok Downloader Bot

Простой Telegram-бот на Python (aiogram 3), который скачивает видео и фото-карусели из TikTok без водяных знаков.

## Что умеет

- Скачивает видео без вотермарки (до 50 МБ — лимит Telegram Bot API).
- Загружает фото-посты: одно фото присылает отдельно, длинные карусели (до 35 фото) — несколькими альбомами по 10.
- Сам переподключается при обрывах связи, в том числе если сети нет на старте. Корректно останавливается по Ctrl+C и SIGTERM.
- Обрабатывает до 3 ссылок одновременно, остальные ждут очереди.
- Понимает разные ссылки: обычные, мобильные (`vt.tiktok.com`, `vm.tiktok.com`), ссылки из подписей к пересланным сообщениям, спрятанные под текстом и даже если в начале ссылки случайно оказался пробел (`h ttps://`).
- В группах отвечает только на сообщения со ссылкой на TikTok.

## Ограничения

Бот скачивает через сторонние сервисы: основной — ssstik.io, запасной — tikwm.com. У них нет официального API для такого использования, поэтому изменения на их стороне могут сломать скачивание. Причины сбоев пишутся в лог с уровнем `WARNING`. Ссылки пользователей передаются этим сервисам.

## Быстрый старт

1. Клонируйте репозиторий:
   ```bash
   git clone https://github.com/gnidamewq/tiktok_bot.git
   cd tiktok_bot
   ```

2. Установите зависимости:
   ```bash
   python -m venv .venv
   # Активируйте окружение (в Windows: .venv\Scripts\activate, в Linux/macOS: source .venv/bin/activate)

   pip install -r requirements.txt
   ```

3. Настройте токен бота:
   Создайте файл `.env` (можно скопировать из `.env.example`) и вставьте туда токен от [@BotFather](https://t.me/BotFather):
   ```env
   BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
   ```

4. Запустите:
   ```bash
   python bot.py
   ```

## Стек

- Python 3.10+
- [aiogram 3](https://github.com/aiogram/aiogram)
- [aiohttp](https://github.com/aio-libs/aiohttp)
- [python-dotenv](https://github.com/theskumar/python-dotenv)
