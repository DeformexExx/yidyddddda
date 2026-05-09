#!/data/data/com.termux/files/usr/bin/bash

PROJECT_DIR="$HOME/aegis_watchdog"
cd "$PROJECT_DIR"

echo "🛡 [Aegis V13] Оптимизация окружения под Android..."

# 1. Установка системных компиляторов и готовых бинарников
pkg install -y python python-psutil python-cryptography clang make ndk-sysroot rust -y

# 2. Установка легких библиотек через pip
# Используем --no-cache-dir чтобы не забивать память телефона
pip install --no-cache-dir aiogram loguru requests aiohttp

# 3. Удержание системы (Wake Lock)
termux-wake-lock

# 4. Проверка конфига
if [ ! -f "config.json" ]; then
    echo "⚠️ Ошибка: Настрой config.json (токен и ID) перед запуском!"
    exit 1
fi

echo "🚀 Все зависимости в норме. Запуск Aegis..."
python bot.py