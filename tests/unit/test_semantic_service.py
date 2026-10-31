from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from dcx.config import AppPaths, IngestConfig
from dcx.deepdocx.ingest import IngestService
from dcx.deepdocx.library import DocumentLibrary, LibraryStore
from dcx.deepdocx.semantic.client import InferenceClient
from dcx.deepdocx.semantic.service import TERMINAL, IndexService
from dcx.deepdocx.semantic.settings import CaptionConfig, IndexConfig
from dcx.web.indexes import index_routes
from tests.support.document_fixture import document_library
from tests.support.inference_fixture import inference_server
from tests.support.pdf_fixture import small_pdf


def configuration(url):
    return (
        IndexConfig(
            protocol="gitee",
            model="Qwen3-VL-Embedding-8B",
            base_url=url,
            joint_base_url=url,
            api_key_env="DCX_TEST_EMBED_KEY",
            joint_key_env="DCX_TEST_JOINT_KEY",
            dimensions=64,
            joint_protocol="openai",
            joint_model="Qwen3-VL-Embedding-8B",
            joint_dimensions=64,
        ),
        CaptionConfig(base_url=url, api_key_env="DCX_TEST_CAP_KEY"),
        {
            "DCX_TEST_EMBED_KEY": "test-embed",
            "DCX_TEST_JOINT_KEY": "test-joint",
            "DCX_TEST_CAP_KEY": "test-caption",
        },
    )


def wait(service, library, ident):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = service.status(library)
        job = next(j for j in status["jobs"] if j["id"] == ident)
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.1)
    raise AssertionError("worker did not finish")


def test_real_worker_incremental_cache_and_missing_process_recovery(tmp_path):
    library = DocumentLibrary(document_library(tmp_path / "corpus"))
    with inference_server() as (url, requests):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state", index, caption, env)
        first = service.start(library)
        assert service.start(library)["id"] == first["id"]
        final = wait(service, library, first["id"])
        assert final["status"] == "succeeded", final
        assert service.status(library)["indexed_documents"] == 1
        assert any(
            "image" in item and "text" in item
            for request in requests
            if request["path"].endswith("/embeddings")
            for item in request["body"]["input"]
        )
        before = len(requests)
        second = service.start(library)
        assert wait(service, library, second["id"])["status"] == "succeeded"
        assert len(requests) == before
        # A new host instance recovers the same persisted index and preferences.
        restarted = IndexService(tmp_path / "state", index, caption, env)
        assert restarted.status(library)["auto_sync"]
        assert restarted.status(library)["counts"]["image_caption_generated"] == 1
        store = service.engine(library).store
        store.update_job("dead-worker", status="running", pid=99999999, spec_path="/nonexistent")
        assert (
            next(j for j in restarted.status(library)["jobs"] if j["id"] == "dead-worker")["status"]
            == "failed"
        )
        assert all(
            "test-embed" not in path.read_text()
            for path in (tmp_path / "state").rglob("settings.json")
        )


def test_queued_cancel_stays_cancelled_and_does_not_call_inference(tmp_path):
    library = DocumentLibrary(document_library(tmp_path / "corpus"))
    with inference_server() as (url, requests):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state", index, caption, env)
        from dcx.session.lock import FileLock

        with FileLock(tmp_path / "state/semantic/worker.lock"):
            job = service.start(library)
            cancelled = service.cancel(library, job["id"])
            assert cancelled["status"] == "cancelled"
        time.sleep(0.4)
        assert not requests
        assert service.engine(library).store.job(job["id"])["status"] == "cancelled"


def test_dashscope_worker_encodes_every_representation_without_gitee_embedding(tmp_path):
    from dataclasses import replace

    library = DocumentLibrary(document_library(tmp_path / "corpus"))
    with inference_server() as (url, requests):
        _, caption, env = configuration(url)
        index = replace(IndexConfig(), base_url=url, joint_base_url=url)
        env["DASHSCOPE_API_KEY"] = "test-dashscope"
        service = IndexService(tmp_path / "state", index, caption, env)
        job = service.start(library, documents=["paper_alpha"])
        final = wait(service, library, job["id"])
        assert final["status"] == "succeeded", final
        assert final["documents"] == ["paper_alpha"]
        assert all(value == 1 for value in service.status(library)["counts"].values())
        assert not any(request["path"].endswith("/embeddings") for request in requests)
        for request in requests:
            if request["path"].endswith("/multimodal-embedding"):
                assert request["body"]["parameters"]["dimension"] == 2560
        assert service.engine(library).search("memory")["hits"]
        with service.engine(library).store.connect() as conn:
            assert {r[0] for r in conn.execute("SELECT space FROM vectors")} == {
                "dashscope",
                "joint",
            }


