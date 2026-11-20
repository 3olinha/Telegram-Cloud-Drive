import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import messagebox

APP_VERSION = "1.0.0"
APP_NAME = "Telegram Cloud Drive"
INSTALL_FOLDER_NAME = "TelegramCloudDrive"
UNINSTALL_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TelegramCloudDrive"


def get_bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).parent.resolve()


def get_install_dir() -> Path:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    return local_app_data / "Programs" / INSTALL_FOLDER_NAME


def unblock_windows_file(path: Path):
    """Remove o fluxo NTFS Zone.Identifier em Python puro."""
    try:
        os.unlink(f"{path}:Zone.Identifier")
    except Exception:
        pass


def get_clean_pyinstaller_env() -> dict:
    """
    Remove variáveis de ambiente temporárias do PyInstaller (_MEIPASS2, _PYI_*, etc.)
    para que o aplicativo instalado inicie com seu próprio ambiente limpo sem erro de python312.dll.
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


def register_windows_uninstaller(target_exe: Path, icon_file: Path, install_dir: Path):
    """
    Registra o programa oficialmente no Painel de Controle do Windows (Programas e Recursos)
    e em Configurações > Aplicativos > Aplicativos Instalados (Windows 10 e 11).
    """
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "By Bolinha")
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(icon_file))
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
            uninstall_cmd = f'"{target_exe}" --uninstall'
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_cmd)
            winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ, uninstall_cmd)
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, 24500)
    except Exception:
        pass


def create_windows_shortcuts(target_exe: Path, icon_file: Path, work_dir: Path):
    """Cria os atalhos na Área de Trabalho e no Menu Iniciar (incluindo atalho de Desinstalar) via WScript.Shell."""
    temp_dir = Path(os.environ.get("TEMP", str(work_dir)))
    vbs_file = temp_dir / "_create_tcd_shortcuts.vbs"
    vbs_content = f'''
On Error Resume Next
Set WshShell = CreateObject("WScript.Shell")

DesktopPath = WshShell.SpecialFolders("Desktop")
Set Shortcut = WshShell.CreateShortcut(DesktopPath & "\\{APP_NAME}.lnk")
Shortcut.TargetPath = "{target_exe}"
Shortcut.WorkingDirectory = "{work_dir}"
Shortcut.IconLocation = "{icon_file},0"
Shortcut.Description = "{APP_NAME} v{APP_VERSION} - Gerenciador de Upload e Download para Telegram (By Bolinha)"
Shortcut.Save

ProgramsPath = WshShell.SpecialFolders("Programs")
Set StartShortcut = WshShell.CreateShortcut(ProgramsPath & "\\{APP_NAME}.lnk")
StartShortcut.TargetPath = "{target_exe}"
StartShortcut.WorkingDirectory = "{work_dir}"
StartShortcut.IconLocation = "{icon_file},0"
StartShortcut.Description = "{APP_NAME} v{APP_VERSION} - Gerenciador de Upload e Download para Telegram (By Bolinha)"
StartShortcut.Save

Set UninstShortcut = WshShell.CreateShortcut(ProgramsPath & "\\Desinstalar {APP_NAME}.lnk")
UninstShortcut.TargetPath = "{target_exe}"
UninstShortcut.Arguments = "--uninstall"
UninstShortcut.WorkingDirectory = "{work_dir}"
UninstShortcut.IconLocation = "{icon_file},0"
UninstShortcut.Description = "Desinstalar {APP_NAME} e remover dados de login do Telegram"
UninstShortcut.Save
'''
    try:
        vbs_file.write_text(vbs_content.strip(), encoding="mbcs", errors="replace")
        subprocess.run(
            ["cscript.exe", "//Nologo", "//B", str(vbs_file)],
            creationflags=0x08000000,
            timeout=15,
        )
    except Exception:
        pass
    finally:
        try:
            if vbs_file.exists():
                vbs_file.unlink()
        except Exception:
            pass


class InstallerWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Instalador — {APP_NAME} v{APP_VERSION}")
        self.geometry("520x270")
        self.resizable(False, False)
        self.configure(bg="#0f172a")

        bundle_dir = get_bundle_dir()
        ico_path = bundle_dir / "telegram_icon.ico"
        if ico_path.exists():
            try:
                self.iconbitmap(str(ico_path))
            except Exception:
                pass

        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw // 2) - 260
        y = (sh // 2) - 135
        self.geometry(f"520x270+{max(x, 20)}+{max(y, 20)}")
        self.attributes("-topmost", True)

        header = tk.Frame(self, bg="#1e3a8a", height=68)
        header.pack(fill="x", padx=14, pady=(14, 10))

        tk.Label(
            header,
            text=f"🚀 Instalando {APP_NAME} v{APP_VERSION}",
            font=("Segoe UI", 15, "bold"),
            fg="#ffffff",
            bg="#1e3a8a",
        ).pack(pady=(10, 2))

        tk.Label(
            header,
            text="Gerenciador de Upload e Download de arquivos para Telegram",
            font=("Segoe UI", 10),
            fg="#bfdbfe",
            bg="#1e3a8a",
        ).pack(pady=(0, 10))

        self.status_label = tk.Label(
            self,
            text="Preparando arquivos de instalação...",
            font=("Segoe UI", 11, "bold"),
            fg="#38bdf8",
            bg="#0f172a",
        )
        self.status_label.pack(pady=(8, 8))

        self.bar_canvas = tk.Canvas(
            self,
            height=26,
            bg="#1e293b",
            highlightthickness=1,
            highlightbackground="#334155",
            bd=0,
        )
        self.bar_canvas.pack(fill="x", padx=24, pady=(0, 14))
        self._progress = 0.0
        self.bar_canvas.bind("<Configure>", lambda _e: self._draw_progress())

        tk.Label(
            self,
            text="By Bolinha",
            font=("Segoe UI", 10, "bold"),
            fg="#64748b",
            bg="#0f172a",
        ).pack(side="bottom", pady=(0, 10))

        self.after(250, self._start_install_thread)

    def _draw_progress(self):
        self.bar_canvas.delete("all")
        w = max(self.bar_canvas.winfo_width(), 10)
        h = max(self.bar_canvas.winfo_height(), 26)
        fill_w = int(w * self._progress)
        if fill_w > 0:
            self.bar_canvas.create_rectangle(
                0, 0, fill_w, h, fill="#16a34a", width=0
            )
        pct_txt = f"{self._progress * 100:.0f}%"
        self.bar_canvas.create_text(
            w // 2, h // 2, text=pct_txt, fill="#ffffff", font=("Segoe UI", 10, "bold")
        )

    def set_progress(self, frac: float, status_text: str):
        self._progress = max(0.0, min(1.0, frac))
        self.status_label.configure(text=status_text)
        self._draw_progress()
        self.update_idletasks()

    def _start_install_thread(self):
        threading.Thread(target=self._run_installation, daemon=True).start()

    def _run_installation(self):
        try:
            bundle_dir = get_bundle_dir()
            install_dir = get_install_dir()

            self.after(
                0,
                lambda: self.set_progress(
                    0.15, "Criando diretório do programa no Windows..."
                ),
            )
            install_dir.mkdir(parents=True, exist_ok=True)

            payload_exe = bundle_dir / "payload_app.exe"
            if not payload_exe.exists():
                candidates = list(bundle_dir.glob("Telegram_Cloud_Drive*.exe"))
                if candidates:
                    payload_exe = candidates[0]
                else:
                    raise FileNotFoundError(
                        "Arquivo principal do aplicativo não encontrado no pacote do instalador."
                    )

            target_exe = install_dir / "Telegram_Cloud_Drive.exe"
            target_ico = install_dir / "telegram_icon.ico"
            target_png = install_dir / "telegram_icon.png"

            self.after(
                0,
                lambda: self.set_progress(
                    0.35, "Instalando executável principal no computador..."
                ),
            )

            total_sz = payload_exe.stat().st_size
            copied = 0
            with open(payload_exe, "rb") as src_f, open(target_exe, "wb") as dst_f:
                while True:
                    buf = src_f.read(256 * 1024)
                    if not buf:
                        break
                    dst_f.write(buf)
                    copied += len(buf)
                    frac = 0.35 + 0.40 * (copied / max(total_sz, 1))
                    self.after(
                        0,
                        lambda f=frac: self.set_progress(
                            f, "Copiando arquivos do programa..."
                        ),
                    )

            unblock_windows_file(target_exe)

            for asset_name, dst_path in (
                ("telegram_icon.ico", target_ico),
                ("telegram_icon.png", target_png),
            ):
                src_asset = bundle_dir / asset_name
                if src_asset.exists():
                    shutil.copy2(src_asset, dst_path)

            self.after(
                0,
                lambda: self.set_progress(
                    0.85, "Registrando no Windows e criando ícone na Área de Trabalho..."
                ),
            )
            register_windows_uninstaller(target_exe, target_ico, install_dir)
            create_windows_shortcuts(target_exe, target_ico, install_dir)

            self.after(
                0,
                lambda: self.set_progress(
                    1.0,
                    "✓ Instalação Concluída! Ícone criado na Área de Trabalho.",
                ),
            )
            time.sleep(0.9)

            # Abre o programa instalado com ambiente limpo (sem herdar _MEIPASS do instalador)
            subprocess.Popen(
                [str(target_exe)],
                cwd=str(install_dir),
                env=get_clean_pyinstaller_env(),
            )

            self.after(200, self.destroy)

        except Exception as exc:
            self.after(
                0,
                lambda e=exc: (
                    messagebox.showerror(
                        "Erro na Instalação",
                        f"Não foi possível concluir a instalação:\n{e}",
                    ),
                    self.destroy(),
                ),
            )


if __name__ == "__main__":
    app = InstallerWindow()
    app.mainloop()
