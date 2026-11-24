"""
Gerenciador de autoatualização via GitHub Releases, limpeza de variáveis _MEI* do PyInstaller
e desinstalador oficial com revogação de sessão no Telegram.
"""

import asyncio
import faulthandler
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
from telethon import TelegramClient

from storage.paths import (
    APP_NAME,
    APP_VERSION,
    APP_TITLE,
    GITHUB_RELEASES_API,
    UNINSTALL_REG_KEY,
    EXE_DIR,
    USER_DATA_DIR,
    SESSION_PERSONAL,
    DEFAULT_API_ID,
    DEFAULT_API_HASH,
    parse_version_tuple,
    unblock_windows_file,
    get_clean_pyinstaller_env,
    append_persistent_log,
)
from telegram_core.models_and_errors import format_size


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

    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        import storage.paths as _sp

        if getattr(_sp, "_crash_fh", None):
            _sp._crash_fh.close()
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


class GitHubUpdaterMixin:
    """Mixin com a lógica de verificação, download e aplicação de atualizações via GitHub Releases."""

    def start_periodic_update_checks(self, interval_ms: int = 1800000):
        """
        Agenda verificações periódicas ultraleves de atualização em segundo plano (padrão: a cada 30 minutos).
        Usa uma única requisição HTTP pequena em thread separada — zero impacto em uploads/downloads.
        """
        def _tick():
            try:
                if self.winfo_exists():
                    self.check_for_updates(
                        silent_if_up_to_date=True, background_toast=True
                    )
                    self.after(interval_ms, _tick)
            except Exception:
                pass

        self.after(interval_ms, _tick)

    def on_update_button_clicked(self):
        """Ação do botão '🔄 Atualizar' no topo: para o alerta visual e abre o modal de atualização."""
        self._stop_update_btn_pulse()
        self._dismiss_update_toast()
        pending = getattr(self, "_pending_update_info", None)
        if pending:
            remote_ver, release_title, release_notes, exe_url, exe_size = pending
            self._show_update_available_dialog(
                remote_ver, release_title, release_notes, exe_url, exe_size
            )
        else:
            self.check_for_updates(silent_if_up_to_date=False, background_toast=False)

    def _start_update_btn_pulse(self):
        """Faz o botão '🔄 Atualizar' no topo piscar de forma suave até o usuário clicar nele."""
        if getattr(self, "_update_pulse_active", False):
            return
        self._update_pulse_active = True
        self._update_pulse_step = 0

        def _pulse():
            if not getattr(self, "_update_pulse_active", False):
                return
            try:
                if not self.winfo_exists() or not hasattr(self, "update_top_btn"):
                    self._update_pulse_active = False
                    return
                self._update_pulse_step = (self._update_pulse_step + 1) % 2
                if self._update_pulse_step == 1:
                    self.update_top_btn.configure(
                        fg_color="#10b981",
                        border_width=2,
                        border_color="#fde047",
                        text="🔔 Nova Versão!",
                    )
                else:
                    self.update_top_btn.configure(
                        fg_color="#047857",
                        border_width=2,
                        border_color="#86efac",
                        text="🔄 Atualizar",
                    )
                self.after(750, _pulse)
            except Exception:
                self._update_pulse_active = False

        _pulse()

    def _stop_update_btn_pulse(self):
        """Interrompe o pulso suave do botão Atualizar e restaura o visual padrão."""
        self._update_pulse_active = False
        try:
            if hasattr(self, "update_top_btn") and self.update_top_btn.winfo_exists():
                self.update_top_btn.configure(
                    fg_color="#059669",
                    border_width=0,
                    text="🔄 Atualizar",
                )
        except Exception:
            pass

    def _dismiss_update_toast(self):
        """Fecha o balão flutuante de aviso de nova versão, se estiver visível."""
        toast = getattr(self, "_update_toast_frame", None)
        if toast is not None:
            try:
                if toast.winfo_exists():
                    toast.place_forget()
                    toast.destroy()
            except Exception:
                pass
            self._update_toast_frame = None

    def _show_update_toast_banner(self, remote_ver: str):
        """
        Exibe um balão flutuante discreto no canto superior direito avisando sobre a nova versão.
        Desaparece automaticamente após 30 segundos sem interromper uploads/downloads.
        """
        self._dismiss_update_toast()
        try:
            toast = ctk.CTkFrame(
                self,
                fg_color="#064e3b",
                border_width=2,
                border_color="#34d399",
                corner_radius=10,
            )
            self._update_toast_frame = toast

            ctk.CTkLabel(
                toast,
                text=f"🚀 Nova versão disponível: v{remote_ver}!",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#fef08a",
                anchor="w",
            ).grid(row=0, column=0, padx=(12, 8), pady=(8, 2), sticky="w")

            ctk.CTkButton(
                toast,
                text="✕",
                width=24,
                height=24,
                fg_color="transparent",
                hover_color="#065f46",
                text_color="#d1fae5",
                font=ctk.CTkFont(size=12, weight="bold"),
                command=self._dismiss_update_toast,
            ).grid(row=0, column=1, padx=(0, 8), pady=(6, 0), sticky="ne")

            ctk.CTkLabel(
                toast,
                text=(
                    "Clique no botão '🔄 Atualizar' no topo quando quiser atualizar.\n"
                    "(Este aviso desaparecerá automaticamente em 30 segundos.)"
                ),
                font=ctk.CTkFont(size=11),
                text_color="#ecfdf5",
                justify="left",
                anchor="w",
            ).grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 8), sticky="w")

            toast.place(relx=0.985, y=58, anchor="ne")
            toast.lift()

            # Some automaticamente após 30 segundos
            self.after(30000, self._dismiss_update_toast)
        except Exception:
            pass

    def check_for_updates(
        self, silent_if_up_to_date: bool = True, background_toast: bool = False
    ):
        """
        Consulta a última Release publicada no GitHub (3olinha/Telegram-Cloud-Drive).
        - Se `background_toast=True` (durante o uso): exibe balão de 30s + botão Atualizar piscando suave.
        - Se `background_toast=False` (manual ou inicialização): abre o modal perguntando se deseja atualizar.
        """
        if getattr(self, "_is_checking_updates", False):
            return
        self._is_checking_updates = True

        def _worker():
            try:
                req = urllib.request.Request(
                    GITHUB_RELEASES_API,
                    headers={
                        "User-Agent": f"TelegramCloudDrive/{APP_VERSION}",
                        "Accept": "application/vnd.github+json",
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
                    self._pending_update_info = (
                        remote_ver,
                        release_title,
                        release_notes,
                        exe_url,
                        exe_size,
                    )
                    last_notified = getattr(self, "_last_toast_notified_ver", "")
                    if background_toast:
                        if last_notified != remote_ver:
                            self._last_toast_notified_ver = remote_ver
                            self.log(
                                f"🚀 Nova atualização detectada em segundo plano: v{remote_ver} (atual: v{APP_VERSION})."
                            )
                            self.after(0, lambda v=remote_ver: self._show_update_toast_banner(v))
                        self.after(0, self._start_update_btn_pulse)
                    else:
                        self.log(
                            f"🚀 Nova atualização detectada no GitHub: v{remote_ver} (atual: v{APP_VERSION})."
                        )
                        self.after(0, self._start_update_btn_pulse)
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
        from ui.widgets import LabeledProgressBar

        if (
            hasattr(self, "welcome_win")
            and self.welcome_win is not None
            and self.welcome_win.winfo_exists()
        ):
            self.after(
                1500,
                lambda: self._show_update_available_dialog(
                    remote_ver, release_title, release_notes, download_url, asset_size
                ),
            )
            return

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
        prog_bar,
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
