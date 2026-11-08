"""A print whose 3MF never reached Bambuddy merges into its file, uploaded since.

A print sent from Bambu Cloud lands on the printer's internal storage, so
Bambuddy archives it without a file (``file_path=""``): time, photo, video and
run, but no 3MF. The user uploads the file as an archive of its own and merges
the two by hand: the one with the file stays and takes the print.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.core.config import settings as app_settings
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_alias import ArchiveAlias
from backend.app.models.print_log import PrintLogEntry
from backend.app.utils.archive_paths import archive_photos_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T1 = datetime(2026, 9, 1, 9)
PRINT_DATA = {"subtask_name": "part", "raw_data": {"mapping": [0, 1]}}


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    return tmp_path


@pytest.fixture
async def pair(db_session, printer_factory, files):
    """``cloud``: a finished cloud print with a photo and a run, no file.
    ``upload``: its 3MF, uploaded later, never printed. ``other``: another file."""
    printer = await printer_factory(name="A")
    cloud = PrintArchive(
        printer_id=printer.id,
        filename="part.gcode.3mf",
        print_name="part",
        file_path="",
        file_size=0,
        status="completed",
        started_at=T1,
        completed_at=T1 + timedelta(hours=1),
        photos=["finish.jpg"],
        thumbnail_path=None,
        extra_data={"no_3mf_available": True, "_print_data": PRINT_DATA},
    )
    db_session.add(cloud)
    await db_session.flush()

    def uploaded(name):
        folder = files / "archive" / "uploads" / name
        folder.mkdir(parents=True)
        (folder / "part.gcode.3mf").write_bytes(name.encode())
        (folder / "thumb.png").write_bytes(b"png")
        return PrintArchive(
            filename="part.gcode.3mf",
            print_name="Part (sliced)",
            file_path=str((folder / "part.gcode.3mf").relative_to(files)),
            file_size=3,
            content_hash=name[0] * 64,
            thumbnail_path=str((folder / "thumb.png").relative_to(files)),
            filament_used_grams=21.5,
            # As an upload lands: stamped completed now, never printed.
            status="archived",
            completed_at=datetime.now(),
            extra_data={"print_name": "Part (sliced)"},
        )

    upload, other = uploaded("upload"), uploaded("other")
    db_session.add_all([upload, other])
    await db_session.flush()

    photos = archive_photos_dir(cloud)
    photos.mkdir(parents=True, exist_ok=True)
    (photos / "finish.jpg").write_bytes(b"photo")

    run = PrintLogEntry(
        archive_id=cloud.id,
        printer_id=printer.id,
        status="completed",
        started_at=T1,
        completed_at=T1 + timedelta(hours=1),
        photos=["finish.jpg"],
    )
    db_session.add(run)
    await db_session.commit()
    return SimpleNamespace(cloud=cloud.id, upload=upload.id, other=other.id, run=run.id, printer=printer.id)


async def _get(db_session, model, row_id):
    db_session.expire_all()
    return await db_session.get(model, row_id)


async def test_the_file_takes_the_print(async_client, db_session, pair):
    # Picked in either order: the archive with the file stays.
    response = await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": [pair.cloud, pair.upload]})

    assert response.status_code == 200, response.text
    assert response.json() == {"survivor_id": pair.upload, "merged_id": pair.cloud}

    run = await _get(db_session, PrintLogEntry, pair.run)
    assert run.archive_id == pair.upload
    archive = await _get(db_session, PrintArchive, pair.upload)
    # Its print's outcome, times and printer; its own file and metadata.
    assert (archive.status, archive.started_at, archive.printer_id) == ("completed", T1, pair.printer)
    assert archive.print_name == "Part (sliced)"
    assert archive.filament_used_grams == 21.5
    assert archive.photos == ["finish.jpg"]
    assert (archive_photos_dir(archive) / "finish.jpg").read_bytes() == b"photo"


async def test_only_the_print_it_had_is_counted(async_client, db_session, pair):
    """The upload was never printed: no run is written for it."""
    await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": [pair.upload, pair.cloud]})

    db_session.expire_all()
    runs = list(await db_session.scalars(select(PrintLogEntry).where(PrintLogEntry.archive_id == pair.upload)))
    assert [r.id for r in runs] == [pair.run]


async def test_the_print_data_is_kept_by_run(async_client, db_session, pair):
    await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": [pair.upload, pair.cloud]})

    archive = await _get(db_session, PrintArchive, pair.upload)
    assert archive.extra_data["run_print_data"] == {str(pair.run): PRINT_DATA}
    assert archive.extra_data["print_name"] == "Part (sliced)"


async def test_the_file_less_one_stays_deleted_with_an_alias(async_client, db_session, pair):
    await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": [pair.upload, pair.cloud]})

    assert (await _get(db_session, PrintArchive, pair.cloud)).deleted_at is not None
    assert (await db_session.get(ArchiveAlias, pair.cloud)).archive_id == pair.upload


@pytest.mark.parametrize("which", ["two_files", "same", "one"])
async def test_only_a_file_and_a_file_less_print_merge(async_client, db_session, pair, which):
    ids = {
        "two_files": [pair.upload, pair.other],
        "same": [pair.upload, pair.upload],
        "one": [pair.upload],
    }[which]

    response = await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": ids})

    assert response.status_code in (409, 422), response.text
    for archive_id in (pair.upload, pair.other, pair.cloud):
        assert (await _get(db_session, PrintArchive, archive_id)).deleted_at is None


async def test_two_file_less_prints_do_not_merge(async_client, db_session, pair):
    second = PrintArchive(filename="x", print_name="x", file_path="", file_size=0, status="completed")
    db_session.add(second)
    await db_session.commit()

    response = await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": [pair.cloud, second.id]})

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "not_a_pair"


async def test_a_print_still_running_is_left_alone(async_client, db_session, pair):
    cloud = await _get(db_session, PrintArchive, pair.cloud)
    cloud.status = "printing"
    await db_session.commit()

    response = await async_client.post("/api/v1/archives/merge-pair", json={"archive_ids": [pair.upload, pair.cloud]})

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "busy"
    assert (await _get(db_session, PrintLogEntry, pair.run)).archive_id == pair.cloud
