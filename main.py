import asyncio
import ctypes
import faulthandler
import hashlib
import json
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Set, Tuple, Union

import customtkinter as ctk
import qrcode
from PIL import Image, ImageDraw
from telethon import TelegramClient, errors, helpers, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.custom.inputsizedfile import InputSizedFile
from telethon.tl.functions import InitConnectionRequest, InvokeWithLayerRequest
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.functions.upload import (
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import (
    DocumentAttributeFilename,
    InputFile,
    InputFileBig,
    InputMessagesFilterDocument,
    MessageMediaDocument,
    MessageMediaPhoto,
)

try:
    import cryptg  # Acelerador nativo em C/Rust para criptografia AES-IGE do Telethon
except Exception:
    cryptg = None

# Configurações de aparência do CustomTkinter
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

APP_VERSION = "0.14"
APP_NAME = "Telegram Cloud Drive"
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
APP_SUBTITLE = "Gerenciador de Upload e Download de arquivos para Telegram"
GITHUB_REPO = "3olinha/Telegram-Cloud-Drive"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UNINSTALL_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TelegramCloudDrive"

# Limite máximo por arquivo no Telegram (2 GB = 4000 partes de 512 KB = 2.000 MiB)
MAX_TELEGRAM_FILE_SIZE = 2000 * 1024 * 1024


def parse_version_tuple(v_str: str) -> Tuple[int, ...]:
    """Converte strings como 'v0.10' ou '0.09' em tupla numérica (0, 10) para comparação segura."""
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

# Diretório onde estão os recursos do app (.ico / .png)
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    EXE_DIR = Path(sys.executable).parent.resolve()
else:
    BUNDLE_DIR = Path(__file__).parent.resolve()
    EXE_DIR = BUNDLE_DIR


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
LOG_FILE = USER_DATA_DIR / "app_activity.log"
CRASH_LOG_FILE = USER_DATA_DIR / "crash_trace.log"
SESSION_PERSONAL = str(USER_DATA_DIR / "session_personal")

try:
    _crash_fh = open(CRASH_LOG_FILE, "a", encoding="utf-8")
    faulthandler.enable(file=_crash_fh, all_threads=True)
except Exception:
    pass


def append_persistent_log(line: str, level: str = "INFO"):
    """Grava todas as mensagens e erros em disco para diagnóstico mesmo se o programa fechar."""
    try:
        ts = time.strftime("%d/%m/%Y %H:%M:%S")
        formatted = line if line.startswith("[") else f"[{ts}] [{level}] {line}"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted if formatted.endswith("\n") else formatted + "\n")
    except Exception:
        pass


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


# -----------------------------------------------------------------------------
# Gerador de Ícones Gráficos Nítidos (Anti-Aliased 4x Supersampling)
# -----------------------------------------------------------------------------
def _create_nav_icons() -> Dict[str, ctk.CTkImage]:
    size = 128  # Desenha em 128x128 e reduz para 28x28 com LANCZOS para bordas perfeitas
    display_size = (28, 28)

    # 1. Ícone UPLOAD: Setinha Verde para Cima em badge arredondado escuro/verde
    img_up = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_up = ImageDraw.Draw(img_up)
    d_up.rounded_rectangle(
        (6, 6, size - 6, size - 6),
        radius=26,
        fill=(22, 163, 74, 255),
        outline=(134, 239, 172, 255),
        width=6,
    )
    # Ponta da seta p/ cima + haste
    d_up.polygon([(64, 22), (26, 64), (47, 64), (47, 102), (81, 102), (81, 64), (102, 64)], fill=(255, 255, 255, 255))
    img_up_small = img_up.resize(display_size, Image.Resampling.LANCZOS)

    # 2. Ícone DOWNLOAD: Setinha Azul para Baixo em badge arredondado azul
    img_dl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_dl = ImageDraw.Draw(img_dl)
    d_dl.rounded_rectangle(
        (6, 6, size - 6, size - 6),
        radius=26,
        fill=(2, 132, 199, 255),
        outline=(125, 211, 252, 255),
        width=6,
    )
    # Ponta da seta p/ baixo + haste
    d_dl.polygon([(64, 106), (26, 64), (47, 64), (47, 26), (81, 26), (81, 64), (102, 64)], fill=(255, 255, 255, 255))
    img_dl_small = img_dl.resize(display_size, Image.Resampling.LANCZOS)

    # 3. Ícone RELÓGIO: Histórico de Enviados
    img_clk = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_clk = ImageDraw.Draw(img_clk)
    d_clk.ellipse(
        (8, 8, size - 8, size - 8),
        fill=(217, 119, 6, 255),
        outline=(253, 224, 71, 255),
        width=8,
    )
    # Marcadores de horas
    for angle_deg in range(0, 360, 90):
        rad = math.radians(angle_deg)
        x1 = 64 + int(38 * math.cos(rad))
        y1 = 64 + int(38 * math.sin(rad))
        x2 = 64 + int(44 * math.cos(rad))
        y2 = 64 + int(44 * math.sin(rad))
        d_clk.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 255), width=6)
    # Ponteiros do relógio
    d_clk.line([(64, 64), (64, 32)], fill=(255, 255, 255, 255), width=10)
    d_clk.line([(64, 64), (90, 76)], fill=(255, 255, 255, 255), width=9)
    d_clk.ellipse((56, 56, 72, 72), fill=(255, 255, 255, 255))
    img_clk_small = img_clk.resize(display_size, Image.Resampling.LANCZOS)

    # 4. Ícone TELEGRAM: Círculo Azul Telegram + Aviãozinho de Papel Branco
    img_tg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_tg = ImageDraw.Draw(img_tg)
    d_tg.ellipse(
        (6, 6, size - 6, size - 6),
        fill=(36, 161, 222, 255),
        outline=(186, 230, 253, 255),
        width=6,
    )
    # Aviãozinho de papel clássico do Telegram
    d_tg.polygon(
        [(22, 64), (100, 30), (84, 98), (58, 78), (44, 92), (46, 70)],
        fill=(255, 255, 255, 255),
    )
    d_tg.polygon(
        [(46, 70), (88, 38), (58, 78)],
        fill=(219, 234, 254, 255),
    )
    img_tg_small = img_tg.resize(display_size, Image.Resampling.LANCZOS)

    # 5. Ícone PLAY/PAUSE combinado (estilo player de vídeo: ▶ + ❚❚)
    btn_icon_size = (22, 22)
    img_pp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_pp = ImageDraw.Draw(img_pp)
    # Triângulo Play à esquerda
    d_pp.polygon([(14, 24), (14, 104), (66, 64)], fill=(255, 255, 255, 255))
    # Duas barras Pause à direita
    d_pp.rounded_rectangle((78, 24, 94, 104), radius=4, fill=(255, 255, 255, 255))
    d_pp.rounded_rectangle((104, 24, 120, 104), radius=4, fill=(255, 255, 255, 255))
    img_pp_small = img_pp.resize(btn_icon_size, Image.Resampling.LANCZOS)

    # 6. Ícone PAUSE (duas barras verticais ❚❚ + pequeno ▶ ao lado)
    img_pause = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_pause = ImageDraw.Draw(img_pause)
    d_pause.rounded_rectangle((22, 20, 48, 108), radius=6, fill=(255, 255, 255, 255))
    d_pause.rounded_rectangle((62, 20, 88, 108), radius=6, fill=(255, 255, 255, 255))
    d_pause.polygon([(98, 40), (98, 88), (122, 64)], fill=(255, 255, 255, 220))
    img_pause_small = img_pause.resize(btn_icon_size, Image.Resampling.LANCZOS)

    # 7. Ícone PLAY (triângulo de reprodução ▶)
    img_play = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_play = ImageDraw.Draw(img_play)
    d_play.polygon([(24, 16), (24, 112), (110, 64)], fill=(255, 255, 255, 255))
    img_play_small = img_play.resize(btn_icon_size, Image.Resampling.LANCZOS)

    return {
        "upload": ctk.CTkImage(light_image=img_up_small, dark_image=img_up_small, size=display_size),
        "download": ctk.CTkImage(light_image=img_dl_small, dark_image=img_dl_small, size=display_size),
        "history": ctk.CTkImage(light_image=img_clk_small, dark_image=img_clk_small, size=display_size),
        "account": ctk.CTkImage(light_image=img_tg_small, dark_image=img_tg_small, size=display_size),
        "play_pause": ctk.CTkImage(light_image=img_pp_small, dark_image=img_pp_small, size=btn_icon_size),
        "pause": ctk.CTkImage(light_image=img_pause_small, dark_image=img_pause_small, size=btn_icon_size),
        "play": ctk.CTkImage(light_image=img_play_small, dark_image=img_play_small, size=btn_icon_size),
    }


def draw_telegram_wallpaper_canvas(canvas: tk.Canvas):
    """Desenha o background oficial estilizado do Telegram (padrão geométrico + aviãozinho central) no Canvas."""
    canvas.delete("tg_bg")
    w = max(canvas.winfo_width(), 200)
    h = max(canvas.winfo_height(), 120)

    # Fundo base azul escuro estilo Telegram Desktop Night Theme
    canvas.configure(bg="#0e1621")

    # Padrão de ícones sutis de fundo do Telegram (nuvens, aviõezinhos, pastas, estrelas)
    pattern_color = "#18283a"
    accent_pattern = "#1c3148"
    step_x = 130
    step_y = 95

    for row_idx, y in enumerate(range(25, h + 60, step_y)):
        offset_x = 65 if (row_idx % 2 == 1) else 0
        for col_idx, x in enumerate(range(30 + offset_x, w + 60, step_x)):
            kind = (row_idx + col_idx) % 4
            if kind == 0:
                # Mini aviãozinho de papel
                canvas.create_polygon(
                    x - 14, y + 2,
                    x + 14, y - 10,
                    x + 8, y + 14,
                    x - 2, y + 6,
                    outline=pattern_color,
                    fill="",
                    width=1.5,
                    tags="tg_bg",
                )
            elif kind == 1:
                # Mini nuvem Telegram Cloud
                canvas.create_oval(x - 14, y - 4, x + 2, y + 10, outline=accent_pattern, width=1.5, tags="tg_bg")
                canvas.create_oval(x - 6, y - 10, x + 12, y + 8, outline=accent_pattern, width=1.5, tags="tg_bg")
                canvas.create_line(x - 10, y + 10, x + 10, y + 10, fill=accent_pattern, width=1.5, tags="tg_bg")
            elif kind == 2:
                # Mini pasta de arquivo
                canvas.create_polygon(
                    x - 12, y - 8,
                    x - 3, y - 8,
                    x, y - 4,
                    x + 12, y - 4,
                    x + 12, y + 9,
                    x - 12, y + 9,
                    outline=pattern_color,
                    fill="",
                    width=1.5,
                    tags="tg_bg",
                )
            else:
                # Mini losango / estrela suave
                canvas.create_polygon(
                    x, y - 8,
                    x + 8, y,
                    x, y + 8,
                    x - 8, y,
                    outline=accent_pattern,
                    fill="",
                    width=1.5,
                    tags="tg_bg",
                )

    # Emblema Central do Telegram (Círculo Azul Oficial + Aviãozinho Branco)
    cx = w // 2
    cy = h // 2
    r = min(max(int(min(w, h) * 0.20), 34), 68)

    if h >= 90:
        # Halo suave ao redor do círculo
        canvas.create_oval(
            cx - r - 10,
            cy - r - 18,
            cx + r + 10,
            cy + r + 2,
            fill="#132334",
            outline="#1e3a56",
            width=2,
            tags="tg_bg",
        )
        # Círculo oficial Azul Telegram (#24A1DE)
        canvas.create_oval(
            cx - r,
            cy - r - 8,
            cx + r,
            cy + r - 8,
            fill="#1d8ac0",
            outline="#38bdf8",
            width=2,
            tags="tg_bg",
        )
        # Aviãozinho de papel proporcional ao raio r
        cy_plane = cy - 8
        canvas.create_polygon(
            cx - int(r * 0.62), cy_plane + int(r * 0.02),
            cx + int(r * 0.54), cy_plane - int(r * 0.46),
            cx + int(r * 0.32), cy_plane + int(r * 0.52),
            cx - int(r * 0.08), cy_plane + int(r * 0.22),
            cx - int(r * 0.28), cy_plane + int(r * 0.42),
            cx - int(r * 0.25), cy_plane + int(r * 0.10),
            fill="#e0f2fe",
            outline="",
            tags="tg_bg",
        )
        canvas.create_polygon(
            cx - int(r * 0.25), cy_plane + int(r * 0.10),
            cx + int(r * 0.38), cy_plane - int(r * 0.34),
            cx - int(r * 0.08), cy_plane + int(r * 0.22),
            fill="#bae6fd",
            outline="",
            tags="tg_bg",
        )
        if h >= 170:
            canvas.create_text(
                cx,
                cy + r + 16,
                text="TELEGRAM CLOUD DRIVE",
                fill="#38bdf8",
                font=("Segoe UI", 11, "bold"),
                tags="tg_bg",
            )


class TransferPausedError(Exception):
    """Exceção interna usada para interromper o envio/download imediatamente ao clicar em Pausar."""


class ItemDeletedFromQueueError(Exception):
    """Exceção interna disparada quando o usuário exclui um arquivo que estava sendo enviado."""


@dataclass
class FileQueueItem:
    item_id: str
    path: Path
    size: int
    status: str = "Pendente"
    progress: float = 0.0
    sent_at: str = ""
    target_chat: str = ""
    error_msg: str = ""
    checked: bool = False
    ignored_by_delete: bool = False


@dataclass
class RemoteCloudFile:
    row_id: str
    message_id: int
    filename: str
    ext: str
    size: int
    date_str: str
    caption: str
    status: str = "Disponível na Nuvem"
    progress: float = 0.0
    checked: bool = False
    message_obj: object = None


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


