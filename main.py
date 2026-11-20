"""
Telegram Cloud Drive — Ponto de Entrada e Orquestrador Leve.

Arquitetura modularizada em pacotes:
- storage/       : Persistência local, fingerprinting de máquina, integridade SHA-256 e cache SQLite (FTS5).
- telegram_core/ : Cliente Telethon (MTProto), login (QR Code / Telefone / 2FA), motor Turbo de upload paralelo
                   (MTProtoSender + 512 KB chunking), varredura incremental e classificador de erros.
- updater/       : Autoatualização via GitHub Releases, limpeza de variáveis _MEI* do PyInstaller e desinstalador.
- ui/            : Componentes CustomTkinter, Treeview com paginação virtual (150.000+ arquivos) e barras de progresso.
"""

import faulthandler
import sys
import traceback
import customtkinter as ctk

# Re-exporta os módulos principais para descoberta automática do PyInstaller (--onefile)
from storage.paths import (
    APP_NAME,
    APP_VERSION,
    APP_TITLE,
    APP_SUBTITLE,
    GITHUB_REPO,
    GITHUB_RELEASES_API,
    UNINSTALL_REG_KEY,
    MAX_TELEGRAM_FILE_SIZE,
    CRASH_LOG_FILE,
    append_persistent_log,
)
from storage.integrity import (
    compute_fast_file_sha256,
    verify_downloaded_file_integrity,
)
from storage.sqlite_cache import GroupCloudSQLiteCache
from telegram_core import (
    AsyncWorker,
    FileQueueItem,
    RemoteCloudFile,
    TransferPausedError,
    ItemDeletedFromQueueError,
    PermanentTransferError,
    classify_transfer_exception,
    TelegramTransferEngineMixin,
)
from updater import (
    GitHubUpdaterMixin,
    run_uninstaller_flow,
)
from ui import (
    LabeledProgressBar,
    TelegramUploaderApp,
)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")
# Obs.: o faulthandler (crash_trace.log) já é habilitado em storage.paths na importação.


def main():
    """Ponto de entrada principal da aplicação e do fluxo de desinstalação (--uninstall)."""
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


if __name__ == "__main__":
    main()
