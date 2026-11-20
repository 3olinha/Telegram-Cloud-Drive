import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class GroupCloudSQLiteCache:
    """
    Banco de dados local SQLite com suporte a busca rápida (FTS5 + índices B-Tree)
    para armazenar e paginar catálogos de grupos com 150.000+ arquivos sem alto consumo de RAM.
    Inclui migração automática e transparente a partir do antigo `group_cloud_cache.json`.
    """

    def __init__(self, db_path: Path, legacy_json_path: Optional[Path] = None):
        self.db_path = db_path
        self.legacy_json_path = legacy_json_path
        self._lock = threading.Lock()
        self._has_fts5 = False
        self._init_schema()
        if self.legacy_json_path and self.legacy_json_path.exists():
            self._migrate_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS group_scan_state (
                        entity_key TEXT PRIMARY KEY,
                        min_id INTEGER DEFAULT 0,
                        max_id INTEGER DEFAULT 0,
                        fully_scanned INTEGER DEFAULT 0,
                        total_count INTEGER DEFAULT 0,
                        total_bytes INTEGER DEFAULT 0,
                        updated_at TEXT DEFAULT ''
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_files (
                        entity_key TEXT NOT NULL,
                        message_id INTEGER NOT NULL,
                        filename TEXT NOT NULL,
                        filename_lower TEXT NOT NULL,
                        ext TEXT DEFAULT '-',
                        size INTEGER DEFAULT 0,
                        date_str TEXT DEFAULT '-',
                        caption TEXT DEFAULT '',
                        sha256 TEXT DEFAULT '',
                        PRIMARY KEY (entity_key, message_id)
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cloud_files_entity_msg
                    ON cloud_files (entity_key, message_id DESC);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cloud_files_entity_fname
                    ON cloud_files (entity_key, filename_lower);
                    """
                )
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS cloud_files_fts USING fts5(
                            entity_key UNINDEXED,
                            message_id UNINDEXED,
                            search_text
                        );
                        """
                    )
                    self._has_fts5 = True
                except Exception:
                    self._has_fts5 = False
                conn.commit()
            finally:
                conn.close()

    def _migrate_legacy_json(self):
        """Migra automaticamente os dados do antigo `group_cloud_cache.json` para o SQLite."""
        try:
            raw_txt = self.legacy_json_path.read_text(encoding="utf-8")
            all_cache = json.loads(raw_txt)
            if not isinstance(all_cache, dict):
                return
            for entity_key, entry in all_cache.items():
                if not isinstance(entry, dict):
                    continue
                files_raw = entry.get("files") or []
                fully_scanned = bool(entry.get("fully_scanned", False))
                rows = []
                for f in files_raw:
                    mid = int(f.get("id", 0) or 0)
                    if not mid:
                        continue
                    fname = str(f.get("fn", f"arquivo_{mid}"))
                    rows.append(
                        {
                            "id": mid,
                            "fn": fname,
                            "ext": str(f.get("ext", Path(fname).suffix.lower() or "-")),
                            "sz": int(f.get("sz", 0) or 0),
                            "dt": str(f.get("dt", "-")),
                            "cap": str(f.get("cap", "")),
                            "sha256": str(f.get("sha256", "")),
                        }
                    )
                if rows:
                    self.upsert_files_batch(
                        str(entity_key), rows, mark_fully_scanned=fully_scanned
                    )
            migrated_path = self.legacy_json_path.with_suffix(".json.migrated")
            try:
                if migrated_path.exists():
                    migrated_path.unlink()
                self.legacy_json_path.replace(migrated_path)
            except Exception:
                pass
        except Exception:
            pass

    def get_group_state(self, entity_key: str) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT min_id, max_id, fully_scanned, total_count, total_bytes
                    FROM group_scan_state
                    WHERE entity_key = ?
                    """,
                    (str(entity_key),),
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "min_id": 0,
                        "max_id": 0,
                        "fully_scanned": False,
                        "total_count": 0,
                        "total_bytes": 0,
                    }
                return {
                    "min_id": int(row[0] or 0),
                    "max_id": int(row[1] or 0),
                    "fully_scanned": bool(row[2]),
                    "total_count": int(row[3] or 0),
                    "total_bytes": int(row[4] or 0),
                }
            finally:
                conn.close()

    def load_all_files_for_group(self, entity_key: str) -> List[dict]:
        """Carrega os metadados de todos os arquivos do grupo ordenados do mais recente para o mais antigo."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT message_id, filename, ext, size, date_str, caption, sha256
                    FROM cloud_files
                    WHERE entity_key = ?
                    ORDER BY message_id DESC
                    """,
                    (str(entity_key),),
                )
                return [
                    {
                        "id": int(r[0]),
                        "fn": str(r[1]),
                        "ext": str(r[2]),
                        "sz": int(r[3] or 0),
                        "dt": str(r[4]),
                        "cap": str(r[5]),
                        "sha256": str(r[6] or ""),
                    }
                    for r in cur.fetchall()
                ]
            finally:
                conn.close()

    def upsert_files_batch(
        self,
        entity_key: str,
        files_dicts: List[dict],
        mark_fully_scanned: Optional[bool] = None,
    ):
        """Insere ou atualiza um lote de arquivos no SQLite em uma única transação rápida."""
        ek = str(entity_key)
        with self._lock:
            conn = self._connect()
            try:
                if files_dicts:
                    params = [
                        (
                            ek,
                            int(f["id"]),
                            str(f["fn"]),
                            str(f["fn"]).lower(),
                            str(f.get("ext", "-")),
                            int(f.get("sz", 0) or 0),
                            str(f.get("dt", "-")),
                            str(f.get("cap", "")),
                            str(f.get("sha256", "")),
                        )
                        for f in files_dicts
                        if f.get("id")
                    ]
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO cloud_files
                        (entity_key, message_id, filename, filename_lower, ext, size, date_str, caption, sha256)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        params,
                    )

                cur = conn.execute(
                    """
                    SELECT COALESCE(MIN(message_id), 0),
                           COALESCE(MAX(message_id), 0),
                           COUNT(*),
                           COALESCE(SUM(size), 0)
                    FROM cloud_files
                    WHERE entity_key = ?
                    """,
                    (ek,),
                )
                min_id, max_id, cnt, tot_bytes = cur.fetchone()

                prev_cur = conn.execute(
                    "SELECT fully_scanned FROM group_scan_state WHERE entity_key = ?",
                    (ek,),
                )
                prev_row = prev_cur.fetchone()
                prev_full = bool(prev_row[0]) if prev_row else False
                final_full = (
                    prev_full
                    if mark_fully_scanned is None
                    else bool(mark_fully_scanned)
                )

                conn.execute(
                    """
                    INSERT OR REPLACE INTO group_scan_state
                    (entity_key, min_id, max_id, fully_scanned, total_count, total_bytes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ek,
                        int(min_id or 0),
                        int(max_id or 0),
                        1 if final_full else 0,
                        int(cnt or 0),
                        int(tot_bytes or 0),
                        time.strftime("%d/%m/%Y %H:%M:%S"),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def find_file_by_name(self, entity_key: str, filename_lower: str) -> Optional[dict]:
        """Busca O(log N) via índice SQLite se um arquivo já existe no grupo."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT message_id, filename, size, date_str, sha256
                    FROM cloud_files
                    WHERE entity_key = ? AND filename_lower = ?
                    LIMIT 1
                    """,
                    (str(entity_key), filename_lower.lower()),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "id": int(r[0]),
                    "fn": str(r[1]),
                    "sz": int(r[2] or 0),
                    "dt": str(r[3]),
                    "sha256": str(r[4] or ""),
                }
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # API de alto nível usada pela UI / motor Telethon
    # ------------------------------------------------------------------
    def load_group_entry(self, entity_key: str) -> dict:
        """Retorna {min_id, max_id, fully_scanned, files:[...]} no mesmo formato do antigo cache JSON."""
        state = self.get_group_state(entity_key)
        state["files"] = self.load_all_files_for_group(entity_key)
        return state

    def save_group_entry(self, entity_key: str, files_list, fully_scanned: bool):
        """Persiste uma lista de RemoteCloudFile (upsert incremental, sem reescrever o banco inteiro)."""
        rows = [
            {
                "id": int(f.message_id),
                "fn": f.filename,
                "ext": f.ext,
                "sz": int(f.size or 0),
                "dt": f.date_str,
                "cap": f.caption,
                "sha256": getattr(f, "sha256", "") or "",
            }
            for f in files_list
            if getattr(f, "message_id", 0)
        ]
        self.upsert_files_batch(
            str(entity_key), rows, mark_fully_scanned=bool(fully_scanned)
        )

    def delete_messages(self, entity_key: str, msg_ids: List[int]):
        self.delete_files_by_message_ids(entity_key, msg_ids)

    def delete_files_by_message_ids(self, entity_key: str, msg_ids: List[int]):
        if not msg_ids:
            return
        ek = str(entity_key)
        with self._lock:
            conn = self._connect()
            try:
                for i in range(0, len(msg_ids), 200):
                    batch = msg_ids[i : i + 200]
                    placeholders = ",".join("?" for _ in batch)
                    conn.execute(
                        f"DELETE FROM cloud_files WHERE entity_key = ? AND message_id IN ({placeholders})",
                        [ek] + [int(m) for m in batch],
                    )
                cur = conn.execute(
                    """
                    SELECT COALESCE(MIN(message_id), 0),
                           COALESCE(MAX(message_id), 0),
                           COUNT(*),
                           COALESCE(SUM(size), 0)
                    FROM cloud_files
                    WHERE entity_key = ?
                    """,
                    (ek,),
                )
                min_id, max_id, cnt, tot_bytes = cur.fetchone()
                conn.execute(
                    """
                    UPDATE group_scan_state
                    SET min_id = ?, max_id = ?, total_count = ?, total_bytes = ?, updated_at = ?
                    WHERE entity_key = ?
                    """,
                    (
                        int(min_id or 0),
                        int(max_id or 0),
                        int(cnt or 0),
                        int(tot_bytes or 0),
                        time.strftime("%d/%m/%Y %H:%M:%S"),
                        ek,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
