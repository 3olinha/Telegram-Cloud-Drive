import asyncio
import errno
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from telethon import errors

from storage.paths import (  # noqa: F401  (re-exportados para os demais pacotes)
    append_persistent_log,
    format_eta,
    format_size,
    make_file_signature,
)


class TransferPausedError(Exception):
    """Exceção interna usada para interromper o envio/download imediatamente ao clicar em Pausar."""


class ItemDeletedFromQueueError(Exception):
    """Exceção interna disparada quando o usuário exclui um arquivo que estava sendo enviado."""


class PermanentTransferError(Exception):
    """Exceção disparada para erros permanentes que não devem aguardar retentativas de 10s nem ciclo de 1h."""


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
    sha256: str = ""


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
    sha256: str = ""


PERMANENT_TELETHON_ERROR_NAMES = {
    "ChatWriteForbiddenError",
    "UserBannedInChannelError",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "ChatRestrictedError",
    "PeerIdInvalidError",
    "FilePartsInvalidError",
    "FilePartSizeInvalidError",
    "AuthKeyUnregisteredError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "MediaInvalidError",
    "PhotoInvalidDimensionsError",
}


def classify_transfer_exception(
    exc: BaseException, local_file_path: Optional[Path] = None
) -> Tuple[bool, str]:
    """
    Diferencia erros transitórios de rede (que devem usar a política 10x 10s + 1h)
    de erros permanentes (arquivo inexistente, sem permissão no disco ou no grupo do Telegram).
    Retorna (is_permanent: bool, friendly_reason: str).
    """
    if isinstance(exc, PermanentTransferError):
        return True, str(exc)

    if local_file_path is not None and not local_file_path.exists():
        return True, f"Arquivo não existe mais no disco: {local_file_path.name}"

    if isinstance(exc, FileNotFoundError):
        return True, "Arquivo local não encontrado no disco."

    if isinstance(exc, PermissionError):
        return True, "Acesso negado ao arquivo/pasta no Windows (PermissionError)."

    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (
        errno.EACCES,
        errno.ENOENT,
        errno.ENOSPC,
    ):
        if exc.errno == errno.ENOSPC:
            return True, "Espaço em disco insuficiente no computador."
        if exc.errno == errno.ENOENT:
            return True, "Caminho ou arquivo inexistente no disco."
        return True, f"Erro permanente de sistema de arquivos: {exc}"

    exc_cls_name = type(exc).__name__
    if exc_cls_name in PERMANENT_TELETHON_ERROR_NAMES:
        return (
            True,
            f"Sem permissão no grupo/canal do Telegram ou arquivo rejeitado ({exc_cls_name}): {exc}",
        )

    if isinstance(exc, errors.BadRequestError) and exc_cls_name not in (
        "FileReferenceExpiredError",
        "FileMigrateError",
    ):
        return True, f"Requisição recusada pelo Telegram ({exc_cls_name}): {exc}"

    msg_low = str(exc).lower()
    if "limita arquivos a 50 mb" in msg_low or "excede o limite de 2 gb" in msg_low:
        return True, str(exc)

    return False, str(exc)


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
