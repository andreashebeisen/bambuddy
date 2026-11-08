"""Charge a print's spools by hand, after the fact.

A print sent from Bambu Cloud lands on the printer's internal storage, so its
3MF never reaches Bambuddy and its spools are charged nothing: the usage
tracker reads the grams per filament from the 3MF, and a spool without an
RFID tag has no AMS remaining figure to fall back on. Once the file is in the
print's archive (uploaded and merged, see ``archive_merge.merge_pair``), the
user charges the print's spools from it here.

``propose`` reads the file's filaments for the run's plate and suggests a
spool for each: the one assigned to the AMS slot the printer reported for
that filament at the print's start, if its material fits, else the active
spool of that material nearest in colour. ``charge`` books what the user
confirmed, as completion does: the spool's usage, a usage record, and the
run's grams and cost.

A usage record names no run. Which run it belongs to is read from its
printer and time (``usage_repricing._run_of``), so a charge is dated at the
print's end: repricing, usage statistics by date and the check for a run
already charged then all see it as that print's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.utils.safe_path import PathTraversalError, safe_join_under

logger = logging.getLogger(__name__)

MATCH_SLOT = "slot"  # assigned to the AMS slot the printer used
MATCH_COLOR = "color"  # same material, nearest colour
_ENDED = frozenset({"completed", "failed", "cancelled", "aborted", "stopped"})
# Farther than this (RGB distance, 0-441) is not the same colour.
_COLOR_TOLERANCE = 60.0


class ChargeError(Exception):
    """Why a run cannot be charged: ``code`` is stable, for the UI."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class FilamentLine:
    slot_id: int
    material: str | None
    color: str | None
    grams: float
    tray: tuple[int, int] | None = None  # (ams_id, tray_id) the printer used
    spool_id: int | None = None
    match: str | None = None


@dataclass
class Proposal:
    run: PrintLogEntry
    archive: PrintArchive
    filaments: list[FilamentLine] = field(default_factory=list)
    already_charged: bool = False


def _rgb(value: str | None) -> tuple[int, int, int] | None:
    text = (value or "").strip().lstrip("#")
    if len(text) < 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _distance(a: str | None, b: str | None) -> float | None:
    ra, rb = _rgb(a), _rgb(b)
    if ra is None or rb is None:
        return None
    return sum((x - y) ** 2 for x, y in zip(ra, rb, strict=True)) ** 0.5


def _same_material(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().upper() == b.strip().upper())


def _tray_of(global_tray: int) -> tuple[int, int] | None:
    """(ams_id, tray_id) of a global tray id, as the usage tracker keys them."""
    if global_tray < 0:
        return None
    if global_tray >= 254:
        return 255, global_tray - 254
    if global_tray >= 128:
        return global_tray, 0
    return global_tray // 4, global_tray % 4


def _print_data(archive: PrintArchive, run: PrintLogEntry) -> dict | None:
    """What the printer reported at the run's start: kept by run when its
    file-less archive was merged in, else the archive's own."""
    extra = archive.extra_data or {}
    data = (extra.get("run_print_data") or {}).get(str(run.id))
    return data or extra.get("_print_data")


def _slot_trays(data: dict | None) -> list[int] | None:
    """Global tray id per slicer filament slot (0-based), from the printer's mapping."""
    from backend.app.services.usage_tracker import _decode_mqtt_mapping

    if not data:
        return None
    raw = data.get("raw_data") or {}
    mapping = raw.get("mapping") if isinstance(raw, dict) else None
    return _decode_mqtt_mapping(mapping if mapping is not None else data.get("mapping"))


def _file(archive: PrintArchive):
    if not archive.file_path:
        return None
    try:
        path = safe_join_under(settings.base_dir, archive.file_path, http=False)
    except PathTraversalError:
        return None
    return path if path.is_file() else None


async def charged_rows(db: AsyncSession, run: PrintLogEntry) -> list[SpoolUsageHistory]:
    """The usage records charged in ``run``, as repricing assigns them."""
    from backend.app.services.usage_repricing import _run_of

    if run.archive_id is None:
        return []
    rows = (await db.scalars(select(SpoolUsageHistory).where(SpoolUsageHistory.archive_id == run.archive_id))).all()
    runs = (await db.scalars(select(PrintLogEntry).where(PrintLogEntry.archive_id == run.archive_id))).all()
    return [row for row in rows if (owner := _run_of(row, list(runs))) is not None and owner.id == run.id]


async def _load(db: AsyncSession, run_id: int, archive_id: int) -> tuple[PrintLogEntry, PrintArchive]:
    run = await db.get(PrintLogEntry, run_id)
    if run is None or run.archive_id != archive_id:
        raise ChargeError("not_found")
    archive = await db.get(PrintArchive, archive_id)
    if archive is None or archive.deleted_at is not None:
        raise ChargeError("not_found")
    return run, archive


