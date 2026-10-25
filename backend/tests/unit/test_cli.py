from pathlib import Path

import swarmos.cli


def test_alembic_config_resolves_from_container_working_directory(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    backend_root = app_root / "backend"
    backend_root.mkdir(parents=True)
    config_path = backend_root / "alembic.ini"
    config_path.write_text("[alembic]\nscript_location = migrations\n", encoding="utf-8")
    installed_module = tmp_path / "site-packages" / "swarmos" / "cli.py"

    monkeypatch.setattr(swarmos.cli, "__file__", str(installed_module))
    monkeypatch.chdir(app_root)
    monkeypatch.delenv("SWARMOS_ALEMBIC_CONFIG", raising=False)

    config = swarmos.cli._alembic_cfg()

    assert Path(config.config_file_name) == config_path
    assert config.get_main_option("script_location") == "migrations"


def test_alembic_config_honors_explicit_environment_path(tmp_path, monkeypatch):
    config_path = tmp_path / "alembic.ini"
    config_path.write_text("[alembic]\nscript_location = migrations\n", encoding="utf-8")
    monkeypatch.setenv("SWARMOS_ALEMBIC_CONFIG", str(config_path))

    config = swarmos.cli._alembic_cfg()

    assert Path(config.config_file_name) == config_path
