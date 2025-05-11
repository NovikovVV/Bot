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
import base64

# ==================== КОНФИГУРАЦИЯ ====================
CONFIG = {
    "SETTINGS_FILE": "settings.json",
    "USERS_FILE": "users.json",
    "LOGS_DIR": "logs",
    "KEY_FILE": "secret.key",
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
            os.chmod(self.key_file, 0o600)  # Только владелец может читать/писать

    def _load_key(self):
        with open(self.key_file, 'rb') as f:
            return f.read()

    def encrypt(self, data):
        if not data:
            return None
        fernet = Fernet(self._load_key())
        return fernet.encrypt(data.encode()).decode()

    def decrypt(self, encrypted_data):
        if not encrypted_data:
            return None
        fernet = Fernet(self._load_key())
        return fernet.decrypt(encrypted_data.encode()).decode()

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
        """Инициализация необходимых файлов"""
        Path(CONFIG['LOGS_DIR']).mkdir(exist_ok=True)
        for file in [CONFIG['SETTINGS_FILE'], CONFIG['USERS_FILE']]:
            if not os.path.exists(file):
                with open(file, 'w') as f:
                    json.dump({}, f)
                os.chmod(file, 0o600)

    # ... (Все остальные методы из предыдущей реализации, но с заменой)
    # везде где работаем с паролями/ключами используем self.crypto.encrypt/decrypt

    def create_user(self):
        """Создание нового пользователя с шифрованием данных"""
        username = input("Enter username: ")
        password = getpass("Enter password: ")
        api_key = input("Enter MEXC API key: ")
        api_secret = getpass("Enter MEXC API secret: ")
        pair = input("Enter trading pair (e.g. BTCUSDT): ").upper()

        users = self.load_users()
        users[username] = {
            'password': self.crypto.encrypt(password),
            'api_key': self.crypto.encrypt(api_key),
            'api_secret': self.crypto.encrypt(api_secret),
            'pair': pair
        }

        self.save_users(users)
        self.init_user_settings(username, pair)
        self.log(f"User {username} created successfully")
        return username

    def login(self):
        """Авторизация с проверкой зашифрованного пароля"""
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
            stored_password = self.crypto.decrypt(users[username]['password'])
            
            if password == stored_password:
                self.user = {
                    'username': username,
                    'api_key': self.crypto.decrypt(users[username]['api_key']),
                    'api_secret': self.crypto.decrypt(users[username]['api_secret']),
                    'pair': users[username]['pair']
                }
                self.current_user = username
                self.settings = self.load_settings().get(username, CONFIG['DEFAULT_SETTINGS'])
                return username
            
            print("Wrong password!")

    # ... (Остальные методы класса MexcTrader)

# ==================== ЗАПУСК ====================
def main():
    bot = MexcTrader()
    bot.login()
    
    print("\n=== MEXC DCA Trading Bot ===")
    print("Type 'help' for commands\n")
    
    while True:
        try:
            cmd = input("> ").strip().lower()
            
            if cmd == 'exit':
                bot.stop()
                break
            elif cmd.startswith('set '):
                parts = cmd.split(maxsplit=2)
                if len(parts) == 3:
                    bot.update_settings(parts[1], parts[2])
            elif cmd == 'start':
                bot.start()
            elif cmd == 'stop':
                bot.stop()
            elif cmd == 'pause':
                bot.pause()
            elif cmd == 'pnl':
                bot.show_pnl()
            elif cmd == 'help':
                print_help()
            else:
                print("Unknown command. Type 'help'")
                
        except KeyboardInterrupt:
            print("\nUse 'exit' command to quit properly")
        except Exception as e:
            print(f"Error: {str(e)}")

def print_help():
    print("\nAvailable commands:")
    print("start    - Start trading")
    print("stop     - Stop trading")
    print("pause    - Pause/resume trading")
    print("set [key] [value] - Change setting")
    print("pnl      - Show profit/loss stats")
    print("help     - Show this help")
    print("exit     - Quit program\n")

if __name__ == "__main__":
    main()