async def propose(db: AsyncSession, archive_id: int, run_id: int) -> Proposal:
    """The run's filaments with a spool suggested for each (see the module docstring)."""
    import asyncio

    from backend.app.utils.threemf_tools import extract_filament_usage_from_3mf

    run, archive = await _load(db, run_id, archive_id)
    path = _file(archive)
    if path is None:
        raise ChargeError("no_file")
    usage = await asyncio.to_thread(extract_filament_usage_from_3mf, path, run.plate_id)
    proposal = Proposal(run=run, archive=archive, already_charged=bool(await charged_rows(db, run)))

    trays = _slot_trays(_print_data(archive, run))
    assigned: dict[tuple[int, int], Spool] = {}
    if run.printer_id is not None:
        for assignment, spool in await db.execute(
            select(SpoolAssignment, Spool)
            .join(Spool, Spool.id == SpoolAssignment.spool_id)
            .where(SpoolAssignment.printer_id == run.printer_id, Spool.archived_at.is_(None))
        ):
            assigned[(assignment.ams_id, assignment.tray_id)] = spool
    active = (await db.scalars(select(Spool).where(Spool.archived_at.is_(None)))).all()

    for entry in usage:
        grams = float(entry.get("used_g") or 0)
        if grams <= 0:
            continue
        slot_id = int(entry.get("slot_id") or 0)
        line = FilamentLine(
            slot_id=slot_id, material=entry.get("type"), color=entry.get("color"), grams=round(grams, 1)
        )
        if trays and 0 < slot_id <= len(trays):
            line.tray = _tray_of(trays[slot_id - 1])
        spool = assigned.get(line.tray) if line.tray else None
        if spool is not None and (not line.material or _same_material(spool.material, line.material)):
            line.spool_id, line.match = spool.id, MATCH_SLOT
        else:
            best = None
            for candidate in active:
                if not _same_material(candidate.material, line.material):
                    continue
                distance = _distance(candidate.rgba, line.color)
                if distance is not None and distance <= _COLOR_TOLERANCE and (best is None or distance < best[0]):
                    best = (distance, candidate)
            if best is not None:
                line.spool_id, line.match = best[1].id, MATCH_COLOR
        proposal.filaments.append(line)
    return proposal


async def charge(db: AsyncSession, archive_id: int, run_id: int, lines: list[tuple[int, float]]) -> list[dict]:
    """Charge ``lines`` (spool id, grams) to the run and commit. Returns what
    each spool was charged. Refused (``ChargeError``) for a run still printing,
    one already charged, or in Spoolman mode."""
    from backend.app.api.routes.settings import get_setting
    from backend.app.services.print_cost_estimate import default_cost_per_kg
    from backend.app.services.usage_tracker import _charge_spool

    run, archive = await _load(db, run_id, archive_id)
    if run.status not in _ENDED:
        raise ChargeError("not_ended")
    if ((await get_setting(db, "spoolman_enabled")) or "").lower() == "true":
        raise ChargeError("spoolman")
    lines = [(spool_id, round(float(grams), 1)) for spool_id, grams in lines if grams and grams > 0]
    if not lines:
        raise ChargeError("nothing")
    if await charged_rows(db, run):
        raise ChargeError("already_charged")

    spools = {s.id: s for s in await db.scalars(select(Spool).where(Spool.id.in_([s for s, _ in lines])))}
    if len(spools) != len({s for s, _ in lines}):
        raise ChargeError("spool_not_found")

    default_rate = await default_cost_per_kg(db)
    # At the print's end, so the record is the run's (see the module docstring).
    when: datetime | None = run.completed_at or run.started_at
    results = []
    for spool_id, grams in lines:
        spool = spools[spool_id]
        if not await _charge_spool(db, spool, grams):
            raise ChargeError("spool_not_found")
        rate = spool.cost_per_kg if spool.cost_per_kg is not None else default_rate
        cost = round(grams / 1000.0 * rate, 2) if rate and rate > 0 else None
        percent = round(grams / spool.label_weight * 100) if spool.label_weight else 0
        row = SpoolUsageHistory(
            spool_id=spool.id,
            printer_id=run.printer_id,
            print_name=run.print_name or archive.print_name,
            archive_id=archive.id,
            weight_used=grams,
            percent_used=percent,
            status=run.status,
            cost=cost,
        )
        if when is not None:
            row.created_at = when
        db.add(row)
        results.append({"spool_id": spool.id, "weight_used": grams, "cost": cost})

    run.filament_used_grams = round(sum(r["weight_used"] for r in results), 1)
    priced = [r["cost"] for r in results if r["cost"] is not None]
    if priced:
        run.cost = round(sum(priced), 2)
    await db.flush()

    # The archive shows one full print of its file: from a completed run
    # that is its only one, as completion sets it.
    from backend.app.services.print_log import other_runs_of

    if priced and run.status == "completed" and not await other_runs_of(db, archive):
        archive.cost = run.cost
    await db.commit()
    logger.info("Charged %s to spools of run %s (archive %s) by hand", results, run.id, archive.id)
    return results
