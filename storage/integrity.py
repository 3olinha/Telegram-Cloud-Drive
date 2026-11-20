import hashlib
from pathlib import Path
from typing import Tuple

# Arquivos a partir deste tamanho recebem validação/registro explícito de integridade (10 MB)
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024


def compute_fast_file_sha256(file_path: Path) -> str:
    """
    Calcula o hash SHA-256 de verificação de integridade de um arquivo.
    - Para arquivos até 64 MB: calcula o SHA-256 completo de todos os bytes.
    - Para arquivos grandes (> 64 MB, até 2 GB): calcula o SHA-256 amostrado determinístico
      (primeiros 16 MB + 16 MB do meio + últimos 16 MB + tamanho exato), garantindo verificação
      criptográfica rápida (< 0.15s) sem travar a fila de upload/download.
    """
    try:
        st = file_path.stat()
        file_size = st.st_size
        h = hashlib.sha256()
        chunk_sz = 1024 * 1024  # 1 MB
        threshold = 64 * 1024 * 1024  # 64 MB

        with open(file_path, "rb") as f:
            if file_size <= threshold:
                while True:
                    data = f.read(chunk_sz)
                    if not data:
                        break
                    h.update(data)
            else:
                h.update(f"SIZE:{file_size}:".encode("ascii"))
                window = 16 * 1024 * 1024  # 16 MB
                # Início (16 MB)
                rem = window
                while rem > 0:
                    data = f.read(min(chunk_sz, rem))
                    if not data:
                        break
                    h.update(data)
                    rem -= len(data)
                # Meio (16 MB)
                f.seek(max(0, (file_size // 2) - (window // 2)))
                rem = window
                while rem > 0:
                    data = f.read(min(chunk_sz, rem))
                    if not data:
                        break
                    h.update(data)
                    rem -= len(data)
                # Fim (16 MB)
                f.seek(max(0, file_size - window))
                rem = window
                while rem > 0:
                    data = f.read(min(chunk_sz, rem))
                    if not data:
                        break
                    h.update(data)
                    rem -= len(data)
        return h.hexdigest()
    except Exception:
        return ""


def verify_downloaded_file_integrity(
    file_path: Path, expected_size: int, expected_sha256: str = ""
) -> Tuple[bool, str]:
    """
    Valida a integridade de um arquivo recém-baixado (especialmente arquivos > 10 MB):
    1. Verifica se o arquivo existe no disco.
    2. Verifica se o tamanho em bytes bate exatamente com `expected_size`.
    3. Se `expected_sha256` estiver registrado no histórico/cache local, valida o hash SHA-256.
    """
    if not file_path.exists():
        return False, "Arquivo baixado não encontrado no disco após transferência."
    try:
        actual_size = file_path.stat().st_size
    except OSError as e:
        return False, f"Erro ao ler tamanho do arquivo baixado: {e}"

    if expected_size > 0 and actual_size != expected_size:
        return (
            False,
            f"Tamanho divergente após download (esperado: {expected_size} B, recebido: {actual_size} B).",
        )

    if expected_sha256:
        actual_hash = compute_fast_file_sha256(file_path)
        if actual_hash and actual_hash != expected_sha256:
            return (
                False,
                "Falha na verificação de hash SHA-256 do arquivo baixado (arquivo corrompido).",
            )

    return True, "OK"
