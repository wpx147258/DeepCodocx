from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, replace

import httpx
import pytest

from dcx.config import ConfigError, load_config
from dcx.deepdocx.ingest.control import IngestCancelled
from dcx.deepdocx.library import DocumentLibrary
from dcx.deepdocx.semantic.client import InferenceClient, normalize
from dcx.deepdocx.semantic.engine import SemanticEngine
from dcx.deepdocx.semantic.native_server import create_app
from dcx.deepdocx.semantic.samples import _blocks
from dcx.deepdocx.semantic.service import IndexService
from dcx.deepdocx.semantic.settings import MODEL, CaptionConfig, IndexConfig, parse_settings
from dcx.deepdocx.semantic.tools import call
from dcx.deepdocx.server import DocumentServer
from tests.support.document_fixture import document_library


class FakeInference(InferenceClient):
    def __init__(self, index=None, caption=None):
        super().__init__(
            index
            or IndexConfig(
                protocol="gitee",
                base_url="https://ai.gitee.com/v1",
                api_key_env="GITEE_API_KEY",
                model=MODEL,
                dimensions=64,
                joint_dimensions=64,
                joint_protocol="openai",
                joint_model=MODEL,
                joint_key_env="QWEN_VL_JOINT_API_KEY",
            ),
            caption or CaptionConfig(),
            {},
        )
        self.inputs = []
        self.descriptions = 0
        self.native = True
        self.fail_caption = False

    def ready(self, caption=False):
        return True

    def joint_ready(self):
        return self.native

    def embed(self, inputs, **kwargs):
        self.inputs.extend(inputs)
        vectors = []
        for item in inputs:
            text = item.get("text", "").lower()
            # Deterministic vectors, useful for testing ranking and real SQL cosine.
            vector = [1.0, 1.0 if "action" in text else 0.0] + [0.0] * 62
            vectors.append(normalize(vector, 64))
        return vectors

    def native_query(self, text="", image=""):
        return self.embed(
            [{**({"text": text} if text else {}), **({"image": image} if image else {})}]
        )[0]

    def describe(self, *args):
        self.descriptions += 1
        if self.fail_caption:
            raise ValueError("caption unavailable")
        return "A visual memory architecture that conditions action."


def engine(tmp_path, *, fake=None):
    root = document_library(tmp_path / "corpus")
    client = fake or FakeInference()
    return SemanticEngine(
        DocumentLibrary(root), tmp_path / "state", client.index, client.caption, client
    )


def test_all_representations_are_persisted_and_unchanged_sync_uses_no_inference(tmp_path):
    e = engine(tmp_path)
    hashes = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in e.library.root.rglob("*")
        if p.is_file()
    }
    result = e.sync()
    assert not result["errors"]
    assert e.status()["counts"] == {
        "text_block": 1,
        "image": 1,
        "caption_original": 1,
        "caption_generated": 1,
        "image_caption_original": 1,
        "image_caption_generated": 1,
    }
    assert e.status()["indexed_documents"] == 1
    assert any("image" in item and "text" in item for item in e.client.inputs)
    calls = len(e.client.inputs)
    descriptions = e.client.descriptions
    assert e.sync()["unchanged_documents"] == 1
    assert len(e.client.inputs) == calls and e.client.descriptions == descriptions
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in hashes.items())
    result = e.search("memory")
    assert result["vector_search"] and result["hits"]
    for hit in result["hits"]:
        assert e.library.resolve(hit["locator"])
        assert "image" not in hit and "context" not in hit
    assert e.search(image=tmp_path / "corpus/paper_alpha/v1/marker/assets/page.png")["hits"]
    assert e.search("memory", image=tmp_path / "corpus/paper_alpha/v1/marker/assets/page.png")[
        "hits"
    ]


def test_generated_and_original_captions_keep_provenance(tmp_path):
    e = engine(tmp_path)
    e.sync()
    hits = e.search("memory", representations=["caption_generated"])["hits"]
    assert len(hits) == 1
    assert hits[0]["caption_provenance"]["source"] == "generated"
    assert hits[0]["caption_provenance"]["model"] == "qwen3.8-flash"
    original = e.search("memory", representations=["caption_original"])["hits"][0]
    assert original["caption_provenance"]["source"] == "original"
    assert original["snippet"] == "Figure 1: memory architecture"


