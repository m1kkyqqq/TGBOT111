import logging
from pathlib import Path
from typing import Dict, List, Optional

from config import ACCOUNTS_DIR

logger = logging.getLogger(__name__)


class DirectTDataManager:
    """Прямая работа с tdata папками."""

    def __init__(self) -> None:
        ACCOUNTS_DIR.mkdir(exist_ok=True)

    def scan_tdata_accounts(self) -> List[Dict]:
        """Сканирует tdata папки и собирает информацию об аккаунтах."""
        logger.info("🔍 Сканируем tdata папки...")

        accounts: List[Dict] = []

        if not ACCOUNTS_DIR.exists():
            logger.warning("❌ Директория accounts не существует")
            return accounts

        for tdata_path in self.find_all_tdata_paths():
            if not self.validate_tdata_structure(tdata_path):
                logger.warning("⚠️ Пропускаем tdata без ключевых файлов: %s", tdata_path)
                continue

            phone = self.extract_phone_from_tdata(tdata_path)
            proxy = self.extract_proxy_from_folder(tdata_path.parent)

            account_data = {
                "phone": phone,
                "tdata_path": tdata_path,
                "account_folder": tdata_path.parent,
                "session_valid": True,
                "proxy": proxy,
            }

            accounts.append(account_data)
            logger.info("✅ Найден аккаунт: %s -> %s", phone, tdata_path)

        logger.info("📊 Найдено аккаунтов: %s", len(accounts))
        return accounts

    def find_all_tdata_paths(self) -> List[Path]:
        """Находит все возможные tdata папки."""
        tdata_paths: List[Path] = []

        for tdata_folder in ACCOUNTS_DIR.rglob("tdata"):
            if tdata_folder.is_dir():
                tdata_paths.append(tdata_folder)

        for tdata_folder in ACCOUNTS_DIR.rglob("TData"):
            if tdata_folder.is_dir():
                tdata_paths.append(tdata_folder)

        for account_folder in ACCOUNTS_DIR.iterdir():
            if account_folder.is_dir():
                for candidate in ("tdata", "TData"):
                    tdata_path = account_folder / candidate
                    if tdata_path.exists() and tdata_path.is_dir():
                        tdata_paths.append(tdata_path)

        # Убираем дубликаты
        return list({path.resolve() for path in tdata_paths})

    def extract_phone_from_tdata(self, tdata_path: Path) -> str:
        """Извлекает номер телефона из файлов tdata."""
        try:
            map_file = tdata_path / "map.csv"
            if map_file.exists():
                with open(map_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if "phone" in line.lower():
                            parts = line.split(",")
                            if len(parts) > 1:
                                phone = parts[1].strip().strip('"')
                                if phone and phone != "0":
                                    return phone

            parent_name = tdata_path.parent.name
            if parent_name.startswith("+") or (any(c.isdigit() for c in parent_name) and len(parent_name) > 5):
                return parent_name

            return f"account_{hash(str(tdata_path)) % 10000:04d}"
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("⚠️ Не удалось извлечь номер из %s: %s", tdata_path, exc)
            return f"account_{hash(str(tdata_path)) % 10000:04d}"

    def extract_proxy_from_folder(self, folder: Path) -> Optional[str]:
        """Читает прокси из файлов внутри папки аккаунта."""
        proxy_files = ["proxy.txt", "proxies.txt", "proxy"]
        for file_name in proxy_files:
            file_path = folder / file_name
            if file_path.exists() and file_path.is_file():
                try:
                    with open(file_path, "r", encoding="utf-8") as handler:
                        for line in handler:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                return line
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning("⚠️ Не удалось прочитать прокси из %s: %s", file_path, exc)
        return None

    def validate_tdata_structure(self, tdata_path: Path) -> bool:
        """Проверяет базовую структуру tdata."""
        try:
            required_items = [
                "D877F783D5D3EF8C",
                "map.csv",
            ]

            for item in required_items:
                item_path = tdata_path / item
                if item_path.exists():
                    continue

                if any(found_item.exists() for found_item in tdata_path.rglob(item)):
                    continue

                logger.warning("⚠️ Не найден %s в %s", item, tdata_path)
                return False

            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("❌ Ошибка проверки tdata %s: %s", tdata_path, exc)
            return False