class AsyncWorker:
    """Executa um loop asyncio dedicado em uma thread separada para o Telethon."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coro(self, coro, callback=None, error_callback=None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(fut):
            try:
                res = fut.result()
                if callback:
                    callback(res)
            except Exception as exc:
                tb_str = traceback.format_exc()
                append_persistent_log(
                    f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] [ERRO ASYNC] {exc}\n{tb_str}"
                )
                if error_callback:
                    error_callback(exc)

        future.add_done_callback(_done)
        return future


class LabeledProgressBar(tk.Canvas):
    """Barra de progresso estilizada com contador percentual e tempo previsto centralizados dentro da barra."""

    def __init__(
        self,
        master,
        height: int = 20,
        progress_color: str = "#2563eb",
        bg_color: str = "#0f172a",
        border_color: str = "#334155",
        **kwargs,
    ):
        super().__init__(
            master,
            height=height,
            bg=bg_color,
            highlightthickness=1,
            highlightbackground=border_color,
            bd=0,
            **kwargs,
        )
        self._progress_color = progress_color
        self._bg_color = bg_color
        self._value = 0.0
        self._custom_text: Optional[str] = None
        self.bind("<Configure>", lambda _e: self._redraw())

    def set(self, value: float, custom_text: Optional[str] = None):
        try:
            v = float(value)
        except Exception:
            v = 0.0
        self._value = max(0.0, min(1.0, v))
        self._custom_text = custom_text
        self._redraw()

    def get(self) -> float:
        return self._value

    def _redraw(self):
        try:
            self.delete("all")
            w = max(self.winfo_width(), 100)
            h = max(self.winfo_height(), 18)
            fill_w = int(round(w * self._value))
            if fill_w > 0:
                self.create_rectangle(
                    0, 0, fill_w, h, fill=self._progress_color, outline=""
                )
            display_str = (
                self._custom_text
                if self._custom_text is not None
                else f"{self._value * 100:0.1f}%"
            )
            cx = w // 2
            cy = h // 2
            # Sombra escura discreta para contraste perfeito tanto sobre o fundo quanto sobre a barra cheia
            self.create_text(
                cx + 1,
                cy + 1,
                text=display_str,
                fill="#020617",
                font=("Segoe UI", 9, "bold"),
            )
            self.create_text(
                cx,
                cy,
                text=display_str,
                fill="#ffffff",
                font=("Segoe UI", 9, "bold"),
            )
        except Exception:
            pass


class TelegramUploaderApp(ctk.CTk):
    def __init__(self):
        self._main_thread_id = threading.get_ident()
        self._ui_dispatch_queue: "queue.Queue[Tuple]" = queue.Queue()
        super().__init__()
        self._install_global_error_handlers()

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"bolinha.telegram.clouddrive.v{APP_VERSION}"
            )
        except Exception:
            pass

        self.title(APP_TITLE)
        self.geometry("1180x810")
        self.minsize(1040, 700)
        self._apply_window_icon()

        self.nav_icons = _create_nav_icons()
        self.worker = AsyncWorker()
        self._migrate_legacy_local_files_if_same_pc()
        self.config_data = self._load_config()
        self.history_records: List[dict] = self._load_history()
        self.sent_signatures: Dict[str, dict] = {
            rec.get("signature", ""): rec
            for rec in self.history_records
            if rec.get("signature")
        }

        self.client: Optional[TelegramClient] = None
        self.connected_mode: Optional[str] = None
        self.dialogs_map: Dict[str, Union[int, str]] = {}
        self.current_user_info: Dict[str, str] = {
            "name": self.config_data.get("user_display_name", ""),
            "username": self.config_data.get("user_username", ""),
            "phone": self.config_data.get("user_phone", ""),
            "id": self.config_data.get("user_id", ""),
        }

        # Estruturas da Fila de Upload
        self.queue: List[FileQueueItem] = []
        self.queue_by_id: Dict[str, FileQueueItem] = {}
        self.queued_paths: Set[Path] = set()
        self._next_id_counter = 1
        self.queue_filter_mode: str = "Todos"
        self.resume_upload_on_connect: bool = False

        # Estruturas da Aba Download (Drive / Nuvem Telegram)
        self.remote_files: List[RemoteCloudFile] = []
        self.remote_by_id: Dict[str, RemoteCloudFile] = {}
        self.remote_by_filename_lower: Dict[str, RemoteCloudFile] = {}
        self.is_scanning_remote = False
        self._remote_scan_token = 0
        self._dl_filter_render_token = 0

        # Estados de Transferência
        self.is_uploading = False
        self.is_paused = False
        self.is_scanning = False
        self.cancel_requested = False

        self.is_downloading = False
        self.is_download_paused = False
        self.cancel_download_requested = False

        # Estado de login por telefone em andamento
        self._pending_phone_login: Optional[Tuple[str, str]] = None
        self._parallel_upload_senders: List[MTProtoSender] = []
        self._parallel_senders_dc_id: Optional[int] = None

        self.active_page_key: str = "upload"
        self.nav_buttons: Dict[str, ctk.CTkButton] = {}
        self.page_frames: Dict[str, ctk.CTkFrame] = {}

        self._build_ui()
        self._populate_history_tree()
        self._load_previous_logs_into_ui()
        self._restore_saved_queue_state()
        self.switch_page("upload")

        self.protocol("WM_DELETE_WINDOW", self._on_app_closing)
        super().after(25, self._drain_ui_dispatch_queue)
        super().after(300, self._try_auto_connect)
        if not self.config_data.get("hide_welcome_guide", False):
            super().after(450, self.show_welcome_guide)
        super().after(1800, lambda: self.check_for_updates(silent_if_up_to_date=True))

    def after(self, ms, func=None, *args):
        """
        Sobrescreve `after` para garantir 100% de segurança entre threads no Windows 11:
        Chamadas vindas de threads em segundo plano nunca tocam o Tcl/Tk diretamente;
        são enfileiradas em `self._ui_dispatch_queue` e executadas pela thread principal.
        """
        if func is not None and threading.get_ident() != self._main_thread_id:
            self._ui_dispatch_queue.put((func, args))
            return "queued_bg"
        return super().after(ms, func, *args)

    def _drain_ui_dispatch_queue(self):
        try:
            for _ in range(60):
                func, args = self._ui_dispatch_queue.get_nowait()
                try:
                    func(*args)
                except Exception as exc:
                    tb = traceback.format_exc()
                    append_persistent_log(
                        f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] [ERRO UI CALLBACK] {exc}\n{tb}"
                    )
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                super().after(25, self._drain_ui_dispatch_queue)
        except Exception:
            pass

    def _install_global_error_handlers(self):
        def _handle_uncaught(exc_type, exc_value, exc_tb):
            tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            msg = f"[ERRO CRÍTICO DO SISTEMA] {exc_type.__name__}: {exc_value}\n{tb_text}"
            append_persistent_log(f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] {msg}")
            try:
                self.log(f"⚠ Falha capturada pelo sistema: {exc_type.__name__}: {exc_value}")
            except Exception:
                pass

        def _handle_thread_exc(args):
            _handle_uncaught(args.exc_type, args.exc_value, args.exc_traceback)

        sys.excepthook = _handle_uncaught
        threading.excepthook = _handle_thread_exc

    def report_callback_exception(self, exc, val, tb):
        tb_text = "".join(traceback.format_exception(exc, val, tb))
        append_persistent_log(
            f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] [ERRO TKINTER] {val}\n{tb_text}"
        )
        try:
            self.log(f"⚠ Aviso de interface capturado e recuperado: {val}")
        except Exception:
            pass

    def _apply_window_icon(self, win=None):
        target = win or self
        ico_path = find_resource_path("telegram_icon.ico") or find_resource_path("bolinha.ico")
        if ico_path:
            try:
                if win is None:
                    self.iconbitmap(default=str(ico_path))
                target.after(200, lambda: target.iconbitmap(str(ico_path)))
            except Exception:
                pass

    def _migrate_legacy_local_files_if_same_pc(self):
        for folder in (EXE_DIR, BUNDLE_DIR):
            legacy_session = folder / "session_personal.session"
            if legacy_session.exists():
                target_session = Path(f"{SESSION_PERSONAL}.session")
                if not target_session.exists():
                    try:
                        legacy_session.replace(target_session)
                    except Exception:
                        pass
                else:
                    try:
                        legacy_session.unlink()
                    except Exception:
                        pass
            for fname in ("config.json", "sent_history.json"):
                legacy_f = folder / fname
                target_f = USER_DATA_DIR / fname
                if legacy_f.exists():
                    if not target_f.exists():
                        try:
                            legacy_f.replace(target_f)
                        except Exception:
                            pass
                    else:
                        try:
                            legacy_f.unlink()
                        except Exception:
                            pass

    # -------------------------------------------------------------------------
    # Persistência de Configuração e Histórico (Isolado por Computador)
    # -------------------------------------------------------------------------
    def _load_config(self) -> dict:
        default_dl = str(DEFAULT_DOWNLOAD_DIR)
        default = {
            "mode": "personal",
            "api_id": DEFAULT_API_ID,
            "api_hash": DEFAULT_API_HASH,
            "phone": "",
            "bot_token": "",
            "bot_chat_id": "",
            "last_target": "Mensagens Salvas (Saved Messages)",
            "force_document": True,
            "caption_template": "{filename}",
            "use_caption": False,
            "skip_already_sent": True,
            "ignore_duplicates_prompt": True,
            "hide_welcome_guide": False,
            "download_dir": default_dl,
        }
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                default.update(loaded)
            except Exception:
                pass
        return default

    def _save_config(self):
        try:
            CONFIG_FILE.write_text(
                json.dumps(self.config_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            self.log(f"Aviso ao salvar config: {e}")

    def _load_history(self) -> List[dict]:
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save_history(self):
        try:
            HISTORY_FILE.write_text(
                json.dumps(self.history_records, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            self.log(f"Aviso ao salvar histórico: {e}")

    def _record_sent_file(self, item: FileQueueItem, target_label: str):
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        sig = make_file_signature(item.path, item.size)
        record = {
            "signature": sig,
            "filename": item.path.name,
            "path": str(item.path),
            "size": item.size,
            "size_str": format_size(item.size),
            "target": target_label,
            "sent_at": now_str,
        }
        item.sent_at = now_str
        item.target_chat = target_label
        self.history_records.insert(0, record)
        self.sent_signatures[sig] = record
        self._save_history()

        def _ui():
            if hasattr(self, "history_tree") and self.history_tree.winfo_exists():
                self.history_tree.insert(
                    "",
                    0,
                    values=(
                        f"✓ {record['filename']}",
                        record["size_str"],
                        record["target"],
                        record["sent_at"],
                        record["path"],
                    ),
                    tags=("Concluído",),
                )
                self._update_history_summary()

        self.after(0, _ui)

    # -------------------------------------------------------------------------
    # Tela Explicativa de Boas-Vindas (Como Usar)
    # -------------------------------------------------------------------------
    def show_welcome_guide(self):
        if (
            hasattr(self, "welcome_win")
            and self.welcome_win is not None
            and self.welcome_win.winfo_exists()
        ):
            self.welcome_win.lift()
            self.welcome_win.focus_force()
            return

        self.welcome_win = ctk.CTkToplevel(self)
        self.welcome_win.title(f"Bem-vindo ao {APP_TITLE}")
        self.welcome_win.geometry("750x620")
        self.welcome_win.minsize(680, 540)
        self.welcome_win.transient(self)
        self.welcome_win.attributes("-topmost", True)

        self._apply_window_icon(self.welcome_win)

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 375
        y = self.winfo_y() + (self.winfo_height() // 2) - 310
        self.welcome_win.geometry(f"+{max(x, 40)}+{max(y, 40)}")

        self.welcome_win.grid_columnconfigure(0, weight=1)
        self.welcome_win.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self.welcome_win, fg_color="#1e3a8a", corner_radius=10)
        header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")

        ctk.CTkLabel(
            header,
            text=APP_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#ffffff",
        ).pack(padx=16, pady=(12, 2))

        ctk.CTkLabel(
            header,
            text=APP_SUBTITLE,
            font=ctk.CTkFont(size=13),
            text_color="#bfdbfe",
        ).pack(padx=16, pady=(0, 12))

        body = ctk.CTkScrollableFrame(self.welcome_win, corner_radius=10)
        body.grid(row=1, column=0, padx=16, pady=6, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        steps = [
            (
                "Para que serve este programa?",
                f"O {APP_TITLE} transforma o seu Telegram em um verdadeiro Google Drive pessoal e ilimitado!\n"
                "Com ele você envia múltiplos arquivos e várias pastas de uma só vez para fazer BACKUP na nuvem do Telegram, "
                "e também visualiza, pesquisa e faz DOWNLOAD de qualquer arquivo salvo nos seus grupos!",
            ),
            (
                "Passo 1: Crie um Grupo de Backup no seu Telegram",
                "• Abra o seu Telegram (no celular ou no Telegram Web) e crie um Novo Grupo ou Canal Privado.\n"
                "• Dê um nome específico para o seu backup (ex: 'Backup Bolinha - Documentos', 'Meus Arquivos PC').",
            ),
            (
                "Passo 2: Como fazer Login com Segurança",
                "• Clique no botão 'CONTA DO TELEGRAM' na barra principal e conecte escaneando o QR Code na própria tela ou escolhendo o código do país e digitando seu telefone.\n"
                "• Segurança Automática: Seu login fica vinculado somente a ESTE computador. Se você passar o programa "
                "em um pendrive para outra pessoa ou abrir em outro computador, ele pedirá um novo login automaticamente!",
            ),
            (
                "Passo 3: Escolha o Grupo (Varredura Automática de Arquivos)",
                "• No topo do programa, escolha o seu Grupo na caixa destacada 'Grupo / Chat Ativo'.\n"
                "• Assim que você escolher o grupo, o programa fará uma varredura automática e listará todos os arquivos "
                "daquele grupo na opção 'DOWNLOAD DO TELEGRAM'!",
            ),
            (
                "Passo 4: Fazendo Uploads (Várias Pastas) e Downloads (Com Busca)",
                "• Para ENVIAR (Botão UPLOAD PARA TELEGRAM): Use '+ Selecionar Arquivos' ou '+ Adicionar Várias Pastas' para adicionar quantas pastas quiser e clique em '▶ INICIAR UPLOAD'.\n"
                "• Para BAIXAR (Botão DOWNLOAD DO TELEGRAM): Use a Barra de Busca para encontrar qualquer arquivo no grupo, escolha a pasta onde quer salvar no PC e clique em '⬇ BAIXAR SELECIONADOS'!",
            ),
        ]

        for idx, (title_text, desc_text) in enumerate(steps):
            card = ctk.CTkFrame(body, fg_color="#1e293b", corner_radius=8)
            card.grid(row=idx, column=0, padx=6, pady=5, sticky="ew")
            card.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                card,
                text=title_text,
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color="#38bdf8",
                anchor="w",
            ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="ew")

            ctk.CTkLabel(
                card,
                text=desc_text,
                font=ctk.CTkFont(size=12),
                text_color="#e2e8f0",
                justify="left",
                anchor="w",
                wraplength=660,
            ).grid(row=1, column=0, padx=12, pady=(0, 10), sticky="ew")

        footer = ctk.CTkFrame(self.welcome_win, fg_color="transparent")
        footer.grid(row=2, column=0, padx=16, pady=(6, 14), sticky="ew")

        dont_show_var = ctk.BooleanVar(
            value=bool(self.config_data.get("hide_welcome_guide", False))
        )

        def _on_toggle_dont_show():
            self.config_data["hide_welcome_guide"] = bool(dont_show_var.get())
            if hasattr(self, "hide_welcome_opt_var"):
                self.hide_welcome_opt_var.set(bool(dont_show_var.get()))
            self._save_config()

        def _close_welcome():
            _on_toggle_dont_show()
            self.welcome_win.destroy()

        self.welcome_win.protocol("WM_DELETE_WINDOW", _close_welcome)

        ctk.CTkCheckBox(
            footer,
            text="Não mostrar novamente (você pode ver estas instruções quando quiser no menu ⚙ Opções)",
            variable=dont_show_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#cbd5e1",
            command=_on_toggle_dont_show,
        ).pack(anchor="w", pady=(0, 8))

        ctk.CTkButton(
            footer,
            text=f"✓ Entendi, Começar a Usar o {APP_TITLE}!",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#16a34a",
            hover_color="#15803d",
            height=42,
            command=_close_welcome,
        ).pack(fill="x")

    # -------------------------------------------------------------------------
    # Persistência Automática da Fila de Tarefas e Logs (Auto-Save / Recovery)
    # -------------------------------------------------------------------------
    def _save_queue_state(self):
        """Salva no disco a fila de tarefas de upload e se havia tarefa em execução."""
        try:
            items_data = []
            for item in self.queue:
                items_data.append(
                    {
                        "path": str(item.path),
                        "size": item.size,
                        "status": item.status,
                        "progress": item.progress,
                        "sent_at": item.sent_at,
                        "target_chat": item.target_chat,
                        "checked": item.checked,
                    }
                )
            state = {
                "saved_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "was_uploading": bool(self.is_uploading),
                "target": (
                    self.target_combo.get()
                    if hasattr(self, "target_combo")
                    else self.config_data.get("last_target", "")
                ),
                "items": items_data,
            }
            QUEUE_STATE_FILE.write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            append_persistent_log(f"Aviso ao salvar estado da fila: {e}", "WARN")

    def _restore_saved_queue_state(self):
        """Restaura as tarefas que estavam na fila (ou em execução) quando o programa fechou."""
        if not QUEUE_STATE_FILE.exists():
            return
        try:
            state = json.loads(QUEUE_STATE_FILE.read_text(encoding="utf-8"))
            items_data = state.get("items", [])
            was_uploading = bool(state.get("was_uploading", False))
            saved_target = state.get("target", "")
            restored_count = 0
            pending_count = 0

            for entry in items_data:
                p_str = entry.get("path", "")
                if not p_str:
                    continue
                p = Path(p_str)
                if not p.exists() or not p.is_file():
                    continue
                if p in self.queued_paths:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = entry.get("size", 0)

                status = entry.get("status", "Pendente")
                # Arquivos maiores que 2 GB são automaticamente ignorados
                if size > MAX_TELEGRAM_FILE_SIZE:
                    status = "Ignorado (> 2GB)"
                elif status.startswith("Enviando") or status.startswith("Aguardando"):
                    status = "Pendente"

                item_id = f"item_{self._next_id_counter}"
                self._next_id_counter += 1

                sig = make_file_signature(p, size)
                sent_rec = self.sent_signatures.get(sig)
                if size > MAX_TELEGRAM_FILE_SIZE:
                    item = FileQueueItem(
                        item_id=item_id,
                        path=p,
                        size=size,
                        status="Ignorado (> 2GB)",
                        progress=1.0,
                        sent_at="-",
                        target_chat=entry.get("target_chat", ""),
                        checked=bool(entry.get("checked", False)),
                    )
                elif sent_rec and status in ("Concluído", "Já Enviado"):
                    item = FileQueueItem(
                        item_id=item_id,
                        path=p,
                        size=size,
                        status=status,
                        progress=1.0,
                        sent_at=sent_rec.get("sent_at", entry.get("sent_at", "")),
                        target_chat=sent_rec.get("target", entry.get("target_chat", "")),
                        checked=bool(entry.get("checked", False)),
                    )
                else:
                    item = FileQueueItem(
                        item_id=item_id,
                        path=p,
                        size=size,
                        status=status,
                        progress=1.0 if status in ("Concluído", "Já Enviado") else 0.0,
                        sent_at=entry.get("sent_at", ""),
                        target_chat=entry.get("target_chat", ""),
                        checked=bool(entry.get("checked", False)),
                    )
                    if status in ("Pendente", "Pausado", "Erro") or status.startswith("Erro"):
                        pending_count += 1

                self.queue.append(item)
                self.queue_by_id[item_id] = item
                self.queued_paths.add(p)
                if self._item_matches_filter(item):
                    self._insert_item_row(item)
                restored_count += 1

            if restored_count > 0:
                self._update_queue_summary()
                self.log(
                    f"♻ Memória de Tarefas: {restored_count} arquivo(s) restaurado(s) da sessão anterior ({pending_count} pendente(s))."
                )
                if was_uploading and pending_count > 0:
                    self.resume_upload_on_connect = True
                    self.log(
                        "⚡ Tarefa de upload interrompida detectada! O envio será retomado automaticamente assim que conectar ao Telegram."
                    )
                    self.current_file_label.configure(
                        text=f"♻ {pending_count} tarefa(s) interrompida(s) restaurada(s). Retomando após conexão..."
                    )
                elif pending_count > 0:
                    self.current_file_label.configure(
                        text=f"♻ {restored_count} arquivo(s) lembrado(s) da última sessão ({pending_count} pendente(s) para envio)."
                    )
            if saved_target and hasattr(self, "target_combo"):
                self.config_data["last_target"] = saved_target
        except Exception as e:
            self.log(f"Aviso ao restaurar fila salva: {e}")

    def _load_previous_logs_into_ui(self):
        """Carrega no painel de LOG as últimas linhas de log e eventuais erros/crashes anteriores."""
        if not hasattr(self, "log_box"):
            return
        try:
            if CRASH_LOG_FILE.exists():
                crash_txt = CRASH_LOG_FILE.read_text(encoding="utf-8", errors="ignore").strip()
                if crash_txt:
                    tail_crash = "\n".join(crash_txt.splitlines()[-25:])
                    self.log_box.insert(
                        "end",
                        "=== [ALERTA DE DIAGNÓSTICO / REGISTRO DE FALHA ANTERIOR] ===\n"
                        f"{tail_crash}\n"
                        "============================================================\n",
                    )
                    try:
                        CRASH_LOG_FILE.write_text("", encoding="utf-8")
                    except Exception:
                        pass
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                recent = lines[-80:] if len(lines) > 80 else lines
                if recent:
                    self.log_box.insert(
                        "end",
                        "--- Histórico recente de atividades (sessões anteriores) ---\n"
                        + "\n".join(recent)
                        + "\n--- Nova sessão iniciada ---\n",
                    )
                    self.log_box.see("end")
        except Exception:
            pass

    def _on_app_closing(self):
        """Salva estado da fila e configurações antes de fechar a janela."""
        try:
            self._save_ui_prefs()
            self._save_queue_state()
            append_persistent_log("Programa fechado pelo usuário (estado da fila salvo).", "INFO")
        except Exception:
            pass
        self.destroy()

    # -------------------------------------------------------------------------
    # Atualizador Automático via GitHub Releases (3olinha/Telegram-Cloud-Drive)
    # -------------------------------------------------------------------------
    def check_for_updates(self, silent_if_up_to_date: bool = True):
        """
        Verifica em segundo plano no GitHub Releases se existe uma versão mais recente do programa.
        Se houver, abre uma janela perguntando se o usuário deseja atualizar automaticamente (SIM / NÃO).
        """
        if getattr(self, "_is_checking_updates", False):
            return
        self._is_checking_updates = True

        def _worker():
            try:
                req = urllib.request.Request(
                    GITHUB_RELEASES_API,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": f"TelegramCloudDrive/{APP_VERSION}",
                    },
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    release_data = json.loads(resp.read().decode("utf-8", errors="replace"))

                tag_name = str(release_data.get("tag_name", "")).strip()
                remote_ver = tag_name.lstrip("vV").strip() or str(release_data.get("name", "")).strip()
                release_title = str(release_data.get("name") or f"Telegram Cloud Drive v{remote_ver}")
                release_notes = str(release_data.get("body") or "Melhorias de desempenho, estabilidade e novas funções.")

                remote_tuple = parse_version_tuple(remote_ver)
                local_tuple = parse_version_tuple(APP_VERSION)

                exe_url = ""
                exe_size = 0
                assets = release_data.get("assets") or []
                for asset in assets:
                    a_name = str(asset.get("name", "")).lower()
                    if not a_name.endswith(".exe"):
                        continue
                    # Ignora executáveis de instalador/desinstalador para baixar apenas o executável principal do app
                    if any(
                        w in a_name
                        for w in ("instalador", "setup", "uninst", "desinstal")
                    ):
                        continue
                    exe_url = str(asset.get("browser_download_url", ""))
                    exe_size = int(asset.get("size", 0) or 0)
                    if a_name.startswith("telegram_cloud_drive"):
                        break

                if remote_tuple > local_tuple and exe_url:
                    self.log(f"🚀 Nova atualização detectada no GitHub: v{remote_ver} (atual: v{APP_VERSION}).")
                    self.after(
                        0,
                        lambda: self._show_update_available_dialog(
                            remote_ver, release_title, release_notes, exe_url, exe_size
                        ),
                    )
                else:
                    if not silent_if_up_to_date:
                        self.after(
                            0,
                            lambda: messagebox.showinfo(
                                "Programa Atualizado",
                                f"Você já está utilizando a versão mais recente!\n\nVersão atual: {APP_TITLE}",
                            ),
                        )
            except urllib.error.HTTPError as http_err:
                if not silent_if_up_to_date:
                    if http_err.code == 404:
                        self.after(
                            0,
                            lambda: messagebox.showinfo(
                                "Programa Atualizado",
                                f"Você já está utilizando a versão mais recente ({APP_TITLE})!",
                            ),
                        )
                    else:
                        self.after(
                            0,
                            lambda: messagebox.showwarning(
                                "Verificação de Atualização",
                                f"Não foi possível consultar o servidor de atualizações (HTTP {http_err.code}).",
                            ),
                        )
            except Exception as exc:
                if not silent_if_up_to_date:
                    self.after(
                        0,
                        lambda e=exc: messagebox.showwarning(
                            "Verificação de Atualização",
                            f"Não foi possível verificar atualizações no momento:\n{e}",
                        ),
                    )
            finally:
                self._is_checking_updates = False

        threading.Thread(target=_worker, daemon=True).start()

    def _show_update_available_dialog(
        self,
        remote_ver: str,
        release_title: str,
        release_notes: str,
        download_url: str,
        asset_size: int,
    ):
        """Exibe a janela perguntando ao usuário se deseja baixar e instalar a nova versão automaticamente (SIM / NÃO)."""
        if (
            hasattr(self, "update_win")
            and self.update_win is not None
            and self.update_win.winfo_exists()
        ):
            self.update_win.lift()
            self.update_win.focus_force()
            return

        win = ctk.CTkToplevel(self)
        self.update_win = win
        win.title(f"Nova Atualização Disponível — v{remote_ver}")
        win.geometry("600x430")
        win.resizable(False, False)
        win.transient(self)
        win.attributes("-topmost", True)
        win.configure(fg_color="#0f172a")
        self._apply_window_icon(win)

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 300
        y = self.winfo_y() + (self.winfo_height() // 2) - 215
        win.geometry(f"600x430+{max(x, 40)}+{max(y, 40)}")

        header = ctk.CTkFrame(win, fg_color="#1e3a8a", corner_radius=10)
        header.pack(fill="x", padx=14, pady=(12, 8))

        ctk.CTkLabel(
            header,
            text=f"🚀 Nova Atualização Disponível! (v{remote_ver})",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#ffffff",
        ).pack(pady=(10, 2))

        size_info = f" ({format_size(asset_size)})" if asset_size > 0 else ""
        ctk.CTkLabel(
            header,
            text=f"Sua versão: v{APP_VERSION}   ➔   Nova versão: v{remote_ver}{size_info}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#93c5fd",
        ).pack(pady=(0, 10))

        notes_frame = ctk.CTkFrame(win, fg_color="#17212b", corner_radius=8, border_width=1, border_color="#1e3a5f")
        notes_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        ctk.CTkLabel(
            notes_frame,
            text=f"Novidades — {release_title}:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 4))

        notes_box = ctk.CTkTextbox(
            notes_frame,
            height=115,
            fg_color="#0e1621",
            text_color="#e2e8f0",
            font=ctk.CTkFont(size=12),
        )
        notes_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        notes_box.insert("1.0", release_notes.strip())
        notes_box.configure(state="disabled")

        question_lbl = ctk.CTkLabel(
            win,
            text="Deseja atualizar o software automaticamente agora?",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#4ade80",
        )
        question_lbl.pack(pady=(2, 6))

        prog_frame = ctk.CTkFrame(win, fg_color="transparent")
        prog_status_lbl = ctk.CTkLabel(
            prog_frame,
            text="Iniciando download da atualização...",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )
        prog_status_lbl.pack(pady=(0, 4))
        prog_bar = LabeledProgressBar(prog_frame, height=22, progress_color="#16a34a")
        prog_bar.pack(fill="x", padx=20, pady=(0, 6))
        prog_bar.set(0.0)

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(4, 14))
        btn_row.grid_columnconfigure((0, 1), weight=1)

        def _on_yes():
            yes_btn.configure(state="disabled", text="⏳ Baixando atualização...")
            no_btn.configure(state="disabled")
            prog_frame.pack(fill="x", before=btn_row, pady=(0, 6))
            self._start_auto_update_download(
                win, remote_ver, download_url, asset_size, prog_bar, prog_status_lbl, yes_btn, no_btn
            )

        def _on_no():
            win.destroy()

        yes_btn = ctk.CTkButton(
            btn_row,
            text="✓ SIM, ATUALIZAR AGORA",
            height=42,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=_on_yes,
        )
        yes_btn.grid(row=0, column=0, padx=(4, 8), sticky="ew")

        no_btn = ctk.CTkButton(
            btn_row,
            text="✕ NÃO, DEPOIS",
            height=42,
            fg_color="#475569",
            hover_color="#334155",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=_on_no,
        )
        no_btn.grid(row=0, column=1, padx=(8, 4), sticky="ew")

    def _start_auto_update_download(
        self,
        win,
        remote_ver: str,
        download_url: str,
        expected_size: int,
        prog_bar: LabeledProgressBar,
        prog_status_lbl: ctk.CTkLabel,
        yes_btn: ctk.CTkButton,
        no_btn: ctk.CTkButton,
    ):
        """Baixa o novo executável do GitHub com barra de progresso e substitui/reinicia automaticamente."""
        def _dl_worker():
            target_dir = EXE_DIR
            if not getattr(sys, "frozen", False):
                if (EXE_DIR / "Portatil").is_dir():
                    target_dir = EXE_DIR / "Portatil"
                elif (EXE_DIR / "Pendrive_Portatil").is_dir():
                    target_dir = EXE_DIR / "Pendrive_Portatil"

            new_exe_name = f"Telegram_Cloud_Drive_v{remote_ver}.exe"
            new_exe_path = target_dir / new_exe_name
            tmp_exe_path = target_dir / f"_downloading_{new_exe_name}.part"

            try:
                req = urllib.request.Request(
                    download_url,
                    headers={"User-Agent": f"TelegramCloudDrive/{APP_VERSION}"},
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    total_bytes = int(resp.headers.get("Content-Length", 0) or expected_size or 0)
                    downloaded = 0
                    last_ui_ts = 0.0

                    with open(tmp_exe_path, "wb") as out_f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            out_f.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if now - last_ui_ts >= 0.08 or (total_bytes and downloaded >= total_bytes):
                                last_ui_ts = now
                                frac = (downloaded / total_bytes) if total_bytes > 0 else 0.5
                                dl_str = format_size(downloaded)
                                tot_str = format_size(total_bytes) if total_bytes > 0 else "..."
                                self.after(
                                    0,
                                    lambda f=frac, d=dl_str, t=tot_str: (
                                        prog_bar.set(f) if win.winfo_exists() else None,
                                        prog_status_lbl.configure(
                                            text=f"Baixando atualização: {d} de {t}"
                                        )
                                        if win.winfo_exists()
                                        else None,
                                    ),
                                )

                if not tmp_exe_path.exists() or tmp_exe_path.stat().st_size < 1024 * 500:
                    raise RuntimeError("O arquivo baixado está incompleto ou corrompido.")

                if new_exe_path.exists():
                    try:
                        new_exe_path.unlink()
                    except Exception:
                        pass
                tmp_exe_path.replace(new_exe_path)

                self.after(
                    0,
                    lambda: (
                        prog_bar.set(1.0) if win.winfo_exists() else None,
                        prog_status_lbl.configure(
                            text="✓ Download concluído! Aplicando atualização e reiniciando..."
                        )
                        if win.winfo_exists()
                        else None,
                    ),
                )
                time.sleep(0.6)
                self.after(0, lambda: self._launch_self_updater_and_exit(target_dir, new_exe_path, remote_ver))

            except Exception as exc:
                try:
                    if tmp_exe_path.exists():
                        tmp_exe_path.unlink()
                except Exception:
                    pass

                def _on_err():
                    if win.winfo_exists():
                        prog_status_lbl.configure(
                            text=f"Erro ao baixar atualização: {exc}", text_color="#f87171"
                        )
                        yes_btn.configure(state="normal", text="✓ TENTAR NOVAMENTE")
                        no_btn.configure(state="normal")

                self.after(0, _on_err)

        threading.Thread(target=_dl_worker, daemon=True).start()

    def _launch_self_updater_and_exit(self, target_dir: Path, new_exe_path: Path, remote_ver: str):
        """Gera script automático de substituição, limpa ambiente PyInstaller (_MEI), fecha o programa e abre a nova versão."""
        try:
            self._save_ui_prefs()
            self._save_queue_state()
        except Exception:
            pass

        unblock_windows_file(new_exe_path)

        temp_dir = Path(os.environ.get("TEMP", str(USER_DATA_DIR)))
        updater_bat = temp_dir / "_aplicar_atualizacao_bolinha.bat"
        current_exe = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
        is_installed_fixed_name = bool(
            current_exe and current_exe.name.lower() == "telegram_cloud_drive.exe"
        )
        old_ver_Cleanup = ""
        if (
            current_exe
            and not is_installed_fixed_name
            and current_exe != new_exe_path.resolve()
            and current_exe.parent == target_dir.resolve()
        ):
            old_ver_Cleanup = f'del /f /q "{current_exe}" >nul 2>&1'

        # Atualiza também o .bat lançador na pasta portátil se existir
        portable_bat = target_dir / "Abrir_Telegram_Cloud_Drive.bat"
        if portable_bat.exists():
            try:
                bat_txt = portable_bat.read_text(encoding="utf-8", errors="ignore")
                bat_txt = re.sub(
                    r"Telegram_Cloud_Drive_v[0-9.]+\.exe",
                    new_exe_path.name,
                    bat_txt,
                )
                bat_txt = re.sub(
                    r"Telegram Cloud Drive v[0-9.]+",
                    f"Telegram Cloud Drive v{remote_ver}",
                    bat_txt,
                )
                portable_bat.write_text(bat_txt, encoding="utf-8")
            except Exception:
                pass

        installed_exe_path = target_dir / "Telegram_Cloud_Drive.exe"
        launch_target = installed_exe_path if is_installed_fixed_name else new_exe_path
        cleanup_temp_v = f'del /f /q "{new_exe_path}" >nul 2>&1' if is_installed_fixed_name else ""

        bat_script = f"""@echo off
