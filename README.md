# TikTok Downloader Bot

Простой Telegram-бот на Python (aiogram 3), который скачивает видео и фото-карусели из TikTok без водяных знаков.

## Что умеет

- Скачивает видео без вотермарки в исходном качестве (до 50 МБ — лимит Telegram Bot API).
- Загружает фото-карусели и присылает их единым альбомом.
- Не падает при обрывах связи и таймаутах сети (автоматический реконнект).
- Понимает разные ссылки: обычные, мобильные (`vt.tiktok.com`, `vm.tiktok.com`), ссылки из подписей к пересланным сообщениям и даже если в начале ссылки случайно оказался пробел (`h ttps://`).

## Быстрый старт

1. Клонируйте репозиторий:
   ```bash
   git clone https://github.com/your-username/tiktok-downloader-bot.git
   cd tiktok-downloader-bot
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
