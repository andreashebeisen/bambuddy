"""Import the sliced files on a printer's storage into the archives.

A print sent from Bambu Cloud lands on the printer, not in Bambuddy, so its
archive has no file (see ``archive_merge.merge_pair``). Its ``.gcode.3mf``
often still sits on the printer's card. ``scan`` lists those files on every
printer the caller may use, leaving out the ones already archived; ``import_file``
downloads one and archives it as an upload would.

Already archived means an archive with a file of the same name and size. A
file that is new by that measure but whose content an archive already holds
(the same hash) is not archived again either: ``import_file`` reports it as a
duplicate.

The database is read in short sessions, never held across FTP (#2572).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)

# Where printers keep the jobs sent to them; one level deep, as the printer's
# own storage probe looks (services/print_storage).
SCAN_DIRECTORIES = ("/", "/cache", "/model", "/data")
SUFFIX = ".gcode.3mf"
# A sliced file larger than this is not a print job.
_MAX_BYTES = 2 * 1024**3
_DOWNLOAD_TIMEOUT = 30 * 60

IMPORTED = "imported"
DUPLICATE = "duplicate"


class ImportError_(Exception):
    """Why a file was not imported: ``code`` is stable, for the UI."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class PrinterFiles:
    printer_id: int
    name: str
    available: bool = True
    files: list[dict] = field(default_factory=list)
    already_imported: int = 0


def _key(name: str, size: int | None) -> tuple[str, int | None]:
    return name.lower(), size


async def _archived_keys() -> set[tuple[str, int | None]]:
    """(filename, size) of every archive that holds a file."""
    async with database.async_session() as db:
        rows = await db.execute(
            select(func.lower(PrintArchive.filename), PrintArchive.file_size).where(
                PrintArchive.deleted_at.is_(None), PrintArchive.file_path != ""
            )
        )
        return {(name, size) for name, size in rows.tuples().all() if name}


async def _list_printer(printer: Printer) -> tuple[bool, list[dict]]:
    """The printer's sliced files, and whether it answered at all."""
    from backend.app.services.bambu_ftp import list_files_result_async

    found: dict[str, dict] = {}
    answered = False
    for directory in SCAN_DIRECTORIES:
        listing = await list_files_result_async(
            printer.ip_address, printer.access_code, directory, printer_model=printer.model
        )
        if listing.available or listing.missing:
            answered = True
        elif not answered:
            # Not reachable: the other folders would only wait out the same timeout.
            return False, []
        for entry in listing.files:
            name = entry.get("name") or ""
            if entry.get("is_directory") or not name.lower().endswith(SUFFIX):
                continue
            path = f"{directory.rstrip('/')}/{name}"
            mtime = entry.get("mtime")
            found.setdefault(
                path,
                {
                    "path": path,
                    "name": name,
                    "size": entry.get("size"),
                    "mtime": mtime.isoformat() if isinstance(mtime, datetime) else None,
                },
            )
    # The printer keeps a job in more than one folder, now and then under
    # another name (the project's in /, the plate's in /cache): one file of
    # the same size and time is offered once, from the first folder scanned.
    unique: dict[tuple, dict] = {}
    for entry in found.values():
        same = (entry["size"], entry["mtime"]) if entry["size"] and entry["mtime"] else (entry["path"],)
        unique.setdefault(same, entry)
    return answered, list(unique.values())


async def scan(printers: list[Printer]) -> list[PrinterFiles]:
    """The files on ``printers`` not archived yet, newest first."""
    archived = await _archived_keys()

    async def one(printer: Printer) -> PrinterFiles:
        result = PrinterFiles(printer_id=printer.id, name=printer.name)
        try:
            result.available, files = await _list_printer(printer)
        except Exception:
            logger.exception("Listing the files of printer %s for import failed", printer.id)
            result.available, files = False, []
        for entry in files:
            if _key(entry["name"], entry["size"]) in archived:
                result.already_imported += 1
            else:
                result.files.append(entry)
        result.files.sort(key=lambda f: f["mtime"] or "", reverse=True)
        return result

    return list(await asyncio.gather(*(one(p) for p in printers)))


async def import_file(printer: Printer, path: str, created_by_id: int | None) -> tuple[str, int]:
    """Download ``path`` from the printer and archive it. Returns ``IMPORTED``
    and the new archive, or ``DUPLICATE`` and the archive already holding it."""
    from backend.app.services.archive import ArchiveService
    from backend.app.services.bambu_ftp import download_file_async

    name = path.rsplit("/", 1)[-1]
    if not name.lower().endswith(SUFFIX) or ".." in path.split("/"):
        raise ImportError_("not_a_print_file")
    if not any(path == f"{d.rstrip('/')}/{name}" for d in SCAN_DIRECTORIES):
        raise ImportError_("not_a_print_file")

    staging = settings.archive_dir / "temp" / f"printer-import-{secrets.token_hex(8)}"
    staging.mkdir(parents=True, exist_ok=True)
    local = staging / name
    try:
        ok = await download_file_async(
            printer.ip_address,
            printer.access_code,
            path,
            local,
            timeout=_DOWNLOAD_TIMEOUT,
            printer_model=printer.model,
            max_bytes=_MAX_BYTES,
            max_total_timeout=_DOWNLOAD_TIMEOUT,
        )
        if not ok or not local.is_file():
            raise ImportError_("download_failed")

        content_hash = await asyncio.to_thread(ArchiveService.compute_file_hash, local)
        async with database.async_session() as db:
            existing = await db.scalar(
                select(PrintArchive.id)
                .where(
                    PrintArchive.content_hash == content_hash,
                    PrintArchive.deleted_at.is_(None),
                    PrintArchive.file_path != "",
                )
                .order_by(PrintArchive.id)
                .limit(1)
            )
            if existing is not None:
                return DUPLICATE, existing
            archive = await ArchiveService(db).archive_print(
                printer_id=printer.id,
                source_file=local,
                created_by_id=created_by_id,
                original_filename=name,
            )
            if archive is None:
                raise ImportError_("archive_failed")
            logger.info("Imported %s from printer %s as archive %s", path, printer.id, archive.id)
            return IMPORTED, archive.id
    finally:
        await asyncio.to_thread(_remove, staging)


def _remove(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)
