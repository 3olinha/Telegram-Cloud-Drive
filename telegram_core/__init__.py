"""
Pacote central de integração com a API MTProto do Telegram (Telethon):
- Modelos de fila e arquivos remotos
- Classificador inteligente de erros transitórios vs. erros permanentes
- Worker assíncrono dedicado (AsyncWorker)
- Motor Turbo de upload paralelo (MTProtoSender + chunking 512 KB), varredura incremental e download manager
"""

from telegram_core.models_and_errors import (
    AsyncWorker,
    FileQueueItem,
    RemoteCloudFile,
    TransferPausedError,
    ItemDeletedFromQueueError,
    PermanentTransferError,
    classify_transfer_exception,
    format_size,
    format_eta,
    make_file_signature,
)
from telegram_core.engine import TelegramTransferEngineMixin

__all__ = [
    "AsyncWorker",
    "FileQueueItem",
    "RemoteCloudFile",
    "TransferPausedError",
    "ItemDeletedFromQueueError",
    "PermanentTransferError",
    "classify_transfer_exception",
    "format_size",
    "format_eta",
    "make_file_signature",
    "TelegramTransferEngineMixin",
]
