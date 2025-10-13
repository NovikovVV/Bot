from pathlib import Path
from cryptography.fernet import Fernet
BASE = Path(__file__).resolve().parent
DATA = BASE / 'data'
DATA.mkdir(exist_ok=True)
KEY = DATA / 'encryption.key'
if not KEY.exists():
    KEY.write_bytes(Fernet.generate_key())
FERNET = Fernet(KEY.read_bytes())

def encrypt(s: str) -> str:
    if not s:
        return ''
    return FERNET.encrypt(s.encode()).decode()

def decrypt(s: str) -> str:
    if not s:
        return ''
    return FERNET.decrypt(s.encode()).decode()
