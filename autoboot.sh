#!/data/data/com.termux/files/usr/bin/bash

# Aegis V13: Скрипт автоматического деплоя и запуска
PROJECT_DIR="$HOME/aegis_watchdog"

echo "🛡 [Aegis V13] Начинаю установку системы..."

# 1. Обновление пакетов Termux
pkg update -y && pkg upgrade -y
pkg install -y python git sqlite tsudo termux-api

# 2. Подготовка директории
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# 3. Установка зависимостей
echo "📦 Установка библиотек Python..."
pip install --upgrade pip
pip install aiogram loguru psutil requests aiohttp

# 4. Настройка прав (Wake Lock, чтобы Android не спал)
termux-wake-lock

# 5. Запуск
echo "🚀 Запуск Aegis Watchdog..."
python bot.py