from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from dcx.config import IngestConfig
from dcx.deepdocx.ingest.commands import run_command
from dcx.deepdocx.ingest.control import IngestCancelled
from dcx.deepdocx.ingest.identity import sha256_file
from dcx.deepdocx.ingest.index import build_basic_index, update_basic_index
from dcx.deepdocx.ingest.publish import publish
from dcx.deepdocx.ingest.worker import run
from dcx.deepdocx.library import DocumentLibrary
from dcx.deepdocx.server import DocumentServer
from dcx.web.ingest import ingest_routes
from tests.support.document_fixture import document_library
from tests.unit.test_ingest import _host


def _builder(pdf: Path, out: Path, doc_id: str, **kwargs):
    fixture = out / "fixture"
    document_library(fixture)
    for mode in ("basic", "marker"):
        source = fixture / "paper_alpha/v1" / mode
        target = out / doc_id / "v1" / mode
        shutil.copytree(source, target, dirs_exist_ok=True)
        path = target / "manifest.json"
        manifest = json.loads(path.read_text())
        if mode == "basic":
            manifest["doc"]["doc_id"] = doc_id
            manifest["doc"]["title"] = pdf.stem
        else:
            manifest["doc_id"] = doc_id
        path.write_text(json.dumps(manifest))
    shutil.rmtree(fixture)
    return {"doc_id": doc_id}


def _job(tmp_path, mode="basic"):
    host = _host(tmp_path)
    job = host.ingest.create_job("lib_test", host.bundle.libraries.root, mode)
    host.ingest.add_file(job["id"], "New Memory.pdf", b"%PDF-1.4\nsource")
    return host.ingest, job


def _fail(*args, **kwargs):
    raise RuntimeError("converter failed")


@pytest.mark.parametrize("failed_phase", ["basic", "marker"])
def test_both_preserves_each_rendition_result_and_partial_status(
    tmp_path, monkeypatch, failed_phase
):
    service, job = _job(tmp_path, "both")
    for phase in ("basic", "marker"):
        monkeypatch.setattr(
            f"dcx.deepdocx.ingest.worker.build_{phase}",
            _fail if phase == failed_phase else _builder,
        )
    assert run(service.store.path(job["id"])) == 1
    result = service.get(job["id"])
    assert result["status"] == "partial"
    item = result["files"][0]
    assert item["status"] == "partial"
    assert item["modes"][failed_phase] == "failed"
    assert item["modes"]["marker" if failed_phase == "basic" else "basic"] == "published"
    assert result["warnings"]
    if failed_phase == "marker":
        assert result["result"]["index"]["documents"] == 1
        assert DocumentLibrary(Path(job["library_root"])).search("Memory")["hits"]