@pytest.mark.skipif(
    not shutil.which("pdftotext") or not shutil.which("pdftoppm"), reason="Poppler is optional"
)
def test_new_ingestion_enqueues_independent_index_job_after_first_sync(tmp_path):
    library = DocumentLibrary(document_library(tmp_path / "corpus/paper-bundles"))
    with inference_server() as (url, requests):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state", index, caption, env)
        job = service.start(library)
        assert wait(service, library, job["id"])["status"] == "succeeded"
        # An unrelated old pending paper must not get indexed by the new upload.
        pending = library.root / "paper_pending/v1/basic"
        shutil.copytree(library.root / "paper_alpha/v1/basic", pending)
        manifest = pending / "manifest.json"
        raw = json.loads(manifest.read_text())
        raw["doc"]["doc_id"] = "paper_pending"
        manifest.write_text(json.dumps(raw))
        pdf = tmp_path / "New evidence.pdf"
        pdf.write_bytes(small_pdf())
        paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path, "test")
        ingest = IngestService(paths, IngestConfig(), index=index, caption=caption, env=env)
        job = ingest.create_job(library.id, library.root, "basic")
        ingest.add_file(job["id"], pdf.name, pdf)
        ingest.start(job["id"])
        final = ingest.wait(job["id"], timeout=20)
        # Ingestion reaches its own terminal state before semantic work is queued.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            final = ingest.get(job["id"])
            ident = final.get("result", {}).get("semantic_job_id")
            if ident:
                break
            time.sleep(0.1)
        assert final["status"] == "succeeded" and ident, final
        assert wait(service, library, ident)["status"] == "succeeded"
        assert service.status(library)["indexed_documents"] == 2
        assert service.status(library)["pending_documents"] == 1
        assert service.engine(library).store.job(ident)["documents"] == [final["files"][0]["doc_id"]]
        assert not list((tmp_path / "state").rglob("session.jsonl"))


def test_ingestion_during_selected_job_coalesces_only_the_new_document(tmp_path):
    from dcx.session.lock import FileLock

    library = DocumentLibrary(document_library(tmp_path / "corpus"))
    target = library.root / "paper_beta/v1/basic"
    shutil.copytree(library.root / "paper_alpha/v1/basic", target)
    manifest = target / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["doc"]["doc_id"] = "paper_beta"
    manifest.write_text(json.dumps(payload))
    with inference_server() as (url, _):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state", index, caption, env)
        with FileLock(tmp_path / "state/semantic/worker.lock"):
            job = service.start(library, documents=["paper_alpha"])
            with pytest.raises(ValueError, match="different document selection"):
                service.start(library, documents=["paper_beta"])
            assert service.automatic(library, documents=["paper_beta"])["id"] == job["id"]
            assert service.engine(library).store.preference("resync_documents") == ["paper_beta"]
        final = wait(service, library, job["id"])
        assert final["status"] == "succeeded", final
        assert final["processed"] == final["total"] == 2
        assert service.status(library)["indexed_documents"] == 2


async def test_api_validation_and_search_scope(tmp_path):
    library = DocumentLibrary(document_library(tmp_path / "corpus"))
    with inference_server() as (url, requests):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state", index, caption, env)
        store = LibraryStore(tmp_path / "state", configured=(str(library.root),))
        host = SimpleNamespace(
            indexes=service, bundle=SimpleNamespace(libraries=store, document_library=library)
        )
        app = Starlette(routes=index_routes(host))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/index/status")).status_code == 200
            for value in (
                {"documents": []},
                {"documents": "paper_alpha"},
                {"documents": ["missing"]},
                {"documents": [True]},
                {"force": "false"},
                {"generate_captions": 1},
                {"caption_model": []},
                {"caption_model": "other"},
                {"library_id": []},
            ):
                response = await client.post("/api/index/sync", json=value)
                assert response.status_code == 400, response.text
            response = await client.post(
                "/api/index/sync",
                json={"generate_captions": True, "caption_model": "DeepSeek-V4.1-Flash-1017"},
            )
            assert response.status_code == 202, response.text
            assert wait(service, library, response.json()["id"])["status"] == "succeeded"
            response = await client.post("/api/index/search", json={"text": "memory"})
            assert response.status_code == 200 and response.json()["hits"]
            host.bundle.document_library = DocumentLibrary(library.root, ("outside",))
            assert not (await client.post("/api/index/search", json={"text": "memory"})).json()[
                "hits"
            ]
            for value in (
                {"text": []},
                {"text": "x", "top_k": True},
                {"text": "x", "representations": ["unknown"]},
                {"text": "x", "image": "http://remote/image.png"},
            ):
                response = await client.post("/api/index/search", json=value)
                assert response.status_code == 400, response.text


