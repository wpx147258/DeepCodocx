from __future__ import annotations

import json
import shlex
import shutil
import sys

import pytest
from click.testing import CliRunner

from dcx.deepdocx.ingest.builders import build_basic, build_marker
from dcx.deepdocx.ingest.identity import sha256_file
from dcx.deepdocx.ingest.publish import PublishError, publish, validate_bundle
from dcx.deepdocx.library import DocumentLibrary
from dcx.deepdocx.server import TOOLS
from dcx.entry import main
from tests.support.pdf_fixture import small_pdf

pytestmark = pytest.mark.skipif(
    not shutil.which("pdftoppm") or not shutil.which("pdftotext"),
    reason="Poppler is optional on test hosts",
)


def test_real_basic_round_trip_and_no_model_cli(tmp_path):
    pdf = tmp_path / "Local evidence.pdf"
    pdf.write_bytes(small_pdf())
    library = tmp_path / "paper-bundles"
    library.mkdir()
    env = {"XDG_DATA_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    result = CliRunner().invoke(
        main, ["docs", "ingest", "--library", str(library), str(pdf)], env=env
    )
    assert result.exit_code == 0, result.output
    job = json.loads(result.output)
    assert job["status"] == "succeeded"
    assert DocumentLibrary(library).search("acceptance")["hits"]
    listed = CliRunner().invoke(main, ["docs", "jobs"], env=env)
    assert listed.exit_code == 0
    assert json.loads(listed.output)[0]["id"] == job["id"]
    skipped = CliRunner().invoke(
        main, ["docs", "ingest", "--library", str(library), str(pdf)], env=env
    )
    assert skipped.exit_code == 0, skipped.output
    assert json.loads(skipped.output)["files"][0]["modes"]["basic"] == "skipped"
    docs = CliRunner().invoke(main, ["docs", "list", "--library", str(library)], env=env)
    assert len(json.loads(docs.output)) == 1
    assert {tool["name"] for tool in TOOLS} == {
        "doc_library",
        "doc_search",
        "doc_read",
        "doc_view",
        "doc_guide",
    }


def test_invalid_pdf_cli_exits_nonzero_with_failed_task(tmp_path):
    pdf = tmp_path / "invalid.pdf"
    pdf.write_bytes(b"%PDF-invalid")
    library = tmp_path / "library"
    library.mkdir()
    result = CliRunner().invoke(
        main,
        ["docs", "ingest", "--library", str(library), str(pdf)],
        env={"XDG_DATA_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "failed"


def test_marker_adapter_builds_self_consistent_bundle_without_marker_model(tmp_path):
    pdf = tmp_path / "Marker evidence.pdf"
    pdf.write_bytes(small_pdf())
    document = {
        "block_type": "Document",
        "children": [
            {
                "block_type": "Page",
                "id": "/page/0/Page/0",
                "bbox": [0, 0, 300, 200],
                "children": [
                    {
                        "block_type": "SectionHeader",
                        "id": "/page/0/SectionHeader/1",
                        "bbox": [20, 10, 280, 35],
                        "html": "<h1>Memory mechanism</h1>",
                    },
                    {
                        "block_type": "Text",
                        "id": "/page/0/Text/2",
                        "bbox": [20, 35, 280, 60],
                        "html": "<p>Memory retains evidence for action.</p>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/1"},
                    },
                    {
                        "block_type": "FigureGroup",
                        "id": "/page/0/FigureGroup/3",
                        "bbox": [20, 60, 150, 115],
                        "children": [
                            {
                                "block_type": "Figure",
                                "id": "/page/0/Figure/4",
                                "bbox": [20, 60, 150, 100],
                            },
                            {
                                "block_type": "Caption",
                                "id": "/page/0/Caption/5",
                                "bbox": [20, 100, 150, 115],
                                "html": "<p>Figure 1: memory architecture</p>",
                            },
                        ],
                    },
                    {
                        "block_type": "TableGroup",
                        "id": "/page/0/TableGroup/6",
                        "bbox": [20, 120, 280, 170],
                        "children": [
                            {
                                "block_type": "Table",
                                "id": "/page/0/Table/7",
                                "bbox": [20, 120, 280, 150],
                                "html": "<table><tr><th>Method</th><th>Score</th></tr><tr><td>Memory</td><td>90</td></tr></table>",
                            },
                            {
                                "block_type": "Caption",
                                "id": "/page/0/Caption/8",
                                "bbox": [20, 150, 280, 170],
                                "html": "<p>Table 1: comparison</p>",
                            },
                        ],
                    },
                    {
                        "block_type": "Equation",
                        "id": "/page/0/Equation/9",
                        "bbox": [20, 170, 280, 190],
                        "html": "<p>x = y + 1</p>",
                    },
                ],
            }
        ],
    }
    captured = tmp_path / "marker.json"
    captured.write_text(json.dumps(document))
    script = tmp_path / "fake_marker.py"
    script.write_text(
        "import pathlib,sys,shutil\nout=pathlib.Path(sys.argv[sys.argv.index('--output_dir')+1]); shutil.copyfile(sys.argv[1],out/'parsed.json')\n"
    )
    staging = tmp_path / "stage"
    staging.mkdir()
    result = build_marker(
        pdf,
        staging,
        "paper_marker",
        marker_command=shlex.join([sys.executable, str(script), str(captured)]),
    )
    assert result["blocks"] == 5
    bundle = staging / "paper_marker/v1/marker"
    manifest = validate_bundle(bundle, "marker")
    assert manifest["source_version"] == "v1"
    assert (bundle / "manifest.sha256").read_text().strip() == sha256_file(bundle / "manifest.json")
    for relative, info in manifest["files"].items():
        assert sha256_file(bundle / relative) == info["sha256"]
    assert all(
        json.loads(line)["source_version"] == "v1"
        for line in (bundle / "units.jsonl").read_text().splitlines()
    )
    root = tmp_path / "paper-bundles"
    root.mkdir()
    publish(staging, root, "paper_marker", "marker", pdf)
    library = DocumentLibrary(root)
    hits = library.search("memory", kind="figure")["hits"]
    assert hits and hits[0]["locator"].startswith("doc:paper_marker@v1/marker-json-v1/")
    image, mime, _ = library.image(hits[0]["locator"])
    assert mime == "image/png" and image.stat().st_size > 0
    assert not list(staging.glob(".dcx-marker-*"))


def test_publish_detects_corrupt_page_preview_before_committing(tmp_path):
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(small_pdf())
    root = tmp_path / "stage"
    build_basic(pdf, root, doc_id="paper_test")
    bundle = root / "paper_test/v1/basic"
    (bundle / "pages.json").write_text('{"pages": []}')
    with pytest.raises(PublishError, match="page previews"):
        publish(root, tmp_path / "library", "paper_test", "basic", pdf)
    assert not (tmp_path / "library/paper_test/v1/basic").exists()
