from pathlib import Path

from dcx.config import load_config


def test_ingest_config_defaults_and_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[deepdocx.ingest]
basic_dpi = 96
max_upload_mb = 42
max_files = 3
marker_command = "/opt/marker"
marker_timeout = 900
""",
        encoding="utf-8",
    )
    config = load_config(config_path, cwd=tmp_path)
    assert config.ingest.basic_dpi == 96
    assert config.ingest.max_upload_mb == 42
    assert config.ingest.max_files == 3
    assert config.ingest.marker_command == "/opt/marker"
    assert config.ingest.marker_timeout == 900


def test_ingest_config_rejects_invalid_dpi(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[deepdocx.ingest]\nbasic_dpi = 1\n", encoding="utf-8")
    try:
        load_config(config_path, cwd=tmp_path)
    except Exception as exc:  # noqa: BLE001 - message is the contract
        assert "basic_dpi" in str(exc)
    else:
        raise AssertionError("invalid dpi was accepted")
