import os
import logging
import shutil
import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from config import ACCOUNTS_DIR

logger = logging.getLogger(__name__)

class DirectTDataManager:
    """Прямая работа с tdata папками"""
    
    def __init__(self):
        pass
    
    def scan_tdata_accounts(self) -> List[Dict]:
        """Сканирует tdata папки и проверяет их валидность"""
        logger.info("🔍 Сканируем tdata папки...")
        
        accounts = []
        
        if not ACCOUNTS_DIR.exists():
            logger.warning("❌ Директория accounts не существует")
            ACCOUNTS_DIR.mkdir(exist_ok=True)
            return accounts
        
        # Ищем все возможные tdata папки
        tdata_paths = self.find_all_tdata_paths()
        
        for tdata_path in tdata_paths:
            phone = self.extract_phone_from_tdata(tdata_path)
            
            account_data = {
                'phone': phone,
                'tdata_path': tdata_path,
                'account_folder': tdata_path.parent,
                'session_valid': True
            }
            
            accounts.append(account_data)
            logger.info(f"✅ Найден аккаунт: {phone} -> {tdata_path}")
        
        logger.info(f"📊 Найдено аккаунтов: {len(accounts)}")
        return accounts
    
    def find_all_tdata_paths(self) -> List[Path]:
        """Находит все возможные tdata папки"""
        tdata_paths = []
        
        # Ищем папки с именем tdata
        for tdata_folder in ACCOUNTS_DIR.rglob("tdata"):
            if tdata_folder.is_dir():
                tdata_paths.append(tdata_folder)
        
        # Ищем папки с именем TData (регистрозависимо)
        for tdata_folder in ACCOUNTS_DIR.rglob("TData"):
            if tdata_folder.is_dir():
                tdata_paths.append(tdata_folder)
        
        # Ищем папки аккаунтов, содержащие tdata внутри
        for account_folder in ACCOUNTS_DIR.iterdir():
            if account_folder.is_dir():
                tdata_path = account_folder / "tdata"
                if tdata_path.exists() and tdata_path.is_dir():
                    tdata_paths.append(tdata_path)
                
                tdata_path2 = account_folder / "TData"
                if tdata_path2.exists() and tdata_path2.is_dir():
                    tdata_paths.append(tdata_path2)
        
        # Убираем дубликаты
        return list(set(tdata_paths))
    
    def extract_phone_from_tdata(self, tdata_path: Path) -> str:
        """Извлекает номер телефона из tdata"""
        try:
            # Пробуем прочитать номер из map.csv
            map_file = tdata_path / "map.csv"
            if map_file.exists():
                with open(map_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if 'phone' in line.lower():
                            parts = line.split(',')
                            if len(parts) > 1:
                                phone = parts[1].strip().strip('"')
                                if phone and phone != "0":
                                    return phone
            
            # Используем имя родительской папки
            parent_name = tdata_path.parent.name
            if parent_name.startswith('+') or (any(c.isdigit() for c in parent_name) and len(parent_name) > 5):
                return parent_name
            
            # Генерируем уникальный ID
            return f"account_{hash(str(tdata_path)) % 10000:04d}"
            
        except Exception as e:
            logger.warning(f"⚠️ Не удалось извлечь номер из {tdata_path}: {e}")
            return f"account_{hash(str(tdata_path)) % 10000:04d}"
    
    def validate_tdata_structure(self, tdata_path: Path) -> bool:
        """Проверяет базовую структуру tdata"""
        try:
            # Проверяем наличие ключевых файлов/папок
            required_items = [
                "D877F783D5D3EF8C",  # Основная папка с сессией
                "map.csv",            # Файл с маппингом
            ]
            
            for item in required_items:
                item_path = tdata_path / item
                if not item_path.exists():
                    # Проверяем рекурсивно
                    found = False
                    for found_item in tdata_path.rglob(item):
                        if found_item.exists():
                            found = True
                            break
                    if not found:
                        logger.warning(f"⚠️ Не найден: {item} в {tdata_path}")
                        return False
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка проверки tdata: {e}")
            return False