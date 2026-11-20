import faulthandler
import hashlib
import math
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

APP_VERSION = "1.1.0"
APP_NAME = "Telegram Cloud Drive"
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
APP_SUBTITLE = "Gerenciador de Upload e Download de arquivos para Telegram"
GITHUB_REPO = "3olinha/Telegram-Cloud-Drive"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UNINSTALL_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TelegramCloudDrive"

# Limite máximo por arquivo no Telegram (2 GB = 4000 partes de 512 KB = 2.000 MiB)
MAX_TELEGRAM_FILE_SIZE = 2000 * 1024 * 1024

COUNTRY_CODES = [
    "+55 (Brasil)",
    "+1 (EUA / Canadá)",
    "+351 (Portugal)",
    "+54 (Argentina)",
    "+595 (Paraguai)",
    "+598 (Uruguai)",
    "+56 (Chile)",
    "+57 (Colômbia)",
    "+52 (México)",
    "+34 (Espanha)",
    "+44 (Reino Unido)",
    "+49 (Alemanha)",
    "+33 (França)",
    "+39 (Itália)",
    "+41 (Suíça)",
    "+81 (Japão)",
]

DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "Bolinha_Telegram_Downloads"
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "$RECYCLE.BIN",
    "System Volume Information",
}

if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    EXE_DIR = Path(sys.executable).parent.resolve()
else:
    BUNDLE_DIR = Path(__file__).resolve().parent.parent
    EXE_DIR = BUNDLE_DIR


def parse_version_tuple(v_str: str) -> Tuple[int, ...]:
    """Converte strings como 'v0.14' ou '1.0.0' em tupla numérica para comparação segura."""
    nums = [int(p) for p in re.findall(r"\d+", str(v_str or ""))]
    return tuple(nums) if nums else (0, 0)


def unblock_windows_file(path: Path):
    """Remove o atributo Zone.Identifier (SmartScreen) via Python puro sem invocar PowerShell."""
    try:
        os.unlink(f"{path}:Zone.Identifier")
    except Exception:
        pass


def get_clean_pyinstaller_env() -> dict:
    """
    Remove variáveis de ambiente temporárias do PyInstaller (_MEIPASS2, _PYI_*, etc.)
    para evitar o erro 'Failed to load Python DLL ... _MEI...\\python312.dll' ao abrir outro .exe.
    """
    clean_env = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if (
            ku.startswith("_MEI")
            or ku.startswith("_PYI")
            or ku in ("TCL_LIBRARY", "TK_LIBRARY", "PYTHONPATH", "PYTHONHOME")
        ):
            continue
        if ku == "PATH":
            parts = [p for p in v.split(os.pathsep) if "_MEI" not in p.upper()]
            v = os.pathsep.join(parts)
        clean_env[k] = v
    return clean_env


def get_windows_desktop_dir() -> Path:
    """Resolve dinamicamente o diretório da Área de Trabalho do usuário sem caminhos hardcoded."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw_val, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = os.path.expandvars(str(raw_val))
            p = Path(expanded)
            if p.is_dir():
                return p
    except Exception:
        pass

    home = Path.home()
    for candidate_rel in ("Desktop", "Área de Trabalho"):
        p = home / candidate_rel
        if p.is_dir():
            return p
    try:
        for child in home.iterdir():
            if child.is_dir() and child.name.lower().startswith("onedrive"):
                for candidate_rel in ("Área de Trabalho", "Desktop"):
                    p = child / candidate_rel
                    if p.is_dir():
                        return p
    except Exception:
        pass
    return home / "Desktop"


def get_machine_fingerprint() -> str:
    """Gera uma assinatura única vinculada exclusivamente a este computador e usuário do Windows."""
    raw = f"{platform.node()}|{os.environ.get('USERNAME', '')}|{os.environ.get('COMPUTERNAME', '')}"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            raw += f"|{guid}"
    except Exception:
        pass
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def get_local_user_data_dir() -> Path:
    """
    Salva a sessão do Telegram e configurações APENAS no computador local (%LOCALAPPDATA%),
    nunca dentro da pasta compartilhada/pendrive.
    """
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    data_dir = base / "BolinhaTelegramUploader" / f"pc_{get_machine_fingerprint()}"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


USER_DATA_DIR = get_local_user_data_dir()
CONFIG_FILE = USER_DATA_DIR / "config.json"
HISTORY_FILE = USER_DATA_DIR / "sent_history.json"
QUEUE_STATE_FILE = USER_DATA_DIR / "queue_state.json"
GROUP_CACHE_FILE = USER_DATA_DIR / "group_cloud_cache.json"
GROUP_CACHE_DB_FILE = USER_DATA_DIR / "group_cloud_cache.db"
LOG_FILE = USER_DATA_DIR / "app_activity.log"
CRASH_LOG_FILE = USER_DATA_DIR / "crash_trace.log"
SESSION_PERSONAL = str(USER_DATA_DIR / "session_personal")

try:
    _crash_fh = open(CRASH_LOG_FILE, "a", encoding="utf-8")
    faulthandler.enable(file=_crash_fh, all_threads=True)
except Exception:
    _crash_fh = None


def append_persistent_log(line: str, level: str = "INFO"):
    """Grava todas as mensagens e erros em disco para diagnóstico mesmo se o programa fechar."""
    try:
        ts = time.strftime("%d/%m/%Y %H:%M:%S")
        formatted = line if line.startswith("[") else f"[{ts}] [{level}] {line}"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted if formatted.endswith("\n") else formatted + "\n")
    except Exception:
        pass


def format_size(num_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def format_eta(seconds: float) -> str:
    """Formata o tempo previsto restante (segundos) em formato legível (ex: 45s, 04m 12s, 01h 20m 05s)."""
    if seconds is None or seconds < 0 or math.isnan(seconds) or math.isinf(seconds):
        return "calculando..."
    sec_int = int(round(seconds))
    if sec_int <= 0:
        return "0s"
    if sec_int < 60:
        return f"{sec_int}s"
    mins, secs = divmod(sec_int, 60)
    if mins < 60:
        return f"{mins:02d}m {secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}h {mins:02d}m {secs:02d}s"


def make_file_signature(path: Path, size: int) -> str:
    return f"{path.name.lower()}::{size}"


def find_resource_path(filename: str) -> Optional[Path]:
    for folder in (BUNDLE_DIR, EXE_DIR, EXE_DIR / "_internal"):
        candidate = folder / filename
        if candidate.exists():
            return candidate
    return None
