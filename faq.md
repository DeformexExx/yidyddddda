# Aegis V13 Watchdog — Termux FAQ (TeleBot / Zero-Compile)

## Migration commands
```bash
pip uninstall aiogram aiohttp -y
pip install pyTelegramBotAPI loguru psutil requests
python bot.py
```

## Full fresh setup (recommended)
```bash
pkg update -y && pkg upgrade -y
pkg install -y python git sqlite nano
git clone https://github.com/DeformexExx/yidyddddda ~/aegis_watchdog
cd aegis_watchdog
pip install --upgrade pip
pip install -r requirements.txt
nano config.json
python bot.py
```

## Notes
- Bot engine now uses [`telebot.TeleBot`](bot.py).
- Monitor loops are executed in a background thread with async tasks from [`SystemMonitor`](core/monitor.py:16).
- Remote update is available via [`/update`](bot.py) and restarts process automatically.


cd ~/aegis_watchdog && git pull && bash autoboot.sh

