"""
Janela principal do Telegram Cloud Drive (CustomTkinter + Treeview com Paginação Virtual).
Integra os módulos:
- storage (config, histórico, estado da fila, cache SQLite com FTS5, SHA-256)
- telegram_core (motor Telethon MTProto paralelo, login QR/Telefone/2FA, classificador de erros)
- updater (GitHub Releases auto-update e desinstalador completo)
"""

import ctypes
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import traceback
from typing import Dict, List, Optional, Set, Tuple, Union
import webbrowser

import customtkinter as ctk
from PIL import Image
from telethon import TelegramClient
from telethon.network import MTProtoSender

from storage.paths import (
    APP_VERSION,
    APP_TITLE,
    APP_SUBTITLE,
    BUNDLE_DIR,
    EXE_DIR,
    USER_DATA_DIR,
    CONFIG_FILE,
    HISTORY_FILE,
    QUEUE_STATE_FILE,
    GROUP_CACHE_FILE,
    GROUP_CACHE_DB_FILE,
    LOG_FILE,
    CRASH_LOG_FILE,
    SESSION_PERSONAL,
    COUNTRY_CODES,
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_API_ID,
    DEFAULT_API_HASH,
    IGNORED_DIRS,
    MAX_TELEGRAM_FILE_SIZE,
    append_persistent_log,
    find_resource_path,
)
from storage.sqlite_cache import GroupCloudSQLiteCache
from telegram_core.models_and_errors import (
    AsyncWorker,
    FileQueueItem,
    RemoteCloudFile,
    format_size,
    make_file_signature,
)
from telegram_core.engine import TelegramTransferEngineMixin
from updater.github_updater import GitHubUpdaterMixin, run_uninstaller_flow
from ui.widgets import (
    _create_nav_icons,
    draw_telegram_wallpaper_canvas,
    LabeledProgressBar,
)


