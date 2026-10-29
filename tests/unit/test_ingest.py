from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from dcx.config import AppPaths, IngestConfig
from dcx.deepdocx.ingest import IngestService
from dcx.deepdocx.ingest.identity import doc_id_for_name, sanitize_filename
from dcx.deepdocx.ingest.index import build_basic_index
from dcx.deepdocx.ingest.jobs import JobStore
from dcx.deepdocx.ingest.publish import PublishError, publish
from dcx.deepdocx.library import DocumentLibrary
from dcx.web.ingest import ingest_routes
from tests.support.document_fixture import document_library


def test_identity_matches_basic_pipeline_name() -> None:
    assert doc_id_for_name("RAAP.pdf") == "paper_ceeb183c40ba"
    assert sanitize_filename("../../x.pdf") == "x.pdf"


def test_job_store_validates_and_persists_uploads(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    job = store.create("lib", tmp_path / "paper-bundles", "basic")
    data = b"%PDF-1.4\nhello"
    job = store.add_file(job["id"], "paper.pdf", data, max_bytes=1024)
    assert job["files"][0]["doc_id"] == doc_id_for_name("paper.pdf")
    assert Path(job["files"][0]["stored"]).read_bytes() == data
    duplicate = store.add_file(job["id"], "paper.pdf", data, max_bytes=1024)
    assert len(duplicate["files"]) == 1
    with pytest.raises(ValueError, match="PDF"):
        store.add_file(job["id"], "bad.pdf", b"not a pdf", max_bytes=1024)
    with pytest.raises(ValueError, match="exceeds"):
        store.add_file(job["id"], "large.pdf", b"%PDF-" + b"x" * 20, max_bytes=10)


def test_publish_and_atomic_basic_index(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    document_library(staging)
    library = tmp_path / "data" / "paper-bundles"
    library.mkdir(parents=True)
    source = tmp_path / "Alpha Memory.pdf"
    source.write_bytes(b"%PDF-1.4")
    result = publish(staging, library, "paper_alpha", "basic", source)
    assert result["status"] == "published"
    assert (library / "paper_alpha/v1/basic/manifest.json").is_file()
    assert (library / "paper_alpha/v1/basic/source/original.pdf").is_file()
    assert (tmp_path / "data" / "sources" / "paper_alpha.pdf").is_file()
    stats = build_basic_index(library)
    assert stats["documents"] == 1
    assert stats["units"] >= 1
    hits = DocumentLibrary(tmp_path / "data").search("Memory", top_k=2)
    assert hits["hits"]


def test_publish_skips_existing_and_force_replaces(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    document_library(staging)
    library = tmp_path / "data" / "paper-bundles"
    library.mkdir(parents=True)
    source = tmp_path / "Alpha Memory.pdf"
    source.write_bytes(b"%PDF-1.4")
    first = publish(staging, library, "paper_alpha", "basic", source)
    assert first["status"] == "published"

    staging2 = tmp_path / "staging2"
    document_library(staging2)
    (staging2 / "paper_alpha/v1/basic/manifest.json").write_text(
        json.dumps(
            {
                "schema": "dda.bundle/1",
                "doc": {
                    "doc_id": "paper_alpha",
                    "version": "v1",
                    "title": "Replacement",
                    "page_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    second = publish(staging2, library, "paper_alpha", "basic", source)
    assert second["status"] == "skipped"
    staging3 = tmp_path / "staging3"
    document_library(staging3)
    (staging3 / "paper_alpha/v1/basic/manifest.json").write_text(
        json.dumps(
            {
                "schema": "dda.bundle/1",
                "doc": {
                    "doc_id": "paper_alpha",
                    "version": "v1",
                    "title": "Replacement",
                    "page_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    forced = publish(staging3, library, "paper_alpha", "basic", source, force=True)
    assert forced["status"] == "published"
    assert "Replacement" in (library / "paper_alpha/v1/basic/manifest.json").read_text()


def test_publish_rejects_incomplete_bundle(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "paper_alpha/v1/basic").mkdir(parents=True)
    source = tmp_path / "x.pdf"
    source.write_bytes(b"%PDF-1.4")
    with pytest.raises(PublishError):
        publish(staging, tmp_path / "library", "paper_alpha", "basic", source)


class _Libraries:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, ident: str) -> dict[str, str]:
        return {"id": ident, "root": str(self.root)}

    def selected_id(self) -> str:
        return "lib_test"


def _host(tmp_path: Path, *, marker: str = "/definitely/not/marker") -> SimpleNamespace:
    library = tmp_path / "data" / "paper-bundles"
    library.mkdir(parents=True)
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "state",
        cwd=tmp_path,
        project_id="test",
    )
    service = IngestService(paths, IngestConfig(marker_command=marker))
    bundle = SimpleNamespace(libraries=_Libraries(library), document_library=None)
    return SimpleNamespace(ingest=service, bundle=bundle)


@pytest.mark.asyncio
async def test_ingest_upload_api_and_marker_preflight(tmp_path: Path) -> None:
    host = _host(tmp_path)
    app = Starlette(routes=ingest_routes(host))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/ingest/jobs", json={"library_id": "lib_test", "mode": "basic"}
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        uploaded = await client.post(
            f"/api/ingest/jobs/{job_id}/files?name=paper.pdf",
            content=b"%PDF-1.4\n",
            headers={"content-type": "application/pdf"},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["files"][0]["name"] == "paper.pdf"
        status = await client.get(f"/api/ingest/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "uploading"

        marker_job = (
            await client.post(
                "/api/ingest/jobs", json={"library_id": "lib_test", "mode": "marker"}
            )
        ).json()
        await client.post(
            f"/api/ingest/jobs/{marker_job['id']}/files?name=paper.pdf",
            content=b"%PDF-1.4\n",
        )
        blocked = await client.post(f"/api/ingest/jobs/{marker_job['id']}/start")
        assert blocked.status_code == 400
        assert "Marker" in blocked.json()["error"]