def test_gitee_protocol_orders_vectors_validates_model_and_preserves_joint_request(
    tmp_path, monkeypatch
):
    index = IndexConfig(
        protocol="gitee",
        base_url="https://ai.gitee.com/v1",
        api_key_env="GITEE_API_KEY",
        model="Qwen3-VL-Embedding-8B",
        dimensions=64,
        joint_base_url="http://127.0.0.1:1234/v1",
        joint_protocol="openai",
        joint_model="Qwen3-VL-Embedding-8B",
        joint_dimensions=64,
        joint_key_env="QWEN_VL_JOINT_API_KEY",
    )
    client = InferenceClient(
        index, CaptionConfig(), {"GITEE_API_KEY": "key", "QWEN_VL_JOINT_API_KEY": "joint"}
    )
    calls = []

    def post(config, path, body):
        calls.append((config.base_url, body))
        return {
            "model": index.model,
            "data": [
                {"index": i, "embedding": [1.0] + [0.0] * 63}
                for i in reversed(range(len(body["input"])))
            ],
        }

    monkeypatch.setattr(client, "_post", post)
    assert len(client.embed([{"text": "one"}, {"text": "two"}])) == 2
    client.combined("data:image/png;base64,test", "caption", [], [])
    assert calls[-1][0] == index.joint_base_url
    assert calls[-1][1]["input"] == [{"image": "data:image/png;base64,test", "text": "caption"}]
    monkeypatch.setattr(client, "_post", lambda *args: {"model": "other", "data": []})
    with pytest.raises(ValueError, match="different model"):
        client.embed([{"text": "x"}])


async def test_persistent_stdio_mcp_reads_index_without_agent_loop(tmp_path):
    import sys

    from dcx.mcp.client import ServerConfig, StdioClient

    library = DocumentLibrary(document_library(tmp_path / "corpus"))
    with inference_server() as (url, requests):
        index, caption, env = configuration(url)
        service = IndexService(tmp_path / "state", index, caption, env)
        job = service.start(library)
        assert wait(service, library, job["id"])["status"] == "succeeded"
        spec = tmp_path / "semantic-settings.json"
        spec.write_text(
            json.dumps(
                {
                    "data_dir": str(tmp_path / "state"),
                    "index": asdict(index),
                    "caption": asdict(caption),
                }
            )
        )
        client = StdioClient(
            ServerConfig(
                "documents",
                (
                    sys.executable,
                    "-m",
                    "dcx.deepdocx.server",
                    "--root",
                    str(library.root),
                    "--semantic-settings",
                    str(spec),
                    "--workspace",
                    str(tmp_path / "workspace"),
                ),
                env=env,
            )
        )
        try:
            tools = await client.tools()
            assert {"doc_semantic_search", "doc_index_status"} <= {tool["name"] for tool in tools}
            assert all(
                tool["name"] not in {"doc_build", "doc_index_sync", "doc_caption"} for tool in tools
            )
            result = await client.request(
                "tools/call", {"name": "doc_semantic_search", "arguments": {"query": "memory"}}
            )
            assert not result["isError"]
            assert json.loads(result["content"][0]["text"])["hits"]
            # UI preferences changed after this process started must be observed.
            changed = service.start(library, caption_model="DeepSeek-V4.1-Flash-1017")
            assert wait(service, library, changed["id"])["status"] == "succeeded"
            result = await client.request(
                "tools/call", {"name": "doc_semantic_search", "arguments": {"query": "memory"}}
            )
            assert json.loads(result["content"][0]["text"])["hits"]
        finally:
            await client.close()