class TelegramUploaderApp(
    ctk.CTk, TelegramTransferEngineMixin, GitHubUpdaterMixin
):
    """Janela principal e orquestrador de UI do Telegram Cloud Drive."""

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
        try:
            self.state("zoomed")
            super().after(50, lambda: self.state("zoomed"))
        except Exception:
            pass

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

        # Banco SQLite local para cache de grupos com migração automática do JSON antigo
        self.sqlite_cache = GroupCloudSQLiteCache(
            GROUP_CACHE_DB_FILE, GROUP_CACHE_FILE
        )

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

        # Estruturas da Aba Download (Drive / Nuvem Telegram + Paginação Virtual para 150.000+ arquivos)
        self.remote_files: List[RemoteCloudFile] = []
        self.remote_by_id: Dict[str, RemoteCloudFile] = {}
        self.remote_by_filename_lower: Dict[str, RemoteCloudFile] = {}
        self.dl_filtered_files: List[RemoteCloudFile] = []
        self.dl_page_size: int = 500
        self.dl_page_index: int = 0
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

        # Estado de login por telefone e conexões paralelas MTProto
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
        super().after(
            1800, lambda: self.check_for_updates(silent_if_up_to_date=True)
        )

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
            tb_text = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
            msg = f"[ERRO CRÍTICO DO SISTEMA] {exc_type.__name__}: {exc_value}\n{tb_text}"
            append_persistent_log(
                f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] {msg}"
            )
            try:
                self.log(
                    f"⚠ Falha capturada pelo sistema: {exc_type.__name__}: {exc_value}"
                )
            except Exception:
                pass

        def _handle_thread_exc(args):
            _handle_uncaught(
                args.exc_type, args.exc_value, args.exc_traceback
            )

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
        ico_path = find_resource_path("telegram_icon.ico") or find_resource_path(
            "bolinha.ico"
        )
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
            "sha256": getattr(item, "sha256", "") or "",
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

        header = ctk.CTkFrame(
            self.welcome_win, fg_color="#1e3a8a", corner_radius=10
        )
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
                "Passo 2: Como fazer Login com Segurança (+ Credenciais Próprias Opcionais)",
                "• Clique no botão 'CONTA DO TELEGRAM' na barra principal e conecte escaneando o QR Code na própria tela ou escolhendo o código do país e digitando seu telefone.\n"
                "• Você também pode cadastrar seu próprio API ID e API Hash gratuitos em 1 minuto pelo site my.telegram.org diretamente na aba Conta.\n"
                "• Segurança Automática: Seu login fica vinculado somente a ESTE computador!",
            ),
            (
                "Passo 3: Escolha o Grupo (Varredura Automática em 2º Plano + Cache SQLite)",
                "• No topo do programa, escolha o seu Grupo na caixa destacada 'Grupo / Chat Ativo'.\n"
                "• O programa carrega instantaneamente os arquivos do banco local SQLite e sincroniza arquivos novos em 2º plano sem travar a tela!",
            ),
            (
                "Passo 4: Fazendo Uploads (Várias Pastas) e Downloads (Com Busca e Paginação)",
                "• Para ENVIAR (Botão UPLOAD PARA TELEGRAM): Use '+ Selecionar Arquivos' para adicionar arquivos ou pastas e clique em '▶ INICIAR UPLOAD'.\n"
                "• Para BAIXAR (Botão DOWNLOAD DO TELEGRAM): Use a Barra de Busca ou navegue pelas páginas (suporta mais de 150.000 arquivos), escolha a pasta e clique em '⬇ BAIXAR SELECIONADOS'!",
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
                        "sha256": getattr(item, "sha256", "") or "",
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
                if size > MAX_TELEGRAM_FILE_SIZE:
                    status = "Ignorado (> 2GB)"
                elif status.startswith("Enviando") or status.startswith(
                    "Aguardando"
                ):
                    status = "Pendente"

                item_id = f"item_{self._next_id_counter}"
                self._next_id_counter += 1

                sig = make_file_signature(p, size)
                sent_rec = self.sent_signatures.get(sig)
                saved_sha = str(entry.get("sha256", "") or "")
                if not saved_sha and sent_rec:
                    saved_sha = str(sent_rec.get("sha256", "") or "")

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
                        sha256=saved_sha,
                    )
                elif sent_rec and status in ("Concluído", "Já Enviado"):
                    item = FileQueueItem(
                        item_id=item_id,
                        path=p,
                        size=size,
                        status=status,
                        progress=1.0,
                        sent_at=sent_rec.get(
                            "sent_at", entry.get("sent_at", "")
                        ),
                        target_chat=sent_rec.get(
                            "target", entry.get("target_chat", "")
                        ),
                        checked=bool(entry.get("checked", False)),
                        sha256=saved_sha,
                    )
                else:
                    item = FileQueueItem(
                        item_id=item_id,
                        path=p,
                        size=size,
                        status=status,
                        progress=(
                            1.0
                            if status in ("Concluído", "Já Enviado")
                            else 0.0
                        ),
                        sent_at=entry.get("sent_at", ""),
                        target_chat=entry.get("target_chat", ""),
                        checked=bool(entry.get("checked", False)),
                        sha256=saved_sha,
                    )
                    if status in ("Pendente", "Pausado", "Erro"):
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
                crash_txt = (
                    CRASH_LOG_FILE.read_text(
                        encoding="utf-8", errors="ignore"
                    ).strip()
                )
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
                lines = LOG_FILE.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
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
            append_persistent_log(
                "Programa fechado pelo usuário (estado da fila salvo).", "INFO"
            )
        except Exception:
            pass
        self.destroy()

    def uninstall_program_from_ui(self):
        """Aciona a desinstalação completa a partir do menu Opções, removendo login e programa."""
        if (
            hasattr(self, "options_win")
            and self.options_win is not None
            and self.options_win.winfo_exists()
        ):
            try:
                self.options_win.destroy()
            except Exception:
                pass
        run_uninstaller_flow(
            parent_win=self,
            active_client=self.client,
            async_worker=getattr(self, "worker", None),
        )

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
        self.options_win.geometry("680x550")
        self.options_win.resizable(False, False)
        self.options_win.transient(self)
        self.options_win.attributes("-topmost", True)
        self._apply_window_icon(self.options_win)

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 340
        y = self.winfo_y() + (self.winfo_height() // 2) - 275
        self.options_win.geometry(f"680x550+{max(x, 40)}+{max(y, 40)}")

        tabs = ctk.CTkTabview(
            self.options_win, fg_color="#17212b", corner_radius=10
        )
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
            self.config_data["hide_welcome_guide"] = bool(
                self.hide_welcome_opt_var.get()
            )
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
            text="👤 Informações da Conta do Telegram & Credenciais Próprias (my.telegram.org)",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#38bdf8",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(8, 6))

        user_info_frame = ctk.CTkFrame(
            tab_account_info, fg_color="#0e1621", corner_radius=8
        )
        user_info_frame.pack(fill="x", padx=14, pady=(0, 8))

        u_name = self.current_user_info.get("name", "Não conectado")
        u_user = self.current_user_info.get("username", "-")
        u_phone = self.current_user_info.get(
            "phone", self.config_data.get("phone", "-")
        )
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

        guide_frame = ctk.CTkFrame(
            tab_account_info,
            fg_color="#0f172a",
            border_width=1,
            border_color="#0284c7",
            corner_radius=8,
        )
        guide_frame.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(
            guide_frame,
            text=(
                "💡 Como gerar seu próprio API ID e API Hash em 1 minuto (Recomendado):\n"
                "1. Clique em '🌐 Abrir my.telegram.org' abaixo e faça login com seu número do Telegram.\n"
                "2. Clique em 'API development tools' e preencha qualquer nome (ex: MeuCloudDrive).\n"
                "3. Copie o 'App api_id' e o 'App api_hash', cole nos campos abaixo e clique em Salvar."
            ),
            font=ctk.CTkFont(size=11),
            text_color="#e2e8f0",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=12, pady=6)

        api_frame = ctk.CTkFrame(
            tab_account_info, fg_color="#0e1621", corner_radius=8
        )
        api_frame.pack(fill="x", padx=14, pady=(0, 8))
        api_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            api_frame,
            text="API ID:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e2e8f0",
        ).grid(row=0, column=0, padx=12, pady=(10, 6), sticky="w")

        opt_api_id_entry = ctk.CTkEntry(api_frame, placeholder_text="Ex: 2040")
        opt_api_id_entry.grid(
            row=0, column=1, padx=12, pady=(10, 6), sticky="ew"
        )
        opt_api_id_entry.insert(
            0, str(self.config_data.get("api_id", DEFAULT_API_ID))
        )

        ctk.CTkLabel(
            api_frame,
            text="API Hash:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e2e8f0",
        ).grid(row=1, column=0, padx=12, pady=(4, 10), sticky="w")

        opt_api_hash_entry = ctk.CTkEntry(
            api_frame, placeholder_text="Hash da API do Telegram"
        )
        opt_api_hash_entry.grid(
            row=1, column=1, padx=12, pady=(4, 10), sticky="ew"
        )
        opt_api_hash_entry.insert(
            0, str(self.config_data.get("api_hash", DEFAULT_API_HASH))
        )

        def _save_api_info():
            new_id = opt_api_id_entry.get().strip() or str(DEFAULT_API_ID)
            new_hash = opt_api_hash_entry.get().strip() or DEFAULT_API_HASH
            try:
                self.config_data["api_id"] = int(new_id)
            except ValueError:
                self.config_data["api_id"] = DEFAULT_API_ID
            self.config_data["api_hash"] = new_hash
            self._save_config()
            if hasattr(self, "acc_api_id_entry"):
                self.acc_api_id_entry.delete(0, "end")
                self.acc_api_id_entry.insert(
                    0, str(self.config_data["api_id"])
                )
            if hasattr(self, "acc_api_hash_entry"):
                self.acc_api_hash_entry.delete(0, "end")
                self.acc_api_hash_entry.insert(
                    0, str(self.config_data["api_hash"])
                )
            self.log("✓ Informações de API ID e API Hash salvas com sucesso.")
            self.options_win.destroy()

        btn_api_row = ctk.CTkFrame(tab_account_info, fg_color="transparent")
        btn_api_row.pack(pady=(4, 8))

        ctk.CTkButton(
            btn_api_row,
            text="🌐 Abrir my.telegram.org",
            width=190,
            height=34,
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: webbrowser.open("https://my.telegram.org"),
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            btn_api_row,
            text="💾 Salvar Credenciais de API",
            width=210,
            height=34,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=_save_api_info,
        ).pack(side="left", padx=6)

    # -------------------------------------------------------------------------
    # Construção da Interface Gráfica Principal + Barra de Navegação Destacada
    # -------------------------------------------------------------------------
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self.ignore_duplicates_var = ctk.BooleanVar(
            value=self.config_data.get("ignore_duplicates_prompt", True)
        )

        # 1. Banner de Título no Topo
        brand_frame = ctk.CTkFrame(self, fg_color="#1e3a8a", corner_radius=10)
        brand_frame.grid(row=0, column=0, padx=14, pady=(8, 4), sticky="ew")
        brand_frame.grid_columnconfigure(0, weight=1)

        title_box = ctk.CTkFrame(brand_frame, fg_color="transparent")
        title_box.grid(row=0, column=0, padx=14, pady=6, sticky="w")

        png_path = find_resource_path("telegram_icon.png") or find_resource_path(
            "bolinha.png"
        )
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

        # 2. Barra de Status da Conexão e Seleção do Grupo/Chat Ativo
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

        # 3. BARRA DE NAVEGAÇÃO LARGA E DESTACADA
        nav_bar_frame = ctk.CTkFrame(
            self,
            fg_color="#0f172a",
            corner_radius=10,
            border_width=2,
            border_color="#334155",
        )
        nav_bar_frame.grid(row=2, column=0, padx=14, pady=(4, 4), sticky="ew")
        for col_idx in range(4):
            nav_bar_frame.grid_columnconfigure(
                col_idx, weight=1, uniform="nav_cols"
            )

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

        # Estilo compartilhado das Tabelas
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
        self.pages_container.grid(
            row=3, column=0, padx=14, pady=4, sticky="nsew"
        )
        self.pages_container.grid_columnconfigure(0, weight=1)
        self.pages_container.grid_rowconfigure(0, weight=1)

        self.tab_upload = ctk.CTkFrame(
            self.pages_container, fg_color="transparent"
        )
        self.tab_download = ctk.CTkFrame(
            self.pages_container, fg_color="transparent"
        )
        self.tab_history = ctk.CTkFrame(
            self.pages_container, fg_color="transparent"
        )
        self.tab_account = ctk.CTkFrame(
            self.pages_container, fg_color="transparent"
        )

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
        self.file_progress_title_label.grid(
            row=1, column=0, padx=12, sticky="w"
        )

        self.file_progress_bar = LabeledProgressBar(
            bottom_frame, height=20, progress_color="#2563eb"
        )
        self.file_progress_bar.grid(
            row=2, column=0, padx=12, pady=(1, 4), sticky="ew"
        )
        self.file_progress_bar.set(0)

        self.total_progress_title_label = ctk.CTkLabel(
            bottom_frame,
            text="Progresso Total da Operação:   •   Tempo previsto para conclusão: --",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#cbd5e1",
        )
        self.total_progress_title_label.grid(
            row=3, column=0, padx=12, sticky="w"
        )

        self.total_progress_bar = LabeledProgressBar(
            bottom_frame, height=20, progress_color="#10b981"
        )
        self.total_progress_bar.grid(
            row=4, column=0, padx=12, pady=(1, 6), sticky="ew"
        )
        self.total_progress_bar.set(0)

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
            "upload": ("#14532d", "#4ade80"),
            "download": ("#0c4a6e", "#38bdf8"),
            "history": ("#78350f", "#fbbf24"),
            "account": ("#1e3a8a", "#60a5fa"),
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
        A varredura do grupo no Telegram (self.is_scanning_remote) roda 100% em segundo plano.
        """
        busy = bool(self.is_scanning)
        sub_txt = (
            detail_msg
            if detail_msg
            else "Aguarde o término da leitura local para evitar travamentos..."
        )

        if hasattr(self, "upload_loading_overlay"):
            if busy:
                self.upload_loading_sub_label.configure(text=sub_txt)
                self.upload_loading_overlay.place(
                    relx=0.5, rely=0.5, anchor="center"
                )
                self.upload_loading_overlay.lift()
            else:
                self.upload_loading_overlay.place_forget()

        if hasattr(self, "download_loading_overlay"):
            self.download_loading_overlay.place_forget()

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
    # SEÇÃO 1: UPLOAD PARA TELEGRAM
    # -------------------------------------------------------------------------
    def _build_upload_tab(self):
        self.tab_upload.grid_columnconfigure(0, weight=1)
        self.tab_upload.grid_rowconfigure(1, weight=1)

        btn_bar = ctk.CTkFrame(self.tab_upload, fg_color="transparent")
        btn_bar.grid(row=0, column=0, padx=8, pady=6, sticky="ew")

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

        columns = (
            "check",
            "name",
            "folder",
            "size",
            "status",
            "progress",
            "sent_at",
        )
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
        self.tree.column("status", width=140, anchor="center", stretch=False)
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
        self.tree.tag_configure("Erro Permanente", foreground="#fb7185")
        self.tree.tag_configure("Cancelado", foreground="#fbbf24")

        self.tree.bind("<Button-1>", self._on_upload_tree_click)
        self.tree.bind("<space>", self._on_upload_tree_space)

        scrollbar = ctk.CTkScrollbar(table_container, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.grid(
            row=0, column=0, padx=(6, 0), pady=(6, 0), sticky="new"
        )
        scrollbar.grid(row=0, column=1, padx=6, pady=6, sticky="ns")

        self.tree.bind("<Delete>", lambda _event: self.remove_selected_items())

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

        self.use_caption_var = ctk.BooleanVar(value=False)

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
    # SEÇÃO 2: DOWNLOAD DO TELEGRAM (Cache SQLite + Paginação Virtual para 150.000+ Arquivos)
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
            width=100,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.search_var.set(""),
        ).grid(row=0, column=2, padx=3)

        # Controles de Paginação Virtual (Suporte fluido a 150.000+ arquivos sem travar a Treeview)
        page_ctrl_frame = ctk.CTkFrame(search_bar, fg_color="#0f172a", corner_radius=6)
        page_ctrl_frame.grid(row=0, column=3, padx=4)

        self.dl_prev_page_btn = ctk.CTkButton(
            page_ctrl_frame,
            text="◀",
            width=30,
            height=28,
            fg_color="#1e293b",
            hover_color="#2563eb",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._change_dl_page(-1),
        )
        self.dl_prev_page_btn.pack(side="left", padx=(4, 2), pady=3)

        self.dl_page_label = ctk.CTkLabel(
            page_ctrl_frame,
            text="Pág. 1/1",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#e2e8f0",
            width=82,
        )
        self.dl_page_label.pack(side="left", padx=4)

        self.dl_next_page_btn = ctk.CTkButton(
            page_ctrl_frame,
            text="▶",
            width=30,
            height=28,
            fg_color="#1e293b",
            hover_color="#2563eb",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._change_dl_page(1),
        )
        self.dl_next_page_btn.pack(side="left", padx=(2, 4), pady=3)

        self.scan_cloud_btn = ctk.CTkButton(
            search_bar,
            text="🔄 Escanear Arquivos do Grupo",
            width=200,
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self.scan_current_group_files,
        )
        self.scan_cloud_btn.grid(row=0, column=4, padx=(4, 4))

        self.cloud_summary_label = ctk.CTkLabel(
            search_bar,
            text="0 arquivos encontrados",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )
        self.cloud_summary_label.grid(
            row=0, column=5, padx=(4, 4), sticky="e"
        )

        # Tabela de Arquivos Disponíveis na Nuvem do Grupo
        dl_table_container = ctk.CTkFrame(self.tab_download, corner_radius=8)
        dl_table_container.grid(row=1, column=0, padx=8, pady=6, sticky="nsew")
        dl_table_container.grid_columnconfigure(0, weight=1)
        dl_table_container.grid_rowconfigure(0, weight=1)

        dl_cols = (
            "check",
            "filename",
            "ext",
            "size",
            "date",
            "status",
            "progress",
        )
        self.dl_tree = ttk.Treeview(
            dl_table_container,
            columns=dl_cols,
            show="headings",
            selectmode="extended",
            style="Dark.Treeview",
        )
        self.dl_tree.heading(
            "check",
            text="☑",
            anchor="center",
            command=self.toggle_all_download_checkboxes,
        )
        self.dl_tree.heading(
            "filename", text="Nome do Arquivo na Nuvem", anchor="w"
        )
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
        self.dl_tree.tag_configure("Erro Permanente", foreground="#fb7185")

        self.dl_tree.bind("<Button-1>", self._on_dl_tree_click)
        self.dl_tree.bind("<space>", self._on_dl_tree_space)
        self.dl_tree.bind(
            "<Delete>", lambda _event: self.remove_selected_download_items()
        )

        dl_scroll = ctk.CTkScrollbar(
            dl_table_container, command=self.dl_tree.yview
        )
        self.dl_tree.configure(yscrollcommand=dl_scroll.set)

        self.dl_tree.grid(row=0, column=0, padx=(6, 0), pady=6, sticky="nsew")
        dl_scroll.grid(row=0, column=1, padx=6, pady=6, sticky="ns")

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
        self.history_tree.heading(
            "target", text="Grupo / Chat de Destino", anchor="w"
        )
        self.history_tree.heading(
            "sent_at", text="Data / Hora do Envio", anchor="center"
        )
        self.history_tree.heading("path", text="Caminho Original", anchor="w")

        self.history_tree.column("filename", width=260, anchor="w", stretch=True)
        self.history_tree.column("size", width=95, anchor="e", stretch=False)
        self.history_tree.column("target", width=220, anchor="w", stretch=False)
        self.history_tree.column(
            "sent_at", width=150, anchor="center", stretch=False
        )
        self.history_tree.column("path", width=280, anchor="w", stretch=True)

        self.history_tree.tag_configure("Concluído", foreground="#4ade80")

        hist_scroll = ctk.CTkScrollbar(
            hist_container, command=self.history_tree.yview
        )
        self.history_tree.configure(yscrollcommand=hist_scroll.set)

        self.history_tree.grid(
            row=0, column=0, padx=(6, 0), pady=6, sticky="nsew"
        )
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
    # SEÇÃO 4: CONTA DO TELEGRAM (QR CODE INLINE + CREDENCIAIS PRÓPRIAS MY.TELEGRAM.ORG + TELEFONE + LOG)
    # -------------------------------------------------------------------------
    def _build_account_tab(self):
        self.tab_account.grid_columnconfigure(0, weight=5)
        self.tab_account.grid_columnconfigure(1, weight=6)
        self.tab_account.grid_rowconfigure(1, weight=1)

        def _bind_responsive_wrap(container, labels, padding: int = 28):
            """Ajusta automaticamente a quebra de linha (wraplength) conforme a largura real da caixa, sem jamais cortar palavras."""
            def _on_resize(event):
                avail = max(180, int(event.width) - padding)
                for lbl in labels:
                    try:
                        if lbl.winfo_exists():
                            lbl.configure(wraplength=avail)
                    except Exception:
                        pass

            container.bind("<Configure>", _on_resize, add="+")

        info_banner = ctk.CTkFrame(
            self.tab_account, fg_color="#1e293b", corner_radius=8
        )
        info_banner.grid(
            row=0, column=0, columnspan=2, padx=10, pady=(4, 6), sticky="ew"
        )

        info_banner_lbl = ctk.CTkLabel(
            info_banner,
            text=(
                "🔒 Segurança Ativada: Sua sessão de login fica vinculada exclusivamente a ESTE computador. "
                "Recomendamos configurar seu próprio API ID / API Hash gratuito (my.telegram.org) abaixo para máxima estabilidade."
            ),
            justify="left",
            anchor="w",
            wraplength=960,
            font=ctk.CTkFont(size=12),
            text_color="#cbd5e1",
        )
        info_banner_lbl.pack(fill="x", padx=12, pady=6, anchor="w")
        _bind_responsive_wrap(info_banner, [info_banner_lbl], padding=28)

        personal_frame = ctk.CTkScrollableFrame(
            self.tab_account, fg_color="#17212b", corner_radius=10
        )
        personal_frame.grid(row=1, column=0, padx=(10, 6), pady=4, sticky="nsew")
        personal_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            personal_frame,
            text="Conectar Conta do Telegram",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#38bdf8",
            anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(8, 6), sticky="ew")

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
        self.qr_inline_frame.grid(
            row=2, column=0, padx=12, pady=(2, 8), sticky="ew"
        )
        self.qr_inline_frame.grid_columnconfigure(0, weight=1)

        self.qr_status_label = ctk.CTkLabel(
            self.qr_inline_frame,
            text="Clique em '📱 Entrar com QR Code' acima para gerar o código aqui na tela.",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            justify="center",
            anchor="center",
            wraplength=320,
        )
        self.qr_status_label.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        _bind_responsive_wrap(self.qr_inline_frame, [self.qr_status_label], padding=28)

        self.qr_image_label = ctk.CTkLabel(
            self.qr_inline_frame,
            text="",
            height=10,
        )
        self.qr_image_label.grid(row=1, column=0, padx=10, pady=(2, 8))

        # 2. Ou entrar com número de telefone
        phone_section = ctk.CTkFrame(
            personal_frame,
            fg_color="#0e1621",
            corner_radius=10,
            border_width=1,
            border_color="#334155",
        )
        phone_section.grid(row=3, column=0, padx=12, pady=(4, 8), sticky="ew")
        phone_section.grid_columnconfigure(1, weight=1)

        phone_title_lbl = ctk.CTkLabel(
            phone_section,
            text="📞 Ou entrar com número de telefone:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
            justify="left",
            anchor="w",
            wraplength=320,
        )
        phone_title_lbl.grid(row=0, column=0, columnspan=2, padx=12, pady=(8, 4), sticky="ew")
        _bind_responsive_wrap(phone_section, [phone_title_lbl], padding=28)

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
        self.country_code_combo.grid(
            row=1, column=1, padx=(4, 12), pady=4, sticky="ew"
        )
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
        self.phone_entry.grid(
            row=2, column=1, padx=(4, 12), pady=4, sticky="ew"
        )
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

        code_row = ctk.CTkFrame(phone_section, fg_color="transparent")
        code_row.grid(
            row=4, column=0, columnspan=2, padx=12, pady=(2, 6), sticky="ew"
        )
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

        # 3. Card de Credenciais Próprias da API do Telegram (abaixo do telefone e acima de Desconectar)
        api_card = ctk.CTkFrame(
            personal_frame,
            fg_color="#0e1621",
            corner_radius=10,
            border_width=1,
            border_color="#0284c7",
        )
        api_card.grid(row=4, column=0, padx=12, pady=(4, 8), sticky="ew")
        api_card.grid_columnconfigure(1, weight=1)

        api_title_lbl = ctk.CTkLabel(
            api_card,
            text="🔑 Credenciais Próprias da API (my.telegram.org — Recomendado):",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
            justify="left",
            anchor="w",
            wraplength=320,
        )
        api_title_lbl.grid(
            row=0, column=0, columnspan=3, padx=10, pady=(8, 4), sticky="ew"
        )

        api_desc_lbl = ctk.CTkLabel(
            api_card,
            text=(
                "Gere em 1 min:\n"
                "1) Acesse my.telegram.org\n"
                "2) Clique em 'API development tools'\n"
                "3) Copie seu api_id e api_hash abaixo:"
            ),
            font=ctk.CTkFont(size=11),
            text_color="#cbd5e1",
            justify="left",
            anchor="w",
            wraplength=320,
        )
        api_desc_lbl.grid(
            row=1, column=0, columnspan=3, padx=10, pady=(0, 6), sticky="ew"
        )
        _bind_responsive_wrap(api_card, [api_title_lbl, api_desc_lbl], padding=28)

        ctk.CTkLabel(
            api_card,
            text="API ID:",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=2, column=0, padx=(10, 4), pady=3, sticky="w")

        self.acc_api_id_entry = ctk.CTkEntry(
            api_card, placeholder_text="Ex: 2040", height=28
        )
        self.acc_api_id_entry.grid(
            row=2, column=1, columnspan=2, padx=(4, 10), pady=3, sticky="ew"
        )
        self.acc_api_id_entry.insert(
            0, str(self.config_data.get("api_id", DEFAULT_API_ID))
        )

        ctk.CTkLabel(
            api_card,
            text="API Hash:",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=3, column=0, padx=(10, 4), pady=3, sticky="w")

        self.acc_api_hash_entry = ctk.CTkEntry(
            api_card, placeholder_text="Hash de 32 caracteres", height=28
        )
        self.acc_api_hash_entry.grid(
            row=3, column=1, columnspan=2, padx=(4, 10), pady=3, sticky="ew"
        )
        self.acc_api_hash_entry.insert(
            0, str(self.config_data.get("api_hash", DEFAULT_API_HASH))
        )

        api_btn_row = ctk.CTkFrame(api_card, fg_color="transparent")
        api_btn_row.grid(
            row=4, column=0, columnspan=3, padx=10, pady=(4, 8), sticky="ew"
        )
        api_btn_row.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            api_btn_row,
            text="🌐 Abrir my.telegram.org",
            height=28,
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: webbrowser.open("https://my.telegram.org"),
        ).grid(row=0, column=0, padx=(0, 4), sticky="ew")

        ctk.CTkButton(
            api_btn_row,
            text="💾 Salvar API ID / Hash",
            height=28,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._save_inline_api_credentials,
        ).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        self.logout_btn = ctk.CTkButton(
            personal_frame,
            text="Desconectar Sessão Atual (Trocar de Conta)",
            fg_color="#dc2626",
            hover_color="#b91c1c",
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.logout_session,
        )
        self.logout_btn.grid(
            row=5, column=0, padx=12, pady=(6, 10), sticky="ew"
        )

        # Coluna Direita: LOG + Opção Bot
        right_frame = ctk.CTkFrame(
            self.tab_account, fg_color="#17212b", corner_radius=10
        )
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
        self.log_box.grid(row=1, column=0, padx=14, pady=(2, 8), sticky="nsew")

        bot_box = ctk.CTkFrame(
            right_frame, fg_color="#0e1621", corner_radius=8
        )
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
        self.bot_token_entry.grid(
            row=1, column=1, padx=4, pady=(2, 8), sticky="ew"
        )
        if self.config_data.get("bot_token"):
            self.bot_token_entry.insert(0, self.config_data.get("bot_token"))

        ctk.CTkLabel(bot_box, text="Chat ID:", font=ctk.CTkFont(size=11)).grid(
            row=1, column=2, padx=(6, 4), pady=(2, 8), sticky="w"
        )
        self.bot_chat_entry = ctk.CTkEntry(
            bot_box, placeholder_text="-100... ou @canal", height=28
        )
        self.bot_chat_entry.grid(
            row=1, column=3, padx=4, pady=(2, 8), sticky="ew"
        )
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

    def _save_inline_api_credentials(self):
        """Salva o API ID e API Hash configurados diretamente na aba Conta do Telegram."""
        raw_id = self.acc_api_id_entry.get().strip() or str(DEFAULT_API_ID)
        raw_hash = self.acc_api_hash_entry.get().strip() or DEFAULT_API_HASH
        try:
            api_id = int(raw_id)
        except ValueError:
            messagebox.showwarning(
                "API ID Inválido",
                "O campo 'API ID' deve conter apenas números (ex: 2040 ou o número gerado em my.telegram.org).",
            )
            return
        self.config_data["api_id"] = api_id
        self.config_data["api_hash"] = raw_hash
        self._save_config()
        self.log(
            f"✓ Credenciais de API salvas com sucesso (API ID: {api_id})."
        )
        messagebox.showinfo(
            "Credenciais Salvas",
            f"✓ Suas credenciais de API do Telegram (API ID: {api_id}) foram salvas com sucesso!",
        )

    # -------------------------------------------------------------------------
    # Utilitários de Log e Eventos Globais
    # -------------------------------------------------------------------------
    def log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        level = (
            "ERROR"
            if any(
                w in message.lower()
                for w in (
                    "erro",
                    "falha",
                    "exceção",
                    "exception",
                    "traceback",
                )
            )
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
            self.config_data["ignore_duplicates_prompt"] = (
                self.ignore_duplicates_var.get()
            )
        self.config_data["download_dir"] = self.dl_dir_entry.get().strip()
        self._save_config()

    def _on_target_changed(self, choice: str):
        self.config_data["last_target"] = choice
        self._save_config()
        self._save_queue_state()
        if self.connected_mode == "personal":
            self.scan_current_group_files()

    # -------------------------------------------------------------------------
    # GERENCIADOR DE DOWNLOAD (Cache SQLite + Paginação Virtual para 150.000+ Arquivos)
    # -------------------------------------------------------------------------
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
        self.dl_filtered_files.clear()
        self.dl_page_index = 0
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)

        dl_dir = Path(
            self.config_data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR))
        )

        # 1. Carrega instantaneamente do banco SQLite local deste grupo
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
                        and (
                            fsize == 0
                            or local_candidate.stat().st_size == fsize
                        )
                    )
                    else "Disponível na Nuvem"
                )
                rf = RemoteCloudFile(
                    row_id=row_id,
                    message_id=mid,
                    filename=fname,
                    ext=str(
                        entry.get("ext", Path(fname).suffix.lower() or "-")
                    ),
                    size=fsize,
                    date_str=str(entry.get("dt", "-")),
                    caption=str(entry.get("cap", "")),
                    status=status,
                    progress=1.0 if status == "Já no PC ✓" else 0.0,
                    message_obj=None,
                    sha256=str(entry.get("sha256", "") or ""),
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
                f"⚡ {len(self.remote_files)} arquivo(s) carregados instantaneamente do banco SQLite de '{target_label}'. Sincronizando novos em 2º plano..."
            )
        else:
            self._update_dl_pagination_controls()
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

        matching_new: List[RemoteCloudFile] = []
        for rf in new_items:
            if query:
                haystack = f"{rf.filename.lower()} {rf.ext.lower()} {rf.caption.lower()} {rf.date_str.lower()}"
                if query not in haystack:
                    continue
            matching_new.append(rf)

        if matching_new:
            if prepend:
                self.dl_filtered_files = matching_new + self.dl_filtered_files
                if self.dl_page_index == 0:
                    self._render_current_dl_page()
            else:
                prev_len = len(self.dl_filtered_files)
                self.dl_filtered_files.extend(matching_new)
                page_start = self.dl_page_index * self.dl_page_size
                page_end = page_start + self.dl_page_size
                # Insere na Treeview apenas os itens que caem dentro da página atual (máx 500 itens na tela)
                if prev_len < page_end:
                    slice_start = max(0, page_start - prev_len)
                    slice_end = max(0, page_end - prev_len)
                    for rf in matching_new[slice_start:slice_end]:
                        if not self.dl_tree.exists(rf.row_id):
                            self._insert_dl_row(rf)

        self._update_dl_pagination_controls()
        total_count = len(self.remote_files)
        total_bytes = sum(f.size for f in self.remote_files)
        self.cloud_summary_label.configure(
            text=f"🔄 2º plano: {len(self.dl_filtered_files)}/{total_count} arq. ({format_size(total_bytes)})"
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
            self._update_dl_pagination_controls()
            self._update_dl_checked_summary()
            total_count = len(self.remote_files)
            total_sz = sum(f.size for f in self.remote_files)
            if (
                not self.is_uploading
                and not self.is_downloading
                and not self.is_scanning
            ):
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
        query = self.search_var.get().strip().lower()

        matching: List[RemoteCloudFile] = []
        for item in self.remote_files:
            if query:
                haystack = f"{item.filename.lower()} {item.ext.lower()} {item.caption.lower()} {item.date_str.lower()}"
                if query not in haystack:
                    continue
            matching.append(item)

        self.dl_filtered_files = matching
        self.dl_page_index = 0
        self._render_current_dl_page()
        self._update_dl_checked_summary()

    def _total_dl_pages(self) -> int:
        if not self.dl_filtered_files:
            return 1
        return max(
            1, math.ceil(len(self.dl_filtered_files) / self.dl_page_size)
        )

    def _update_dl_pagination_controls(self):
        total_pages = self._total_dl_pages()
        if self.dl_page_index >= total_pages:
            self.dl_page_index = max(0, total_pages - 1)
        cur_page = self.dl_page_index + 1
        if hasattr(self, "dl_page_label"):
            self.dl_page_label.configure(
                text=f"Pág. {cur_page}/{total_pages}"
            )
        if hasattr(self, "dl_prev_page_btn"):
            self.dl_prev_page_btn.configure(
                state="normal" if self.dl_page_index > 0 else "disabled"
            )
        if hasattr(self, "dl_next_page_btn"):
            self.dl_next_page_btn.configure(
                state="normal"
                if self.dl_page_index + 1 < total_pages
                else "disabled"
            )

    def _change_dl_page(self, delta: int):
        total_pages = self._total_dl_pages()
        new_idx = max(0, min(total_pages - 1, self.dl_page_index + delta))
        if new_idx == self.dl_page_index:
            return
        self.dl_page_index = new_idx
        self._render_current_dl_page()
        self._update_dl_checked_summary()

    def _render_current_dl_page(self):
        """Renderiza apenas a página atual (500 linhas) na Treeview para suportar 150.000+ arquivos instantaneamente."""
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)

        self._update_dl_pagination_controls()
        start = self.dl_page_index * self.dl_page_size
        end = min(start + self.dl_page_size, len(self.dl_filtered_files))
        for i in range(start, end):
            rf = self.dl_filtered_files[i]
            if not self.dl_tree.exists(rf.row_id):
                self._insert_dl_row(rf)

    def _insert_dl_row(self, item: RemoteCloudFile):
        chk_str = "☑" if item.checked else "☐"
        icon = "✓" if "✓" in item.status else "☁️"
        pct = "100%" if "✓" in item.status else f"{item.progress * 100:0.0f}%"
        tag = (
            "Marcado"
            if item.checked and item.status == "Disponível na Nuvem"
            else item.status
        )
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

    def _toggle_dl_row_check(
        self, row_id: str, force_val: Optional[bool] = None
    ):
        item = self.remote_by_id.get(row_id)
        if not item:
            return
        item.checked = (not item.checked) if force_val is None else force_val
        if self.dl_tree.exists(row_id):
            chk_str = "☑" if item.checked else "☐"
            self.dl_tree.set(row_id, "check", chk_str)
            tag = (
                "Marcado"
                if item.checked and item.status == "Disponível na Nuvem"
                else item.status
            )
            self.dl_tree.item(row_id, tags=(tag,))

    def _on_dl_tree_click(self, event):
        region = self.dl_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.dl_tree.identify_column(event.x)
        row_id = self.dl_tree.identify_row(event.y)
        if not row_id:
            return
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
        filtered_count = len(self.dl_filtered_files)
        total_sz = sum(f.size for f in self.dl_filtered_files)
        if checked_items:
            self.cloud_summary_label.configure(
                text=f"☑ {len(checked_items)} marcado(s) ({format_size(checked_sz)}) | Total: {filtered_count}/{len(self.remote_files)}"
            )
        else:
            self.cloud_summary_label.configure(
                text=f"{filtered_count} de {len(self.remote_files)} arq. ({format_size(total_sz)})"
            )

    def choose_download_directory(self):
        current_dir = (
            self.dl_dir_entry.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        )
        chosen = filedialog.askdirectory(
            title="Escolha a pasta onde deseja salvar os arquivos baixados",
            initialdir=(
                current_dir
                if Path(current_dir).exists()
                else str(Path.home())
            ),
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
        """
        Exibe diálogo de confirmação explícita com a quantidade total de arquivos selecionados,
        tamanho total e prévia dos nomes antes de executar `delete_messages(..., revoke=True)` no Telegram.
        """
        checked_iids = [f.row_id for f in self.remote_files if f.checked]
        selected_iids = list(
            set(checked_iids) | set(self.dl_tree.selection())
        )
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
        total_del_count = len(items_to_del)
        total_del_bytes = sum(max(it.size, 0) for it in items_to_del)
        preview_names = "\n".join(
            f"  • {it.filename} ({format_size(it.size)})"
            for it in items_to_del[:5]
        )
        if total_del_count > 5:
            preview_names += (
                f"\n  ... e mais {total_del_count - 5} arquivo(s)."
            )

        confirm_msg = (
            f"⚠️ CONFIRMAÇÃO DE EXCLUSÃO PERMANENTE NO TELEGRAM ⚠️\n\n"
            f"• Quantidade total selecionada: {total_del_count} arquivo(s)\n"
            f"• Tamanho total a excluir: {format_size(total_del_bytes)}\n"
            f"• Grupo / Chat no Telegram: {target_label}\n\n"
            f"Arquivos selecionados:\n{preview_names}\n\n"
            f"Esta operação executará a exclusão definitiva (revoke=True) no servidor do Telegram para todos os participantes do grupo.\n\n"
            f"Deseja realmente APAGAR permanentemente estes {total_del_count} arquivo(s)?"
        )
        if not messagebox.askyesno(
            f"Confirmar Exclusão de {total_del_count} Arquivo(s) no Telegram",
            confirm_msg,
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
            self.dl_filtered_files = [
                f
                for f in self.dl_filtered_files
                if f.row_id in self.remote_by_id
            ]
            self.remote_by_filename_lower = {
                f.filename.lower(): f for f in self.remote_files
            }
            self._render_current_dl_page()
            self._update_dl_checked_summary()
            if "download" in self.nav_buttons:
                suffix = (
                    f" ({len(self.remote_files)})" if self.remote_files else ""
                )
                self.nav_buttons["download"].configure(
                    text=f"  DOWNLOAD DO TELEGRAM{suffix}"
                )
            target_entity = self.dialogs_map.get(target_label, "me")
            entity_key = str(target_entity)
            if hasattr(self, "sqlite_cache") and self.sqlite_cache is not None:
                threading.Thread(
                    target=self.sqlite_cache.delete_messages,
                    args=(entity_key, msg_ids),
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

        checked_iids = [f.row_id for f in self.remote_files if f.checked]
        selected_iids = (
            checked_iids if checked_iids else list(self.dl_tree.selection())
        )

        if not selected_iids:
            visible_iids = list(self.dl_tree.get_children())
            if not visible_iids:
                messagebox.showinfo(
                    "Download",
                    "Nenhum arquivo listado para baixar. Selecione um grupo ou clique em '🔄 Escanear Arquivos do Grupo'.",
                )
                return
            if not messagebox.askyesno(
                "Baixar Todos os Listados na Página?",
                f"Você não marcou nenhuma caixinha (☐) na frente dos arquivos.\nDeseja marcar e baixar todos os {len(visible_iids)} arquivo(s) exibidos nesta página agora?",
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
                self.dl_tree.set(
                    item.row_id, "filename", f"{icon} {item.filename}"
                )
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
                "Erro Permanente",
                "Cancelado",
                "Aguardando 1h",
            )
        return True

    def _on_filter_changed(self, value: str):
        self.queue_filter_mode = value
        self._rebuild_queue_tree()

    def _sync_upload_tree_height(self):
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
                else (
                    "Falha Perm."
                    if item.status == "Erro Permanente"
                    else f"{item.progress * 100:0.0f}%"
                )
            )
        )
        tag = (
            "Marcado"
            if item.checked and item.status == "Pendente"
            else item.status
        )
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
        self._start_background_file_ingest(
            file_list, source_label="seleção de arquivos"
        )

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
        self.multi_folder_win.title(
            f"Selecionar Arquivos ou Pastas — {APP_TITLE}"
        )
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
        self.add_files_btn.configure(
            state="disabled", text="⏳ Lendo arquivos..."
        )
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
                        for root, dirs, files in os.walk(
                            folder_path, topdown=True
                        ):
                            dirs[:] = [
                                d
                                for d in dirs
                                if d not in IGNORED_DIRS
                                and not d.startswith(".")
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
                            for entry in sorted(
                                entries, key=lambda e: e.name.lower()
                            ):
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
                    self.log(
                        f"Aviso durante leitura da pasta {folder_path}: {exc}"
                    )

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
        self.add_files_btn.configure(
            state="disabled", text="⏳ Lendo arquivos..."
        )
        self._update_scan_lock_ui(
            "Analisando arquivos selecionados... Aguarde concluir."
        )

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
        check_sent = (
            self.skip_sent_var.get() and not self.ignore_duplicates_var.get()
        )

        for i in range(start_idx, end_idx):
            fpath, fsize = files_with_sizes[i]
            if fpath in self.queued_paths:
                continue

            if fsize > MAX_TELEGRAM_FILE_SIZE:
                ignored_over_2gb_count += 1
                self.log(
                    f"⏭ Ignorado automaticamente (> 2 GB): '{fpath.name}' ({format_size(fsize)}) excede o limite de 2 GB do Telegram."
                )
                continue

            item_id = f"item_{self._next_id_counter}"
            self._next_id_counter += 1

            sig = make_file_signature(fpath, fsize)
            prev_record = (
                self.sent_signatures.get(sig) if check_sent else None
            )

            if prev_record:
                item = FileQueueItem(
                    item_id=item_id,
                    path=fpath,
                    size=fsize,
                    status="Já Enviado",
                    progress=1.0,
                    sent_at=prev_record.get("sent_at", ""),
                    target_chat=prev_record.get("target", ""),
                    sha256=str(prev_record.get("sha256", "") or ""),
                )
                already_sent_count += 1
            else:
                item = FileQueueItem(
                    item_id=item_id, path=fpath, size=fsize
                )

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
        selected_iids = list(
            set(checked_iids) | set(self.tree.selection())
        )
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
            self.queue = [
                i for i in self.queue if i.item_id in self.queue_by_id
            ]
            self._update_queue_summary()
            self._save_queue_state()
            self.log(
                f"🗑 {removed} arquivo(s) ignorado(s) e removido(s) da lista de upload."
            )

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
            if i.status
            in ("Pendente", "Pausado", "Erro", "Cancelado", "Aguardando 1h")
        )
        chk_prefix = f"☑ {checked_count} | " if checked_count > 0 else ""
        self.queue_summary_label.configure(
            text=f"{chk_prefix}{len(self.queue)} arq. ({format_size(total_bytes)}) | Pend: {pending}"
        )
        self._sync_upload_tree_height()

    # -------------------------------------------------------------------------
    # Controles de Execução da Fila de Upload
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
        self._snap_ignore_duplicates = bool(
            self.ignore_duplicates_var.get()
        )
        self._snap_use_caption = False
        self._snap_caption_tpl = "{filename}"
        self._snap_force_doc = bool(self.force_doc_var.get())

        self.is_uploading = True
        self.is_paused = False
        self.cancel_requested = False
        self._save_queue_state()

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
            self._async_process_queue(
                pending_items, target_entity, target_label
            ),
            callback=self._on_queue_finished,
            error_callback=self._on_queue_error,
        )

    def toggle_pause_upload(self):
        if self.is_scanning:
            return
        if not self.is_paused and self.is_uploading:
            self.is_paused = True
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

    def _file_exists_in_download_scan(
        self, item: FileQueueItem
    ) -> Optional[str]:
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
