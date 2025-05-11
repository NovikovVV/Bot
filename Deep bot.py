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

# Конфигурация
CONFIG = {
    "SETTINGS_FILE": "settings.json",
    "USERS_FILE": "users.json",
    "LOGS_DIR": "logs",
    "API_URL": "https://api.mexc.com/api/v3",
    "DEFAULT_SETTINGS": {
        "profit_percent": 0.3,
        "drop_percent": 1.0,
        "delay": 30,
        "order_size": 5.0,
        "trading_pair": "BTCUSDT",
        "test_mode": True
    }
}

class MexcTrader:
    def __init__(self):
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
        
        self.setup_files()
        
    def setup_files(self):
        """Инициализация необходимых файлов"""
        Path(CONFIG['LOGS_DIR']).mkdir(exist_ok=True)
        for file in [CONFIG['SETTINGS_FILE'], CONFIG['USERS_FILE']]:
            if not os.path.exists(file):
                with open(file, 'w') as f:
                    json.dump({}, f)
    
    # API методы
    def sign_request(self, secret, params):
        query = urllib.parse.urlencode(params)
        return hmac.new(
            secret.encode('utf-8'),
            query.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def api_request(self, endpoint, params=None, method='GET'):
        url = f"{CONFIG['API_URL']}/{endpoint}"
        headers = {"X-MEXC-APIKEY": self.user['api_key']}
        
        if params is None:
            params = {}
        
        params['timestamp'] = int(time.time() * 1000)
        params['signature'] = self.sign_request(self.user['api_secret'], params)
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, params=params)
            else:
                response = requests.post(url, headers=headers, params=params)
            
            return response.json()
        except Exception as e:
            self.log(f"API Error: {str(e)}")
            return None
    
    # Торговые методы
    def place_order(self, side, price=None):
        """Размещение ордера"""
        if self.settings['test_mode']:
            order_id = f"TEST_{int(time.time())}"
            self.log(f"[TEST] {side} order placed at {price or 'market'}")
            return {'orderId': order_id}
            
        params = {
            'symbol': self.settings['trading_pair'],
            'side': side,
            'type': 'LIMIT' if price else 'MARKET',
            'quantity': self.calculate_quantity(),
            'recvWindow': 5000
        }
        
        if price:
            params['price'] = price
            params['timeInForce'] = 'GTC'
        
        return self.api_request('order', params, 'POST')
    
    def calculate_quantity(self):
        """Расчет количества для ордера"""
        price = self.get_price()
        if not price:
            return 0.0
        return round(self.settings['order_size'] / price, 6)
    
    def get_price(self):
        """Получение текущей цены"""
        params = {'symbol': self.settings['trading_pair']}
        data = self.api_request('ticker/price', params)
        return float(data['price']) if data else None
    
    # Основная логика
    def trading_cycle(self):
        """Основной торговый цикл"""
        self.log("Trading cycle started")
        
        while self.running:
            if self.paused:
                time.sleep(1)
                continue
                
            price = self.get_price()
            if not price:
                time.sleep(5)
                continue
            
            # Первая покупка
            if not self.orders:
                self.buy_and_set_sell(price)
            else:
                # Проверка на снижение цены для докупки
                last_buy = self.orders[-1]['buy_price']
                if price <= last_buy * (1 - self.settings['drop_percent']/100):
                    self.buy_and_set_sell(price)
                
                # Проверка исполнения ордеров на продажу
                self.check_sell_orders(price)
            
            time.sleep(self.settings['delay'])
    
    def buy_and_set_sell(self, price):
        """Покупка и установка ордера на продажу"""
        buy_order = self.place_order('BUY')
        if not buy_order:
            return
            
        sell_price = round(price * (1 + self.settings['profit_percent']/100), 2)
        sell_order = self.place_order('SELL', sell_price)
        
        if sell_order:
            self.orders.append({
                'buy_price': price,
                'sell_price': sell_price,
                'sell_order_id': sell_order['orderId'],
                'timestamp': datetime.now().timestamp()
            })
            self.pnl['active_orders'] = len(self.orders)
            self.log(f"New DCA level: buy@{price}, sell@{sell_price}")
    
    def check_sell_orders(self, current_price):
        """Проверка исполнения ордеров"""
        for order in self.orders[:]:
            if current_price >= order['sell_price']:
                profit = (order['sell_price'] - order['buy_price']) * self.calculate_quantity()
                self.pnl['total_profit'] += profit
                self.pnl['total_trades'] += 1
                self.pnl['history'].append({
                    'buy': order['buy_price'],
                    'sell': order['sell_price'],
                    'profit': profit,
                    'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                self.orders.remove(order)
                self.pnl['active_orders'] = len(self.orders)
                self.log(f"Order executed! Profit: {profit:.2f} USD")
    
    # Управление
    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.trading_cycle)
            self.thread.start()
            self.log("Bot started")
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        self.log("Bot stopped")
    
    def pause(self):
        self.paused = not self.paused
        status = "paused" if self.paused else "resumed"
        self.log(f"Bot {status}")
    
    # Пользовательский интерфейс
    def create_user(self):
        """Создание нового пользователя"""
        username = input("Enter username: ")
        password = getpass("Enter password: ")
        api_key = input("Enter MEXC API key: ")
        api_secret = getpass("Enter MEXC API secret: ")
        pair = input("Enter trading pair (e.g. BTCUSDT): ").upper()
        
        users = self.load_users()
        users[username] = {
            'password': password,
            'api_key': api_key,
            'api_secret': api_secret,
            'pair': pair
        }
        
        with open(CONFIG['USERS_FILE'], 'w') as f:
            json.dump(users, f, indent=2)
        
        # Создаем настройки по умолчанию
        settings = self.load_settings()
        settings[username] = CONFIG['DEFAULT_SETTINGS'].copy()
        settings[username]['trading_pair'] = pair
        
        with open(CONFIG['SETTINGS_FILE'], 'w') as f:
            json.dump(settings, f, indent=2)
        
        self.log(f"User {username} created")
        return username
    
    def login(self):
        """Авторизация пользователя"""
        users = self.load_users()
        
        if not users:
            print("No users found. Creating new one.")
            return self.create_user()
        
        print("Existing users:")
        for user in users:
            print(f"- {user}")
        
        while True:
            username = input("Select username: ")
            if username not in users:
                print("User not found!")
                continue
                
            password = getpass("Enter password: ")
            if password == users[username]['password']:
                self.user = users[username]
                self.settings = self.load_settings()[username]
                return username
            
            print("Wrong password!")
    
    # Утилиты
    def load_users(self):
        with open(CONFIG['USERS_FILE'], 'r') as f:
            return json.load(f)
    
    def load_settings(self):
        with open(CONFIG['SETTINGS_FILE'], 'r') as f:
            return json.load(f)
    
    def update_settings(self, key, value):
        """Обновление настроек"""
        try:
            if key in ['profit_percent', 'drop_percent', 'delay', 'order_size']:
                self.settings[key] = float(value)
            elif key == 'trading_pair':
                self.settings[key] = value.upper()
            elif key == 'test_mode':
                self.settings[key] = value.lower() in ['true', '1', 'yes']
            
            # Сохраняем в файл
            settings = self.load_settings()
            settings[self.current_user] = self.settings
            with open(CONFIG['SETTINGS_FILE'], 'w') as f:
                json.dump(settings, f, indent=2)
            
            self.log(f"Setting updated: {key} = {value}")
            return True
        except Exception as e:
            self.log(f"Error updating setting: {str(e)}")
            return False
    
    def log(self, message):
        """Логирование сообщений"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}"
        
        # Запись в файл
        log_file = Path(CONFIG['LOGS_DIR']) / f"{self.current_user}.log"
        with open(log_file, 'a') as f:
            f.write(log_entry + "\n")
        
        # Вывод в консоль
        print(log_entry)
    
    def show_pnl(self):
        """Отображение PnL статистики"""
        print("\n--- Trading Statistics ---")
        print(f"Total Profit: {self.pnl['total_profit']:.2f} USD")
        print(f"Total Trades: {self.pnl['total_trades']}")
        print(f"Active Orders: {self.pnl['active_orders']}")
        print("-------------------------\n")

# CLI интерфейс
def main():
    bot = MexcTrader()
    bot.current_user = bot.login()
    
    commands = {
        'start': bot.start,
        'stop': bot.stop,
        'pause': bot.pause,
        'pnl': bot.show_pnl,
        'help': lambda: print("\n".join([
            "Available commands:",
            "start - Start trading",
            "stop - Stop trading",
            "pause - Pause/resume trading",
            "set [key] [value] - Change setting",
            "pnl - Show profit/loss stats",
            "help - Show this help",
            "exit - Quit program"
        ]))
    }
    
    print("\nWelcome to MEXC DCA Trading Bot!")
    print("Type 'help' for available commands\n")
    
    while True:
        try:
            cmd = input("> ").strip().lower()
            
            if cmd == 'exit':
                bot.stop()
                break
            elif cmd.startswith('set '):
                parts = cmd.split()
                if len(parts) == 3:
                    bot.update_settings(parts[1], parts[2])
                else:
                    print("Usage: set [key] [value]")
            elif cmd in commands:
                commands[cmd]()
            else:
                print("Unknown command. Type 'help' for available commands")
                
        except KeyboardInterrupt:
            print("\nUse 'exit' command to quit properly")
        except Exception as e:
            print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
