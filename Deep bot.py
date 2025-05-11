#!/usr/bin/env python3
import os
import json
import time
import threading
import hmac
import hashlib
import urllib.parse
from getpass import getpass
from datetime import datetime
from pathlib import Path
import requests
from cryptography.fernet import Fernet

# ==================== КОНФИГУРАЦИЯ ====================
CONFIG = {
    "SETTINGS_FILE": "settings.json",
    "USERS_FILE": "users.json",
    "LOGS_DIR": "logs",
    "KEY_FILE": ".secret.key",
    "API_URL": "https://api.mexc.com/api/v3",
    "DEFAULT_SETTINGS": {
        "profit_percent": 0.3,    # Процент прибыли (0.3%)
        "drop_percent": 1.0,      # Процент для докупки (1%)
        "delay": 30,              # Задержка между проверками (сек)
        "order_size": 5.0,        # Размер ордера ($)
        "trading_pair": "BTCUSDT",
        "test_mode": True         # Тестовый режим без реальных сделок
    }
}

# ==================== ШИФРОВАНИЕ ====================
class CryptoManager:
    def __init__(self):
        self.key_file = CONFIG['KEY_FILE']
        self._ensure_key_exists()

    def _ensure_key_exists(self):
        if not os.path.exists(self.key_file):
            key = Fernet.generate_key()
            with open(self.key_file, 'wb') as f:
                f.write(key)
            os.chmod(self.key_file, 0o600)

    def _load_key(self):
        with open(self.key_file, 'rb') as f:
            return f.read()

    def encrypt(self, data):
        if not data: return None
        return Fernet(self._load_key()).encrypt(data.encode()).decode()

    def decrypt(self, encrypted_data):
        if not encrypted_data: return None
        return Fernet(self._load_key()).decrypt(encrypted_data.encode()).decode()

