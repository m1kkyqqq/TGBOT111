import os
from pathlib import Path

# Настройки бота
BOT_TOKEN = "8044566206:AAEifWWSHGYlacy2RfWn1C3-HKjKyHozxVE"
ADMIN_IDS = [992251837]

# Пути
BASE_DIR = Path(__file__).parent
ACCOUNTS_DIR = BASE_DIR / "accounts"
PROXIES_FILE = BASE_DIR / "proxies.txt"
DB_FILE = BASE_DIR / "premium_bot.db"
LOGS_DIR = BASE_DIR / "logs"
ARCHIVES_DIR = BASE_DIR / "archives"

# Создаем директории
ACCOUNTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
ARCHIVES_DIR.mkdir(exist_ok=True)

# Настройки Telegram API
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# Настройки обработки
DELAY_BETWEEN_ACCOUNTS = 30
MAX_RETRIES = 3
BANK_CONFIRMATION_DELAY = 30