chcp 65001 >nul
set _MEIPASS=
set _MEIPASS2=
set _PYI_APPLICATION_HOME_DIR=
set _PYI_ARCHIVE_FILE=
set _PYI_PARENT_PROCESS_LEVEL=
set _PYI_SPLASH_IPC=
set TCL_LIBRARY=
set TK_LIBRARY=
set PYTHONPATH=
set PYTHONHOME=
ping 127.0.0.1 -n 3 >nul
for /L %%i in (1,1,15) do (
    copy /y "{new_exe_path}" "{installed_exe_path}" >nul 2>&1 && goto :COPIED
    ping 127.0.0.1 -n 2 >nul
)
:COPIED
{old_ver_Cleanup}
{cleanup_temp_v}
start "" "{launch_target}"
del "%~f0" >nul 2>&1
"""
        try:
            updater_bat.write_text(bat_script, encoding="utf-8")
            creation_flags = 0x08000000  # CREATE_NO_WINDOW
            subprocess.Popen(
                ["cmd.exe", "/c", str(updater_bat)],
                cwd=str(target_dir),
                env=get_clean_pyinstaller_env(),
                creationflags=creation_flags,
            )
        except Exception as e:
            append_persistent_log(f"Erro ao iniciar script atualizador: {e}", "ERROR")

        try:
            self.quit()
            self.destroy()
        except Exception:
            pass
        sys.exit(0)

    def uninstall_program_from_ui(self):
        """Aciona a desinstalação completa a partir do menu Opções, removendo login e programa."""
        if hasattr(self, "options_win") and self.options_win is not None and self.options_win.winfo_exists():
            try:
                self.options_win.destroy()
            except Exception:
                pass
        run_uninstaller_flow(parent_win=self, active_client=self.client, async_worker=getattr(self, "worker", None))

    def open_options_window(self):
        """Abre a janela de Opções com guias: '⚙ Opções Gerais' e '👤 Informações da Conta' (API ID / API Hash)."""
        if (
            hasattr(self, "options_win")
            and self.options_win is not None
            and self.options_win.winfo_exists()
        ):
            self.options_win.lift()
            self.options_win.focus_force()
            return

        self.options_win = ctk.CTkToplevel(self)
        self.options_win.title(f"Opções — {APP_TITLE}")
        self.options_win.geometry("650x520")
        self.options_win.resizable(False, False)
        self.options_win.transient(self)
        self.options_win.attributes("-topmost", True)
        self._apply_window_icon(self.options_win)

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 325
        y = self.winfo_y() + (self.winfo_height() // 2) - 260
        self.options_win.geometry(f"650x520+{max(x, 40)}+{max(y, 40)}")

        tabs = ctk.CTkTabview(self.options_win, fg_color="#17212b", corner_radius=10)
        tabs.pack(fill="both", expand=True, padx=14, pady=(10, 14))

        tab_general = tabs.add("⚙ Opções Gerais")
        tab_account_info = tabs.add("👤 Informações da Conta")

        # --- ABA 1: OPÇÕES GERAIS ---
        ctk.CTkLabel(
            tab_general,
            text="⚙ Opções de Upload, Instruções e Verificação",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#38bdf8",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 6))

        ctk.CTkCheckBox(
            tab_general,
            text="Ignorar repetidos",
            variable=self.ignore_duplicates_var,
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#f8fafc",
            command=self._save_ui_prefs,
        ).pack(anchor="w", padx=16, pady=(4, 6))

        ctk.CTkLabel(
            tab_general,
            text=(
                "Quando ativado: Ao fazer upload de um arquivo, verifica na lista de Downloads (varredura)\n"
                "se o arquivo já existe no grupo. Se já existir, abre uma caixa perguntando se deseja subir\n"
                "novamente com contagem regressiva de 10 segundos (SIM / NÃO / IGNORAR TODOS).\n"
                "• Arquivos maiores que 2 GB são ignorados automaticamente."
            ),
            font=ctk.CTkFont(size=12),
            text_color="#cbd5e1",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 10))

        self.hide_welcome_opt_var = ctk.BooleanVar(
            value=bool(self.config_data.get("hide_welcome_guide", False))
        )

        def _toggle_hide_welcome_from_opt():
            self.config_data["hide_welcome_guide"] = bool(self.hide_welcome_opt_var.get())
            self._save_config()

        ctk.CTkCheckBox(
            tab_general,
            text="Não mostrar mensagem inicial de instruções ao abrir o programa",
            variable=self.hide_welcome_opt_var,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#f8fafc",
            command=_toggle_hide_welcome_from_opt,
        ).pack(anchor="w", padx=16, pady=(4, 12))

        opt_btn_row = ctk.CTkFrame(tab_general, fg_color="transparent")
        opt_btn_row.pack(pady=(6, 10))

        ctk.CTkButton(
            opt_btn_row,
            text="📖 Ver Instruções de Uso",
            width=185,
            height=34,
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.show_welcome_guide,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            opt_btn_row,
            text="🔄 Verificar Atualizações",
            width=180,
            height=34,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.check_for_updates(silent_if_up_to_date=False),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            opt_btn_row,
            text="✓ Fechar e Salvar",
            width=150,
            height=34,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.options_win.destroy,
        ).pack(side="left", padx=5)

        uninstall_row = ctk.CTkFrame(tab_general, fg_color="transparent")
        uninstall_row.pack(pady=(2, 8))

        ctk.CTkButton(
            uninstall_row,
            text="🗑 Desinstalar Programa e Apagar Dados de Login do Telegram",
            width=430,
            height=34,
            fg_color="#dc2626",
            hover_color="#b91c1c",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.uninstall_program_from_ui,
        ).pack()

        # --- ABA 2: INFORMAÇÕES DA CONTA (API ID, API HASH E DADOS DO USUÁRIO) ---
        ctk.CTkLabel(
            tab_account_info,
            text="👤 Informações da Conta do Telegram & Credenciais de API",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#38bdf8",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(8, 6))

        user_info_frame = ctk.CTkFrame(tab_account_info, fg_color="#0e1621", corner_radius=8)
        user_info_frame.pack(fill="x", padx=14, pady=(0, 10))

        u_name = self.current_user_info.get("name", "Não conectado")
        u_user = self.current_user_info.get("username", "-")
        u_phone = self.current_user_info.get("phone", self.config_data.get("phone", "-"))
        u_id = self.current_user_info.get("id", "-")

        ctk.CTkLabel(
            user_info_frame,
            text=(
                f"Usuário Conectado: {u_name}   |   Username: {u_user}\n"
                f"Telefone: {u_phone}   |   ID da Conta: {u_id}"
            ),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#4ade80" if u_name != "Não conectado" else "#f87171",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=12, pady=8)

        api_frame = ctk.CTkFrame(tab_account_info, fg_color="#0e1621", corner_radius=8)
        api_frame.pack(fill="x", padx=14, pady=(0, 10))
        api_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            api_frame,
            text="API ID:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e2e8f0",
        ).grid(row=0, column=0, padx=12, pady=(10, 6), sticky="w")

        opt_api_id_entry = ctk.CTkEntry(api_frame, placeholder_text="Ex: 2040")
        opt_api_id_entry.grid(row=0, column=1, padx=12, pady=(10, 6), sticky="ew")
        opt_api_id_entry.insert(0, str(self.config_data.get("api_id", DEFAULT_API_ID)))

        ctk.CTkLabel(
            api_frame,
            text="API Hash:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e2e8f0",
        ).grid(row=1, column=0, padx=12, pady=(4, 10), sticky="w")

        opt_api_hash_entry = ctk.CTkEntry(api_frame, placeholder_text="Hash da API do Telegram")
        opt_api_hash_entry.grid(row=1, column=1, padx=12, pady=(4, 10), sticky="ew")
        opt_api_hash_entry.insert(0, str(self.config_data.get("api_hash", DEFAULT_API_HASH)))

        def _save_api_info():
            new_id = opt_api_id_entry.get().strip() or DEFAULT_API_ID
            new_hash = opt_api_hash_entry.get().strip() or DEFAULT_API_HASH
            self.config_data["api_id"] = new_id
            self.config_data["api_hash"] = new_hash
            self._save_config()
            self.log("✓ Informações de API ID e API Hash salvas com sucesso.")
            self.options_win.destroy()

        ctk.CTkButton(
            tab_account_info,
            text="💾 Salvar Informações da Conta",
            width=210,
            height=34,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=_save_api_info,
        ).pack(pady=(4, 8))

    # -------------------------------------------------------------------------
    # Construção da Interface Gráfica Principal + Barra de Navegação Destacada
    # -------------------------------------------------------------------------
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self.ignore_duplicates_var = ctk.BooleanVar(
            value=self.config_data.get("ignore_duplicates_prompt", True)
        )

        # 1. Banner de Título no Topo com Ícone Oficial Inspirado no Telegram e Botão Pequeno de Opções
        brand_frame = ctk.CTkFrame(self, fg_color="#1e3a8a", corner_radius=10)
        brand_frame.grid(row=0, column=0, padx=14, pady=(8, 4), sticky="ew")
        brand_frame.grid_columnconfigure(0, weight=1)

        title_box = ctk.CTkFrame(brand_frame, fg_color="transparent")
        title_box.grid(row=0, column=0, padx=14, pady=6, sticky="w")

        png_path = find_resource_path("telegram_icon.png") or find_resource_path("bolinha.png")
        if png_path:
            try:
                pil_logo = Image.open(png_path)
                self.logo_ctk = ctk.CTkImage(
                    light_image=pil_logo, dark_image=pil_logo, size=(38, 38)
                )
                ctk.CTkLabel(title_box, text="", image=self.logo_ctk).pack(
                    side="left", padx=(0, 10)
                )
            except Exception:
                pass

        ctk.CTkLabel(
            title_box,
            text=APP_TITLE,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#ffffff",
        ).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(
            title_box,
            text=f"• {APP_SUBTITLE}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#bfdbfe",
        ).pack(side="left")

        top_right_btns = ctk.CTkFrame(brand_frame, fg_color="transparent")
        top_right_btns.grid(row=0, column=1, padx=10, pady=6, sticky="e")

        ctk.CTkButton(
            top_right_btns,
            text="🔄 Atualizar",
            width=92,
            height=26,
            fg_color="#059669",
            hover_color="#047857",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.check_for_updates(silent_if_up_to_date=False),
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            top_right_btns,
            text="? Como Usar / Ajuda",
            width=135,
            height=26,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.show_welcome_guide,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            top_right_btns,
            text="⚙ Opções",
            width=82,
            height=26,
            fg_color="#0f172a",
            hover_color="#334155",
            border_width=1,
            border_color="#60a5fa",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.open_options_window,
        ).pack(side="left")

        # 2. Barra de Status da Conexão e Seleção do Grupo/Chat Ativo (Mais Destacado e Sem [número])
        top_frame = ctk.CTkFrame(self, corner_radius=10)
        top_frame.grid(row=1, column=0, padx=14, pady=(4, 4), sticky="ew")
        top_frame.grid_columnconfigure(1, weight=1)

        self.status_badge = ctk.CTkLabel(
            top_frame,
            text="● Desconectado",
            text_color="#f87171",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.status_badge.grid(row=0, column=0, padx=14, pady=10, sticky="w")

        dest_frame = ctk.CTkFrame(top_frame, fg_color="transparent")
        dest_frame.grid(row=0, column=1, padx=8, pady=6, sticky="ew")
        dest_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            dest_frame,
            text="Grupo / Chat Ativo:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.target_combo = ctk.CTkComboBox(
            dest_frame,
            values=["Mensagens Salvas (Saved Messages)"],
            width=420,
            height=36,
            font=ctk.CTkFont(size=15, weight="bold"),
            dropdown_font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#0c4a6e",
            border_color="#38bdf8",
            border_width=2,
            button_color="#0284c7",
            button_hover_color="#0369a1",
            text_color="#ffffff",
            command=self._on_target_changed,
        )
        self.target_combo.grid(row=0, column=1, padx=4, sticky="ew")
        saved_last_target = self.config_data.get(
            "last_target", "Mensagens Salvas (Saved Messages)"
        )
        # Remove sufixo antigo [numero] caso estivesse salvo de versões anteriores
        if " [" in saved_last_target and saved_last_target.endswith("]"):
            saved_last_target = saved_last_target.rsplit(" [", 1)[0]
        self.target_combo.set(saved_last_target)

        self.refresh_chats_btn = ctk.CTkButton(
            dest_frame,
            text="↻ Atualizar Grupos",
            width=155,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.refresh_dialogs,
        )
        self.refresh_chats_btn.grid(row=0, column=2, padx=(8, 6))

        # 3. BARRA DE NAVEGAÇÃO LARGA E DESTACADA COM BOTÕES RETANGULARES E ÍCONES
        nav_bar_frame = ctk.CTkFrame(
            self,
            fg_color="#0f172a",
            corner_radius=10,
            border_width=2,
            border_color="#334155",
        )
        nav_bar_frame.grid(row=2, column=0, padx=14, pady=(4, 4), sticky="ew")
        for col_idx in range(4):
            nav_bar_frame.grid_columnconfigure(col_idx, weight=1, uniform="nav_cols")

        nav_specs = [
            ("upload", "UPLOAD PARA TELEGRAM", self.nav_icons["upload"]),
            ("download", "DOWNLOAD DO TELEGRAM", self.nav_icons["download"]),
            ("history", "HISTÓRICO DE ENVIADOS", self.nav_icons["history"]),
            ("account", "CONTA DO TELEGRAM", self.nav_icons["account"]),
        ]

        for idx, (key, label_text, icon_img) in enumerate(nav_specs):
            btn = ctk.CTkButton(
                nav_bar_frame,
                text=f"  {label_text}",
                image=icon_img,
                compound="left",
                height=50,
                corner_radius=6,
                border_width=2,
                border_color="#334155",
                fg_color="#1e293b",
                hover_color="#2563eb",
                text_color="#f8fafc",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda k=key: self.switch_page(k),
            )
            btn.grid(row=0, column=idx, padx=6, pady=7, sticky="ew")
            self.nav_buttons[key] = btn

        # Estilo compartilhado das Tabelas (Cores Oficiais Telegram Night Theme + Fonte Tamanho 12)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Dark.Treeview",
            background="#17212b",
            foreground="#f8fafc",
            fieldbackground="#0e1621",
            borderwidth=0,
            rowheight=32,
            font=("Segoe UI", 12),
        )
        style.configure(
            "Dark.Treeview.Heading",
            background="#132337",
            foreground="#38bdf8",
            relief="flat",
            font=("Segoe UI", 12, "bold"),
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", "#2481cc")],
            foreground=[("selected", "#ffffff")],
        )
        style.map(
            "Dark.Treeview.Heading",
            background=[("active", "#1e3a5f")],
        )

        # 4. Container das Páginas
        self.pages_container = ctk.CTkFrame(self, corner_radius=10)
        self.pages_container.grid(row=3, column=0, padx=14, pady=4, sticky="nsew")
        self.pages_container.grid_columnconfigure(0, weight=1)
        self.pages_container.grid_rowconfigure(0, weight=1)

        self.tab_upload = ctk.CTkFrame(self.pages_container, fg_color="transparent")
        self.tab_download = ctk.CTkFrame(self.pages_container, fg_color="transparent")
        self.tab_history = ctk.CTkFrame(self.pages_container, fg_color="transparent")
        self.tab_account = ctk.CTkFrame(self.pages_container, fg_color="transparent")

        self.page_frames = {
            "upload": self.tab_upload,
            "download": self.tab_download,
            "history": self.tab_history,
            "account": self.tab_account,
        }

        for frame in self.page_frames.values():
            frame.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")

        self._build_upload_tab()
        self._build_download_tab()
        self._build_history_tab()
        self._build_account_tab()

        # 5. Rodapé Inferior de Progresso + Assinatura "By Bolinha"
        bottom_frame = ctk.CTkFrame(self, corner_radius=10)
        bottom_frame.grid(row=4, column=0, padx=14, pady=(2, 2), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)

        info_row = ctk.CTkFrame(bottom_frame, fg_color="transparent")
        info_row.grid(row=0, column=0, padx=12, pady=(5, 2), sticky="ew")
        info_row.grid_columnconfigure(0, weight=1)

        self.current_file_label = ctk.CTkLabel(
            info_row,
            text="Pronto. Selecione um grupo no topo para gerenciar seus uploads e downloads.",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self.current_file_label.grid(row=0, column=0, sticky="w")

        self.speed_label = ctk.CTkLabel(
            info_row,
            text="Velocidade: -- MB/s | Progresso Geral: 0/0",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8",
            anchor="e",
        )
        self.speed_label.grid(row=0, column=1, sticky="e")

        self.file_progress_title_label = ctk.CTkLabel(
            bottom_frame,
            text="Progresso do Arquivo Atual:   •   Tempo previsto para conclusão: --",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#cbd5e1",
        )
        self.file_progress_title_label.grid(row=1, column=0, padx=12, sticky="w")

        self.file_progress_bar = LabeledProgressBar(
            bottom_frame, height=20, progress_color="#2563eb"
        )
        self.file_progress_bar.grid(row=2, column=0, padx=12, pady=(1, 4), sticky="ew")
        self.file_progress_bar.set(0)

        self.total_progress_title_label = ctk.CTkLabel(
            bottom_frame,
            text="Progresso Total da Operação:   •   Tempo previsto para conclusão: --",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#cbd5e1",
        )
        self.total_progress_title_label.grid(row=3, column=0, padx=12, sticky="w")

        self.total_progress_bar = LabeledProgressBar(
            bottom_frame, height=20, progress_color="#10b981"
        )
        self.total_progress_bar.grid(
            row=4, column=0, padx=12, pady=(1, 6), sticky="ew"
        )
        self.total_progress_bar.set(0)

        # Assinatura fixa na parte inferior da janela
        credits_bar = ctk.CTkFrame(self, fg_color="transparent")
        credits_bar.grid(row=5, column=0, padx=14, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(
            credits_bar,
            text="By Bolinha",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        ).pack(side="right", padx=6)
        ctk.CTkLabel(
            credits_bar,
            text=f"{APP_TITLE} — {APP_SUBTITLE}",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
        ).pack(side="left", padx=6)

    def switch_page(self, page_key: str):
        """Alterna entre as 4 seções principais destacando o botão retangular ativo."""
        self.active_page_key = page_key
        for k, frame in self.page_frames.items():
            if k == page_key:
                frame.tkraise()

        active_colors = {
            "upload": ("#14532d", "#4ade80"),    # Destaque verde escuro com borda verde brilhante
            "download": ("#0c4a6e", "#38bdf8"),  # Destaque azul escuro com borda azul brilhante
            "history": ("#78350f", "#fbbf24"),   # Destaque âmbar com borda dourada
            "account": ("#1e3a8a", "#60a5fa"),   # Destaque azul Telegram
        }
        for k, btn in self.nav_buttons.items():
            if k == page_key:
                fg_c, border_c = active_colors.get(k, ("#1d4ed8", "#60a5fa"))
                btn.configure(
                    fg_color=fg_c,
                    border_color=border_c,
                    border_width=2,
                    text_color="#ffffff",
                )
            else:
                btn.configure(
                    fg_color="#1e293b",
                    border_color="#334155",
                    border_width=1,
                    text_color="#cbd5e1",
                )

    def _update_scan_lock_ui(self, detail_msg: str = ""):
        """
        Exibe/oculta o aviso central 'Lendo os arquivos, aguarde...' APENAS durante a leitura
        de pastas locais grandes no PC (self.is_scanning).
        A varredura do grupo no Telegram (self.is_scanning_remote) roda 100% em segundo plano
        sem bloquear o uso do programa e sem desabilitar os botões Iniciar Upload / Download.
        """
        busy = bool(self.is_scanning)
        sub_txt = (
            detail_msg
            if detail_msg
            else "Aguarde o término da leitura local para evitar travamentos..."
        )

        # Atualiza aviso central na aba Upload apenas quando estiver lendo pastas do computador
        if hasattr(self, "upload_loading_overlay"):
            if busy:
                self.upload_loading_sub_label.configure(text=sub_txt)
                self.upload_loading_overlay.place(relx=0.5, rely=0.5, anchor="center")
                self.upload_loading_overlay.lift()
            else:
                self.upload_loading_overlay.place_forget()

        if hasattr(self, "download_loading_overlay"):
            self.download_loading_overlay.place_forget()

        # Atualiza cor/estado dos botões Iniciar/Pausar (Cinza quando lendo, Colorido quando pronto)
        if hasattr(self, "start_btn"):
            if busy:
                self.start_btn.configure(
                    state="disabled",
                    text="  AGUARDE LEITURA...",
                    image=self.nav_icons["play_pause"],
                    fg_color="#475569",
                    hover_color="#475569",
                    text_color="#cbd5e1",
                )
            elif not self.is_uploading and not self.is_paused:
                self.start_btn.configure(
                    state="normal",
                    text="  INICIAR UPLOAD",
                    image=self.nav_icons["play"],
                    fg_color="#16a34a",
                    hover_color="#15803d",
                    text_color="#ffffff",
                )
            elif self.is_paused:
                self.start_btn.configure(
                    state="normal",
                    text="  CONTINUAR UPLOAD",
                    image=self.nav_icons["play"],
                    fg_color="#16a34a",
                    hover_color="#15803d",
                    text_color="#ffffff",
                )

        if hasattr(self, "dl_start_btn"):
            if busy:
                self.dl_start_btn.configure(
                    state="disabled",
                    text="  AGUARDE LEITURA...",
                    image=self.nav_icons["play_pause"],
                    fg_color="#475569",
                    hover_color="#475569",
                    text_color="#cbd5e1",
                )
            elif not self.is_downloading and not self.is_download_paused:
                self.dl_start_btn.configure(
                    state="normal",
                    text="  BAIXAR SELECIONADOS",
                    image=self.nav_icons["play"],
                    fg_color="#16a34a",
                    hover_color="#15803d",
                    text_color="#ffffff",
                )
            elif self.is_download_paused:
                self.dl_start_btn.configure(
                    state="normal",
                    text="  CONTINUAR DOWNLOAD",
                    image=self.nav_icons["play"],
                    fg_color="#16a34a",
                    hover_color="#15803d",
                    text_color="#ffffff",
                )

    # -------------------------------------------------------------------------
    # SEÇÃO 1: UPLOAD PARA TELEGRAM (Layout Clean + Background Telegram)
    # -------------------------------------------------------------------------
    def _build_upload_tab(self):
        self.tab_upload.grid_columnconfigure(0, weight=1)
        self.tab_upload.grid_rowconfigure(1, weight=1)

        btn_bar = ctk.CTkFrame(self.tab_upload, fg_color="transparent")
        btn_bar.grid(row=0, column=0, padx=8, pady=6, sticky="ew")

        # 1. Botão único "+ Selecionar Arquivos" (abre janela para arquivos, pasta ou diretório com várias pastas)
        self.add_files_btn = ctk.CTkButton(
            btn_bar,
            text="+ Selecionar Arquivos",
            width=165,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self.open_multi_folder_selector,
        )
        self.add_files_btn.pack(side="left", padx=(0, 8))
        # Mantém referência compatível
        self.add_folder_btn = self.add_files_btn

        self.recursive_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            btn_bar,
            text="Subpastas",
            variable=self.recursive_var,
            width=85,
            checkbox_width=18,
            checkbox_height=18,
        ).pack(side="left", padx=(2, 6))

        self.skip_sent_var = ctk.BooleanVar(
            value=self.config_data.get("skip_already_sent", True)
        )
        ctk.CTkCheckBox(
            btn_bar,
            text="Marcar já enviados",
            variable=self.skip_sent_var,
            width=130,
            checkbox_width=18,
            checkbox_height=18,
            command=self._save_ui_prefs,
        ).pack(side="left", padx=(2, 8))

        # 2. Caixa de seleção compacta (Todos / Pendentes) sem 'Enviados' para economizar espaço
        ctk.CTkLabel(
            btn_bar,
            text="Exibir:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#cbd5e1",
        ).pack(side="left", padx=(2, 4))

        self.filter_seg = ctk.CTkComboBox(
            btn_bar,
            values=["Todos", "Pendentes"],
            command=self._on_filter_changed,
            width=120,
            height=28,
            state="readonly",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.filter_seg.set("Todos")
        self.filter_seg.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_bar,
            text="Limpar Tudo",
            width=95,
            height=28,
            fg_color="#64748b",
            hover_color="#475569",
            command=self.clear_all,
        ).pack(side="left", padx=4)

        self.queue_summary_label = ctk.CTkLabel(
            btn_bar,
            text="0 arq. (0.0 B) | Pend: 0",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )
        self.queue_summary_label.pack(side="right", padx=6)

        # Tabela Principal de Upload com Background Oficial do Telegram no Fundo
        table_container = ctk.CTkFrame(
            self.tab_upload,
            fg_color="#0e1621",
            border_width=1,
            border_color="#1e3a5f",
            corner_radius=8,
        )
        table_container.grid(row=1, column=0, padx=8, pady=6, sticky="nsew")
        table_container.grid_columnconfigure(0, weight=1)
        table_container.grid_rowconfigure(0, weight=1)

        # Canvas de Background do Telegram preenchendo toda a área demarcada
        self.upload_bg_canvas = tk.Canvas(
            table_container,
            bg="#0e1621",
            highlightthickness=0,
            bd=0,
        )
        self.upload_bg_canvas.grid(
            row=0, column=0, padx=(6, 0), pady=6, sticky="nsew"
        )
        self.upload_bg_canvas.bind(
            "<Configure>",
            lambda _e: draw_telegram_wallpaper_canvas(self.upload_bg_canvas),
        )

        columns = ("check", "name", "folder", "size", "status", "progress", "sent_at")
        self.tree = ttk.Treeview(
            table_container,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Dark.Treeview",
            height=0,
        )
        self.tree.heading(
            "check",
            text="☑",
            anchor="center",
            command=self.toggle_all_upload_checkboxes,
        )
        self.tree.heading("name", text="Arquivo", anchor="w")
        self.tree.heading("folder", text="Pasta de Origem", anchor="w")
        self.tree.heading("size", text="Tamanho", anchor="e")
        self.tree.heading("status", text="Status", anchor="center")
        self.tree.heading("progress", text="Progresso", anchor="e")
        self.tree.heading("sent_at", text="Enviado em", anchor="center")

        self.tree.column("check", width=50, anchor="center", stretch=False)
        self.tree.column("name", width=260, anchor="w", stretch=True)
        self.tree.column("folder", width=210, anchor="w", stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("status", width=135, anchor="center", stretch=False)
        self.tree.column("progress", width=85, anchor="e", stretch=False)
        self.tree.column("sent_at", width=135, anchor="center", stretch=False)

        self.tree.tag_configure("Pendente", foreground="#e2e8f0")
        self.tree.tag_configure("Marcado", foreground="#38bdf8")
        self.tree.tag_configure("Enviando", foreground="#38bdf8")
        self.tree.tag_configure("Pausado", foreground="#fbbf24")
        self.tree.tag_configure("Aguardando 1h", foreground="#fbbf24")
        self.tree.tag_configure("Ignorado (Repetido)", foreground="#94a3b8")
        self.tree.tag_configure("Ignorado (> 2GB)", foreground="#94a3b8")
        self.tree.tag_configure("Concluído", foreground="#4ade80")
        self.tree.tag_configure("Já Enviado", foreground="#86efac")
        self.tree.tag_configure("Erro", foreground="#f87171")
        self.tree.tag_configure("Cancelado", foreground="#fbbf24")

        self.tree.bind("<Button-1>", self._on_upload_tree_click)
        self.tree.bind("<space>", self._on_upload_tree_space)

        scrollbar = ctk.CTkScrollbar(table_container, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.grid(row=0, column=0, padx=(6, 0), pady=(6, 0), sticky="new")
        scrollbar.grid(row=0, column=1, padx=6, pady=6, sticky="ns")

        self.tree.bind("<Delete>", lambda _event: self.remove_selected_items())

        # Painel Central de Aviso "Lendo os arquivos aguarde" (Item 7)
        self.upload_loading_overlay = ctk.CTkFrame(
            table_container,
            fg_color="#0f172a",
            border_width=2,
            border_color="#38bdf8",
            corner_radius=12,
        )
        ctk.CTkLabel(
            self.upload_loading_overlay,
            text="⏳ Lendo os arquivos, aguarde...",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#38bdf8",
        ).pack(padx=28, pady=(16, 4))
        self.upload_loading_sub_label = ctk.CTkLabel(
            self.upload_loading_overlay,
            text="Varrendo diretórios e preparando a lista. O botão Iniciar será liberado ao concluir.",
            font=ctk.CTkFont(size=12),
            text_color="#e2e8f0",
        )
        self.upload_loading_sub_label.pack(padx=28, pady=(0, 16))

        # Barra Inferior de Controles de Upload (Sem barra {filename}, com Excluir Selecionado e Botão Único Iniciar/Pausar)
        options_bar = ctk.CTkFrame(self.tab_upload)
        options_bar.grid(row=2, column=0, padx=8, pady=(6, 4), sticky="ew")
        options_bar.grid_columnconfigure(1, weight=1)

        self.force_doc_var = ctk.BooleanVar(
            value=self.config_data.get("force_document", True)
        )
        ctk.CTkCheckBox(
            options_bar,
            text="Enviar como Documento (sem compressão)",
            variable=self.force_doc_var,
            command=self._save_ui_prefs,
        ).grid(row=0, column=0, padx=12, pady=10, sticky="w")

        # Mantém atributos internos sem exibir a barra {filename}
        self.use_caption_var = ctk.BooleanVar(value=False)

        # 3. Botão Excluir Selecionado na parte inferior perto do Parar
        self.remove_sel_btn = ctk.CTkButton(
            options_bar,
            text="🗑 Excluir Selecionado",
            width=160,
            height=38,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#dc2626",
            hover_color="#b91c1c",
            command=self.remove_selected_items,
        )
        self.remove_sel_btn.grid(row=0, column=2, padx=(8, 4), pady=8)

        # 5. Botão unificado Iniciar / Pausar (Verde aguardando -> Amarelo ao iniciar -> Verde ao pausar)
        self.start_btn = ctk.CTkButton(
            options_bar,
            text="  INICIAR UPLOAD",
            image=self.nav_icons["play"],
            compound="left",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#16a34a",
            hover_color="#15803d",
            text_color="#ffffff",
            height=38,
            width=210,
            command=self.toggle_pause_upload,
        )
        self.start_btn.grid(row=0, column=3, padx=4, pady=8)
        self.pause_btn = self.start_btn

        self.cancel_btn = ctk.CTkButton(
            options_bar,
            text="■ Parar",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#dc2626",
            hover_color="#b91c1c",
            height=38,
            width=95,
            state="disabled",
            command=self.stop_upload_queue,
        )
        self.cancel_btn.grid(row=0, column=4, padx=(4, 10), pady=8)

    # -------------------------------------------------------------------------
    # SEÇÃO 2: DOWNLOAD DO TELEGRAM (Gerenciador de Nuvem Estilo Google Drive)
    # -------------------------------------------------------------------------
    def _build_download_tab(self):
        self.tab_download.grid_columnconfigure(0, weight=1)
        self.tab_download.grid_rowconfigure(1, weight=1)

        search_bar = ctk.CTkFrame(self.tab_download, fg_color="transparent")
        search_bar.grid(row=0, column=0, padx=8, pady=6, sticky="ew")
        search_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            search_bar,
            text="🔍 Buscar Arquivo no Grupo:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=(4, 8), sticky="w")

        self.search_var = tk.StringVar()
        self.search_var.trace_add(
            "write", lambda *_: self._apply_download_search_filter()
        )
        self.search_entry = ctk.CTkEntry(
            search_bar,
            textvariable=self.search_var,
            placeholder_text="Digite parte do nome do arquivo ou extensão (ex: relatorio, .pdf, .zip, .mp4, foto)...",
            height=34,
        )
        self.search_entry.grid(row=0, column=1, padx=4, sticky="ew")

        ctk.CTkButton(
            search_bar,
            text="Limpar Busca",
            width=105,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.search_var.set(""),
        ).grid(row=0, column=2, padx=4)

        self.scan_cloud_btn = ctk.CTkButton(
            search_bar,
            text="🔄 Escanear Arquivos do Grupo",
            width=210,
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self.scan_current_group_files,
        )
        self.scan_cloud_btn.grid(row=0, column=3, padx=(6, 6))

        self.cloud_summary_label = ctk.CTkLabel(
            search_bar,
            text="0 arquivos encontrados",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )
        self.cloud_summary_label.grid(row=0, column=4, padx=(6, 4), sticky="e")

        # Tabela de Arquivos Disponíveis na Nuvem do Grupo (Com Caixa de Seleção ☑ / ☐)
        dl_table_container = ctk.CTkFrame(self.tab_download, corner_radius=8)
        dl_table_container.grid(row=1, column=0, padx=8, pady=6, sticky="nsew")
        dl_table_container.grid_columnconfigure(0, weight=1)
        dl_table_container.grid_rowconfigure(0, weight=1)

        dl_cols = ("check", "filename", "ext", "size", "date", "status", "progress")
        self.dl_tree = ttk.Treeview(
            dl_table_container,
            columns=dl_cols,
            show="headings",
            selectmode="extended",
            style="Dark.Treeview",
        )
        self.dl_tree.heading("check", text="☑", anchor="center", command=self.toggle_all_download_checkboxes)
        self.dl_tree.heading("filename", text="Nome do Arquivo na Nuvem", anchor="w")
        self.dl_tree.heading("ext", text="Tipo", anchor="center")
        self.dl_tree.heading("size", text="Tamanho", anchor="e")
        self.dl_tree.heading("date", text="Data no Telegram", anchor="center")
        self.dl_tree.heading("status", text="Status", anchor="center")
        self.dl_tree.heading("progress", text="Download", anchor="e")

        self.dl_tree.column("check", width=50, anchor="center", stretch=False)
        self.dl_tree.column("filename", width=340, anchor="w", stretch=True)
        self.dl_tree.column("ext", width=80, anchor="center", stretch=False)
        self.dl_tree.column("size", width=100, anchor="e", stretch=False)
        self.dl_tree.column("date", width=140, anchor="center", stretch=False)
        self.dl_tree.column("status", width=165, anchor="center", stretch=False)
        self.dl_tree.column("progress", width=90, anchor="e", stretch=False)

        self.dl_tree.tag_configure("Disponível na Nuvem", foreground="#e2e8f0")
        self.dl_tree.tag_configure("Marcado", foreground="#38bdf8")
        self.dl_tree.tag_configure("Baixando...", foreground="#38bdf8")
        self.dl_tree.tag_configure("Pausado", foreground="#fbbf24")
        self.dl_tree.tag_configure("Aguardando 1h", foreground="#fbbf24")
        self.dl_tree.tag_configure("Baixado ✓", foreground="#4ade80")
        self.dl_tree.tag_configure("Já no PC ✓", foreground="#86efac")
        self.dl_tree.tag_configure("Erro", foreground="#f87171")

        self.dl_tree.bind("<Button-1>", self._on_dl_tree_click)
        self.dl_tree.bind("<space>", self._on_dl_tree_space)
        self.dl_tree.bind("<Delete>", lambda _event: self.remove_selected_download_items())

        dl_scroll = ctk.CTkScrollbar(dl_table_container, command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dl_scroll.set)

        self.dl_tree.grid(row=0, column=0, padx=(6, 0), pady=6, sticky="nsew")
        dl_scroll.grid(row=0, column=1, padx=6, pady=6, sticky="ns")

        # Painel Central de Aviso "Lendo os arquivos aguarde" também na aba Download (Item 7)
        self.download_loading_overlay = ctk.CTkFrame(
            dl_table_container,
            fg_color="#0f172a",
            border_width=2,
            border_color="#38bdf8",
            corner_radius=12,
        )
        ctk.CTkLabel(
            self.download_loading_overlay,
            text="⏳ Lendo os arquivos, aguarde...",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#38bdf8",
        ).pack(padx=28, pady=(16, 4))
        self.download_loading_sub_label = ctk.CTkLabel(
            self.download_loading_overlay,
            text="Varrendo arquivos do grupo no Telegram. Aguarde concluir para iniciar.",
            font=ctk.CTkFont(size=12),
            text_color="#e2e8f0",
        )
        self.download_loading_sub_label.pack(padx=28, pady=(0, 16))

        # Barra Inferior da Aba Download
        dl_bottom_bar = ctk.CTkFrame(self.tab_download)
        dl_bottom_bar.grid(row=2, column=0, padx=8, pady=(6, 4), sticky="ew")
        dl_bottom_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            dl_bottom_bar,
            text="📁 Escolher Pasta para Salvar",
            width=195,
            fg_color="#0d9488",
            hover_color="#0f766e",
            command=self.choose_download_directory,
        ).grid(row=0, column=0, padx=(10, 6), pady=8)

        self.dl_dir_entry = ctk.CTkEntry(dl_bottom_bar)
        self.dl_dir_entry.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        self.dl_dir_entry.insert(
            0, self.config_data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR))
        )

        ctk.CTkButton(
            dl_bottom_bar,
            text="📂 Abrir Pasta",
            width=100,
            fg_color="#475569",
            hover_color="#334155",
            command=self.open_download_folder_in_explorer,
        ).grid(row=0, column=2, padx=4, pady=8)

        ctk.CTkButton(
            dl_bottom_bar,
            text="Selecionar Todos",
            width=120,
            fg_color="#334155",
            hover_color="#1e293b",
            command=self.select_all_download_items,
        ).grid(row=0, column=3, padx=4, pady=8)

        ctk.CTkButton(
            dl_bottom_bar,
            text="🗑 Excluir Selecionado",
            width=155,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#dc2626",
            hover_color="#b91c1c",
            command=self.remove_selected_download_items,
        ).grid(row=0, column=4, padx=4, pady=8)

        self.dl_start_btn = ctk.CTkButton(
            dl_bottom_bar,
            text="  BAIXAR SELECIONADOS",
            image=self.nav_icons["play"],
            compound="left",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#16a34a",
            hover_color="#15803d",
            text_color="#ffffff",
            height=38,
            width=215,
            command=self.toggle_pause_download,
        )
        self.dl_start_btn.grid(row=0, column=5, padx=(8, 4), pady=8)
        self.dl_pause_btn = self.dl_start_btn

        self.dl_cancel_btn = ctk.CTkButton(
            dl_bottom_bar,
            text="■ Parar",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#dc2626",
            hover_color="#b91c1c",
            height=38,
            width=85,
            state="disabled",
            command=self.stop_download_queue,
        )
        self.dl_cancel_btn.grid(row=0, column=6, padx=(4, 10), pady=8)

    # -------------------------------------------------------------------------
    # SEÇÃO 3: HISTÓRICO DE ARQUIVOS ENVIADOS
    # -------------------------------------------------------------------------
    def _build_history_tab(self):
        self.tab_history.grid_columnconfigure(0, weight=1)
        self.tab_history.grid_rowconfigure(1, weight=1)

        top_bar = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        top_bar.grid(row=0, column=0, padx=8, pady=6, sticky="ew")

        ctk.CTkLabel(
            top_bar,
            text="✅ Registro Completo de Arquivos Enviados neste Computador",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left", padx=6)

        self.history_count_label = ctk.CTkLabel(
            top_bar,
            text="0 arquivos enviados",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#4ade80",
        )
        self.history_count_label.pack(side="left", padx=16)

        ctk.CTkButton(
            top_bar,
            text="Limpar Histórico de Enviados",
            width=190,
            fg_color="#475569",
            hover_color="#dc2626",
            command=self.clear_sent_history,
        ).pack(side="right", padx=6)

        hist_container = ctk.CTkFrame(self.tab_history, corner_radius=8)
        hist_container.grid(row=1, column=0, padx=8, pady=6, sticky="nsew")
        hist_container.grid_columnconfigure(0, weight=1)
        hist_container.grid_rowconfigure(0, weight=1)

        cols = ("filename", "size", "target", "sent_at", "path")
        self.history_tree = ttk.Treeview(
            hist_container,
            columns=cols,
            show="headings",
            selectmode="extended",
            style="Dark.Treeview",
        )
        self.history_tree.heading("filename", text="Arquivo Enviado", anchor="w")
        self.history_tree.heading("size", text="Tamanho", anchor="e")
        self.history_tree.heading("target", text="Grupo / Chat de Destino", anchor="w")
        self.history_tree.heading(
            "sent_at", text="Data / Hora do Envio", anchor="center"
        )
        self.history_tree.heading("path", text="Caminho Original", anchor="w")

        self.history_tree.column("filename", width=260, anchor="w", stretch=True)
        self.history_tree.column("size", width=95, anchor="e", stretch=False)
        self.history_tree.column("target", width=220, anchor="w", stretch=False)
        self.history_tree.column("sent_at", width=150, anchor="center", stretch=False)
        self.history_tree.column("path", width=280, anchor="w", stretch=True)

        self.history_tree.tag_configure("Concluído", foreground="#4ade80")

        hist_scroll = ctk.CTkScrollbar(hist_container, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=hist_scroll.set)

        self.history_tree.grid(row=0, column=0, padx=(6, 0), pady=6, sticky="nsew")
        hist_scroll.grid(row=0, column=1, padx=6, pady=6, sticky="ns")

    def _populate_history_tree(self):
        for iid in self.history_tree.get_children():
            self.history_tree.delete(iid)
        for rec in self.history_records:
            self.history_tree.insert(
                "",
                "end",
                values=(
                    f"✓ {rec.get('filename', '')}",
                    rec.get("size_str", ""),
                    rec.get("target", ""),
                    rec.get("sent_at", ""),
                    rec.get("path", ""),
                ),
                tags=("Concluído",),
            )
        self._update_history_summary()

    def _update_history_summary(self):
        total_sent = len(self.history_records)
        total_bytes = sum(int(r.get("size", 0)) for r in self.history_records)
        self.history_count_label.configure(
            text=f"Total já enviado: {total_sent} arquivo(s) ({format_size(total_bytes)})"
        )
        if "history" in self.nav_buttons:
            suffix = f" ({total_sent})" if total_sent > 0 else ""
            self.nav_buttons["history"].configure(
                text=f"  HISTÓRICO DE ENVIADOS{suffix}"
            )

    def clear_sent_history(self):
        if not self.history_records:
            return
        if not messagebox.askyesno(
            "Limpar Histórico",
            "Deseja realmente apagar a lista de histórico de arquivos enviados?",
        ):
            return
        self.history_records.clear()
        self.sent_signatures.clear()
        self._save_history()
        self._populate_history_tree()
        self.log("Histórico de arquivos enviados foi limpo.")

    # -------------------------------------------------------------------------
    # SEÇÃO 4: CONTA DO TELEGRAM (QR CODE INLINE + CÓDIGO DO PAÍS + LOG COMPLETO)
    # -------------------------------------------------------------------------
    def _build_account_tab(self):
        self.tab_account.grid_columnconfigure(0, weight=5)
        self.tab_account.grid_columnconfigure(1, weight=6)
        self.tab_account.grid_rowconfigure(1, weight=1)

        info_banner = ctk.CTkFrame(
            self.tab_account, fg_color="#1e293b", corner_radius=8
        )
        info_banner.grid(
            row=0, column=0, columnspan=2, padx=10, pady=(4, 6), sticky="ew"
        )

        ctk.CTkLabel(
            info_banner,
            text=(
                "🔒 Segurança Ativada: Sua sessão de login fica vinculada exclusivamente a ESTE computador. "
                "As credenciais de API ID / API Hash e dados da conta podem ser consultados no botão '⚙ Opções' > 'Informações da Conta'."
            ),
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#cbd5e1",
        ).pack(padx=12, pady=6, anchor="w")

        # Coluna Esquerda: Login Conta do Telegram (QR Code na mesma tela + Telefone com Código do País)
        personal_frame = ctk.CTkScrollableFrame(self.tab_account, fg_color="#17212b", corner_radius=10)
        personal_frame.grid(row=1, column=0, padx=(10, 6), pady=4, sticky="nsew")
        personal_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            personal_frame,
            text="Conectar Conta do Telegram",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#38bdf8",
        ).grid(row=0, column=0, padx=12, pady=(8, 6), sticky="w")

        # 1. Botão Entrar com QR Code + Quadro Inline na mesma tela
        self.qr_btn = ctk.CTkButton(
            personal_frame,
            text="📱 Entrar com QR Code",
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_qr_login,
        )
        self.qr_btn.grid(row=1, column=0, padx=12, pady=(2, 6), sticky="ew")

        self.qr_inline_frame = ctk.CTkFrame(
            personal_frame,
            fg_color="#0e1621",
            corner_radius=10,
            border_width=1,
            border_color="#334155",
        )
        self.qr_inline_frame.grid(row=2, column=0, padx=12, pady=(2, 8), sticky="ew")
        self.qr_inline_frame.grid_columnconfigure(0, weight=1)

        self.qr_status_label = ctk.CTkLabel(
            self.qr_inline_frame,
            text="Clique em '📱 Entrar com QR Code' acima para gerar o código aqui na tela.",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            justify="center",
            wraplength=360,
        )
        self.qr_status_label.grid(row=0, column=0, padx=10, pady=(8, 4))

        self.qr_image_label = ctk.CTkLabel(
            self.qr_inline_frame,
            text="",
            height=10,
        )
        self.qr_image_label.grid(row=1, column=0, padx=10, pady=(2, 8))

        # 2. Ou entrar com número de telefone (Código do País + Número)
        phone_section = ctk.CTkFrame(
            personal_frame,
            fg_color="#0e1621",
            corner_radius=10,
            border_width=1,
            border_color="#334155",
        )
        phone_section.grid(row=3, column=0, padx=12, pady=(4, 8), sticky="ew")
        phone_section.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            phone_section,
            text="📞 Ou entrar com número de telefone:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(8, 4), sticky="w")

        ctk.CTkLabel(
            phone_section,
            text="Código do País:",
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, padx=(12, 6), pady=4, sticky="w")

        self.country_code_combo = ctk.CTkComboBox(
            phone_section,
            values=COUNTRY_CODES,
            width=210,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.country_code_combo.grid(row=1, column=1, padx=(4, 12), pady=4, sticky="ew")
        self.country_code_combo.set(
            self.config_data.get("country_code", "+55 (Brasil)")
        )

        ctk.CTkLabel(
            phone_section,
            text="DDD + Telefone:",
            font=ctk.CTkFont(size=12),
        ).grid(row=2, column=0, padx=(12, 6), pady=4, sticky="w")

        self.phone_entry = ctk.CTkEntry(
            phone_section, placeholder_text="Ex: 11999999999"
        )
        self.phone_entry.grid(row=2, column=1, padx=(4, 12), pady=4, sticky="ew")
        saved_phone = self.config_data.get("phone_national", "")
        if saved_phone:
            self.phone_entry.insert(0, saved_phone)

        self.send_code_btn = ctk.CTkButton(
            phone_section,
            text="📨 Enviar Código SMS / Telegram",
            fg_color="#0284c7",
            hover_color="#0369a1",
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.send_phone_code,
        )
        self.send_code_btn.grid(
            row=3, column=0, columnspan=2, padx=12, pady=(6, 4), sticky="ew"
        )

        # Campo inline para digitar o código recebido no celular sem precisar de janela externa
        code_row = ctk.CTkFrame(phone_section, fg_color="transparent")
        code_row.grid(row=4, column=0, columnspan=2, padx=12, pady=(2, 6), sticky="ew")
        code_row.grid_columnconfigure(0, weight=1)

        self.phone_code_entry = ctk.CTkEntry(
            code_row,
            placeholder_text="Digite aqui o Código recebido no Telegram",
        )
        self.phone_code_entry.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.confirm_code_btn = ctk.CTkButton(
            code_row,
            text="✓ Confirmar Código",
            width=145,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.confirm_inline_phone_code,
        )
        self.confirm_code_btn.grid(row=0, column=1)

        ctk.CTkLabel(
            phone_section,
            text="Senha 2FA (se tiver):",
            font=ctk.CTkFont(size=12),
        ).grid(row=5, column=0, padx=(12, 6), pady=(2, 8), sticky="w")
        self.password_2fa_entry = ctk.CTkEntry(
            phone_section,
            show="*",
            placeholder_text="Apenas se tiver verificação em 2 etapas",
        )
        self.password_2fa_entry.grid(
            row=5, column=1, padx=(4, 12), pady=(2, 8), sticky="ew"
        )

        self.logout_btn = ctk.CTkButton(
            personal_frame,
            text="Desconectar Sessão Atual (Trocar de Conta)",
            fg_color="#dc2626",
            hover_color="#b91c1c",
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.logout_session,
        )
        self.logout_btn.grid(row=4, column=0, padx=12, pady=(6, 10), sticky="ew")

        # Coluna Direita: Registro Completo de Atividades e Diagnóstico de Erros (LOG) + Opção Bot
        right_frame = ctk.CTkFrame(self.tab_account, fg_color="#17212b", corner_radius=10)
        right_frame.grid(row=1, column=1, padx=(6, 10), pady=4, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(1, weight=1)

        log_header = ctk.CTkFrame(right_frame, fg_color="transparent")
        log_header.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header,
            text="📋 Registro de Atividades e Erros do Sistema (LOG):",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#38bdf8",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            log_header,
            text="Limpar Log da Tela",
            width=125,
            height=26,
            fg_color="#334155",
            hover_color="#475569",
            font=ctk.CTkFont(size=11),
            command=lambda: self.log_box.delete("1.0", "end"),
        ).grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(
            right_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#0e1621",
            text_color="#e2e8f0",
        )
        self.log_box.grid(
            row=1, column=0, padx=14, pady=(2, 8), sticky="nsew"
        )

        # Opção compacta de Bot Token na parte inferior direita
        bot_box = ctk.CTkFrame(right_frame, fg_color="#0e1621", corner_radius=8)
        bot_box.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="ew")
        bot_box.grid_columnconfigure(1, weight=1)
        bot_box.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            bot_box,
            text="Modo Bot (Opcional até 50MB):",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
        ).grid(row=0, column=0, columnspan=5, padx=10, pady=(6, 2), sticky="w")

        ctk.CTkLabel(bot_box, text="Token:", font=ctk.CTkFont(size=11)).grid(
            row=1, column=0, padx=(10, 4), pady=(2, 8), sticky="w"
        )
        self.bot_token_entry = ctk.CTkEntry(
            bot_box, placeholder_text="123456:ABC...", height=28
        )
        self.bot_token_entry.grid(row=1, column=1, padx=4, pady=(2, 8), sticky="ew")
        if self.config_data.get("bot_token"):
            self.bot_token_entry.insert(0, self.config_data.get("bot_token"))

        ctk.CTkLabel(bot_box, text="Chat ID:", font=ctk.CTkFont(size=11)).grid(
            row=1, column=2, padx=(6, 4), pady=(2, 8), sticky="w"
        )
        self.bot_chat_entry = ctk.CTkEntry(
            bot_box, placeholder_text="-100... ou @canal", height=28
        )
        self.bot_chat_entry.grid(row=1, column=3, padx=4, pady=(2, 8), sticky="ew")
        if self.config_data.get("bot_chat_id"):
            self.bot_chat_entry.insert(0, self.config_data.get("bot_chat_id"))

        ctk.CTkButton(
            bot_box,
            text="Ativar Bot",
            width=90,
            height=28,
            fg_color="#0d9488",
            hover_color="#0f766e",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.activate_bot_mode,
        ).grid(row=1, column=4, padx=(4, 10), pady=(2, 8))

        self.log(f"{APP_TITLE} iniciado.")

    # -------------------------------------------------------------------------
    # Utilitários de Log e Eventos Globais (Com Gravação Persistente em Disco)
    # -------------------------------------------------------------------------
    def log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        level = (
            "ERROR"
            if any(w in message.lower() for w in ("erro", "falha", "exceção", "exception", "traceback"))
            else "INFO"
        )
        append_persistent_log(message, level=level)

        def _append():
            if self.winfo_exists() and hasattr(self, "log_box"):
                self.log_box.insert("end", line)
                self.log_box.see("end")

        self.after(0, _append)

    def _save_ui_prefs(self):
        self.config_data["force_document"] = self.force_doc_var.get()
        self.config_data["use_caption"] = False
        self.config_data["skip_already_sent"] = self.skip_sent_var.get()
        if hasattr(self, "ignore_duplicates_var"):
            self.config_data["ignore_duplicates_prompt"] = self.ignore_duplicates_var.get()
        self.config_data["download_dir"] = self.dl_dir_entry.get().strip()
        self._save_config()

    def _on_target_changed(self, choice: str):
        self.config_data["last_target"] = choice
        self._save_config()
        self._save_queue_state()
        if self.connected_mode == "personal":
            self.scan_current_group_files()

    # -------------------------------------------------------------------------
    # Conexão e Login no Telegram
    # -------------------------------------------------------------------------
    def _get_api_credentials(self) -> Tuple[int, str]:
        try:
            api_id = int(str(self.config_data.get("api_id", DEFAULT_API_ID)).strip())
        except ValueError:
            api_id = DEFAULT_API_ID
        api_hash = str(self.config_data.get("api_hash", DEFAULT_API_HASH)).strip() or DEFAULT_API_HASH
        self.config_data["api_id"] = api_id
        self.config_data["api_hash"] = api_hash
        self._save_config()
        return api_id, api_hash

    def _try_auto_connect(self):
        session_file = Path(f"{SESSION_PERSONAL}.session")
        if session_file.exists():
            self.log("Sessão local deste PC encontrada. Conectando ao Telegram...")
            self.worker.run_coro(
                self._async_connect_existing(),
                callback=self._on_auto_connect_done,
                error_callback=lambda e: self.log(f"Falha ao reconectar sessão: {e}"),
            )
        elif self.config_data.get("mode") == "bot" and self.config_data.get("bot_token"):
            self.activate_bot_mode()
        else:
            self.log(
                "Nenhuma conta conectada neste computador. Clique em 'CONTA DO TELEGRAM' para entrar."
            )

    async def _async_connect_existing(self):
        api_id, api_hash = self._get_api_credentials()
        if self.client is None:
            self.client = TelegramClient(SESSION_PERSONAL, api_id, api_hash)
        if not self.client.is_connected():
            await self.client.connect()
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            dialogs = await self._fetch_dialogs_list()
            return me, dialogs
        return None, []

    def _on_auto_connect_done(self, result):
        me, dialogs = result
        if me:
            first_name = (getattr(me, "first_name", "") or "").strip()
            last_name = (getattr(me, "last_name", "") or "").strip()
            username = (getattr(me, "username", "") or "").strip()
            phone = (getattr(me, "phone", "") or "").strip()
            full_name = f"{first_name} {last_name}".strip()
            if not full_name:
                full_name = f"@{username}" if username else str(me.id)

            self.current_user_info = {
                "name": full_name,
                "username": f"@{username}" if username else "-",
                "phone": f"+{phone}" if phone and not phone.startswith("+") else (phone or "-"),
                "id": str(me.id),
            }
            self.config_data["connected_user_name"] = full_name
            self._save_config()

            self.connected_mode = "personal"
            self._update_connected_ui(f"Conectado: {full_name}", dialogs, auto_scan=True)
            self.log(f"✓ Conectado ao Telegram como Usuário: {full_name} (ID: {me.id})!")

            # Se havia tarefa interrompida quando o programa fechou, retoma após escanear o grupo
            if getattr(self, "resume_upload_on_connect", False):
                self.resume_upload_on_connect = False
                self.after(2500, self._auto_resume_interrupted_upload)
        else:
            self.log(
                "Sessão não autorizada. Faça login na opção 'CONTA DO TELEGRAM'."
            )

    def _auto_resume_interrupted_upload(self):
        """Retoma automaticamente o upload de tarefas que estavam em andamento quando o programa fechou."""
        if self.is_uploading or not self.connected_mode:
            return
        pending = [
            it
            for it in self.queue
            if it.status in ("Pendente", "Pausado", "Erro") or it.status.startswith("Erro")
        ]
        if pending:
            self.log(
                f"♻ Retomando automaticamente o upload de {len(pending)} arquivo(s) pendente(s) da sessão anterior..."
            )
            self.start_upload_queue()

    async def _fetch_dialogs_list(self) -> List[Tuple[str, Union[int, str]]]:
        items: List[Tuple[str, Union[int, str]]] = [
            ("⭐ Mensagens Salvas (Saved Messages)", "me")
        ]
        if not self.client:
            return items
        used_labels: Dict[str, int] = {"⭐ Mensagens Salvas (Saved Messages)": 1}
        async for dialog in self.client.iter_dialogs(limit=250):
            if dialog.is_user and getattr(dialog.entity, "self", False):
                continue
            title = (dialog.name or "Sem Nome").strip()
            if dialog.is_channel:
                prefix = (
                    "📢 Canal: "
                    if getattr(dialog.entity, "broadcast", False)
                    else "👥 Grupo: "
                )
            elif dialog.is_group:
                prefix = "👥 Grupo: "
            else:
                prefix = "👤 "
            base_label = f"{prefix}{title}"
            if base_label in used_labels:
                used_labels[base_label] += 1
                label = f"{base_label} ({used_labels[base_label]})"
            else:
                used_labels[base_label] = 1
                label = base_label
            items.append((label, dialog.id))
        return items

    def _update_connected_ui(
        self,
        badge_text: str,
        dialogs: List[Tuple[str, Union[int, str]]],
        auto_scan: bool = False,
    ):
        def _apply():
            self.status_badge.configure(text=f"● {badge_text}", text_color="#4ade80")
            self.dialogs_map.clear()
            labels = []
            for label, entity_id in dialogs:
                self.dialogs_map[label] = entity_id
                labels.append(label)
            if labels:
                self.target_combo.configure(values=labels)
                saved_target = self.config_data.get("last_target", "")
                if saved_target and " [" in saved_target and saved_target.endswith("]"):
                    saved_target = saved_target.rsplit(" [", 1)[0]
                if saved_target in self.dialogs_map:
                    self.target_combo.set(saved_target)
                else:
                    # Tenta encontrar correspondência parcial pelo título do grupo
                    matched = next(
                        (lbl for lbl in labels if saved_target and saved_target in lbl),
                        labels[0],
                    )
                    self.target_combo.set(matched)
            if auto_scan and self.connected_mode == "personal":
                self.scan_current_group_files()

        self.after(0, _apply)

    def refresh_dialogs(self):
        if self.connected_mode != "personal" or not self.client:
            messagebox.showinfo(
                "Atualizar Grupos",
                "Conecte sua Conta Pessoal primeiro na opção 'CONTA DO TELEGRAM' para listar seus grupos e chats.",
            )
            return
        self.log("Atualizando lista de grupos, canais e chats...")
        self.worker.run_coro(
            self._fetch_dialogs_list(),
            callback=lambda d: (
                self._update_connected_ui(
                    self.status_badge.cget("text").replace("● ", ""), d, auto_scan=True
                ),
                self.log(f"{len(d)} grupos/chats carregados."),
            ),
            error_callback=lambda e: self.log(f"Erro ao carregar chats: {e}"),
        )

    # --- Login via QR Code (Gerado na Mesma Tela sem Abrir Janela Externa) ---
    def start_qr_login(self):
        self.log("Gerando QR Code de login na tela Conta do Telegram...")
        if hasattr(self, "qr_status_label"):
            self.qr_status_label.configure(
                text="⏳ Gerando QR Code do Telegram... Aguarde um instante.",
                text_color="#38bdf8",
            )
        pwd_snapshot = (
            self.password_2fa_entry.get().strip()
            if hasattr(self, "password_2fa_entry")
            else ""
        )
        self.worker.run_coro(
            self._async_qr_login_flow(pwd_snapshot),
            error_callback=lambda e: self.log(f"Erro no login QR: {e}"),
        )

    async def _async_qr_login_flow(self, pwd_snapshot: str = ""):
        api_id, api_hash = self._get_api_credentials()
        if self.client is None:
            self.client = TelegramClient(SESSION_PERSONAL, api_id, api_hash)
        if not self.client.is_connected():
            await self.client.connect()

        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            dialogs = await self._fetch_dialogs_list()
            self._on_auto_connect_done((me, dialogs))
            self.after(0, self._close_qr_window)
            return

        qr_login = await self.client.qr_login()
        self._show_qr_window(qr_login.url)

        try:
            user = await qr_login.wait(timeout=120)
        except errors.SessionPasswordNeededError:
            pwd = pwd_snapshot
            if not pwd:
                self.log(
                    "Sua conta possui senha de Verificação em Duas Etapas (2FA). Preencha o campo Senha 2FA e clique novamente."
                )
                self.after(0, self._close_qr_window)
                self.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Senha 2FA Necessária",
                        "Sua conta tem Verificação em Duas Etapas (2FA).\nDigite sua senha no campo 'Senha 2FA' na aba de Login e clique em Entrar com QR Code novamente!",
                    ),
                )
                return
            user = await self.client.sign_in(password=pwd)

        self.after(0, self._close_qr_window)
        dialogs = await self._fetch_dialogs_list()
        self.config_data["mode"] = "personal"
        self._save_config()
        self._on_auto_connect_done((user, dialogs))
        self.after(0, lambda: self.switch_page("upload"))

    def _show_qr_window(self, url: str):
        """Renderiza o QR Code diretamente na mesma tela (aba Conta do Telegram), sem abrir janela externa."""
        def _render_inline():
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(200, 200))

            if hasattr(self, "qr_status_label"):
                self.qr_status_label.configure(
                    text=(
                        "📱 Abra o Telegram no celular:\n"
                        "Configurações > Dispositivos > Conectar Dispositivo\n"
                        "Aponte a câmera para o QR Code abaixo:"
                    ),
                    text_color="#4ade80",
                )
            if hasattr(self, "qr_image_label"):
                self.qr_image_label.configure(text="", image=ctk_img, height=210)
                self.qr_image_label.image = ctk_img

        self.after(0, _render_inline)

    def _close_qr_window(self):
        """Limpa o QR Code inline após conectar."""
        if hasattr(self, "qr_image_label"):
            self.qr_image_label.configure(image=None, text="", height=10)
            self.qr_image_label.image = None
        if hasattr(self, "qr_status_label"):
            self.qr_status_label.configure(
                text="✓ QR Code finalizado. Clique em '📱 Entrar com QR Code' caso queira gerar um novo código.",
                text_color="#94a3b8",
            )

    # --- Login via Telefone (Com Seleção de Código do País + Confirmação na Mesma Tela) ---
    def send_phone_code(self):
        country_raw = (
            self.country_code_combo.get().strip()
            if hasattr(self, "country_code_combo")
            else "+55 (Brasil)"
        )
        prefix = country_raw.split()[0].strip() if country_raw else "+55"
        if not prefix.startswith("+"):
            prefix = "+" + prefix

        raw_number = self.phone_entry.get().strip()
        digits = "".join(ch for ch in raw_number if ch.isdigit())
        if not digits:
            messagebox.showwarning(
                "Telefone",
                "Escolha o código do país e digite o seu número de telefone com DDD (ex: 11999999999).",
            )
            return

        # Se o usuário já digitou o +código completo no campo número, evita duplicar
        if raw_number.startswith("+"):
            full_phone = "+" + digits
        else:
            full_phone = f"{prefix}{digits}"

        self.config_data["country_code"] = country_raw
        self.config_data["phone_national"] = digits
        self.config_data["phone"] = full_phone
        self._save_config()

        self.log(f"Enviando código de verificação para {full_phone}...")
        self.worker.run_coro(
            self._async_send_code(full_phone),
            callback=self._prompt_phone_code,
            error_callback=lambda e: self.log(f"Erro ao enviar código para {full_phone}: {e}"),
        )

    async def _async_send_code(self, phone: str):
        api_id, api_hash = self._get_api_credentials()
        if self.client is None:
            self.client = TelegramClient(SESSION_PERSONAL, api_id, api_hash)
        if not self.client.is_connected():
            await self.client.connect()
        sent = await self.client.send_code_request(phone)
        return phone, sent.phone_code_hash

    def _prompt_phone_code(self, data):
        phone, phone_code_hash = data
        self._pending_phone_login = (phone, phone_code_hash)
        self.log(
            f"✓ Código enviado para {phone}! Digite o código no campo 'Código recebido no Telegram' e clique em '✓ Confirmar Código'."
        )
        if hasattr(self, "phone_code_entry"):
            self.phone_code_entry.focus_force()

    def confirm_inline_phone_code(self):
        """Valida o código digitado na própria guia Conta do Telegram."""
        if not hasattr(self, "_pending_phone_login") or not self._pending_phone_login:
            messagebox.showwarning(
                "Código de Verificação",
                "Clique primeiro em '📨 Enviar Código SMS / Telegram' para receber o código no seu celular.",
            )
            return
        code = self.phone_code_entry.get().strip()
        if not code:
            messagebox.showwarning(
                "Código de Verificação",
                "Digite o código numérico que você recebeu no Telegram ou por SMS.",
            )
            return
        phone, phone_code_hash = self._pending_phone_login
        pwd = self.password_2fa_entry.get().strip()
        self.log(f"Validando código de acesso para {phone}...")
        self.worker.run_coro(
            self._async_complete_phone_login(phone, code, phone_code_hash, pwd),
            callback=self._on_auto_connect_done,
            error_callback=lambda e: self.log(f"Erro ao validar código: {e}"),
        )

    async def _async_complete_phone_login(
        self, phone: str, code: str, phone_code_hash: str, password: str
    ):
        try:
            user = await self.client.sign_in(
                phone=phone, code=code, phone_code_hash=phone_code_hash
            )
        except errors.SessionPasswordNeededError:
            if not password:
                raise RuntimeError(
                    "Sua conta exige Senha 2FA. Preencha o campo 'Senha 2FA' e tente novamente."
                )
            user = await self.client.sign_in(password=password)
        dialogs = await self._fetch_dialogs_list()
        self.config_data["mode"] = "personal"
        self._save_config()
        self.after(0, lambda: self.switch_page("upload"))
        return user, dialogs

    # --- Modo Bot Token ---
    def activate_bot_mode(self):
        token = self.bot_token_entry.get().strip()
        chat_id = self.bot_chat_entry.get().strip()
        if not token or not chat_id:
            messagebox.showwarning(
                "Modo Bot",
                "Informe o Bot Token (do @BotFather) e o Chat ID (ou @username do canal).",
            )
            return
        self.config_data["mode"] = "bot"
        self.config_data["bot_token"] = token
        self.config_data["bot_chat_id"] = chat_id
        self._save_config()

        self.connected_mode = "bot"
        label = f"🤖 Bot -> Destino [{chat_id}]"
        self._update_connected_ui(
            f"Modo Bot Ativo ({chat_id})", [(label, chat_id)], auto_scan=False
        )
        self.log(f"Modo Bot configurado para enviar ao chat {chat_id}.")
        self.switch_page("upload")

    def logout_session(self):
        async def _do_logout():
            if self.client and self.client.is_connected():
                try:
                    await self.client.log_out()
                except Exception:
                    await self.client.disconnect()
            self.client = None
            session_file = Path(f"{SESSION_PERSONAL}.session")
            if session_file.exists():
                try:
                    session_file.unlink()
                except Exception:
                    pass

        self.worker.run_coro(
            _do_logout(),
            callback=lambda _: self._reset_disconnected_ui(),
            error_callback=lambda e: self.log(f"Aviso ao desconectar: {e}"),
        )

    def _reset_disconnected_ui(self):
        self.connected_mode = None
        self.status_badge.configure(text="● Desconectado", text_color="#f87171")
        self.remote_files.clear()
        self.remote_by_id.clear()
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)
        self.cloud_summary_label.configure(text="0 arquivos encontrados")
        if "download" in self.nav_buttons:
            self.nav_buttons["download"].configure(text="  DOWNLOAD DO TELEGRAM")
        self.log("Sessão desconectada com sucesso.")

    # -------------------------------------------------------------------------
    # GERENCIADOR DE DOWNLOAD (Varredura Automática, Busca e Download do Grupo)
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # GERENCIADOR DE DOWNLOAD (Varredura Rápida em 2º Plano + Cache Incremental)
    # -------------------------------------------------------------------------
    def _load_group_cache_entry(self, entity_key: str) -> dict:
        if not GROUP_CACHE_FILE.exists():
            return {}
        try:
            all_cache = json.loads(GROUP_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(all_cache, dict):
                return all_cache.get(str(entity_key), {}) or {}
        except Exception:
            pass
        return {}

    def _save_group_cache_entry(
        self,
        entity_key: str,
        files_list: List[RemoteCloudFile],
        fully_scanned: bool,
    ):
        try:
            all_cache = {}
            if GROUP_CACHE_FILE.exists():
                try:
                    loaded = json.loads(GROUP_CACHE_FILE.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        all_cache = loaded
                except Exception:
                    all_cache = {}

            msg_ids = [f.message_id for f in files_list if f.message_id]
            min_id = min(msg_ids) if msg_ids else 0
            max_id = max(msg_ids) if msg_ids else 0
            serialized_files = [
                {
                    "id": f.message_id,
                    "fn": f.filename,
                    "ext": f.ext,
                    "sz": f.size,
                    "dt": f.date_str,
                    "cap": f.caption,
                }
                for f in files_list
            ]
            all_cache[str(entity_key)] = {
                "min_id": min_id,
                "max_id": max_id,
                "fully_scanned": bool(fully_scanned),
                "files": serialized_files,
            }
            GROUP_CACHE_FILE.write_text(
                json.dumps(all_cache, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def scan_current_group_files(self, force_full_rescan: bool = False):
        if self.connected_mode != "personal" or not self.client:
            return

        target_label = self.target_combo.get()
        target_entity = self.dialogs_map.get(target_label, "me")
        entity_key = str(target_entity)

        self._remote_scan_token += 1
        current_token = self._remote_scan_token
        self.is_scanning_remote = True

        self.scan_cloud_btn.configure(
            state="disabled", text="⏳ Sincronizando em 2º plano..."
        )

        self.remote_files.clear()
        self.remote_by_id.clear()
        self.remote_by_filename_lower.clear()
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)

        dl_dir = Path(
            self.config_data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR))
        )

        # 1. Carrega instantaneamente o cache local deste grupo (se existir e não for forçado reset total)
        cache_entry = (
            {} if force_full_rescan else self._load_group_cache_entry(entity_key)
        )
        cached_raw_list = cache_entry.get("files") or []
        min_cached_id = int(cache_entry.get("min_id", 0) or 0)
        max_cached_id = int(cache_entry.get("max_id", 0) or 0)
        fully_scanned = bool(cache_entry.get("fully_scanned", False))

        if cached_raw_list:
            for entry in cached_raw_list:
                mid = int(entry.get("id", 0) or 0)
                if not mid:
                    continue
                fname = str(entry.get("fn", f"arquivo_{mid}"))
                fsize = int(entry.get("sz", 0) or 0)
                row_id = f"dl_{mid}"
                if row_id in self.remote_by_id:
                    continue
                local_candidate = dl_dir / fname
                status = (
                    "Já no PC ✓"
                    if (
                        local_candidate.exists()
                        and (fsize == 0 or local_candidate.stat().st_size == fsize)
                    )
                    else "Disponível na Nuvem"
                )
                rf = RemoteCloudFile(
                    row_id=row_id,
                    message_id=mid,
                    filename=fname,
                    ext=str(entry.get("ext", Path(fname).suffix.lower() or "-")),
                    size=fsize,
                    date_str=str(entry.get("dt", "-")),
                    caption=str(entry.get("cap", "")),
                    status=status,
                    progress=1.0 if status == "Já no PC ✓" else 0.0,
                    message_obj=None,
                )
                self.remote_files.append(rf)
                self.remote_by_id[row_id] = rf
                self.remote_by_filename_lower[fname.lower()] = rf

            self._apply_download_search_filter()
            if "download" in self.nav_buttons:
                self.nav_buttons["download"].configure(
                    text=f"  DOWNLOAD DO TELEGRAM ({len(self.remote_files)})"
                )
            self.log(
                f"⚡ {len(self.remote_files)} arquivo(s) carregados instantaneamente do cache de '{target_label}'. Sincronizando novos em 2º plano..."
            )
        else:
            self.cloud_summary_label.configure(
                text="Sincronizando arquivos em 2º plano..."
            )
            self.log(
                f"🔍 Sincronizando arquivos do grupo '{target_label}' em 2º plano (você já pode usar o programa normalmente)..."
            )

        self.worker.run_coro(
            self._async_scan_group_files(
                target_entity,
                target_label,
                entity_key,
                current_token,
                min_cached_id,
                max_cached_id,
                fully_scanned,
            ),
            callback=lambda res: self._on_scan_group_done(
                res, target_label, entity_key, current_token
            ),
            error_callback=lambda e: self._on_scan_group_error(e),
        )

    def _extract_remote_file_from_msg(
        self, msg, dl_dir: Path
    ) -> Optional[RemoteCloudFile]:
        if not msg or not msg.media:
            return None
        filename = ""
        size = 0
        if isinstance(msg.media, MessageMediaDocument) and msg.media.document:
            doc = msg.media.document
            size = getattr(doc, "size", 0) or 0
            for attr in getattr(doc, "attributes", []):
                if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                    filename = attr.file_name
                    break
            if not filename:
                mime = getattr(doc, "mime_type", "") or ""
                ext_guess = "." + mime.split("/")[-1] if "/" in mime else ".bin"
                filename = f"arquivo_{msg.id}{ext_guess}"
        elif isinstance(msg.media, MessageMediaPhoto) and msg.media.photo:
            filename = f"foto_{msg.id}.jpg"
            sizes = getattr(msg.media.photo, "sizes", [])
            if sizes:
                last_sz = sizes[-1]
                size = getattr(last_sz, "size", 0) or 0
        else:
            return None

        ext = Path(filename).suffix.lower() or "-"
        date_str = (
            msg.date.astimezone().strftime("%d/%m/%Y %H:%M")
            if getattr(msg, "date", None)
            else "-"
        )
        row_id = f"dl_{msg.id}"
        local_candidate = dl_dir / filename
        status = (
            "Já no PC ✓"
            if (
                local_candidate.exists()
                and (size == 0 or local_candidate.stat().st_size == size)
            )
            else "Disponível na Nuvem"
        )
        return RemoteCloudFile(
            row_id=row_id,
            message_id=msg.id,
            filename=filename,
            ext=ext,
            size=size,
            date_str=date_str,
            caption=(msg.message or "").strip(),
            status=status,
            progress=1.0 if status == "Já no PC ✓" else 0.0,
            message_obj=msg,
        )

    def _stream_remote_batch_to_ui(
        self,
        batch: List[RemoteCloudFile],
        prepend: bool,
        token: int,
        entity_key: str,
        save_checkpoint: bool = False,
    ):
        if token != self._remote_scan_token or not batch:
            return
        query = self.search_var.get().strip().lower()
        new_items: List[RemoteCloudFile] = []
        for rf in batch:
            if rf.row_id in self.remote_by_id:
                continue
            self.remote_by_id[rf.row_id] = rf
            self.remote_by_filename_lower[rf.filename.lower()] = rf
            new_items.append(rf)

        if not new_items:
            return

        if prepend:
            self.remote_files = new_items + self.remote_files
        else:
            self.remote_files.extend(new_items)

        for rf in new_items:
            if query:
                haystack = f"{rf.filename.lower()} {rf.ext.lower()} {rf.caption.lower()} {rf.date_str.lower()}"
                if query not in haystack:
                    continue
            self._insert_dl_row(rf)

        total_count = len(self.remote_files)
        total_bytes = sum(f.size for f in self.remote_files)
        shown = len(self.dl_tree.get_children())
        self.cloud_summary_label.configure(
            text=f"🔄 2º plano: {shown}/{total_count} arq. ({format_size(total_bytes)})"
        )
        if "download" in self.nav_buttons:
            self.nav_buttons["download"].configure(
                text=f"  DOWNLOAD DO TELEGRAM ({total_count})"
            )

        if save_checkpoint:
            threading.Thread(
                target=self._save_group_cache_entry,
                args=(entity_key, list(self.remote_files), False),
                daemon=True,
            ).start()

    async def _async_scan_group_files(
        self,
        target_entity: Union[int, str],
        target_label: str,
        entity_key: str,
        token: int,
        min_cached_id: int,
        max_cached_id: int,
        fully_scanned: bool,
    ) -> bool:
        """
        Varredura rápida em 2º plano com filtro de servidor (InputMessagesFilterDocument)
        e leitura incremental (lê apenas mensagens novas acima de max_cached_id, e continua
        abaixo de min_cached_id se a primeira leitura de 60.000+ arquivos ainda não tiver terminado).
        """
        if not self.client.is_connected():
            await self.client.connect()

        dl_dir = Path(
            self.config_data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR))
        )

        # Fase 1: Se já tínhamos cache, busca apenas arquivos NOVOS enviados após max_cached_id
        if max_cached_id > 0:
            new_batch: List[RemoteCloudFile] = []
            async for msg in self.client.iter_messages(
                target_entity,
                min_id=max_cached_id,
                limit=None,
                filter=InputMessagesFilterDocument,
            ):
                if token != self._remote_scan_token:
                    return False
                rf = self._extract_remote_file_from_msg(msg, dl_dir)
                if rf:
                    new_batch.append(rf)
                    if len(new_batch) >= 150:
                        b_copy = list(new_batch)
                        new_batch.clear()
                        self.after(
                            0,
                            lambda b=b_copy: self._stream_remote_batch_to_ui(
                                b, True, token, entity_key, False
                            ),
                        )
            if new_batch and token == self._remote_scan_token:
                b_copy = list(new_batch)
                self.after(
                    0,
                    lambda b=b_copy: self._stream_remote_batch_to_ui(
                        b, True, token, entity_key, True
                    ),
                )

        # Fase 2: Se o histórico completo do grupo ainda não foi 100% varrido, continua de onde parou (max_id)
        if not fully_scanned and token == self._remote_scan_token:
            older_batch: List[RemoteCloudFile] = []
            added_since_save = 0
            iter_kwargs = {
                "limit": None,
                "filter": InputMessagesFilterDocument,
            }
            if min_cached_id > 1:
                iter_kwargs["max_id"] = min_cached_id - 1

            async for msg in self.client.iter_messages(
                target_entity, **iter_kwargs
            ):
                if token != self._remote_scan_token:
                    return False
                rf = self._extract_remote_file_from_msg(msg, dl_dir)
                if rf:
                    older_batch.append(rf)
                    added_since_save += 1
                    if len(older_batch) >= 200:
                        b_copy = list(older_batch)
                        older_batch.clear()
                        should_save = added_since_save >= 1000
                        if should_save:
                            added_since_save = 0
                        self.after(
                            0,
                            lambda b=b_copy, sv=should_save: self._stream_remote_batch_to_ui(
                                b, False, token, entity_key, sv
                            ),
                        )

            if older_batch and token == self._remote_scan_token:
                b_copy = list(older_batch)
                self.after(
                    0,
                    lambda b=b_copy: self._stream_remote_batch_to_ui(
                        b, False, token, entity_key, False
                    ),
                )

        return token == self._remote_scan_token

    def _on_scan_group_done(
        self,
        completed_full: bool,
        target_label: str,
        entity_key: str,
        token: int,
    ):
        if token != self._remote_scan_token:
            return
        self.is_scanning_remote = False

        def _ui():
            self.scan_cloud_btn.configure(
                state="normal", text="🔄 Escanear Arquivos do Grupo"
            )
            self._update_dl_checked_summary()
            total_count = len(self.remote_files)
            total_sz = sum(f.size for f in self.remote_files)
            if not self.is_uploading and not self.is_downloading and not self.is_scanning:
                self.current_file_label.configure(
                    text=f"✓ Sincronização concluída: {total_count} arquivo(s) ({format_size(total_sz)}) disponíveis em DOWNLOAD DO TELEGRAM!"
                )
            if "download" in self.nav_buttons:
                suffix = f" ({total_count})" if total_count > 0 else ""
                self.nav_buttons["download"].configure(
                    text=f"  DOWNLOAD DO TELEGRAM{suffix}"
                )
            if completed_full:
                threading.Thread(
                    target=self._save_group_cache_entry,
                    args=(entity_key, list(self.remote_files), True),
                    daemon=True,
                ).start()

        self.after(0, _ui)
        self.log(
            f"✓ Sincronização em 2º plano concluída: {len(self.remote_files)} arquivo(s) disponíveis no grupo '{target_label}'."
        )

    def _on_scan_group_error(self, exc: Exception):
        self.is_scanning_remote = False
        self.after(
            0,
            lambda: (
                self.scan_cloud_btn.configure(
                    state="normal", text="🔄 Escanear Arquivos do Grupo"
                ),
            ),
        )
        self.log(f"Aviso ao sincronizar grupo em 2º plano: {exc}")

    def _apply_download_search_filter(self):
        self._dl_filter_render_token += 1
        render_token = self._dl_filter_render_token
        query = self.search_var.get().strip().lower()
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)

        matching: List[RemoteCloudFile] = []
        total_bytes = 0
        for item in self.remote_files:
            if query:
                haystack = f"{item.filename.lower()} {item.ext.lower()} {item.caption.lower()} {item.date_str.lower()}"
                if query not in haystack:
                    continue
            matching.append(item)
            total_bytes += item.size

        self.cloud_summary_label.configure(
            text=f"{len(matching)} de {len(self.remote_files)} arq. ({format_size(total_bytes)})"
        )
        self._render_dl_rows_chunked(matching, 0, render_token)

    def _render_dl_rows_chunked(
        self, items: List[RemoteCloudFile], start_idx: int, render_token: int
    ):
        if render_token != self._dl_filter_render_token:
            return
        CHUNK = 400
        end_idx = min(start_idx + CHUNK, len(items))
        for i in range(start_idx, end_idx):
            rf = items[i]
            if not self.dl_tree.exists(rf.row_id):
                self._insert_dl_row(rf)
        if end_idx < len(items):
            self.after(
                4,
                lambda: self._render_dl_rows_chunked(
                    items, end_idx, render_token
                ),
            )

    def _insert_dl_row(self, item: RemoteCloudFile):
        chk_str = "☑" if item.checked else "☐"
        icon = "✓" if "✓" in item.status else "☁️"
        pct = "100%" if "✓" in item.status else f"{item.progress * 100:0.0f}%"
        tag = "Marcado" if item.checked and item.status == "Disponível na Nuvem" else item.status
        self.dl_tree.insert(
            "",
            "end",
            iid=item.row_id,
            values=(
                chk_str,
                f"{icon} {item.filename}",
                item.ext,
                format_size(item.size) if item.size > 0 else "-",
                item.date_str,
                item.status,
                pct,
            ),
            tags=(tag,),
        )

    def _toggle_dl_row_check(self, row_id: str, force_val: Optional[bool] = None):
        item = self.remote_by_id.get(row_id)
        if not item:
            return
        item.checked = (not item.checked) if force_val is None else force_val
        if self.dl_tree.exists(row_id):
            chk_str = "☑" if item.checked else "☐"
            self.dl_tree.set(row_id, "check", chk_str)
            tag = "Marcado" if item.checked and item.status == "Disponível na Nuvem" else item.status
            self.dl_tree.item(row_id, tags=(tag,))

    def _on_dl_tree_click(self, event):
        region = self.dl_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.dl_tree.identify_column(event.x)
        row_id = self.dl_tree.identify_row(event.y)
        if not row_id:
            return
        # Clique na coluna #1 (caixa de seleção ☑/☐) ou no ícone inicial do arquivo
        if col == "#1" or (col == "#2" and event.x < 95):
            self._toggle_dl_row_check(row_id)
            self._update_dl_checked_summary()

    def _on_dl_tree_space(self, _event):
        selected = self.dl_tree.selection()
        if not selected:
            return
        for row_id in selected:
            self._toggle_dl_row_check(row_id)
        self._update_dl_checked_summary()

    def toggle_all_download_checkboxes(self):
        visible_iids = list(self.dl_tree.get_children())
        if not visible_iids:
            return
        any_unchecked = any(
            not self.remote_by_id[iid].checked
            for iid in visible_iids
            if iid in self.remote_by_id
        )
        target_state = True if any_unchecked else False
        for iid in visible_iids:
            self._toggle_dl_row_check(iid, force_val=target_state)
        self._update_dl_checked_summary()

    def _update_dl_checked_summary(self):
        checked_items = [f for f in self.remote_files if f.checked]
        checked_sz = sum(f.size for f in checked_items)
        shown = len(self.dl_tree.get_children())
        total_sz = sum(f.size for f in self.remote_files)
        if checked_items:
            self.cloud_summary_label.configure(
                text=f"☑ {len(checked_items)} marcado(s) ({format_size(checked_sz)}) | Total: {shown}/{len(self.remote_files)}"
            )
        else:
            self.cloud_summary_label.configure(
                text=f"{shown} de {len(self.remote_files)} arq. ({format_size(total_sz)})"
            )

    def choose_download_directory(self):
        current_dir = self.dl_dir_entry.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        chosen = filedialog.askdirectory(
            title="Escolha a pasta onde deseja salvar os arquivos baixados",
            initialdir=current_dir if Path(current_dir).exists() else str(Path.home()),
        )
        if chosen:
            self.dl_dir_entry.delete(0, "end")
            self.dl_dir_entry.insert(0, str(Path(chosen)))
            self._save_ui_prefs()
            self.log(f"Pasta de download definida para: {chosen}")

    def open_download_folder_in_explorer(self):
        dl_dir = Path(
            self.dl_dir_entry.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        )
        dl_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(dl_dir))
        except Exception as e:
            self.log(f"Erro ao abrir pasta: {e}")

    def select_all_download_items(self):
        self.toggle_all_download_checkboxes()
        all_children = self.dl_tree.get_children()
        self.dl_tree.selection_set(all_children)

    def remove_selected_download_items(self):
        checked_iids = [
            f.row_id for f in self.remote_files if f.checked
        ]
        selected_iids = list(set(checked_iids) | set(self.dl_tree.selection()))
        if not selected_iids:
            messagebox.showinfo(
                "Excluir Selecionado",
                "Marque a caixinha (☑) ou clique sobre um ou mais arquivos na lista para excluí-los da lista e do grupo do Telegram.",
            )
            return

        items_to_del: List[RemoteCloudFile] = []
        for iid in selected_iids:
            item = self.remote_by_id.get(iid)
            if item and item.status != "Baixando...":
                items_to_del.append(item)

        if not items_to_del:
            return

        target_label = self.target_combo.get()
        if not messagebox.askyesno(
            "Excluir da Lista e do Grupo do Telegram",
            f"Deseja realmente excluir {len(items_to_del)} arquivo(s) selecionado(s) da lista e APAGAR permanentemente do grupo '{target_label}' no Telegram?",
        ):
            return

        msg_ids: List[int] = []
        removed = 0
        for item in items_to_del:
            iid = item.row_id
            if self.dl_tree.exists(iid):
                self.dl_tree.delete(iid)
            self.remote_by_id.pop(iid, None)
            if item.message_id:
                msg_ids.append(int(item.message_id))
            removed += 1

        if removed:
            self.remote_files = [
                f for f in self.remote_files if f.row_id in self.remote_by_id
            ]
            self.remote_by_filename_lower = {
                f.filename.lower(): f for f in self.remote_files
            }
            self._update_dl_checked_summary()
            if "download" in self.nav_buttons:
                suffix = f" ({len(self.remote_files)})" if self.remote_files else ""
                self.nav_buttons["download"].configure(
                    text=f"  DOWNLOAD DO TELEGRAM{suffix}"
                )
            target_entity = self.dialogs_map.get(target_label, "me")
            threading.Thread(
                target=self._save_group_cache_entry,
                args=(str(target_entity), list(self.remote_files), True),
                daemon=True,
            ).start()
            self.log(
                f"🗑 Removendo {removed} arquivo(s) da lista e apagando do grupo '{target_label}' no Telegram..."
            )
            if msg_ids and self.connected_mode == "personal" and self.client:
                self.worker.run_coro(
                    self._async_delete_messages_from_group(
                        target_entity, target_label, msg_ids
                    ),
                    error_callback=lambda e: self.log(
                        f"⚠ Aviso ao excluir arquivo(s) no grupo do Telegram: {e}"
                    ),
                )

    async def _async_delete_messages_from_group(
        self,
        target_entity: Union[int, str],
        target_label: str,
        msg_ids: List[int],
    ):
        """Apaga as mensagens correspondentes aos arquivos excluídos diretamente no grupo do Telegram."""
        if not self.client:
            return
        if not self.client.is_connected():
            await self.client.connect()

        deleted_total = 0
        for i in range(0, len(msg_ids), 100):
            chunk_ids = msg_ids[i : i + 100]
            await self.client.delete_messages(target_entity, chunk_ids, revoke=True)
            deleted_total += len(chunk_ids)

        self.log(
            f"✓ {deleted_total} arquivo(s) apagado(s) com sucesso do grupo '{target_label}' no Telegram!"
        )

    def start_download_selected(self):
        if self.is_download_paused:
            self.toggle_pause_download()
            return

        if self.is_downloading or self.is_uploading:
            messagebox.showwarning(
                "Operação em andamento",
                "Aguarde ou pause a transferência atual antes de iniciar outra.",
            )
            return

        checked_iids = [
            iid
            for iid in self.dl_tree.get_children()
            if iid in self.remote_by_id and self.remote_by_id[iid].checked
        ]
        if not checked_iids:
            checked_iids = [f.row_id for f in self.remote_files if f.checked]

        selected_iids = checked_iids if checked_iids else list(self.dl_tree.selection())

        if not selected_iids:
            visible_iids = list(self.dl_tree.get_children())
            if not visible_iids:
                messagebox.showinfo(
                    "Download",
                    "Nenhum arquivo listado para baixar. Selecione um grupo ou clique em '🔄 Escanear Arquivos do Grupo'.",
                )
                return
            if not messagebox.askyesno(
                "Baixar Todos os Listados?",
                f"Você não marcou nenhuma caixinha (☐) na frente dos arquivos.\nDeseja marcar e baixar todos os {len(visible_iids)} arquivo(s) exibidos na lista agora?",
            ):
                return
            for iid in visible_iids:
                self._toggle_dl_row_check(iid, force_val=True)
            self._update_dl_checked_summary()
            selected_iids = visible_iids

        items_to_download = [
            self.remote_by_id[iid]
            for iid in selected_iids
            if iid in self.remote_by_id
        ]
        if not items_to_download:
            return

        dl_dir = Path(
            self.dl_dir_entry.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        )
        try:
            dl_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(
                "Erro na Pasta de Destino",
                f"Não foi possível criar/acessar a pasta de destino:\n{dl_dir}\n\nErro: {e}",
            )
            return

        self._save_ui_prefs()
        self.is_downloading = True
        self.is_download_paused = False
        self.cancel_download_requested = False

        # Botão fica AMARELO enquanto estiver baixando (ao clicar nele, pausa e volta a ficar VERDE)
        self.dl_start_btn.configure(
            state="normal",
            text="  PAUSAR DOWNLOAD",
            image=self.nav_icons["pause"],
            fg_color="#d97706",
            hover_color="#b45309",
            text_color="#ffffff",
        )
        self.dl_cancel_btn.configure(state="normal")

        self.log(
            f"⬇ Iniciando download de {len(items_to_download)} arquivo(s) para: {dl_dir}"
        )

        self.worker.run_coro(
            self._async_process_downloads(items_to_download, dl_dir),
            callback=self._on_download_finished,
            error_callback=self._on_download_error,
        )

    def toggle_pause_download(self):
        if self.is_scanning:
            return
        if not self.is_download_paused and self.is_downloading:
            self.is_download_paused = True
            self.dl_start_btn.configure(
                state="normal",
                text="  CONTINUAR DOWNLOAD",
                image=self.nav_icons["play"],
                fg_color="#16a34a",
                hover_color="#15803d",
                text_color="#ffffff",
            )
            self.current_file_label.configure(
                text="Download PAUSADO. Clique no botão verde 'CONTINUAR DOWNLOAD' para retomar."
            )
            self.log("Download pausado pelo usuário.")
        elif self.is_download_paused:
            self.is_download_paused = False
            self.log("▶ Retomando downloads (Play)...")
            self.start_download_selected()
        elif not self.is_downloading:
            self.start_download_selected()

    def stop_download_queue(self):
        if self.is_downloading or self.is_download_paused:
            self.cancel_download_requested = True
            self.is_download_paused = False
            self.log("■ Parando fila de downloads...")
            if not self.is_downloading:
                self._reset_dl_controls()

    def _reset_dl_controls(self):
        self.dl_cancel_btn.configure(state="disabled")
        self._update_scan_lock_ui()

    async def _async_sleep_interruptible_dl(self, seconds: int, status_prefix: str) -> bool:
        """Aguarda `seconds` segundos atualizando a UI a cada 1s; retorna False se cancelado/pausado."""
        for remaining in range(seconds, 0, -1):
            if self.cancel_download_requested or self.is_download_paused:
                return False
            if remaining % 60 == 0 or remaining <= 30 or seconds <= 15:
                if remaining >= 60:
                    mins = remaining // 60
                    secs = remaining % 60
                    time_str = f"{mins}m {secs:02d}s"
                else:
                    time_str = f"{remaining}s"
                self.after(
                    0,
                    lambda msg=f"{status_prefix} (aguardando {time_str}...)": self.current_file_label.configure(
                        text=msg
                    ),
                )
            await asyncio.sleep(1)
        return not (self.cancel_download_requested or self.is_download_paused)

    async def _async_process_downloads(
        self, items: List[RemoteCloudFile], dl_dir: Path
    ):
        total_count = len(items)
        completed_count = 0
        current_batch = list(items)
        cycle_number = 1

        while current_batch and not self.cancel_download_requested and not self.is_download_paused:
            deferred_for_1h: List[RemoteCloudFile] = []
            batch_total_bytes = max(sum(max(it.size, 0) for it in current_batch), 1)
            completed_batch_bytes = 0

            for idx, item in enumerate(current_batch, start=1):
                if self.cancel_download_requested:
                    break
                if self.is_download_paused:
                    self._update_dl_item_ui(item, "Pausado", f"{item.progress * 100:0.0f}%")
                    break

                dest_file = dl_dir / item.filename
                if (
                    dest_file.exists()
                    and item.size > 0
                    and dest_file.stat().st_size != item.size
                ):
                    stem = dest_file.stem
                    suffix = dest_file.suffix
                    dest_file = dl_dir / f"{stem}_{item.message_id}{suffix}"

                file_succeeded = False
                max_attempts = 10

                for attempt in range(1, max_attempts + 1):
                    if self.cancel_download_requested or self.is_download_paused:
                        break

                    att_suffix = f" [Tentativa {attempt}/{max_attempts}]" if attempt > 1 else ""
                    self._update_dl_item_ui(
                        item,
                        "Baixando..." if attempt == 1 else f"Tentativa {attempt}/10",
                        f"{item.progress * 100:0.0f}%",
                        scroll_to=True,
                    )
                    self.after(
                        0,
                        lambda name=item.filename, i=idx, t=len(current_batch), sfx=att_suffix: self.current_file_label.configure(
                            text=f"Baixando ({i}/{t}): {name}{sfx}"
                        ),
                    )

                    start_t = time.time()
                    last_upd = [0.0]
                    done_before_this = completed_batch_bytes

                    def dl_progress_cb(
                        recv_bytes: int,
                        total_bytes: int,
                        current_item=item,
                        t0=start_t,
                        lu=last_upd,
                        prev_b=done_before_this,
                        tot_batch_b=batch_total_bytes,
                    ):
                        if self.cancel_download_requested:
                            raise asyncio.CancelledError("Download cancelado.")
                        if self.is_download_paused:
                            raise TransferPausedError("Download pausado.")

                        now = time.time()
                        if recv_bytes < total_bytes and (now - lu[0]) < 0.07:
                            return
                        lu[0] = now

                        total_b = total_bytes or current_item.size or 1
                        frac = min(recv_bytes / total_b, 1.0)
                        current_item.progress = frac
                        elapsed = max(now - t0, 0.001)
                        speed_bps = recv_bytes / elapsed
                        speed_mb = speed_bps / (1024 * 1024)
                        overall_frac = min(
                            (prev_b + recv_bytes) / max(tot_batch_b, 1),
                            1.0,
                        )
                        pct_str = f"{frac * 100:0.1f}%"

                        rem_file_b = max(0, total_b - recv_bytes)
                        file_eta_str = (
                            format_eta(rem_file_b / speed_bps)
                            if speed_bps > 0
                            else "calculando..."
                        )
                        rem_total_b = max(0, tot_batch_b - (prev_b + recv_bytes))
                        total_eta_str = (
                            format_eta(rem_total_b / speed_bps)
                            if speed_bps > 0
                            else "calculando..."
                        )

                        def _ui():
                            if self.dl_tree.exists(current_item.row_id):
                                self.dl_tree.set(current_item.row_id, "progress", pct_str)
                            self.file_progress_bar.set(
                                frac,
                                f"{frac * 100:0.1f}%   •   Tempo previsto: {file_eta_str}",
                            )
                            self.total_progress_bar.set(
                                overall_frac,
                                f"{overall_frac * 100:0.1f}%   •   Tempo previsto: {total_eta_str}",
                            )
                            if hasattr(self, "file_progress_title_label"):
                                self.file_progress_title_label.configure(
                                    text=f"Progresso do Arquivo Atual:   •   Tempo previsto para conclusão: {file_eta_str}"
                                )
                            if hasattr(self, "total_progress_title_label"):
                                self.total_progress_title_label.configure(
                                    text=f"Progresso Total da Operação:   •   Tempo previsto para conclusão: {total_eta_str}"
                                )
                            self.speed_label.configure(
                                text=f"Velocidade: {speed_mb:0.2f} MB/s | Progresso Geral: {completed_count}/{total_count} ({overall_frac * 100:0.0f}%)"
                            )

                        self.after(0, _ui)

                    try:
                        if not self.client.is_connected():
                            await self.client.connect()

                        if item.message_obj is None:
                            target_label = self.target_combo.get()
                            target_entity = self.dialogs_map.get(target_label, "me")
                            item.message_obj = await self.client.get_messages(
                                target_entity, ids=item.message_id
                            )
                            if item.message_obj is None:
                                raise RuntimeError(
                                    f"Mensagem #{item.message_id} não encontrada no grupo do Telegram."
                                )

                        await self.client.download_media(
                            item.message_obj,
                            file=str(dest_file),
                            progress_callback=dl_progress_cb,
                        )
                        completed_count += 1
                        completed_batch_bytes += max(item.size, 0)
                        item.progress = 1.0
                        file_succeeded = True
                        self._update_dl_item_ui(item, "Baixado ✓", "100%")
                        self.log(f"✓ Baixado ({completed_count}/{total_count}): {dest_file.name}")
                        break

                    except TransferPausedError:
                        self._update_dl_item_ui(item, "Pausado", f"{item.progress * 100:0.0f}%")
                        break
                    except asyncio.CancelledError:
                        self._update_dl_item_ui(
                            item, "Disponível na Nuvem", f"{item.progress * 100:0.0f}%"
                        )
                        self.log(f"■ Download interrompido: {item.filename}")
                        break
                    except errors.FloodWaitError as fw:
                        self.log(
                            f"Limite do Telegram atingido em '{item.filename}'. Aguardando {fw.seconds}s..."
                        )
                        ok = await self._async_sleep_interruptible_dl(
                            fw.seconds, f"Aguardando limite do Telegram ({item.filename})"
                        )
                        if not ok:
                            break
                    except Exception as exc:
                        self._update_dl_item_ui(item, "Erro", f"Falha ({attempt}/10)")
                        self.log(
                            f"⚠ Falha de rede ao baixar '{item.filename}' (tentativa {attempt}/{max_attempts}): {exc}"
                        )
                        try:
                            if self.client and not self.client.is_connected():
                                await self.client.connect()
                        except Exception:
                            pass

                        if attempt < max_attempts:
                            self.log(
                                f"⏳ Aguardando 10 segundos antes da tentativa {attempt + 1}/{max_attempts} para '{item.filename}'..."
                            )
                            ok = await self._async_sleep_interruptible_dl(
                                10,
                                f"Falha de rede em '{item.filename}' — Tentativa {attempt}/{max_attempts}",
                            )
                            if not ok:
                                break
                        else:
                            self._update_dl_item_ui(item, "Aguardando 1h", "Adiado 1h")
                            self.log(
                                f"⏭ '{item.filename}' falhou após 10 tentativas. Pulando para o próximo da fila (nova tentativa automática em 1 hora)."
                            )
                            deferred_for_1h.append(item)

            if self.cancel_download_requested or self.is_download_paused:
                break

            if deferred_for_1h:
                cycle_number += 1
                self.log(
                    f"🕒 {len(deferred_for_1h)} arquivo(s) com falha de rede aguardarão 1 hora (3600s) para nova rodada de tentativas (ou clique em '■ Parar' para cancelar)."
                )
                ok = await self._async_sleep_interruptible_dl(
                    3600,
                    f"🕒 Aguardando 1 hora para tentar baixar novamente {len(deferred_for_1h)} arquivo(s) pendente(s)",
                )
                if not ok:
                    break
                current_batch = deferred_for_1h
            else:
                break

        return completed_count, total_count

    def _update_dl_item_ui(
        self,
        item: RemoteCloudFile,
        status_tag: str,
        progress_text: str,
        scroll_to: bool = False,
    ):
        item.status = status_tag

        def _ui():
            if self.dl_tree.exists(item.row_id):
                chk_str = "☑" if item.checked else "☐"
                icon = "✓" if "✓" in status_tag else "☁️"
                self.dl_tree.set(item.row_id, "check", chk_str)
                self.dl_tree.set(item.row_id, "filename", f"{icon} {item.filename}")
                self.dl_tree.set(item.row_id, "status", status_tag)
                self.dl_tree.set(item.row_id, "progress", progress_text)
                self.dl_tree.item(item.row_id, tags=(status_tag,))
                if scroll_to:
                    self.dl_tree.see(item.row_id)

        self.after(0, _ui)

    def _on_download_finished(self, stats):
        completed, total = stats
        self.is_downloading = False

        def _ui():
            if self.is_download_paused:
                self.dl_start_btn.configure(
                    state="normal",
                    text="  CONTINUAR DOWNLOAD",
                    image=self.nav_icons["play"],
                    fg_color="#16a34a",
                    hover_color="#15803d",
                    text_color="#ffffff",
                )
                self.dl_cancel_btn.configure(state="normal")
            else:
                self._reset_dl_controls()
                self.current_file_label.configure(
                    text=f"Download finalizado! {completed} de {total} arquivo(s) salvos na pasta."
                )

        self.after(0, _ui)

    def _on_download_error(self, exc: Exception):
        self.is_downloading = False
        self.after(0, self._reset_dl_controls)
        self.log(f"Erro na fila de download: {exc}")

    # -------------------------------------------------------------------------
    # GERENCIADOR DE UPLOAD E SELETOR UNIFICADO (ARQUIVOS / PASTA / VÁRIAS PASTAS)
    # -------------------------------------------------------------------------
    def _item_matches_filter(self, item: FileQueueItem) -> bool:
        mode = self.queue_filter_mode
        if mode == "Pendentes":
            return item.status in (
                "Pendente",
                "Pausado",
                "Enviando",
                "Erro",
                "Cancelado",
                "Aguardando 1h",
            )
        return True

    def _on_filter_changed(self, value: str):
        self.queue_filter_mode = value
        self._rebuild_queue_tree()

    def _sync_upload_tree_height(self):
        """Ajusta a altura visível da tabela de Upload para manter o Background do Telegram visível na área livre."""
        try:
            n_rows = len(self.tree.get_children())
            if n_rows < 11:
                self.tree.configure(height=n_rows)
                self.tree.grid_configure(sticky="new", pady=(6, 0))
            else:
                self.tree.configure(height=12)
                self.tree.grid_configure(sticky="nsew", pady=6)
        except Exception:
            pass

    def _rebuild_queue_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for item in self.queue:
            if self._item_matches_filter(item):
                self._insert_item_row(item)
        self._sync_upload_tree_height()

    def _insert_item_row(self, item: FileQueueItem):
        chk_str = "☑" if item.checked else "☐"
        icon = "✓" if item.status in ("Concluído", "Já Enviado") else "📄"
        pct = (
            "100%"
            if item.status in ("Concluído", "Já Enviado")
            else (
                "Ignorado"
                if item.status in ("Ignorado (Repetido)", "Ignorado (> 2GB)")
                else f"{item.progress * 100:0.0f}%"
            )
        )
        tag = "Marcado" if item.checked and item.status == "Pendente" else item.status
        self.tree.insert(
            "",
            "end",
            iid=item.item_id,
            values=(
                chk_str,
                f"{icon} {item.path.name}",
                str(item.path.parent),
                format_size(item.size),
                item.status,
                pct,
                item.sent_at or "-",
            ),
            tags=(tag,),
        )

    def _toggle_upload_row_check(
        self, item_id: str, force_val: Optional[bool] = None
    ):
        item = self.queue_by_id.get(item_id)
        if not item:
            return
        item.checked = (not item.checked) if force_val is None else force_val
        if self.tree.exists(item_id):
            chk_str = "☑" if item.checked else "☐"
            self.tree.set(item_id, "check", chk_str)
            tag = (
                "Marcado"
                if item.checked and item.status == "Pendente"
                else item.status
            )
            self.tree.item(item_id, tags=(tag,))

    def _on_upload_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        if col == "#1" or (col == "#2" and event.x < 95):
            self._toggle_upload_row_check(row_id)
            self._update_queue_summary()

    def _on_upload_tree_space(self, _event):
        selected = self.tree.selection()
        if not selected:
            return
        for row_id in selected:
            self._toggle_upload_row_check(row_id)
        self._update_queue_summary()

    def toggle_all_upload_checkboxes(self):
        visible_iids = list(self.tree.get_children())
        if not visible_iids:
            return
        any_unchecked = any(
            not self.queue_by_id[iid].checked
            for iid in visible_iids
            if iid in self.queue_by_id
        )
        target_state = True if any_unchecked else False
        for iid in visible_iids:
            self._toggle_upload_row_check(iid, force_val=target_state)
        self._update_queue_summary()

    def add_files_dialog(self):
        if self.is_scanning:
            return
        paths = filedialog.askopenfilenames(
            title="Selecione os arquivos para enviar ao Telegram"
        )
        if not paths:
            return
        file_list = [Path(p) for p in paths]
        self._start_background_file_ingest(file_list, source_label="seleção de arquivos")

    def open_multi_folder_selector(self):
        """Janela unificada de '+ Selecionar Arquivos': permite escolher arquivos soltos, pasta única ou diretório com várias pastas."""
        if self.is_scanning:
            return

        if (
            hasattr(self, "multi_folder_win")
            and self.multi_folder_win is not None
            and self.multi_folder_win.winfo_exists()
        ):
            self.multi_folder_win.lift()
            self.multi_folder_win.focus_force()
            return

        self.multi_folder_win = ctk.CTkToplevel(self)
        self.multi_folder_win.title(f"Selecionar Arquivos ou Pastas — {APP_TITLE}")
        self.multi_folder_win.geometry("760x500")
        self.multi_folder_win.minsize(680, 440)
        self.multi_folder_win.transient(self)
        self.multi_folder_win.attributes("-topmost", True)
        self._apply_window_icon(self.multi_folder_win)

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 380
        y = self.winfo_y() + (self.winfo_height() // 2) - 250
        self.multi_folder_win.geometry(f"+{max(x, 40)}+{max(y, 40)}")

        self.multi_folder_win.grid_columnconfigure(0, weight=1)
        self.multi_folder_win.grid_rowconfigure(2, weight=1)

        selected_folders: List[Path] = []

        ctk.CTkLabel(
            self.multi_folder_win,
            text="📂 Escolha abaixo se deseja selecionar Arquivos Soltos, Escolher Pasta ou Selecionar Diretório (com várias pastas):",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
            anchor="w",
        ).grid(row=0, column=0, padx=14, pady=(12, 6), sticky="ew")

        top_btns = ctk.CTkFrame(self.multi_folder_win, fg_color="transparent")
        top_btns.grid(row=1, column=0, padx=14, pady=4, sticky="ew")

        folders_listbox = tk.Listbox(
            self.multi_folder_win,
            selectmode=tk.EXTENDED,
            bg="#1e293b",
            fg="#f8fafc",
            selectbackground="#2563eb",
            font=("Segoe UI", 10),
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#334155",
        )
        folders_listbox.grid(row=2, column=0, padx=14, pady=8, sticky="nsew")

        count_lbl = ctk.CTkLabel(
            top_btns,
            text="0 pasta(s) na lista",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )

        def _refresh_list():
            folders_listbox.delete(0, tk.END)
            for fp in selected_folders:
                folders_listbox.insert(tk.END, f"📁 {fp}")
            count_lbl.configure(text=f"{len(selected_folders)} pasta(s) na lista")

        def _pick_loose_files():
            paths = filedialog.askopenfilenames(
                parent=self.multi_folder_win,
                title="Selecione um ou mais arquivos para enviar ao Telegram",
            )
            if paths:
                file_list = [Path(p) for p in paths]
                self.multi_folder_win.destroy()
                self._start_background_file_ingest(
                    file_list, source_label="seleção de arquivos"
                )

        def _add_one_folder():
            chosen = filedialog.askdirectory(
                parent=self.multi_folder_win,
                title="Escolher Pasta para adicionar à lista de Upload",
            )
            if chosen:
                p = Path(chosen)
                if p not in selected_folders:
                    selected_folders.append(p)
                    _refresh_list()

        def _add_all_subfolders_of_parent():
            parent_dir = filedialog.askdirectory(
                parent=self.multi_folder_win,
                title="Selecionar Diretório (com várias pastas) para marcar subpastas",
            )
            if not parent_dir:
                return
            p_path = Path(parent_dir)
            try:
                subdirs = sorted(
                    [
                        d
                        for d in p_path.iterdir()
                        if d.is_dir()
                        and d.name not in IGNORED_DIRS
                        and not d.name.startswith(".")
                    ]
                )
            except Exception:
                subdirs = []

            if not subdirs:
                if p_path not in selected_folders:
                    selected_folders.append(p_path)
                    _refresh_list()
                return

            sub_win = ctk.CTkToplevel(self.multi_folder_win)
            sub_win.title(f"Marque as pastas dentro de '{p_path.name}'")
            sub_win.geometry("540x450")
            sub_win.transient(self.multi_folder_win)
            sub_win.attributes("-topmost", True)
            self._apply_window_icon(sub_win)
            sub_win.grid_columnconfigure(0, weight=1)
            sub_win.grid_rowconfigure(1, weight=1)

            ctk.CTkLabel(
                sub_win,
                text=f"Marque quais pastas de '{p_path.name}' deseja adicionar:",
                font=ctk.CTkFont(size=13, weight="bold"),
            ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")

            scroll = ctk.CTkScrollableFrame(sub_win)
            scroll.grid(row=1, column=0, padx=12, pady=6, sticky="nsew")

            vars_map: List[Tuple[Path, ctk.BooleanVar]] = []
            for sd in subdirs:
                v = ctk.BooleanVar(value=True)
                ctk.CTkCheckBox(scroll, text=f"📁 {sd.name}", variable=v).pack(
                    anchor="w", padx=8, pady=3
                )
                vars_map.append((sd, v))

            bot = ctk.CTkFrame(sub_win, fg_color="transparent")
            bot.grid(row=2, column=0, padx=12, pady=10, sticky="ew")

            def _toggle_all(state: bool):
                for _, v in vars_map:
                    v.set(state)

            def _confirm_checked():
                for sd, v in vars_map:
                    if v.get() and sd not in selected_folders:
                        selected_folders.append(sd)
                _refresh_list()
                sub_win.destroy()

            ctk.CTkButton(
                bot,
                text="Marcar Todas",
                width=110,
                fg_color="#475569",
                command=lambda: _toggle_all(True),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                bot,
                text="Desmarcar Todas",
                width=120,
                fg_color="#475569",
                command=lambda: _toggle_all(False),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                bot,
                text="✓ Adicionar Marcadas",
                fg_color="#16a34a",
                hover_color="#15803d",
                command=_confirm_checked,
            ).pack(side="right", padx=4)

        def _remove_selected():
            sel = list(folders_listbox.curselection())
            for idx in reversed(sel):
                if 0 <= idx < len(selected_folders):
                    selected_folders.pop(idx)
            _refresh_list()

        def _start_ingest():
            if not selected_folders:
                messagebox.showinfo(
                    "Selecionar Pastas",
                    "Adicione pelo menos uma pasta na lista ou clique em '📄 Selecionar Arquivos Soltos'.",
                    parent=self.multi_folder_win,
                )
                return
            folders_copy = list(selected_folders)
            self.multi_folder_win.destroy()
            self._scan_multiple_folders_in_background(folders_copy)

        ctk.CTkButton(
            top_btns,
            text="📄 Selecionar Arquivos Soltos",
            width=185,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=_pick_loose_files,
        ).pack(side="left", padx=(0, 5))

        ctk.CTkButton(
            top_btns,
            text="📁 Escolher Pasta",
            width=135,
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=_add_one_folder,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            top_btns,
            text="📂 Selecionar Diretório (Várias Pastas)",
            width=235,
            fg_color="#0d9488",
            hover_color="#0f766e",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=_add_all_subfolders_of_parent,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            top_btns,
            text="Remover",
            width=80,
            fg_color="#475569",
            hover_color="#dc2626",
            command=_remove_selected,
        ).pack(side="left", padx=5)

        count_lbl.pack(side="right", padx=4)

        footer = ctk.CTkFrame(self.multi_folder_win, fg_color="transparent")
        footer.grid(row=3, column=0, padx=14, pady=(4, 14), sticky="ew")

        ctk.CTkButton(
            footer,
            text="✅ Carregar Todos os Arquivos das Pastas Selecionadas na Fila",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#16a34a",
            hover_color="#15803d",
            height=40,
            command=_start_ingest,
        ).pack(fill="x")

    def _scan_multiple_folders_in_background(self, folders: List[Path]):
        recursive = self.recursive_var.get()
        self.is_scanning = True
        self.add_files_btn.configure(state="disabled", text="⏳ Lendo arquivos...")
        self.current_file_label.configure(
            text=f"⏳ Lendo os arquivos, aguarde... Varrendo {len(folders)} pasta(s)..."
        )
        self._update_scan_lock_ui(
            f"Varrendo {len(folders)} diretório(s)... O botão Iniciar ficará liberado ao terminar."
        )
        self.log(f"Analisando arquivos de {len(folders)} pasta(s)...")

        def _scan_worker():
            found_files: List[Tuple[Path, int]] = []
            for folder_path in folders:
                try:
                    if recursive:
                        for root, dirs, files in os.walk(folder_path, topdown=True):
                            dirs[:] = [
                                d
                                for d in dirs
                                if d not in IGNORED_DIRS and not d.startswith(".")
                            ]
                            root_path = Path(root)
                            for fname in sorted(files):
                                fpath = root_path / fname
                                try:
                                    st = fpath.stat()
                                    if st.st_size > 0:
                                        found_files.append((fpath, st.st_size))
                                        if len(found_files) % 400 == 0:
                                            cnt = len(found_files)
                                            self.after(
                                                0,
                                                lambda c=cnt: self._update_scan_lock_ui(
                                                    f"Encontrados {c} arquivos nas pastas selecionadas... Aguarde concluir."
                                                ),
                                            )
                                except OSError:
                                    continue
                    else:
                        with os.scandir(folder_path) as entries:
                            for entry in sorted(entries, key=lambda e: e.name.lower()):
                                try:
                                    if entry.is_file():
                                        st = entry.stat()
                                        if st.st_size > 0:
                                            found_files.append(
                                                (Path(entry.path), st.st_size)
                                            )
                                except OSError:
                                    continue
                except Exception as exc:
                    self.log(f"Aviso durante leitura da pasta {folder_path}: {exc}")

            self.after(
                0,
                lambda: self._insert_scanned_batches(
                    found_files, f"{len(folders)} pasta(s)"
                ),
            )

        threading.Thread(target=_scan_worker, daemon=True).start()

    def _start_background_file_ingest(
        self, paths: List[Path], source_label: str
    ):
        self.is_scanning = True
        self.add_files_btn.configure(state="disabled", text="⏳ Lendo arquivos...")
        self._update_scan_lock_ui("Analisando arquivos selecionados... Aguarde concluir.")

        def _stat_worker():
            found: List[Tuple[Path, int]] = []
            for p in paths:
                try:
                    st = p.stat()
                    if st.st_size > 0:
                        found.append((p, st.st_size))
                except OSError:
                    continue
            self.after(
                0, lambda: self._insert_scanned_batches(found, source_label)
            )

        threading.Thread(target=_stat_worker, daemon=True).start()

    def _insert_scanned_batches(
        self,
        files_with_sizes: List[Tuple[Path, int]],
        source_label: str,
        start_idx: int = 0,
        added_so_far: int = 0,
        already_sent_count: int = 0,
        ignored_over_2gb_count: int = 0,
    ):
        BATCH_SIZE = 300
        end_idx = min(start_idx + BATCH_SIZE, len(files_with_sizes))
        check_sent = self.skip_sent_var.get() and not self.ignore_duplicates_var.get()

        for i in range(start_idx, end_idx):
            fpath, fsize = files_with_sizes[i]
            if fpath in self.queued_paths:
                continue

            # Ignora automaticamente arquivos maiores que 2 GB (limite do Telegram)
            if fsize > MAX_TELEGRAM_FILE_SIZE:
                ignored_over_2gb_count += 1
                self.log(
                    f"⏭ Ignorado automaticamente (> 2 GB): '{fpath.name}' ({format_size(fsize)}) excede o limite de 2 GB do Telegram."
                )
                continue

            item_id = f"item_{self._next_id_counter}"
            self._next_id_counter += 1

            sig = make_file_signature(fpath, fsize)
            prev_record = self.sent_signatures.get(sig) if check_sent else None

            if prev_record:
                item = FileQueueItem(
                    item_id=item_id,
                    path=fpath,
                    size=fsize,
                    status="Já Enviado",
                    progress=1.0,
                    sent_at=prev_record.get("sent_at", ""),
                    target_chat=prev_record.get("target", ""),
                )
                already_sent_count += 1
            else:
                item = FileQueueItem(item_id=item_id, path=fpath, size=fsize)

            self.queue.append(item)
            self.queue_by_id[item_id] = item
            self.queued_paths.add(fpath)

            if self._item_matches_filter(item):
                self._insert_item_row(item)

            added_so_far += 1

        self._update_queue_summary()

        if end_idx < len(files_with_sizes):
            progress_txt = f"Carregando arquivos na lista... ({end_idx} de {len(files_with_sizes)})"
            self.current_file_label.configure(
                text=f"⏳ Lendo os arquivos, aguarde... ({end_idx}/{len(files_with_sizes)})"
            )
            self._update_scan_lock_ui(progress_txt)
            self.after(
                5,
                lambda: self._insert_scanned_batches(
                    files_with_sizes,
                    source_label,
                    end_idx,
                    added_so_far,
                    already_sent_count,
                    ignored_over_2gb_count,
                ),
            )
        else:
            self.is_scanning = False
            self.add_files_btn.configure(
                state="normal", text="+ Selecionar Arquivos"
            )
            self._update_scan_lock_ui()
            extra_2gb = (
                f" ({ignored_over_2gb_count} arquivo(s) > 2 GB ignorado(s))"
                if ignored_over_2gb_count > 0
                else ""
            )
            msg = f"Pronto! {added_so_far} arquivo(s) adicionados de {source_label}.{extra_2gb}"
            self.current_file_label.configure(text=msg)
            self.log(msg)
            self._save_queue_state()

    def remove_selected_items(self):
        checked_iids = [i.item_id for i in self.queue if i.checked]
        selected_iids = list(set(checked_iids) | set(self.tree.selection()))
        if not selected_iids:
            messagebox.showinfo(
                "Excluir Selecionado",
                "Marque a caixinha (☑) na frente do arquivo ou clique sobre ele na lista para excluí-lo!",
            )
            return
        removed = 0
        for iid in selected_iids:
            item = self.queue_by_id.get(iid)
            if not item:
                continue
            item.ignored_by_delete = True
            if self.tree.exists(iid):
                self.tree.delete(iid)
            self.queue_by_id.pop(iid, None)
            self.queued_paths.discard(item.path)
            removed += 1
        if removed:
            self.queue = [i for i in self.queue if i.item_id in self.queue_by_id]
            self._update_queue_summary()
            self._save_queue_state()
            self.log(f"🗑 {removed} arquivo(s) ignorado(s) e removido(s) da lista de upload.")

    def clear_all(self):
        if self.is_uploading:
            messagebox.showwarning(
                "Em andamento",
                "Pause ou Pare o envio atual antes de limpar toda a fila.",
            )
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.queue.clear()
        self.queue_by_id.clear()
        self.queued_paths.clear()
        self._update_queue_summary()
        self._save_queue_state()
        self.file_progress_bar.set(0)
        self.total_progress_bar.set(0)
        self.current_file_label.configure(text="Fila de upload limpa.")

    def _update_queue_summary(self):
        total_bytes = sum(i.size for i in self.queue)
        checked_count = sum(1 for i in self.queue if i.checked)
        pending = sum(
            1
            for i in self.queue
            if i.status in ("Pendente", "Pausado", "Erro", "Cancelado", "Aguardando 1h")
        )
        chk_prefix = f"☑ {checked_count} | " if checked_count > 0 else ""
        self.queue_summary_label.configure(
            text=f"{chk_prefix}{len(self.queue)} arq. ({format_size(total_bytes)}) | Pend: {pending}"
        )
        self._sync_upload_tree_height()

    # -------------------------------------------------------------------------
    # Execução do Upload Automático com Botão Único INICIAR (Verde) / PAUSAR (Amarelo)
    # -------------------------------------------------------------------------
    def start_upload_queue(self):
        if self.is_scanning:
            return

        if self.is_paused:
            self.toggle_pause_upload()
            return

        if self.is_uploading or self.is_downloading:
            return

        pending_items = [
            i
            for i in self.queue
            if i.status
            in ("Pendente", "Pausado", "Erro", "Cancelado", "Aguardando 1h")
        ]
        if not pending_items:
            messagebox.showinfo(
                "Nada Pendente",
                "Todos os arquivos da fila já foram enviados ou a fila está vazia!",
            )
            return

        if not self.connected_mode:
            messagebox.showwarning(
                "Conta Não Conectada",
                "Conecte sua Conta Pessoal (QR Code ou Telefone) ou configure um Bot Token na opção 'CONTA DO TELEGRAM' antes de iniciar!",
            )
            self.switch_page("account")
            return

        self._save_ui_prefs()
        # Captura segura na thread principal para evitar acesso concorrente ao Tcl/Tk na thread de upload (Windows 11)
        self._snap_ignore_duplicates = bool(self.ignore_duplicates_var.get())
        self._snap_use_caption = False
        self._snap_caption_tpl = "{filename}"
        self._snap_force_doc = bool(self.force_doc_var.get())

        self.is_uploading = True
        self.is_paused = False
        self.cancel_requested = False
        self._save_queue_state()

        # Ao iniciar, o botão fica AMARELO (indicando que clicar nele irá PAUSAR)
        self.start_btn.configure(
            state="normal",
            text="  PAUSAR UPLOAD",
            image=self.nav_icons["pause"],
            fg_color="#d97706",
            hover_color="#b45309",
            text_color="#ffffff",
        )
        self.cancel_btn.configure(state="normal")

        target_label = self.target_combo.get()
        target_entity = self.dialogs_map.get(target_label, "me")

        self.log(
            f"▶ Iniciando envio de {len(pending_items)} arquivo(s) pendente(s) para: {target_label}"
        )

        self.worker.run_coro(
            self._async_process_queue(pending_items, target_entity, target_label),
            callback=self._on_queue_finished,
            error_callback=self._on_queue_error,
        )

    def toggle_pause_upload(self):
        if self.is_scanning:
            return
        if not self.is_paused and self.is_uploading:
            self.is_paused = True
            # Ao pausar, o botão volta a ficar VERDE aguardando continuar
            self.start_btn.configure(
                state="normal",
                text="  CONTINUAR UPLOAD",
                image=self.nav_icons["play"],
                fg_color="#16a34a",
                hover_color="#15803d",
                text_color="#ffffff",
            )
            self.current_file_label.configure(
                text="Upload PAUSADO pelo usuário. Clique no botão verde 'CONTINUAR UPLOAD' para retomar."
            )
            self.log("Upload pausado pelo usuário.")
        elif self.is_paused:
            self.is_paused = False
            self.log("▶ Retomando fila de upload (Play)...")
            self.start_upload_queue()
        elif not self.is_uploading:
            self.start_upload_queue()

    def stop_upload_queue(self):
        if self.is_uploading or self.is_paused:
            self.cancel_requested = True
            self.is_paused = False
            self.log("■ Parando fila de envio...")
            if not self.is_uploading:
                self._reset_controls_after_stop()

    def _reset_controls_after_stop(self):
        self.cancel_btn.configure(state="disabled")
        self._update_scan_lock_ui()

    def _file_exists_in_download_scan(self, item: FileQueueItem) -> Optional[str]:
        """Verifica instantaneamente (O(1)) se o arquivo já consta na varredura de Downloads do grupo ou no histórico enviado."""
        fname_lower = item.path.name.lower()
        rf = self.remote_by_filename_lower.get(fname_lower)
        if rf is not None:
            return f"Lista de Downloads do Grupo ({rf.date_str} • {format_size(rf.size)})"
        sig = make_file_signature(item.path, item.size)
        prev = self.sent_signatures.get(sig)
        if prev:
            return f"Histórico de Enviados ({prev.get('sent_at', '')})"
        return None

    def _ask_duplicate_upload_10s(
        self, item: FileQueueItem, existing_info: str
    ) -> str:
        """Abre diálogo de 10s com 3 opções: 'yes' (Sim), 'no' (Não) ou 'ignore_all' (Ignorar Todos)."""
        decision_event = threading.Event()
        result_holder = ["no"]

        def _show_modal():
            dlg = ctk.CTkToplevel(self)
            dlg.title(f"Arquivo Repetido Detectado — {APP_TITLE}")
            dlg.geometry("650x320")
            dlg.resizable(False, False)

            card = ctk.CTkFrame(
                dlg,
                fg_color="#0f172a",
                border_width=2,
                border_color="#f59e0b",
                corner_radius=12,
            )
            card.pack(fill="both", expand=True, padx=14, pady=14)

            ctk.CTkLabel(
                card,
                text="⚠️ Este Arquivo Já Existe na Varredura de Downloads!",
                font=ctk.CTkFont(size=16, weight="bold"),
                text_color="#fbbf24",
            ).pack(padx=16, pady=(14, 6))

            ctk.CTkLabel(
                card,
                text=(
                    f"Arquivo: {item.path.name} ({format_size(item.size)})\n"
                    f"Encontrado em: {existing_info}\n\n"
                    "Deseja subir este arquivo novamente ou ignorar os repetidos?"
                ),
                font=ctk.CTkFont(size=13),
                text_color="#f8fafc",
                justify="center",
                wraplength=590,
            ).pack(padx=16, pady=(0, 8))

            timer_lbl = ctk.CTkLabel(
                card,
                text="⏳ Tempo restante: 10 segundos (sem resposta = ignorar e ir para o próximo)",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#38bdf8",
            )
            timer_lbl.pack(padx=16, pady=(2, 6))

            countdown_bar = ctk.CTkProgressBar(
                card,
                width=550,
                height=10,
                progress_color="#f59e0b",
            )
            countdown_bar.pack(padx=20, pady=(0, 14))
            countdown_bar.set(1.0)

            btn_row = ctk.CTkFrame(card, fg_color="transparent")
            btn_row.pack(fill="x", padx=14, pady=(4, 14))

            remaining = [10]
            closed = [False]

            def _finish(choice: str):
                if closed[0]:
                    return
                closed[0] = True
                result_holder[0] = choice
                try:
                    dlg.grab_release()
                except Exception:
                    pass
                try:
                    dlg.destroy()
                except Exception:
                    pass
                decision_event.set()

            btn_yes = ctk.CTkButton(
                btn_row,
                text="✅ SIM (Subir)",
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="#16a34a",
                hover_color="#15803d",
                text_color="#ffffff",
                height=42,
                width=175,
                command=lambda: _finish("yes"),
            )
            btn_yes.pack(side="left", padx=5, expand=True)

            btn_no = ctk.CTkButton(
                btn_row,
                text="❌ NÃO (Ignorar — 10s)",
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="#dc2626",
                hover_color="#b91c1c",
                text_color="#ffffff",
                height=42,
                width=195,
                command=lambda: _finish("no"),
            )
            btn_no.pack(side="left", padx=5, expand=True)

            btn_ignore_all = ctk.CTkButton(
                btn_row,
                text="⏭ IGNORAR TODOS",
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="#d97706",
                hover_color="#b45309",
                text_color="#ffffff",
                height=42,
                width=195,
                command=lambda: _finish("ignore_all"),
            )
            btn_ignore_all.pack(side="left", padx=5, expand=True)

            dlg.protocol("WM_DELETE_WINDOW", lambda: _finish("no"))

            try:
                dlg.transient(self)
                dlg.attributes("-topmost", True)
                dlg.update_idletasks()
                x = self.winfo_x() + (self.winfo_width() // 2) - 325
                y = self.winfo_y() + (self.winfo_height() // 2) - 160
                dlg.geometry(f"650x320+{max(x, 40)}+{max(y, 40)}")
                dlg.lift()
                dlg.focus_force()
                dlg.grab_set()
                self._apply_window_icon(dlg)
            except Exception:
                pass

            def _tick():
                if closed[0]:
                    return
                if self.cancel_requested or self.is_paused:
                    _finish("no")
                    return
                remaining[0] -= 1
                if remaining[0] <= 0:
                    _finish("no")
                    return
                try:
                    sec = remaining[0]
                    timer_lbl.configure(
                        text=f"⏳ Tempo restante: {sec} segundo(s) (sem resposta = ignorar e ir para o próximo)"
                    )
                    countdown_bar.set(sec / 10.0)
                    btn_no.configure(text=f"❌ NÃO (Ignorar — {sec}s)")
                    dlg.after(1000, _tick)
                except Exception:
                    _finish("no")

            dlg.after(1000, _tick)

        self.after(0, _show_modal)
        decision_event.wait(timeout=12.5)
        return result_holder[0]

    async def _async_sleep_interruptible_ul(
        self, seconds: int, status_prefix: str
    ) -> bool:
        """Aguarda `seconds` segundos atualizando a UI a cada 1s; retorna False se cancelado/pausado."""
        for remaining in range(seconds, 0, -1):
            if self.cancel_requested or self.is_paused:
                return False
            if remaining % 60 == 0 or remaining <= 30 or seconds <= 15:
                if remaining >= 60:
                    mins = remaining // 60
                    secs = remaining % 60
                    time_str = f"{mins}m {secs:02d}s"
                else:
                    time_str = f"{remaining}s"
                self.after(
                    0,
                    lambda msg=f"{status_prefix} (aguardando {time_str}...)": self.current_file_label.configure(
                        text=msg
                    ),
                )
            await asyncio.sleep(1)
        return not (self.cancel_requested or self.is_paused)

    async def _async_process_queue(
        self,
        items: List[FileQueueItem],
        target_entity: Union[int, str],
        target_label: str,
    ):
        total_count = len(items)
        completed_count = 0
        current_batch = list(items)
        cycle_number = 1
        ignore_all_duplicates = False
        check_duplicates = getattr(self, "_snap_ignore_duplicates", True)
        use_caption = getattr(self, "_snap_use_caption", False)
        caption_tpl = getattr(self, "_snap_caption_tpl", "{filename}")

        while current_batch and not self.cancel_requested and not self.is_paused:
            deferred_for_1h: List[FileQueueItem] = []
            batch_total_bytes = max(
                sum(
                    max(it.size, 0)
                    for it in current_batch
                    if it.size <= MAX_TELEGRAM_FILE_SIZE
                    and it.status != "Ignorado (Repetido)"
                    and it.item_id in self.queue_by_id
                    and not getattr(it, "ignored_by_delete", False)
                ),
                1,
            )
            completed_batch_bytes = 0

            for idx, item in enumerate(current_batch, start=1):
                # Se o usuário excluiu este arquivo da aba UPLOAD, ignora imediatamente
                if item.item_id not in self.queue_by_id or getattr(
                    item, "ignored_by_delete", False
                ):
                    continue

                if self.cancel_requested:
                    self._update_item_ui(item, "Cancelado", "0%")
                    continue

                if self.is_paused:
                    self._update_item_ui(item, "Pausado", "0%")
                    break

                # Ignora automaticamente arquivos maiores que 2 GB
                if item.size > MAX_TELEGRAM_FILE_SIZE:
                    self._update_item_ui(item, "Ignorado (> 2GB)", "Ignorado")
                    self.log(
                        f"⏭ Ignorado (> 2 GB): '{item.path.name}' ({format_size(item.size)}) excede o limite de 2 GB do Telegram."
                    )
                    self.after(0, self._update_queue_summary)
                    self.after(0, self._save_queue_state)
                    continue

                # Se o arquivo foi marcado como Ignorado (Repetido) pelo botão 'Ignorar Todos', pula direto
                if item.status in ("Ignorado (Repetido)", "Ignorado (> 2GB)"):
                    continue

                # Verifica na lista de Downloads (varredura) se o arquivo já existe quando 'Ignorar repetidos' está ativo
                if check_duplicates and cycle_number == 1:
                    existing_info = self._file_exists_in_download_scan(item)
                    if existing_info:
                        if ignore_all_duplicates:
                            self._update_item_ui(
                                item, "Ignorado (Repetido)", "Ignorado"
                            )
                            batch_total_bytes = max(1, batch_total_bytes - max(item.size, 0))
                            self.after(0, self._update_queue_summary)
                            continue

                        self.after(
                            0,
                            lambda name=item.path.name: self.current_file_label.configure(
                                text=f"Arquivo repetido detectado: {name} (aguardando confirmação de 10s...)"
                            ),
                        )
                        dup_choice = await asyncio.to_thread(
                            self._ask_duplicate_upload_10s, item, existing_info
                        )
                        if dup_choice == "ignore_all":
                            ignore_all_duplicates = True
                            skipped_now = 0
                            for rem_item in current_batch[idx - 1 :]:
                                if self._file_exists_in_download_scan(rem_item):
                                    self._update_item_ui(
                                        rem_item, "Ignorado (Repetido)", "Ignorado"
                                    )
                                    batch_total_bytes = max(
                                        1, batch_total_bytes - max(rem_item.size, 0)
                                    )
                                    skipped_now += 1
                            self.log(
                                f"⏭ IGNORAR TODOS ativado: {skipped_now} arquivo(s) repetido(s) ignorado(s) de uma só vez! Enviando apenas os arquivos novos."
                            )
                            self.after(0, self._update_queue_summary)
                            self.after(0, self._save_queue_state)
                            continue
                        elif dup_choice != "yes":
                            self._update_item_ui(
                                item, "Ignorado (Repetido)", "Ignorado"
                            )
                            batch_total_bytes = max(1, batch_total_bytes - max(item.size, 0))
                            self.log(
                                f"⏭ Ignorado (Repetido): '{item.path.name}' já existe na varredura de Downloads."
                            )
                            self.after(0, self._update_queue_summary)
                            self.after(0, self._save_queue_state)
                            continue

                caption = ""
                if use_caption:
                    caption = (
                        caption_tpl.replace("{filename}", item.path.name)
                        .replace("{stem}", item.path.stem)
                        .replace("{size}", format_size(item.size))
                    )

                max_attempts = 10
                for attempt in range(1, max_attempts + 1):
                    if self.cancel_requested or self.is_paused:
                        break
                    if item.item_id not in self.queue_by_id or getattr(
                        item, "ignored_by_delete", False
                    ):
                        break

                    att_suffix = (
                        f" [Tentativa {attempt}/{max_attempts}]"
                        if attempt > 1
                        else ""
                    )
                    self._update_item_ui(
                        item,
                        "Enviando",
                        f"{item.progress * 100:0.0f}%",
                        scroll_to=True,
                    )
                    self.after(
                        0,
                        lambda name=item.path.name, i=idx, t=len(current_batch), sfx=att_suffix: self.current_file_label.configure(
                            text=f"Enviando ({i}/{t}): {name}{sfx}"
                        ),
                    )

                    start_t = time.time()
                    last_ui_update = [0.0]
                    done_before_this = completed_batch_bytes
                    cur_batch_tot = max(batch_total_bytes, 1)

                    def progress_cb(
                        sent_bytes: int,
                        total_bytes: int,
                        current_item=item,
                        t0=start_t,
                        last_upd=last_ui_update,
                        prev_b=done_before_this,
                        tot_batch_b=cur_batch_tot,
                    ):
                        if self.cancel_requested:
                            raise asyncio.CancelledError(
                                "Envio cancelado pelo usuário."
                            )
                        if self.is_paused:
                            raise TransferPausedError("Envio pausado pelo usuário.")
                        if (
                            current_item.item_id not in self.queue_by_id
                            or getattr(current_item, "ignored_by_delete", False)
                        ):
                            raise ItemDeletedFromQueueError(
                                "Arquivo excluído da fila pelo usuário."
                            )

                        now = time.time()
                        if sent_bytes < total_bytes and (now - last_upd[0]) < 0.07:
                            return
                        last_upd[0] = now

                        total_b = max(total_bytes or current_item.size or 1, 1)
                        frac = min(sent_bytes / total_b, 1.0)
                        current_item.progress = frac
                        elapsed = max(now - t0, 0.001)
                        speed_bps = sent_bytes / elapsed
                        speed_mb = speed_bps / (1024 * 1024)
                        overall_frac = min(
                            (prev_b + sent_bytes) / max(tot_batch_b, 1),
                            1.0,
                        )
                        pct_str = f"{frac * 100:0.1f}%"

                        rem_file_b = max(0, total_b - sent_bytes)
                        file_eta_str = (
                            format_eta(rem_file_b / speed_bps)
                            if speed_bps > 0
                            else "calculando..."
                        )
                        rem_total_b = max(0, tot_batch_b - (prev_b + sent_bytes))
                        total_eta_str = (
                            format_eta(rem_total_b / speed_bps)
                            if speed_bps > 0
                            else "calculando..."
                        )

                        def _ui():
                            if self.tree.exists(current_item.item_id):
                                self.tree.set(
                                    current_item.item_id, "progress", pct_str
                                )
                            self.file_progress_bar.set(
                                frac,
                                f"{frac * 100:0.1f}%   •   Tempo previsto: {file_eta_str}",
                            )
                            self.total_progress_bar.set(
                                overall_frac,
                                f"{overall_frac * 100:0.1f}%   •   Tempo previsto: {total_eta_str}",
                            )
                            if hasattr(self, "file_progress_title_label"):
                                self.file_progress_title_label.configure(
                                    text=f"Progresso do Arquivo Atual:   •   Tempo previsto para conclusão: {file_eta_str}"
                                )
                            if hasattr(self, "total_progress_title_label"):
                                self.total_progress_title_label.configure(
                                    text=f"Progresso Total da Operação:   •   Tempo previsto para conclusão: {total_eta_str}"
                                )
                            self.speed_label.configure(
                                text=f"Velocidade: {speed_mb:0.2f} MB/s | Progresso Geral: {completed_count}/{total_count} ({overall_frac * 100:0.0f}%)"
                            )

                        self.after(0, _ui)

                    try:
                        if self.connected_mode == "personal":
                            await self._upload_via_telethon(
                                item, target_entity, caption, progress_cb
                            )
                        else:
                            await asyncio.to_thread(
                                self._upload_via_bot_http, item, caption, progress_cb
                            )

                        completed_count += 1
                        completed_batch_bytes += max(item.size, 0)
                        item.progress = 1.0
                        self._record_sent_file(item, target_label)
                        self._update_item_ui(item, "Concluído", "100%")
                        self.log(
                            f"✓ Enviado ({completed_count}/{total_count}): {item.path.name}"
                        )
                        self.after(0, self._update_queue_summary)
                        self.after(0, self._save_queue_state)
                        break

                    except ItemDeletedFromQueueError:
                        batch_total_bytes = max(1, batch_total_bytes - max(item.size, 0))
                        self.log(
                            f"🗑 Envio de '{item.path.name}' interrompido e ignorado (arquivo excluído da lista)."
                        )
                        self.after(0, self._update_queue_summary)
                        self.after(0, self._save_queue_state)
                        break
                    except TransferPausedError:
                        self._update_item_ui(
                            item, "Pausado", f"{item.progress * 100:0.0f}%"
                        )
                        self.after(0, self._save_queue_state)
                        break
                    except asyncio.CancelledError:
                        self._update_item_ui(
                            item, "Cancelado", f"{item.progress * 100:0.0f}%"
                        )
                        self.log(f"■ Envio interrompido: {item.path.name}")
                        self.after(0, self._save_queue_state)
                        break
                    except errors.FloodWaitError as fw:
                        wait_sec = fw.seconds
                        self.log(
                            f"Limite de velocidade do Telegram atingido em '{item.path.name}'. Aguardando {wait_sec}s..."
                        )
                        ok = await self._async_sleep_interruptible_ul(
                            wait_sec,
                            f"Aguardando limite do Telegram ({item.path.name})",
                        )
                        if not ok:
                            break
                    except Exception as exc:
                        item.error_msg = str(exc)
                        self._update_item_ui(
                            item, "Erro", f"Falha ({attempt}/10)"
                        )
                        self.log(
                            f"⚠ Falha de rede ao enviar '{item.path.name}' (tentativa {attempt}/{max_attempts}): {exc}"
                        )
                        try:
                            if (
                                self.connected_mode == "personal"
                                and self.client
                                and not self.client.is_connected()
                            ):
                                await self.client.connect()
                        except Exception:
                            pass

                        if attempt < max_attempts:
                            self.log(
                                f"⏳ Aguardando 10 segundos antes da tentativa {attempt + 1}/{max_attempts} para '{item.path.name}'..."
                            )
                            ok = await self._async_sleep_interruptible_ul(
                                10,
                                f"Falha de rede em '{item.path.name}' — Tentativa {attempt}/{max_attempts}",
                            )
                            if not ok:
                                break
                        else:
                            self._update_item_ui(
                                item, "Aguardando 1h", "Adiado 1h"
                            )
                            self.log(
                                f"⏭ '{item.path.name}' falhou após 10 tentativas. Pulando para o próximo da fila (nova tentativa automática em 1 hora)."
                            )
                            self.after(0, self._save_queue_state)
                            deferred_for_1h.append(item)

            if self.cancel_requested or self.is_paused:
                break

            if deferred_for_1h:
                cycle_number += 1
                self.log(
                    f"🕒 {len(deferred_for_1h)} arquivo(s) com falha de rede aguardarão 1 hora (3600s) para nova rodada de envio (ou clique em '■ Parar' para cancelar)."
                )
                ok = await self._async_sleep_interruptible_ul(
                    3600,
                    f"🕒 Aguardando 1 hora para tentar enviar novamente {len(deferred_for_1h)} arquivo(s) pendente(s)",
                )
                if not ok:
                    break
                current_batch = deferred_for_1h
            else:
                break

        return completed_count, total_count

    async def _get_parallel_upload_senders(
        self, desired_connections: int = 4
    ) -> List[MTProtoSender]:
        """
        Mantém um pool de conexões MTProto paralelas autenticadas no mesmo Data Center (DC) da conta
        para multiplicar a velocidade de upload (evitando o gargalo de 1 conexão sequencial).
        """
        if not self.client or not self.client.is_connected():
            await self.client.connect()

        dc_id = self.client.session.dc_id
        auth_key = self.client.session.auth_key
        if self._parallel_senders_dc_id != dc_id:
            for old_s in self._parallel_upload_senders:
                try:
                    await old_s.disconnect()
                except Exception:
                    pass
            self._parallel_upload_senders.clear()
            self._parallel_senders_dc_id = dc_id

        # Mantém apenas conexões ativas
        active_senders: List[MTProtoSender] = []
        for s in self._parallel_upload_senders:
            if getattr(s, "_user_connected", False) and s.is_connected():
                active_senders.append(s)
        self._parallel_upload_senders = active_senders

        needed = max(0, desired_connections - len(self._parallel_upload_senders))
        if needed > 0 and auth_key:
            try:
                dc = await self.client._get_dc(dc_id)
                for _ in range(needed):
                    try:
                        sender = MTProtoSender(auth_key, loggers=self.client._log)
                        await sender.connect(
                            self.client._connection(
                                dc.ip_address,
                                dc.port,
                                dc.id,
                                loggers=self.client._log,
                                proxy=self.client._proxy,
                                local_addr=self.client._local_addr,
                            )
                        )
                        init_req = InitConnectionRequest(
                            api_id=self.client.api_id,
                            device_model="PC 64bit",
                            system_version="Windows 11",
                            app_version=APP_VERSION,
                            system_lang_code="pt",
                            lang_pack="",
                            lang_code="pt",
                            query=GetConfigRequest(),
                        )
                        await sender.send(InvokeWithLayerRequest(LAYER, init_req))
                        self._parallel_upload_senders.append(sender)
                    except Exception:
                        break
            except Exception:
                pass

        return list(self._parallel_upload_senders)

    async def _fast_upload_file_telethon(
        self,
        file_path: Path,
        progress_cb,
    ) -> Union[InputFileBig, InputSizedFile]:
        """
        Motor Turbo de Upload Paralelo:
        Divide o arquivo em blocos máximos de 512 KB e envia até 16 blocos simultaneamente
        distribuídos em múltiplas conexões TCP/MTProto paralelas para atingir a taxa máxima da rede.
        """
        file_size = file_path.stat().st_size
        part_size = 512 * 1024  # Tamanho máximo permitido pelo protocolo MTProto (512 KB)
        part_count = (file_size + part_size - 1) // part_size
        is_big = file_size > 10 * 1024 * 1024
        file_id = helpers.generate_random_long()
        file_name = file_path.name
        hash_md5 = hashlib.md5()

        # Determina quantidade ideal de conexões e workers concorrentes conforme o tamanho do arquivo
        if part_count <= 2:
            num_workers = max(1, part_count)
            senders = []
        elif part_count <= 16:
            num_workers = min(8, part_count)
            senders = await self._get_parallel_upload_senders(desired_connections=2)
        else:
            num_workers = 16
            senders = await self._get_parallel_upload_senders(desired_connections=4)

        chunk_queue: "asyncio.Queue[Optional[Tuple[int, bytes]]]" = asyncio.Queue(
            maxsize=num_workers * 2
        )
        bytes_uploaded = 0
        worker_errors: List[BaseException] = []

        async def _producer():
            try:
                with open(file_path, "rb") as f:
                    for part_idx in range(part_count):
                        if worker_errors:
                            break
                        data = f.read(part_size)
                        if not data:
                            break
                        if not is_big:
                            hash_md5.update(data)
                        await chunk_queue.put((part_idx, data))
            except BaseException as prod_err:
                worker_errors.append(prod_err)
            finally:
                for _ in range(num_workers):
                    await chunk_queue.put(None)

        async def _worker(worker_idx: int):
            nonlocal bytes_uploaded
            assigned_sender = (
                senders[worker_idx % len(senders)] if senders else None
            )
            while True:
                item_tuple = await chunk_queue.get()
                if item_tuple is None:
                    chunk_queue.task_done()
                    break
                if worker_errors:
                    chunk_queue.task_done()
                    continue

                part_idx, part_bytes = item_tuple
                try:
                    if is_big:
                        req = SaveBigFilePartRequest(
                            file_id, part_idx, part_count, part_bytes
                        )
                    else:
                        req = SaveFilePartRequest(file_id, part_idx, part_bytes)

                    sent_ok = False
                    if (
                        assigned_sender is not None
                        and getattr(assigned_sender, "_user_connected", False)
                        and assigned_sender.is_connected()
                    ):
                        try:
                            res = await assigned_sender.send(req)
                            sent_ok = bool(res)
                        except errors.FloodWaitError:
                            raise
                        except Exception:
                            sent_ok = False

                    if not sent_ok:
                        res = await self.client(req)
                        if not res:
                            raise RuntimeError(
                                f"Falha ao enviar parte {part_idx + 1}/{part_count}"
                            )

                    bytes_uploaded += len(part_bytes)
                    if progress_cb:
                        progress_cb(min(bytes_uploaded, file_size), file_size)
                except BaseException as w_err:
                    worker_errors.append(w_err)
                finally:
                    chunk_queue.task_done()

        tasks = [asyncio.create_task(_producer())]
        for w_i in range(num_workers):
            tasks.append(asyncio.create_task(_worker(w_i)))

        await asyncio.gather(*tasks, return_exceptions=True)

        if worker_errors:
            # Propaga prioritariamente pausas, exclusões, cancelamentos ou FloodWait
            for err in worker_errors:
                if isinstance(
                    err,
                    (
                        TransferPausedError,
                        ItemDeletedFromQueueError,
                        asyncio.CancelledError,
                        errors.FloodWaitError,
                    ),
                ):
                    raise err
            raise worker_errors[0]

        if is_big:
            return InputFileBig(file_id, part_count, file_name)
        return InputSizedFile(
            file_id, part_count, file_name, md5=hash_md5, size=file_size
        )

    async def _upload_via_telethon(
        self,
        item: FileQueueItem,
        target_entity: Union[int, str],
        caption: str,
        progress_cb,
    ):
        if not self.client or not self.client.is_connected():
            await self.client.connect()

        force_doc = getattr(self, "_snap_force_doc", True)
        file_str = str(item.path)

        # Usa o motor Turbo de upload paralelo para arquivos >= 512 KB
        if item.size >= 512 * 1024:
            uploaded_handle = await self._fast_upload_file_telethon(
                item.path, progress_cb
            )
            attributes, mime_type = utils.get_attributes(
                file_str,
                force_document=force_doc,
            )
            await self.client.send_file(
                entity=target_entity,
                file=uploaded_handle,
                caption=caption or None,
                force_document=force_doc,
                attributes=attributes,
                mime_type=mime_type,
            )
        else:
            await self.client.send_file(
                entity=target_entity,
                file=file_str,
                caption=caption or None,
                force_document=force_doc,
                part_size_kb=512,
                progress_callback=progress_cb,
            )

    def _upload_via_bot_http(self, item: FileQueueItem, caption: str, progress_cb):
        token = self.config_data.get("bot_token", "").strip()
        chat_id = self.config_data.get("bot_chat_id", "").strip()
        if item.size > 50 * 1024 * 1024:
            raise RuntimeError(
                "A API de Bot padrão limita arquivos a 50 MB. Use a Conta Pessoal (Opção A) para arquivos de até 2 GB/4 GB."
            )

        boundary = f"----TelegramUploaderBoundary{int(time.time() * 1000)}"
        url = f"https://api.telegram.org/bot{token}/sendDocument"

        header_parts = []
        header_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        header_parts.append(
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode(
                "utf-8"
            )
        )
        if caption:
            header_parts.append(f"--{boundary}\r\n".encode("utf-8"))
            header_parts.append(
                f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode(
                    "utf-8"
                )
            )

        header_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        header_parts.append(
            f'Content-Disposition: form-data; name="document"; filename="{item.path.name}"\r\n'.encode(
                "utf-8"
            )
        )
        header_parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        prefix = b"".join(header_parts)
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")

        file_data = item.path.read_bytes()
        body = prefix + file_data + suffix
        progress_cb(len(file_data) // 2, len(file_data))

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))
            if not resp_json.get("ok"):
                raise RuntimeError(resp_json.get("description", "Erro na API do Bot"))
        progress_cb(len(file_data), len(file_data))

    def _update_item_ui(
        self,
        item: FileQueueItem,
        status_tag: str,
        progress_text: str,
        scroll_to: bool = False,
    ):
        item.status = status_tag

        def _ui():
            if not self._item_matches_filter(item):
                if self.tree.exists(item.item_id):
                    self.tree.delete(item.item_id)
                return

            if not self.tree.exists(item.item_id):
                self._insert_item_row(item)

            chk_str = "☑" if item.checked else "☐"
            icon = "✓" if status_tag in ("Concluído", "Já Enviado") else "📄"
            self.tree.set(item.item_id, "check", chk_str)
            self.tree.set(item.item_id, "name", f"{icon} {item.path.name}")
            self.tree.set(item.item_id, "status", status_tag)
            self.tree.set(item.item_id, "progress", progress_text)
            self.tree.set(item.item_id, "sent_at", item.sent_at or "-")
            self.tree.item(item.item_id, tags=(status_tag,))
            if scroll_to:
                self.tree.see(item.item_id)

        self.after(0, _ui)

    def _on_queue_finished(self, stats):
        completed, total = stats
        self.is_uploading = False

        def _ui():
            if self.is_paused:
                self.start_btn.configure(
                    state="normal",
                    text="  CONTINUAR UPLOAD",
                    image=self.nav_icons["play"],
                    fg_color="#16a34a",
                    hover_color="#15803d",
                    text_color_disabled="#e2e8f0",
                )
                self.cancel_btn.configure(state="normal")
            else:
                self._reset_controls_after_stop()
                self.current_file_label.configure(
                    text=f"Fila de upload finalizada! {completed} de {total} arquivo(s) enviado(s)."
                )
                if completed > 0 and self.connected_mode == "personal":
                    self.scan_current_group_files()
            self._update_queue_summary()
            self._save_queue_state()

        self.after(0, _ui)
        if not self.is_paused:
            self.log(
                f"Processo de upload concluído: {completed}/{total} arquivos enviados."
            )

    def _on_queue_error(self, exc: Exception):
        self.is_uploading = False

        def _ui():
            self._reset_controls_after_stop()
            self._save_queue_state()
            self.current_file_label.configure(text=f"Erro na fila: {exc}")

        self.after(0, _ui)
        self.log(f"Erro inesperado na fila: {exc}")


def _find_all_desktop_and_start_menu_dirs() -> List[Path]:
    """Localiza todas as pastas possíveis de Área de Trabalho (incluindo OneDrive) e Menu Iniciar do usuário."""
    dirs: List[Path] = []
    home = Path.home()
    for d_name in ("Desktop", "Área de Trabalho"):
        cand = home / d_name
        if cand.is_dir():
            dirs.append(cand)
    try:
        for child in home.iterdir():
            if child.is_dir() and child.name.lower().startswith("onedrive"):
                for d_name in ("Desktop", "Área de Trabalho"):
                    cand = child / d_name
                    if cand.is_dir():
                        dirs.append(cand)
    except Exception:
        pass
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    start_menu_progs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if start_menu_progs.is_dir():
        dirs.append(start_menu_progs)
    return dirs


def run_uninstaller_flow(parent_win=None, active_client=None, async_worker=None):
    """
    Desinstalador oficial do Telegram Cloud Drive:
    1. Desconecta e apaga todas as informações de login e sessão do Telegram deste computador.
    2. Remove os atalhos da Área de Trabalho e do Menu Iniciar.
    3. Remove o registro do Painel de Controle (Programas e Recursos / Aplicativos Instalados).
    4. Remove a pasta de instalação do programa (%LOCALAPPDATA%\\Programs\\TelegramCloudDrive).
    """
    temp_root = None
    if parent_win is None:
        temp_root = tk.Tk()
        temp_root.withdraw()
        temp_root.attributes("-topmost", True)

    confirm = messagebox.askyesno(
        f"Desinstalar {APP_NAME}",
        (
            f"Deseja realmente desinstalar o {APP_NAME} deste computador?\n\n"
            "• O programa e os atalhos da Área de Trabalho e do Menu Iniciar serão removidos.\n"
            "• Todas as informações de login do Telegram serão apagadas deste computador "
            "(se você instalar o programa novamente no futuro, será necessário fazer um novo login)."
        ),
        parent=parent_win or temp_root,
    )
    if not confirm:
        if temp_root is not None:
            temp_root.destroy()
        return

    # 1. Encerra a sessão no servidor do Telegram se estiver conectada
    try:
        if active_client is not None and async_worker is not None:
            async def _logout_active():
                try:
                    if not active_client.is_connected():
                        await active_client.connect()
                    await active_client.log_out()
                except Exception:
                    try:
                        await active_client.disconnect()
                    except Exception:
                        pass

            fut = asyncio.run_coroutine_threadsafe(_logout_active(), async_worker.loop)
            try:
                fut.result(timeout=4.0)
            except Exception:
                pass
        else:
            session_file = Path(f"{SESSION_PERSONAL}.session")
            if session_file.exists():
                async def _standalone_logout():
                    c = TelegramClient(SESSION_PERSONAL, DEFAULT_API_ID, DEFAULT_API_HASH)
                    try:
                        await asyncio.wait_for(c.connect(), timeout=3.0)
                        if await c.is_user_authorized():
                            await asyncio.wait_for(c.log_out(), timeout=3.0)
                    except Exception:
                        pass
                    finally:
                        try:
                            await c.disconnect()
                        except Exception:
                            pass

                try:
                    asyncio.run(_standalone_logout())
                except Exception:
                    pass
    except Exception:
        pass

    # Fecha handle do log de crash para liberar exclusão da pasta de dados
    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        if "_crash_fh" in globals() and _crash_fh:
            _crash_fh.close()
    except Exception:
        pass

    # 2. Apaga a pasta de dados locais e sessão de login (%LOCALAPPDATA%\BolinhaTelegramUploader)
    local_base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    user_data_root = local_base / "BolinhaTelegramUploader"
    try:
        if user_data_root.exists():
            shutil.rmtree(user_data_root, ignore_errors=True)
    except Exception:
        pass

    # Apaga possíveis arquivos legados na pasta do executável
    for legacy_name in ("session_personal.session", "session_personal.session-journal", "config.json", "sent_history.json", "queue_state.json"):
        try:
            lp = EXE_DIR / legacy_name
            if lp.exists():
                lp.unlink()
        except Exception:
            pass

    # 3. Remove atalhos da Área de Trabalho e do Menu Iniciar
    for folder in _find_all_desktop_and_start_menu_dirs():
        for lnk_name in (f"{APP_NAME}.lnk", f"Desinstalar {APP_NAME}.lnk", "Bolinha Telegram Uploader.lnk"):
            lnk_path = folder / lnk_name
            try:
                if lnk_path.exists():
                    lnk_path.unlink()
            except Exception:
                pass

    # 4. Remove entrada do Painel de Controle / Aplicativos Instalados do Windows (Registro HKCU)
    try:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY)
    except Exception:
        pass

    # 5. Agenda remoção da pasta de instalação (%LOCALAPPDATA%\Programs\TelegramCloudDrive) após fechar o processo
    install_dir = local_base / "Programs" / "TelegramCloudDrive"
    temp_dir = Path(os.environ.get("TEMP", str(local_base)))
    cleanup_bat = temp_dir / "_remove_telegram_cloud_drive.bat"
    bat_content = f"""@echo off
