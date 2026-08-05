import logging

import pytest

from codebase_memory_deployv import cli
from codebase_memory_deployv.indexer import DEFAULT_BATCH_SIZE, DEFAULT_ROOT


@pytest.fixture(autouse=True)
def _drop_log_handlers():
    """Detach the handler main() installs on the package logger.

    It holds the stdout pytest captured for this test; left in place, logging flushes to
    that closed stream when the worker shuts down ("I/O operation on closed file").
    """
    yield
    logger = logging.getLogger(cli.PACKAGE_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert args.repo_path == DEFAULT_ROOT
    assert args.batch_size == DEFAULT_BATCH_SIZE
    assert args.mode == "auto"
    assert args.modules_file is None
    assert not args.skip_install
    assert not args.skip_validate


def test_main_fails_outside_container(tmp_path):
    try:
        cli.main(["--repo-path", str(tmp_path)])
    except SystemExit as error:
        assert "odoo-bin" in str(error)
    else:
        raise AssertionError("main must refuse to run outside the container layout")


def _instance(tmp_path, *module_rel_paths):
    odoo_dir = tmp_path / "odoo"
    odoo_dir.mkdir()
    (odoo_dir / "odoo-bin").write_text("")
    for rel in module_rel_paths:
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "__manifest__.py").write_text("{'name': 'x'}")
    return str(tmp_path)


def test_main_modules_file_wins_over_the_database(tmp_path, monkeypatch, capsys):
    """Production may have modules the local container never installs: the file rules."""
    root = _instance(tmp_path, "extra_addons/vauxoo/sale_extended", "extra_addons/vauxoo/never_installed_here")
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name,state\nsale_extended,installed\nnever_installed_here,installed\nweb,installed\n")
    indexed = []
    monkeypatch.setattr(cli, "ensure_cbm_installed", lambda: "cbm")
    monkeypatch.setattr(cli, "index_repository", lambda root_, project: indexed.append(project))
    monkeypatch.setattr(cli, "installed_modules", lambda root_: pytest.fail("the database must not be queried"))
    monkeypatch.setattr(cli, "validate_scope", lambda project, modules: True)
    monkeypatch.setenv("CBM_PROJECT", "prod_18.0")
    assert cli.main(["--repo-path", root, "--modules-file", str(modules_file)]) == 0
    out = capsys.readouterr().out
    assert "mode=file modules=2" in out
    assert "not_on_disk=1" in out
    assert "not_on_disk web" in out
    assert indexed == ["prod_18.0"]
    cbmignore = (tmp_path / ".cbmignore").read_text()
    assert "!extra_addons/vauxoo/never_installed_here/**/*.py" in cbmignore


def test_main_modules_file_ignored_with_mode_all(tmp_path, monkeypatch, capsys):
    root = _instance(tmp_path, "extra_addons/vauxoo/sale_extended")
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name\nsale_extended\n")
    monkeypatch.setattr(cli, "ensure_cbm_installed", lambda: "cbm")
    monkeypatch.setattr(cli, "index_repository", lambda root_, project: None)
    monkeypatch.setenv("CBM_PROJECT", "prod_18.0")
    assert cli.main(["--repo-path", root, "--mode=all", "--modules-file", str(modules_file), "--skip-validate"]) == 0
    assert "modules_file=ignored" in capsys.readouterr().out


def test_main_modules_file_without_modules_on_disk(tmp_path, monkeypatch):
    root = _instance(tmp_path, "extra_addons/vauxoo/sale_extended")
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name\nonly_in_production\n")
    monkeypatch.setattr(cli, "ensure_cbm_installed", lambda: "cbm")
    try:
        cli.main(["--repo-path", root, "--modules-file", str(modules_file)])
    except SystemExit as error:
        assert "does not list any module" in str(error)
    else:
        raise AssertionError("main must fail when the modules file matches nothing on disk")