def test_deleted_changed_and_out_of_scope_documents_never_match(tmp_path):
    e = engine(tmp_path)
    e.sync()
    scoped = SemanticEngine(
        DocumentLibrary(e.library.root, ("paper_other",)),
        tmp_path / "state",
        e.index,
        e.caption,
        e.client,
    )
    assert not scoped.search("memory")["hits"]
    path = e.library.root / "paper_alpha/v1/marker/units.jsonl"
    path.write_text(path.read_text().replace("memory architecture", "changed architecture"))
    assert not e.search("memory")["hits"]
    assert e.status()["pending_documents"] == 1
    e.sync()
    assert e.search("memory")["hits"]
    shutil.rmtree(e.library.root / "paper_alpha")
    assert not e.search("memory")["hits"]
    e.sync()
    with e.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0


def test_partial_native_failure_keeps_other_views_and_retries_cache(tmp_path):
    client = FakeInference()
    client.native = False
    e = engine(tmp_path, fake=client)
    result = e.sync()
    assert result["errors"] and all("Native joint" in item["error"] for item in result["errors"])
    assert e.status()["partial_documents"] == 1 and e.status()["pending_documents"] == 1
    assert e.status()["counts"]["image_caption_original"] == 0
    assert e.search("memory", representations=["text_block"])["hits"]
    before = len(client.inputs)
    descriptions = client.descriptions
    client.native = True
    assert not e.sync()["errors"]
    assert client.descriptions == descriptions
    assert len(client.inputs) == before + 2  # Only the two missing native joint vectors.
    assert e.status()["indexed_documents"] == 1


def test_caption_failure_is_retryable_and_does_not_hide_originals(tmp_path):
    client = FakeInference()
    client.fail_caption = True
    e = engine(tmp_path, fake=client)
    assert e.sync()["errors"]
    assert e.status()["counts"]["caption_original"] == 1
    assert e.status()["counts"]["caption_generated"] == 0
    client.fail_caption = False
    assert not e.sync()["errors"]
    assert e.status()["counts"]["caption_generated"] == 1


def test_model_configuration_change_invalidates_vectors_and_reuses_correct_space(tmp_path):
    e = engine(tmp_path)
    e.sync()
    new = SemanticEngine(
        e.library,
        tmp_path / "state",
        replace(e.index, revision="new-revision"),
        e.caption,
        e.client,
    )
    assert new.status()["indexed_documents"] == 0
    assert not new.search("memory")["hits"]
    assert replace(e.index, timeout=120, batch_size=4).profile == e.index.profile


def test_cancel_during_inference_preserves_previous_generation(tmp_path):
    e = engine(tmp_path)
    e.sync()
    with e.store.connect() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM vectors ORDER BY id")]
    count = 0

    def check():
        nonlocal count
        count += 1
        if count > 4:
            raise IngestCancelled()

    with pytest.raises(IngestCancelled):
        e.sync(force=True, check=check)
    with e.store.connect() as conn:
        after = [tuple(r) for r in conn.execute("SELECT * FROM vectors ORDER BY id")]
    assert before == after


def test_basic_lines_merge_within_columns_and_keep_paragraph_boundaries():
    units = [
        {"id": str(i), "kind": "text", "page": 1, "text": text, "bbox": box}
        for i, (text, box) in enumerate(
            [
                ("left first", [10, 90, 100, 100]),
                ("right first", [180, 90, 280, 100]),
                ("left second", [10, 78, 100, 88]),
                ("right second", [180, 78, 280, 88]),
                ("new paragraph", [10, 30, 100, 40]),
            ]
        )
    ]
    groups = _blocks(units, True, 1800)
    assert [[u["text"] for u in group] for group in groups] == [
        ["left first", "left second"],
        ["new paragraph"],
        ["right first", "right second"],
    ]