# ==================== ОСНОВНОЙ БОТ ====================
class MexcTrader:
    def __init__(self):
        self.crypto = CryptoManager()
        self.running = False
        self.paused = False
        self.thread = None
        self.orders = []
        self.pnl = {
            "total_profit": 0.0,
            "total_trades": 0,
            "active_orders": 0,
            "history": []
        }
        self.current_user = None
        self.setup_files()

    def setup_files(self):
        """Инициализация файлов"""
        Path(CONFIG['LOGS_DIR']).mkdir(exist_ok=True)
        for file in [CONFIG['SETTINGS_FILE'], CONFIG['USERS_FILE']]:
            if not os.path.exists(file):
                with open(file, 'w') as f:
                    json.dump({}, f)
                os.chmod(file, 0o600)

    # ============ ТОРГОВАЯ ЛОГИКА ============
    def trading_cycle(self):
        """Главный торговый цикл"""
        self.log("Торговый цикл запущен")
        
        while self.running:
            if self.paused:
                time.sleep(1)
                continue
                
            # 1. Получаем текущую цену
            price = self.get_price()
            if not price:
                time.sleep(5)
                continue
            
            # 2. Первая покупка (если нет активных ордеров)
            if not self.orders:
                self.buy_and_set_sell(price)
            else:
                # 3. Проверка на падение цены для докупки
                last_buy = self.orders[-1]['buy_price']
                if price <= last_buy * (1 - self.settings['drop_percent']/100):
                    self.buy_and_set_sell(price)
                
                # 4. Проверка исполнения ордеров
                self.check_sell_orders(price)
            
            time.sleep(self.settings['delay'])

    def buy_and_set_sell(self, price):
        """Покупка + установка тейк-профита"""
        # 1. Покупаем по рынку
        buy_order = self.place_order('BUY')
        if not buy_order: return
        
        # 2. Вычисляем цену продажи
        sell_price = round(price * (1 + self.settings['profit_percent']/100), 2)
        
        # 3. Выставляем лимитный ордер на продажу
        sell_order = self.place_order('SELL', sell_price)
        
        if sell_order:
            self.orders.append({
                'buy_price': price,
                'sell_price': sell_price,
                'sell_order_id': sell_order['orderId'],
                'timestamp': datetime.now().timestamp()
            })
            self.pnl['active_orders'] = len(self.orders)
            self.log(f"Покупка: {price:.2f} → Продажа: {sell_price:.2f}")

    def check_sell_orders(self, current_price):
        """Проверка исполнения ордеров"""
        for order in list(self.orders):
            if current_price >= order['sell_price']:
                # Расчет прибыли
                quantity = self.settings['order_size'] / order['buy_price']
                profit = (order['sell_price'] - order['buy_price']) * quantity
                
                # Обновляем статистику
                self.pnl['total_profit'] += profit
                self.pnl['total_trades'] += 1
                self.pnl['history'].append({
                    'buy': order['buy_price'],
                    'sell': order['sell_price'],
                    'profit': profit,
                    'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                
                # Удаляем исполненный ордер
                self.orders.remove(order)
                self.pnl['active_orders'] = len(self.orders)
                self.log(f"Ордер исполнен! Прибыль: {profit:.2f}$")

    # ============ API МЕТОДЫ ============
    def place_order(self, side, price=None):
        """Отправка ордера на биржу"""
        if self.settings['test_mode']:
            order_id = f"TEST_{int(time.time())}"
            self.log(f"[ТЕСТ] {side} ордер по {price or 'рынку'}")
            return {'orderId': order_id}
            
        params = {
            'symbol': self.settings['trading_pair'],
            'side': side,
            'type': 'LIMIT' if price else 'MARKET',
            'quantity': round(self.settings['order_size'] / self.get_price(), 6),
            'timestamp': int(time.time() * 1000)
        }
        
        if price:
            params['price'] = price
            params['timeInForce'] = 'GTC'
        
        params['signature'] = hmac.new(
            self.user['api_secret'].encode(),
            urllib.parse.urlencode(params).encode(),
            hashlib.sha256
        ).hexdigest()
        
        headers = {"X-MEXC-APIKEY": self.user['api_key']}
        response = requests.post(
            f"{CONFIG['API_URL']}/order",
            headers=headers,
            params=params
        )
        return response.json()

    def get_price(self):
        """Получение текущей цены"""
        params = {'symbol': self.settings['trading_pair']}
        data = requests.get(f"{CONFIG['API_URL']}/ticker/price", params=params).json()
        return float(data['price']) if 'price' in data else None

    # ============ УПРАВЛЕНИЕ ============
    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.trading_cycle)
            self.thread.start()
            self.log("Бот запущен")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        self.log("Бот остановлен")

    def pause(self):
        self.paused = not self.paused
        self.log(f"Бот {'на паузе' if self.paused else 'возобновил работу'}")

    # ============ ПОЛЬЗОВАТЕЛИ ============
    def create_user(self):
        """Создание нового пользователя с шифрованием"""
        username = input("Имя пользователя: ")
        password = getpass("Пароль: ")
        api_key = input("API ключ MEXC: ")
        api_secret = getpass("API секрет: ")
        pair = input("Торговая пара (например BTCUSDT): ").upper()

        users = self.load_users()
        users[username] = {
            'password': self.crypto.encrypt(password),
            'api_key': self.crypto.encrypt(api_key),
            'api_secret': self.crypto.encrypt(api_secret),
            'pair': pair
        }
        self.save_users(users)
        
        # Инициализация настроек
        settings = self.load_settings()
        settings[username] = CONFIG['DEFAULT_SETTINGS'].copy()
        settings[username]['trading_pair'] = pair
        self.save_settings(settings)
        
        self.log(f"Создан пользователь: {username}")
        return username

    def login(self):
        """Авторизация с проверкой зашифрованного пароля"""
        users = self.load_users()
        if not users:
            print("Нет пользователей. Создаем нового.")
            return self.create_user()
        
        print("Существующие пользователи:")
        for user in users:
            print(f"- {user}")
        
        while True:
            username = input("Выберите пользователя: ")
            if username not in users:
                print("Ошибка: пользователь не найден")
                continue
                
            password = getpass("Пароль: ")
            stored_password = self.crypto.decrypt(users[username]['password'])
            
            if password == stored_password:
                self.user = {
                    'username': username,
                    'api_key': self.crypto.decrypt(users[username]['api_key']),
                    'api_secret': self.crypto.decrypt(users[username]['api_secret'])
                }
                self.current_user = username
                self.settings = self.load_settings()[username]
                return username
            
            print("Неверный пароль!")

    # ============ УТИЛИТЫ ============
    def log(self, message):
        """Логирование в файл и консоль"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}"
        
        log_file = Path(CONFIG['LOGS_DIR']) / f"{self.current_user}.log"
        with open(log_file, 'a') as f:
            f.write(log_entry + "\n")
        
        print(log_entry)

    def show_pnl(self):
        """Отображение статистики"""
        print("\n=== Торговая статистика ===")
        print(f"Общая прибыль: {self.pnl['total_profit']:.2f}$")
        print(f"Сделок: {self.pnl['total_trades']}")
        print(f"Активных ордеров: {self.pnl['active_orders']}")
        print("="*30)

    def load_users(self):
        with open(CONFIG['USERS_FILE'], 'r') as f:
            return json.load(f)

    def save_users(self, data):
        with open(CONFIG['USERS_FILE'], 'w') as f:
            json.dump(data, f, indent=2)

    def load_settings(self):
        with open(CONFIG['SETTINGS_FILE'], 'r') as f:
            return json.load(f)

    def save_settings(self, data):
        with open(CONFIG['SETTINGS_FILE'], 'w') as f:
            json.dump(data, f, indent=2)

    def update_setting(self, key, value):
        """Обновление настроек без перезапуска"""
        try:
            if key in ['profit_percent', 'drop_percent', 'delay', 'order_size']:
                self.settings[key] = float(value)
            elif key == 'trading_pair':
                self.settings[key] = value.upper()
            elif key == 'test_mode':
                self.settings[key] = value.lower() in ['true', '1', 'yes']
            
            settings = self.load_settings()
            settings[self.current_user] = self.settings
            self.save_settings(settings)
            self.log(f"Настройка изменена: {key} = {value}")
            return True
        except Exception as e:
            self.log(f"Ошибка: {str(e)}")
            return False

# ==================== CLI ====================
def main():
    bot = MexcTrader()
    bot.current_user = bot.login()
    
    commands = {
        'start': bot.start,
        'stop': bot.stop,
        'pause': bot.pause,
        'pnl': bot.show_pnl,
        'help': lambda: print(
            "Доступные команды:\n"
            "start - Запуск бота\n"
            "stop - Остановка\n"
            "pause - Пауза\n"
            "set [ключ] [значение] - Изменить настройку\n"
            "pnl - Статистика\n"
            "help - Справка\n"
            "exit - Выход"
        )
    }
    
    print("\n=== MEXC DCA Trading Bot ===")
    print("Введите 'help' для списка команд\n")
    
    while True:
        try:
            cmd = input("> ").strip().lower()
            
            if cmd == 'exit':
                bot.stop()
                break
            elif cmd.startswith('set '):
                parts = cmd.split(maxsplit=2)
                if len(parts) == 3:
                    bot.update_setting(parts[1], parts[2])
                else:
                    print("Использование: set [ключ] [значение]")
            elif cmd in commands:
                commands[cmd]()
            else:
                print("Неизвестная команда. Введите 'help'")
                
        except KeyboardInterrupt:
            print("\nДля выхода введите 'exit'")
        except Exception as e:
            print(f"Ошибка: {str(e)}")

if __name__ == "__main__":
    main()
