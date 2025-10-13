import os, hashlib, hmac
from app.database import storage
from app.utils import encrypt

def hash_password(password: str):
    salt = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200000)
    return salt.hex() + ':' + pwd_hash.hex()

def verify_password(user_id: int, password: str) -> bool:
    row = storage.get_user_row(user_id)
    if not row:
        return False
    salt_hex, hash_hex = row['pwd_pbkdf2'].split(':')
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200000)
    return hmac.compare_digest(expected, check)

def create_user(username, password, api_key, api_secret, symbol, mode, deposit_pct, deposit_usdt):
    pwd = hash_password(password)
    api_e = encrypt(api_key) if api_key else ''
    sec_e = encrypt(api_secret) if api_secret else ''
    return storage.create_user(username, pwd, api_e, sec_e, symbol, mode, deposit_pct, deposit_usdt)