chcp 65001 >nul
ping 127.0.0.1 -n 3 >nul
rmdir /s /q "{user_data_root}" >nul 2>&1
rmdir /s /q "{install_dir}" >nul 2>&1
del "%~f0" >nul 2>&1
"""
    try:
        cleanup_bat.write_text(bat_content, encoding="utf-8")
        subprocess.Popen(
            ["cmd.exe", "/c", str(cleanup_bat)],
            cwd=str(temp_dir),
            env=get_clean_pyinstaller_env(),
            creationflags=0x08000000,
        )
    except Exception:
        pass

    messagebox.showinfo(
        f"Desinstalação Concluída — {APP_NAME}",
        (
            f"✓ O {APP_NAME} foi desinstalado com sucesso!\n\n"
            "• Todos os dados de login e sessão do Telegram foram removidos deste computador.\n"
            "• Os atalhos da Área de Trabalho e do Menu Iniciar foram excluídos."
        ),
        parent=parent_win or temp_root,
    )

    try:
        if parent_win is not None:
            parent_win.quit()
            parent_win.destroy()
        if temp_root is not None:
            temp_root.destroy()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    try:
        if "--uninstall" in sys.argv:
            run_uninstaller_flow()
        else:
            app = TelegramUploaderApp()
            app.mainloop()
    except SystemExit:
        pass
    except Exception as fatal_exc:
        tb_str = "".join(
            traceback.format_exception(
                type(fatal_exc), fatal_exc, fatal_exc.__traceback__
            )
        )
        append_persistent_log(
            f"FALHA FATAL AO EXECUTAR PROGRAMA:\n{tb_str}", "CRITICAL"
        )
        raise
