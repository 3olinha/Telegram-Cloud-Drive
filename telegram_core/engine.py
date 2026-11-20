"""
Motor Telethon (MTProto):
- Conexão e Autenticação (QR Code inline, Telefone + SMS/Telegram e Senha 2FA)
- Pool de conexões paralelas MTProtoSender (4 conexões simultâneas no mesmo Data Center)
- Motor Turbo de Upload Paralelo (blocos de 512 KB com 16 workers assíncronos)
- Varredura incremental em 2º plano com InputMessagesFilterDocument + cache SQLite
- Processador de fila de Upload e Download com verificação de integridade (Tamanho + SHA-256)
  e diferenciação inteligente entre erros permanentes e erros transitórios de rede (10x 10s + 1h)
"""

import asyncio
import hashlib
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from tkinter import messagebox
import customtkinter as ctk
import qrcode

from telethon import TelegramClient, errors, helpers, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.custom import InputSizedFile
from telethon.tl.functions import InitConnectionRequest, InvokeWithLayerRequest
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.functions.upload import (
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import (
    DocumentAttributeFilename,
    InputFileBig,
    InputMessagesFilterDocument,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from storage.paths import (
    APP_VERSION,
    SESSION_PERSONAL,
    DEFAULT_API_ID,
    DEFAULT_API_HASH,
    DEFAULT_DOWNLOAD_DIR,
    MAX_TELEGRAM_FILE_SIZE,
)
from storage.integrity import (
    LARGE_FILE_THRESHOLD,
    compute_fast_file_sha256,
    verify_downloaded_file_integrity,
)
from telegram_core.models_and_errors import (
    FileQueueItem,
    RemoteCloudFile,
    TransferPausedError,
    ItemDeletedFromQueueError,
    classify_transfer_exception,
    format_size,
    format_eta,
    make_file_signature,
)


class TelegramTransferEngineMixin:
    """
    Mixin que encapsula toda a comunicação com a API MTProto (Telethon) e Bot HTTP,
    mantendo a janela principal focada na interface gráfica e orquestração.
    """

    # -------------------------------------------------------------------------
    # Conexão e Login no Telegram (QR Code, Telefone e 2FA)
    # -------------------------------------------------------------------------
    def _get_api_credentials(self) -> Tuple[int, str]:
        try:
            api_id = int(str(self.config_data.get("api_id", DEFAULT_API_ID)).strip())
        except ValueError:
            api_id = DEFAULT_API_ID
        api_hash = (
            str(self.config_data.get("api_hash", DEFAULT_API_HASH)).strip()
            or DEFAULT_API_HASH
        )
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
            if it.status in ("Pendente", "Pausado", "Erro")
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

    # --- Login via QR Code ---
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
        if hasattr(self, "qr_image_label"):
            self.qr_image_label.configure(image=None, text="", height=10)
            self.qr_image_label.image = None
        if hasattr(self, "qr_status_label"):
            self.qr_status_label.configure(
                text="✓ QR Code finalizado. Clique em '📱 Entrar com QR Code' caso queira gerar um novo código.",
                text_color="#94a3b8",
            )

    # --- Login via Telefone ---
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
            for s in list(getattr(self, "_parallel_upload_senders", [])):
                try:
                    await s.disconnect()
                except Exception:
                    pass
            self._parallel_upload_senders.clear()
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
        self.remote_by_filename_lower.clear()
        self.dl_filtered_files.clear()
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)
        self.cloud_summary_label.configure(text="0 arquivos encontrados")
        if hasattr(self, "dl_page_label"):
            self.dl_page_label.configure(text="Página 1 de 1")
        if "download" in self.nav_buttons:
            self.nav_buttons["download"].configure(text="  DOWNLOAD DO TELEGRAM")
        self.log("Sessão desconectada com sucesso.")

    # -------------------------------------------------------------------------
    # Varredura Incremental em 2º Plano + Banco SQLite
    # -------------------------------------------------------------------------
    def _load_group_cache_entry(self, entity_key: str) -> dict:
        if hasattr(self, "sqlite_cache") and self.sqlite_cache is not None:
            return self.sqlite_cache.load_group_entry(str(entity_key))
        return {}

    def _save_group_cache_entry(
        self,
        entity_key: str,
        files_list: List[RemoteCloudFile],
        fully_scanned: bool,
    ):
        try:
            if hasattr(self, "sqlite_cache") and self.sqlite_cache is not None:
                self.sqlite_cache.save_group_entry(
                    str(entity_key), files_list, bool(fully_scanned)
                )
        except Exception:
            pass

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
        sig = f"{filename.lower()}::{size}"
        known_sha = ""
        if hasattr(self, "sent_signatures") and sig in self.sent_signatures:
            known_sha = str(self.sent_signatures[sig].get("sha256", "") or "")

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
            sha256=known_sha,
        )

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
        e leitura incremental apoiada pelo banco local SQLite.
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

        # Fase 2: Se o histórico completo do grupo ainda não foi 100% varrido, continua de onde parou
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
                    if len(older_batch) >= 250:
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

    # -------------------------------------------------------------------------
    # Processador Assíncrono de Downloads (Com Verificação SHA-256 + Erros Inteligentes)
    # -------------------------------------------------------------------------
    async def _async_sleep_interruptible_dl(
        self, seconds: int, status_prefix: str
    ) -> bool:
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

        while (
            current_batch
            and not self.cancel_download_requested
            and not self.is_download_paused
        ):
            deferred_for_1h: List[RemoteCloudFile] = []
            batch_total_bytes = max(sum(max(it.size, 0) for it in current_batch), 1)
            completed_batch_bytes = 0

            for idx, item in enumerate(current_batch, start=1):
                if self.cancel_download_requested:
                    break
                if self.is_download_paused:
                    self._update_dl_item_ui(
                        item, "Pausado", f"{item.progress * 100:0.0f}%"
                    )
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

                max_attempts = 10

                for attempt in range(1, max_attempts + 1):
                    if self.cancel_download_requested or self.is_download_paused:
                        break

                    att_suffix = (
                        f" [Tentativa {attempt}/{max_attempts}]"
                        if attempt > 1
                        else ""
                    )
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
                                self.dl_tree.set(
                                    current_item.row_id, "progress", pct_str
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

                        # Validação de integridade pós-download (Tamanho exato + SHA-256 para arquivos > 10 MB ou com hash conhecido)
                        expected_sha = getattr(item, "sha256", "") or ""
                        if not expected_sha and hasattr(self, "sent_signatures"):
                            sig = f"{item.filename.lower()}::{item.size}"
                            rec = self.sent_signatures.get(sig)
                            if rec:
                                expected_sha = str(rec.get("sha256", "") or "")

                        ok_integrity, integrity_msg = await asyncio.to_thread(
                            verify_downloaded_file_integrity,
                            dest_file,
                            item.size,
                            expected_sha,
                        )
                        if not ok_integrity:
                            try:
                                if dest_file.exists():
                                    dest_file.unlink()
                            except Exception:
                                pass
                            raise RuntimeError(
                                f"Falha de integridade no arquivo baixado: {integrity_msg}"
                            )

                        completed_count += 1
                        completed_batch_bytes += max(item.size, 0)
                        item.progress = 1.0
                        self._update_dl_item_ui(item, "Baixado ✓", "100%")
                        integrity_badge = (
                            f" [{integrity_msg}]"
                            if item.size >= LARGE_FILE_THRESHOLD or expected_sha
                            else ""
                        )
                        self.log(
                            f"✓ Baixado ({completed_count}/{total_count}): {dest_file.name}{integrity_badge}"
                        )
                        break

                    except TransferPausedError:
                        self._update_dl_item_ui(
                            item, "Pausado", f"{item.progress * 100:0.0f}%"
                        )
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
                            fw.seconds,
                            f"Aguardando limite do Telegram ({item.filename})",
                        )
                        if not ok:
                            break
                    except Exception as exc:
                        is_perm, reason = classify_transfer_exception(exc, None)
                        if is_perm:
                            self._update_dl_item_ui(
                                item, "Erro Permanente", "Falha Perm."
                            )
                            self.log(
                                f"❌ Erro permanente ao baixar '{item.filename}' (ignorado sem bloquear fila): {reason}"
                            )
                            break

                        self._update_dl_item_ui(
                            item, "Erro", f"Falha ({attempt}/10)"
                        )
                        self.log(
                            f"⚠ Falha transitória de rede ao baixar '{item.filename}' (tentativa {attempt}/{max_attempts}): {exc}"
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
                            self._update_dl_item_ui(
                                item, "Aguardando 1h", "Adiado 1h"
                            )
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

    # -------------------------------------------------------------------------
    # Processador Assíncrono de Uploads (Motor Turbo Paralelo + SHA-256 + Erros Inteligentes)
    # -------------------------------------------------------------------------
    async def _async_sleep_interruptible_ul(
        self, seconds: int, status_prefix: str
    ) -> bool:
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
                    and it.status not in ("Ignorado (Repetido)", "Erro Permanente")
                    and it.item_id in self.queue_by_id
                    and not getattr(it, "ignored_by_delete", False)
                ),
                1,
            )
            completed_batch_bytes = 0

            for idx, item in enumerate(current_batch, start=1):
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

                if item.size > MAX_TELEGRAM_FILE_SIZE:
                    self._update_item_ui(item, "Ignorado (> 2GB)", "Ignorado")
                    self.log(
                        f"⏭ Ignorado (> 2 GB): '{item.path.name}' ({format_size(item.size)}) excede o limite de 2 GB do Telegram."
                    )
                    self.after(0, self._update_queue_summary)
                    self.after(0, self._save_queue_state)
                    continue

                if item.status in (
                    "Ignorado (Repetido)",
                    "Ignorado (> 2GB)",
                    "Erro Permanente",
                ):
                    continue

                if check_duplicates and cycle_number == 1:
                    existing_info = self._file_exists_in_download_scan(item)
                    if existing_info:
                        if ignore_all_duplicates:
                            self._update_item_ui(
                                item, "Ignorado (Repetido)", "Ignorado"
                            )
                            batch_total_bytes = max(
                                1, batch_total_bytes - max(item.size, 0)
                            )
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
                            batch_total_bytes = max(
                                1, batch_total_bytes - max(item.size, 0)
                            )
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

                    # Pré-validação de existência e leitura do arquivo local antes de tentar upload
                    if not item.path.exists() or not item.path.is_file():
                        item.error_msg = f"Arquivo local não encontrado: {item.path}"
                        self._update_item_ui(item, "Erro Permanente", "Ausente")
                        batch_total_bytes = max(
                            1, batch_total_bytes - max(item.size, 0)
                        )
                        self.log(
                            f"❌ Erro permanente em '{item.path.name}': Arquivo não existe mais no disco ({item.path}). Pulando imediatamente."
                        )
                        self.after(0, self._update_queue_summary)
                        self.after(0, self._save_queue_state)
                        break

                    # Calcula assinatura SHA-256 de verificação para arquivos (> 10 MB ou ainda sem hash)
                    if not item.sha256:
                        try:
                            item.sha256 = await asyncio.to_thread(
                                compute_fast_file_sha256, item.path
                            )
                        except Exception:
                            item.sha256 = ""

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
                        sha_info = (
                            f" [SHA-256: {item.sha256[:12]}...]"
                            if item.sha256 and item.size >= LARGE_FILE_THRESHOLD
                            else ""
                        )
                        self.log(
                            f"✓ Enviado ({completed_count}/{total_count}): {item.path.name}{sha_info}"
                        )
                        self.after(0, self._update_queue_summary)
                        self.after(0, self._save_queue_state)
                        break

                    except ItemDeletedFromQueueError:
                        batch_total_bytes = max(
                            1, batch_total_bytes - max(item.size, 0)
                        )
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
                        is_perm, reason = classify_transfer_exception(exc, item.path)
                        if is_perm:
                            self._update_item_ui(
                                item, "Erro Permanente", "Falha Perm."
                            )
                            batch_total_bytes = max(
                                1, batch_total_bytes - max(item.size, 0)
                            )
                            self.log(
                                f"❌ Erro permanente ao enviar '{item.path.name}' (não será repetido em 1h): {reason}"
                            )
                            self.after(0, self._update_queue_summary)
                            self.after(0, self._save_queue_state)
                            break

                        self._update_item_ui(
                            item, "Erro", f"Falha ({attempt}/10)"
                        )
                        self.log(
                            f"⚠ Falha transitória de rede ao enviar '{item.path.name}' (tentativa {attempt}/{max_attempts}): {exc}"
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
        part_size = 512 * 1024
        part_count = (file_size + part_size - 1) // part_size
        is_big = file_size > 10 * 1024 * 1024
        file_id = helpers.generate_random_long()
        file_name = file_path.name
        hash_md5 = hashlib.md5()

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