def test_basic_failure_is_terminal_and_never_reports_success(tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("Poppler is optional on test hosts")
    service, job = _job(tmp_path)
    assert run(service.store.path(job["id"])) == 1
    result = service.get(job["id"])
    assert result["status"] == "failed"
    assert result["files"][0]["modes"] == {"basic": "failed"}
    assert "pdftotext" in result["files"][0]["error"]


def test_success_persists_index_and_refreshes_live_readers(tmp_path, monkeypatch):
    service, job = _job(tmp_path)
    root = Path(job["library_root"])
    library = DocumentLibrary(root)
    server = DocumentServer(library)
    assert library.documents() == []
    monkeypatch.setattr("dcx.deepdocx.ingest.worker.build_basic", _builder)
    assert run(service.store.path(job["id"])) == 0
    result = service.get(job["id"])
    assert result["status"] == "succeeded"
    assert result["result"]["index"]["units"] == 2
    assert len(library.documents()) == 1
    payload = json.loads(server.call("doc_search", {"query": "Memory"})["content"][0]["text"])
    assert payload["hits"]
    assert not (service.store.path(job["id"]).parent / "uploads").exists()


def test_index_failure_does_not_report_success(tmp_path, monkeypatch):
    service, job = _job(tmp_path)
    monkeypatch.setattr("dcx.deepdocx.ingest.worker.build_basic", _builder)
    monkeypatch.setattr("dcx.deepdocx.ingest.worker.update_basic_index", _fail)
    assert run(service.store.path(job["id"])) == 1
    result = service.get(job["id"])
    assert result["status"] == "partial"
    assert "converter failed" in result["error"]
    assert result["files"][0]["modes"]["basic"] == "published"


def test_cancel_during_build_cannot_be_overwritten_or_publish(tmp_path, monkeypatch):
    service, job = _job(tmp_path)

    def cancel_and_build(*args, **kwargs):
        service.cancel(job["id"])
        return _builder(*args, **kwargs)

    monkeypatch.setattr("dcx.deepdocx.ingest.worker.build_basic", cancel_and_build)
    run(service.store.path(job["id"]))
    assert service.get(job["id"])["status"] == "cancelled"
    assert not DocumentLibrary(Path(job["library_root"])).documents()
    assert not (Path(job["library_root"]).parent / ".dcx-ingest" / job["id"]).exists()


def test_cancelling_marker_preserves_published_basic_and_index(tmp_path, monkeypatch):
    service, job = _job(tmp_path, "both")
    monkeypatch.setattr("dcx.deepdocx.ingest.worker.build_basic", _builder)

    def cancel(*args, **kwargs):
        service.cancel(job["id"])
        raise IngestCancelled()

    monkeypatch.setattr("dcx.deepdocx.ingest.worker.build_marker", cancel)
    run(service.store.path(job["id"]))
    result = service.get(job["id"])
    assert result["status"] == "cancelled"
    assert result["result"]["index"]["documents"] == 1
    assert DocumentLibrary(Path(job["library_root"])).search("Memory")["hits"]


def test_start_is_serialized_and_worker_cannot_race_pid_write(tmp_path, monkeypatch):
    service, job = _job(tmp_path)
    processes = []

    def spawn(*args, **kwargs):
        process = SimpleNamespace(pid=123, poll=lambda: None)
        processes.append(process)
        return process

    monkeypatch.setattr(service, "environment", lambda: {"basic_ready": True})
    monkeypatch.setattr("dcx.deepdocx.ingest.service.subprocess.Popen", spawn)

    def start():
        try:
            return service.start(job["id"])["status"]
        except ValueError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert sorted(results) == ["blocked", "queued"]
    assert len(processes) == 1
    assert service.get(job["id"])["pid"] == 123


def test_dead_worker_is_reported_and_cancel_never_signals_untrusted_pid(tmp_path, monkeypatch):
    service, job = _job(tmp_path)
    service.store.update(job["id"], status="running", pid=999999999)
    assert service.get(job["id"])["status"] == "failed"
    service.store.update(job["id"], status="running", pid=1)
    monkeypatch.setattr("os.kill", lambda *args: pytest.fail("must not signal a persisted PID"))
    assert service.cancel(job["id"])["status"] == "cancelled"


def test_environment_checks_all_mode_dependencies_and_invalid_command(tmp_path, monkeypatch):
    host = _host(tmp_path)
    service = host.ingest
    service.settings = IngestConfig(marker_command="'unclosed")
    assert not service.environment()["marker_ready"]
    assert "quoting" in service.environment()["marker_reason"]
    service.settings = IngestConfig(marker_command="marker")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/bin/tool" if name in {"marker", "pdftoppm"} else None
    )
    environment = service.environment()
    assert not environment["basic_ready"] and environment["marker_ready"]
    job = service.create_job("lib_test", host.bundle.libraries.root, "both")
    service.add_file(job["id"], "x.pdf", b"%PDF-test")
    with pytest.raises(ValueError, match="pdftotext"):
        service.start(job["id"])
    assert service.get(job["id"])["status"] == "uploading"


def test_upload_limits_and_same_name_conflicts_are_checked_before_start(tmp_path):
    host = _host(tmp_path)
    service = host.ingest
    service.settings = IngestConfig(max_files=1)
    job = service.create_job("lib_test", host.bundle.libraries.root, "basic")
    service.add_file(job["id"], "x.pdf", b"%PDF-first")
    with pytest.raises(ValueError, match="At most 1"):
        service.add_file(job["id"], "y.pdf", b"%PDF-second")
    service.settings = IngestConfig(max_files=2)
    with pytest.raises(ValueError, match="same filename"):
        service.add_file(job["id"], "x.pdf", b"%PDF-second")
    service.cancel(job["id"])
    with pytest.raises(ValueError, match="accepting"):
        service.add_file(job["id"], "z.pdf", b"%PDF-third")


async def test_api_streaming_limit_strict_options_and_job_filter(tmp_path):
    host = _host(tmp_path)
    host.ingest.settings = IngestConfig(max_upload_mb=1)
    app = Starlette(routes=ingest_routes(host))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for body in ({"force": "false"}, {"force": 1}, {"mode": []}, {"library_id": 8}):
            response = await client.post("/api/ingest/jobs", json=body)
            assert response.status_code == 400
        response = await client.post("/api/ingest/jobs", json={"library_id": "lib_test"})
        job_id = response.json()["id"]

        async def body():
            yield b"%PDF-" + b"x" * (1024 * 1024)
            yield b"x" * 1024

        response = await client.post(
            f"/api/ingest/jobs/{job_id}/files?name=big.pdf", content=body()
        )
        assert response.status_code == 413
        assert host.ingest.get(job_id)["files"] == []
        assert not list(host.ingest.root.glob("incoming-*"))
        assert not (await client.get("/api/ingest/jobs?library_id=other")).json()["jobs"]
        assert len((await client.get("/api/ingest/jobs?library_id=lib_test")).json()["jobs"]) == 1
        response = await client.get("/api/ingest/jobs/..")
        assert response.status_code in {400, 404}