@pytest.mark.parametrize(
    "index,caption",
    [
        ({"model": "other"}, {}),
        ({"dimensions": 63}, {}),
        ({"dimensions": True}, {}),
        ({"batch_size": 0}, {}),
        ({"base_url": "http://external.invalid/v1"}, {}),
        ({"base_url": "https://user:password@example.org"}, {}),
        ({"joint_base_url": "ftp://host"}, {}),
        ({"unexpected": True}, {}),
        ({}, {"max_tokens": 0}),
    ],
)
def test_index_configuration_is_validated(index, caption):
    with pytest.raises(ValueError):
        parse_settings(index, caption)


def test_config_loads_services_without_putting_secrets_in_settings(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[deepdocx.index]\ndimensions=2048\njoint_base_url="http://127.0.0.1:8790/v1"\n[deepdocx.caption]\nmodel="DeepSeek-V4.1-Flash-1017"\n'
    )
    loaded = load_config(
        config,
        cwd=tmp_path,
        env={"XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    assert loaded.index.dimensions == 2048
    assert loaded.caption.model == "DeepSeek-V4.1-Flash-1017"
    assert "api_key" not in asdict(loaded.index)
    config.write_text('[deepdocx.index]\nmodel="other"')
    with pytest.raises(ConfigError):
        load_config(config, cwd=tmp_path, env={})


def test_tool_scope_and_query_image_paths_are_checked(tmp_path):
    e = engine(tmp_path)
    e.sync()
    server = DocumentServer(e.library, e, (tmp_path / "workspace",))
    assert "doc_semantic_search" in {tool["name"] for tool in server.tools}
    assert all("build" not in tool["name"] for tool in server.tools)
    response = json.loads(
        server.call("doc_semantic_search", {"query": "memory"})["content"][0]["text"]
    )
    assert response["hits"]
    external = tmp_path / "outside.png"
    external.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside"):
        call(e, "doc_semantic_search", {"image_path": str(external)}, (tmp_path / "workspace",))
    with pytest.raises(ValueError, match="Unknown"):
        e.search("memory", representations=["invalid"])
    assert not e.search("memory", doc="unselected")["hits"]


async def test_native_endpoint_requires_auth_and_passes_both_modalities_to_one_sample(tmp_path):
    e = engine(tmp_path)
    image_path = e.library.root / "paper_alpha/v1/marker/assets/page.png"
    from dcx.deepdocx.semantic.client import image_data

    class Encoder:
        def __init__(self):
            self.inputs = []

        def encode(self, inputs, **kwargs):
            self.inputs.extend(inputs)
            return [[1.0] + [0.0] * 63 for _ in inputs]

    encoder = Encoder()
    app = create_app(encoder, token="test-key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "model": MODEL,
            "input": [{"text": "memory", "image": image_data(image_path)}],
            "dimensions": 64,
        }
        assert (await client.post("/v1/embeddings", json=body)).status_code == 401
        response = await client.post(
            "/v1/embeddings", json=body, headers={"Authorization": "Bearer test-key"}
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["data"][0]["embedding"]) == 64
        assert len(encoder.inputs) == 1 and set(encoder.inputs[0]) == {"text", "image"}
        assert not isinstance(encoder.inputs[0]["image"], str)
        for invalid in ([{"image": "https://example.org/a.png"}], [{"text": "x", "command": "rm"}]):
            response = await client.post(
                "/v1/embeddings",
                json={**body, "input": invalid},
                headers={"Authorization": "Bearer test-key"},
            )
            assert response.status_code == 400


def test_inference_response_validation_and_no_fusion_substitution(tmp_path):
    for value in ([0.0] * 64, [1.0] * 63, [float("nan")] * 64, [True] * 64):
        with pytest.raises(ValueError):
            normalize(value, 64)
    e = engine(tmp_path)
    client = InferenceClient(e.index, e.caption, {})
    with pytest.raises(ValueError, match="native"):
        client.combined("image", "caption", [1.0] * 64, [1.0] * 64)


def test_unconfigured_automatic_indexing_does_not_contact_services(tmp_path):
    e = engine(tmp_path)
    service = IndexService(tmp_path / "state", e.index, e.caption, {})
    assert service.automatic(e.library) is None
    assert not service.status(e.library)["embedding_ready"]
    with pytest.raises(ValueError, match="GITEE_API_KEY"):
        service.start(e.library)


def test_dashscope_native_space_uses_its_own_query_dimensions(tmp_path):
    index = IndexConfig(protocol="gitee", model=MODEL, dimensions=64, joint_dimensions=256)

    class SeparateSpace(FakeInference):
        def native_query(self, text="", image=""):
            return [1.0] + [0.0] * 255

    client = SeparateSpace(index)
    e = engine(tmp_path, fake=client)
    assert not e.sync()["errors"]
    with e.store.connect() as conn:
        shapes = {
            (row["space"], row["bytes"])
            for row in conn.execute("SELECT space,length(vector) AS bytes FROM vectors")
        }
    assert shapes == {("gitee", 64 * 4), ("joint", 256 * 4)}
    hits = e.search("memory")["hits"]
    assert hits
    assert {value["space"] for hit in hits for value in hit["similarities"]} == {"gitee", "joint"}


def test_dashscope_fusion_protocol_is_explicit_and_rejects_independent_outputs(monkeypatch):
    index = IndexConfig(joint_dimensions=256)
    client = InferenceClient(index, CaptionConfig(), {"DASHSCOPE_API_KEY": "fake-key"})
    captured = []

    def post(config, route, body):
        captured.append((route, body))
        return {"output": {"embeddings": [{"type": "fusion", "embedding": [1.0] + [0.0] * 255}]}}

    monkeypatch.setattr(client, "_post", post)
    assert len(client.native_query("caption", "data:image/png;base64,test")) == 256
    route, body = captured[0]
    assert route == "/services/embeddings/multimodal-embedding/multimodal-embedding"
    assert body["model"] == "qwen3-vl-embedding"
    assert body["parameters"] == {"dimension": 256, "enable_fusion": True}
    assert body["input"]["contents"] == [
        {"text": "caption"},
        {"image": "data:image/png;base64,test"},
    ]
    monkeypatch.setattr(client, "_post", lambda *args: {"output": {"embeddings": [{}, {}]}})
    with pytest.raises(ValueError, match="independent"):
        client.native_query("text", "image")


def test_changing_native_endpoint_reuses_gitee_embeddings_and_captions(tmp_path):
    e = engine(tmp_path)
    e.sync()
    before = len(e.client.inputs)
    descriptions = e.client.descriptions
    changed = replace(e.index, joint_base_url="http://127.0.0.1:9999/v1")
    e.client.index = changed
    updated = SemanticEngine(e.library, tmp_path / "state", changed, e.caption, e.client)
    assert not updated.sync()["errors"]
    assert len(e.client.inputs) == before + 2
    assert e.client.descriptions == descriptions


def test_corrupt_image_in_one_document_does_not_block_healthy_sources(tmp_path):
    e = engine(tmp_path)
    original = e.library.root / "paper_alpha"
    other = e.library.root / "paper_beta"
    shutil.copytree(original, other)
    for mode in ("basic", "marker"):
        path = other / "v1" / mode / "manifest.json"
        raw = json.loads(path.read_text())
        if mode == "basic":
            raw["doc"]["doc_id"] = "paper_beta"
        else:
            raw["doc_id"] = "paper_beta"
        path.write_text(json.dumps(raw))
    assert not e.sync()["errors"]
    (other / "v1/marker/assets/page.png").unlink()
    assert e.status()["source_errors"][0]["doc_id"] == "paper_beta"
    assert all(hit["doc_id"] == "paper_alpha" for hit in e.search("memory")["hits"])
    result = e.sync()
    assert result["errors"][0]["doc_id"] == "paper_beta"


def test_new_basic_conversion_preserves_lines_across_column_gutter():
    from dcx.deepdocx.ingest.basic import Page, Word, _line_blocks

    page = Page(
        1,
        600,
        800,
        [
            Word(20, 50, 80, 60, "Left"),
            Word(85, 50, 130, 60, "column"),
            Word(330, 50, 390, 60, "Right"),
            Word(395, 50, 450, 60, "column"),
        ],
    )
    blocks = _line_blocks(page)
    assert [block["text"] for block in blocks] == ["Left column", "Right column"]


def test_one_query_service_failure_keeps_other_space_results(tmp_path, monkeypatch):
    e = engine(tmp_path)
    e.sync()

    def failed(*args, **kwargs):
        raise ValueError("Gitee is temporarily unavailable")

    monkeypatch.setattr(e.client, "native_query", lambda *args: [1.0] + [0.0] * 63)
    monkeypatch.setattr(e.client, "embed", failed)
    result = e.search("memory")
    assert result["hits"]
    assert result["warnings"] == ["Gitee is temporarily unavailable"]
    assert all(
        all(value["space"] == "joint" for value in hit["similarities"]) for hit in result["hits"]
    )


def test_dashscope_is_the_default_and_both_encoders_use_2560_dimensions():
    index, _ = parse_settings({}, {})
    assert index.protocol == "dashscope" and index.model == "qwen3-vl-embedding"
    assert index.dimensions == index.joint_dimensions == 2560
    assert index.api_key_env == index.joint_key_env == "DASHSCOPE_API_KEY"
    gitee, _ = parse_settings(
        {
            "protocol": "gitee",
            "model": MODEL,
            "dimensions": 1024,
            "base_url": "https://ai.gitee.com/v1",
            "api_key_env": "GITEE_API_KEY",
        },
        {},
    )
    assert gitee.space == "gitee" and gitee.embedding_profile != index.embedding_profile


def test_dashscope_independent_batches_reorder_and_reject_fusion(monkeypatch):
    client = InferenceClient(IndexConfig(dimensions=256), CaptionConfig())
    captured = []

    def post(config, route, body):
        captured.append(body)
        assert not body["parameters"]["enable_fusion"]
        return {
            "output": {
                "embeddings": [
                    {"index": i, "type": "vl", "embedding": [1.0, float(i)] + [0.0] * 254}
                    for i in reversed(range(len(body["input"]["contents"])))
                ]
            }
        }

    monkeypatch.setattr(client, "_post", post)
    vectors = client.embed([{"text": str(i)} for i in range(13)])
    assert len(vectors) == 13
    assert [len(body["input"]["contents"]) for body in captured] == [10, 3]
    assert vectors[0][1] == 0 and vectors[1][1] > 0
    monkeypatch.setattr(
        client,
        "_post",
        lambda *args: {
            "output": {
                "embeddings": [
                    {"index": 0, "type": "fusion", "embedding": [1.0] * 256},
                ]
            }
        },
    )
    with pytest.raises(ValueError, match="independent"):
        client.embed([{"text": "x"}])


def test_selective_sync_preserves_other_documents_and_does_not_contact_inference_for_them(tmp_path):
    e = engine(tmp_path)
    other = e.library.root / "paper_beta"
    shutil.copytree(e.library.root / "paper_alpha", other)
    for mode in ("basic", "marker"):
        path = other / "v1" / mode / "manifest.json"
        data = json.loads(path.read_text())
        (data["doc"] if mode == "basic" else data)["doc_id"] = "paper_beta"
        path.write_text(json.dumps(data))
    assert not e.sync(documents=["paper_alpha"])["errors"]
    assert e.status()["indexed_documents"] == 1
    with e.store.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT doc_id FROM documents")} == {"paper_alpha"}
    assert not e.sync(documents=["paper_beta"])["errors"]
    assert e.status()["indexed_documents"] == 2
    with e.store.connect() as conn:
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM vectors WHERE doc_id='paper_beta'")
        ]
    e.sync(documents=["paper_alpha"], force=True)
    with e.store.connect() as conn:
        assert before == [
            tuple(row) for row in conn.execute("SELECT * FROM vectors WHERE doc_id='paper_beta'")
        ]
    with pytest.raises(ValueError, match="Select"):
        e.sync(documents=[])
    with pytest.raises(ValueError, match="Unknown"):
        e.sync(documents=["missing"])