def test_incremental_index_does_not_reparse_existing_documents_and_rolls_back(tmp_path):
    root = document_library(tmp_path / "library")
    build_basic_index(root)
    before = DocumentLibrary(root).search("Memory")["hits"]
    original = root / "paper_alpha/v1/basic/manifest.json"
    original.write_text("invalid old metadata to prove this is incremental")
    pdf = tmp_path / "second.pdf"
    pdf.write_bytes(b"%PDF-second")
    staging = tmp_path / "stage"
    _builder(pdf, staging, "paper_second")
    publish(staging, root, "paper_second", "basic", pdf)
    result = update_basic_index(root, ["paper_second"])
    assert result["documents"] == 1
    with closing(sqlite3.connect(root / "catalog.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM units_fts").fetchone()[0] == 4
    digest = sha256_file(root / "catalog.sqlite")
    (root / "paper_second/v1/basic/chunks.jsonl").write_text("corrupt")
    with pytest.raises(ValueError, match="chunk"):
        update_basic_index(root, ["paper_second"])
    assert sha256_file(root / "catalog.sqlite") == digest
    assert before


def test_failed_force_publish_restores_bundle_and_source(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-first")
    first = tmp_path / "first"
    _builder(pdf, first, "paper_test")
    publish(first, root, "paper_test", "basic", pdf)
    original = (root / "paper_test/v1/basic/manifest.json").read_bytes()
    replacement = tmp_path / "replacement"
    _builder(pdf, replacement, "paper_test")
    rename = Path.rename
    built = replacement / "paper_test/v1/basic"

    def fail(self, target):
        if self == built:
            raise OSError("simulated publish failure")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError, match="simulated"):
        publish(replacement, root, "paper_test", "basic", pdf, force=True)
    assert (root / "paper_test/v1/basic/manifest.json").read_bytes() == original
    assert (root.parent / "sources/paper_test.pdf").read_bytes() == b"%PDF-first"


def test_converter_can_be_cancelled_while_subprocess_is_running(tmp_path):
    marker = tmp_path / "started"
    with pytest.raises(IngestCancelled):
        run_command(
            [
                sys.executable,
                "-c",
                "import pathlib,time,sys; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
                str(marker),
            ],
            cancelled=marker.exists,
        )
    assert marker.exists()


def test_new_basic_is_searchable_before_index_update_and_scope_stays_fixed(tmp_path):
    root = document_library(tmp_path / "library")
    build_basic_index(root)
    library = DocumentLibrary(root)
    scoped = DocumentLibrary(root, ("paper_alpha",))
    assert len(library.documents()) == 1
    assert len(scoped.documents()) == 1
    pdf = tmp_path / "new.pdf"
    pdf.write_bytes(b"%PDF-new")
    stage = tmp_path / "stage"
    _builder(pdf, stage, "paper_new")
    chunks = stage / "paper_new/v1/basic/chunks.jsonl"
    chunk = json.loads(chunks.read_text().splitlines()[0])
    chunk["text"] = "uniqueunindexedtoken"
    chunks.write_text(json.dumps(chunk))
    publish(stage, root, "paper_new", "basic", pdf)
    assert len(library.documents()) == 2
    assert library.search("uniqueunindexedtoken")["hits"]
    assert len(scoped.documents()) == 1
    assert not scoped.search("uniqueunindexedtoken")["hits"]


def test_force_cannot_mix_different_pdf_contents_under_one_document_id(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    pdf = tmp_path / "same.pdf"
    pdf.write_bytes(b"%PDF-first")
    stage = tmp_path / "first"
    _builder(pdf, stage, "paper_test")
    publish(stage, root, "paper_test", "basic", pdf)
    original = (root / "paper_test/v1/basic/source/original.pdf").read_bytes()
    pdf.write_bytes(b"%PDF-other")  # Identical size must not defeat content checking.
    stage2 = tmp_path / "next"
    _builder(pdf, stage2, "paper_test")
    with pytest.raises(ValueError, match="different PDF"):
        publish(stage2, root, "paper_test", "marker", pdf, force=True)
    assert (root / "paper_test/v1/basic/source/original.pdf").read_bytes() == original
    assert not (root / "paper_test/v1/marker").exists()
