import json
import logging
import os

import pytest

from codebase_memory_deployv import indexer


def test_cumulative_batches_are_cumulative():
    paths = ["m%02d" % number for number in range(60)]
    batches = indexer.cumulative_batches(paths, 25)
    assert [len(batch) for batch in batches] == [25, 50, 60]
    assert batches[0] == batches[1][:25]
    assert batches[-1] == sorted(paths)


def test_cumulative_batches_one_shot():
    paths = ["b", "a"]
    assert indexer.cumulative_batches(paths, 0) == [["a", "b"]]
    assert indexer.cumulative_batches(paths, 25) == [["a", "b"]]


def test_render_cbmignore_includes_core_and_modules():
    content = indexer.render_cbmignore(["extra_addons/vauxoo/sale_extended"])
    assert "\n*\n" in content
    assert "!odoo/odoo/**/" in content
    assert "!extra_addons/vauxoo/sale_extended/**/*.py" in content
    assert "odoo/addons/test_*/" in content
    assert "**/static/lib/" in content


def test_discover_modules_skips_tests_core_and_setup(tmp_path):
    def module(rel):
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "__manifest__.py").write_text("{'name': 'x'}")

    module("extra_addons/vauxoo/sale_extended")
    module("extra_addons/vauxoo/test_sale_extended")
    module("odoo/addons/sale")
    module("odoo/odoo/addons/base")
    module("extra_addons/oca/setup/sale_oca")
    (tmp_path / "odoo" / "odoo-bin").write_text("")
    found = indexer.discover_modules(str(tmp_path))
    assert found == ["extra_addons/vauxoo/sale_extended", "odoo/addons/sale"]


def test_write_cbmignore_backs_up_existing(tmp_path):
    original = tmp_path / ".cbmignore"
    original.write_text("old content\n")
    indexer.write_cbmignore(str(tmp_path), "new content\n")
    assert original.read_text() == "new content\n"
    backup = tmp_path / (".cbmignore" + indexer.CBMIGNORE_BACKUP_SUFFIX)
    assert backup.read_text() == "old content\n"
    indexer.write_cbmignore(str(tmp_path), "newer content\n")
    assert backup.read_text() == "old content\n"


def test_list_installed_modules_script_is_a_packaged_file():
    script = indexer.list_installed_modules_script()
    # basename, not endswith("/..."): the separator is "\" on Windows.
    assert os.path.basename(indexer.LIST_INSTALLED_MODULES_PATH) == "list_installed_modules.py"
    assert os.path.isfile(indexer.LIST_INSTALLED_MODULES_PATH)
    # It runs where odoo-bin shell defines "self"; importing it must stay side-effect free.
    assert 'if "self" in dir():' in script
    assert "cbm_done=1" in script
    compile(script, indexer.LIST_INSTALLED_MODULES_PATH, "exec")


def test_read_modules_file_csv_with_state(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text(
        "name,state\n"
        "sale,installed\n"
        "purchase,uninstalled\n"
        "stock,to upgrade\n"
        "account,installed\n"
        "test_sale,installed\n"
    )
    assert indexer.read_modules_file(str(modules_file)) == ["account", "sale"]


def test_read_modules_file_csv_without_state(tmp_path):
    """No state column means the export was already filtered to the installed modules."""
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name\nsale\npurchase\n")
    assert indexer.read_modules_file(str(modules_file)) == ["purchase", "sale"]


def test_read_modules_file_plain_list(tmp_path):
    modules_file = tmp_path / "modules_installed.txt"
    modules_file.write_text("# exported from production\nsale\n\npurchase\nsale\n")
    assert indexer.read_modules_file(str(modules_file)) == ["purchase", "sale"]


def test_read_modules_file_accepts_exported_labels(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text('"Technical Name","Status"\n"sale","Installed"\n"purchase","Not Installed"\n')
    assert indexer.read_modules_file(str(modules_file)) == ["sale"]


def test_read_modules_file_headerless_with_state(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("sale,installed\npurchase,uninstalled\n")
    assert indexer.read_modules_file(str(modules_file)) == ["sale"]


def test_modules_from_names_maps_paths_and_reports_unknown(tmp_path):
    def module(rel):
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "__manifest__.py").write_text("{'name': 'x'}")

    module("extra_addons/vauxoo/sale_extended")
    module("extra_addons/oca/sale_extended")
    module("odoo/addons/sale")
    modules, unknown = indexer.modules_from_names(str(tmp_path), ["sale", "sale_extended", "only_in_production"])
    # Same module name in two repos: without the database the addons_path order is unknown.
    assert modules == ["extra_addons/oca/sale_extended", "extra_addons/vauxoo/sale_extended", "odoo/addons/sale"]
    assert unknown == ["only_in_production"]


def _fake_query_graph(monkeypatch, file_paths):
    """Stub the codebase-memory-mcp CLI, keeping its "log lines then json" output."""
    calls = []

    def check_output(cmd, **kwargs):
        calls.append(cmd)
        rows = [[path] for path in file_paths]
        return "level=info msg=mem.init\n%s\n" % json.dumps({"columns": ["f.file_path"], "rows": rows})

    monkeypatch.setattr(indexer.subprocess, "check_output", check_output)
    return calls


def _messages(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


def test_indexed_files_queries_the_graph(monkeypatch):
    """search_code caps the scan at 500 grep lines whatever --limit says; query_graph does not."""
    calls = _fake_query_graph(monkeypatch, ["odoo/addons/sale/__manifest__.py", "odoo/addons/sale/models/sale.py"])
    assert indexer.indexed_files("instance_12.0") == [
        "odoo/addons/sale/__manifest__.py",
        "odoo/addons/sale/models/sale.py",
    ]
    assert calls[0][1:3] == ["cli", "query_graph"]
    assert "search_code" not in calls[0]
    query = calls[0][calls[0].index("--query") + 1]
    # f.name and f.extension are unusable: some File nodes hold the basename, others the
    # whole relative path. Only f.file_path is consistent.
    assert query == "MATCH (f:File) RETURN f.file_path"
    assert "--limit" not in calls[0]


def _fake_installer(monkeypatch, version):
    """Stub find_cbm/--version/installer; returns the recorded installer environments."""
    install_envs = []

    def check_output(cmd, **kwargs):
        assert cmd == ["/fake/codebase-memory-mcp", "--version"]
        return "level=info msg=mem.init\ncodebase-memory-mcp %s\n" % version

    def check_call(cmd, **kwargs):
        install_envs.append(kwargs.get("env") or {})

    monkeypatch.setattr(indexer, "find_cbm", lambda: "/fake/codebase-memory-mcp")
    monkeypatch.setattr(indexer.subprocess, "check_output", check_output)
    monkeypatch.setattr(indexer.subprocess, "check_call", check_call)
    return install_envs


def test_ensure_cbm_installed_keeps_a_good_version(monkeypatch):
    install_envs = _fake_installer(monkeypatch, "0.9.1-rc.1")
    assert indexer.ensure_cbm_installed() == "/fake/codebase-memory-mcp"
    assert install_envs == []


def test_ensure_cbm_installed_reinstalls_a_known_bad_version(monkeypatch):
    """0.9.0 silently drops wide directories, so it must be replaced, not trusted."""
    monkeypatch.delenv("CBM_DOWNLOAD_URL", raising=False)
    install_envs = _fake_installer(monkeypatch, "0.9.0")
    assert indexer.ensure_cbm_installed() == "/fake/codebase-memory-mcp"
    assert len(install_envs) == 1
    assert install_envs[0]["CBM_DOWNLOAD_URL"] == indexer.CBM_PINNED_DOWNLOAD_URL


def test_ensure_cbm_installed_respects_download_url_override(monkeypatch):
    monkeypatch.setenv("CBM_DOWNLOAD_URL", "https://example.com/custom")
    install_envs = _fake_installer(monkeypatch, "0.9.0")
    indexer.ensure_cbm_installed()
    assert install_envs[0]["CBM_DOWNLOAD_URL"] == "https://example.com/custom"


def test_parse_query_graph_output_json_format():
    """codebase-memory-mcp <= 0.9.0 prints one JSON object after the log lines."""
    raw = "level=info msg=mem.init\n%s\n" % json.dumps(
        {"columns": ["f.file_path"], "rows": [["a/__manifest__.py"], ["b/x.py"]], "total": 2}
    )
    assert indexer.parse_query_graph_output(raw) == [["a/__manifest__.py"], ["b/x.py"]]


def test_parse_query_graph_output_table_format():
    """codebase-memory-mcp 0.9.1 renders a table; numbers come back double-quoted."""
    raw = (
        "hint: this command started a temporary CBM daemon.\n"
        "rows: 2  (cols: f.file_path)\n"
        "  a/__manifest__.py\n"
        '  "1448"\n'
        "total: 2\n"
    )
    assert indexer.parse_query_graph_output(raw) == [["a/__manifest__.py"], ["1448"]]


def test_parse_query_graph_output_table_empty():
    raw = 'rows: 0  (cols: f.file_path)\ntotal: 0\nhint: "Query returned no results."\n'
    assert indexer.parse_query_graph_output(raw) == []


def test_parse_query_graph_output_unrecognized_raises():
    with pytest.raises(ValueError, match="unrecognized query_graph output"):
        indexer.parse_query_graph_output("something went wrong\n")


def test_indexed_manifest_files_matches_both_manifest_names():
    paths = [
        "odoo/addons/sale/__manifest__.py",
        "odoo/addons/sale/models/sale.py",
        "extra_addons/oca/x/__openerp__.py",
        "extra_addons/oca/x/tools/__manifest__.py.tmpl",
    ]
    assert indexer.indexed_manifest_files(paths) == [
        "odoo/addons/sale/__manifest__.py",
        "extra_addons/oca/x/__openerp__.py",
    ]


def test_validate_scope_clean(monkeypatch, caplog):
    _fake_query_graph(
        monkeypatch,
        [
            "odoo/addons/sale/__manifest__.py",
            "odoo/addons/sale/views/sale.xml",
            "extra_addons/enterprise/quality/__manifest__.py",
            # The core tree is indexed completely by design, so it is out of the comparison.
            "odoo/odoo/addons/base/__manifest__.py",
        ],
    )
    caplog.set_level(logging.INFO)
    assert indexer.validate_scope("instance_12.0", ["odoo/addons/sale", "extra_addons/enterprise/quality"]) is True
    out = _messages(caplog)
    assert "missing_module_roots=0" in out
    assert "extra_indexed_module_roots=0" in out
    assert "extensions_indexed=2 files=4" in out
    assert "unexpected_extensions=0" in out


def test_validate_scope_reports_missing_and_extra(monkeypatch, caplog):
    _fake_query_graph(monkeypatch, ["odoo/addons/sale/__manifest__.py", "odoo/addons/stock/__manifest__.py"])
    caplog.set_level(logging.INFO)
    expected = ["odoo/addons/sale", "extra_addons/enterprise/quality"]
    assert indexer.validate_scope("instance_12.0", expected) is False
    out = _messages(caplog)
    assert "missing extra_addons/enterprise/quality" in out
    assert "extra odoo/addons/stock" in out


def test_validate_scope_rejects_leaked_test_addons(monkeypatch, caplog):
    _fake_query_graph(
        monkeypatch,
        [
            "odoo/addons/test_mail/__manifest__.py",
            "extra_addons/vauxoo/sale/tests/__manifest__.py",
        ],
    )
    caplog.set_level(logging.INFO)
    expected = ["odoo/addons/test_mail", "extra_addons/vauxoo/sale/tests"]
    assert indexer.validate_scope("instance_12.0", expected) is False
    out = _messages(caplog)
    # Only the addons/test_* module leaks; a "tests" folder inside a real module does not.
    assert "test_addon_modules_indexed=1" in out
    assert "leaked odoo/addons/test_mail" in out


def test_outermost_module_roots_drops_nested_manifests():
    """Odoo ships manifests inside real modules; they are files, not modules."""
    roots = [
        "odoo/addons/base_import_module",
        "odoo/addons/base_import_module/tests/test_module",
        "odoo/addons/point_of_sale",
        "odoo/addons/point_of_sale/tools/posbox/overwrite_after_init/home/pi/odoo/addons/point_of_sale",
        "odoo/addons/sale",
    ]
    assert indexer.outermost_module_roots(roots) == {
        "odoo/addons/base_import_module",
        "odoo/addons/point_of_sale",
        "odoo/addons/sale",
    }
    # A sibling whose path merely starts with the same characters is not nested.
    assert indexer.outermost_module_roots(["a/sale", "a/sale_stock"]) == {"a/sale", "a/sale_stock"}


def test_validate_scope_ignores_manifests_nested_in_a_module(monkeypatch, caplog):
    _fake_query_graph(
        monkeypatch,
        [
            "odoo/addons/base_import_module/__manifest__.py",
            "odoo/addons/base_import_module/tests/test_module/__manifest__.py",
        ],
    )
    caplog.set_level(logging.INFO)
    assert indexer.validate_scope("instance_12.0", ["odoo/addons/base_import_module"]) is True
    assert "extra_indexed_module_roots=0" in _messages(caplog)


def test_source_extensions_track_the_cbmignore_globs():
    assert indexer.SOURCE_EXTENSIONS == tuple(sorted(pattern[1:] for pattern in indexer.SOURCE_GLOBS))
    assert ".py" in indexer.SOURCE_EXTENSIONS and "*.py" not in indexer.SOURCE_EXTENSIONS


def test_indexed_extensions_counts_by_path_suffix():
    paths = ["a/b.py", "a/c.py", "a/d.xml", "a/odoo-bin", "a/static/lib/x.min.js"]
    assert indexer.indexed_extensions(paths) == {".py": 2, ".xml": 1, indexer.NO_EXTENSION: 1, ".js": 1}


def test_validate_extensions_rejects_what_cbmignore_should_have_dropped(caplog):
    """A vendored .json or an extensionless binary in the graph means .cbmignore did not apply."""
    paths = ["a/x.py", "a/y.json", "a/z.so", "a/odoo-bin"]
    caplog.set_level(logging.INFO)
    assert indexer.validate_extensions(paths) is False
    out = _messages(caplog)
    assert "unexpected_extensions=3 files=3" in out
    assert "(none),.json,.so" in out
    assert "extension .py      files=1" in out
    assert "extension .json    files=1 (!)" in out


def test_validate_extensions_accepts_the_configured_ones(caplog):
    paths = ["a/x%s" % extension for extension in indexer.SOURCE_EXTENSIONS]
    caplog.set_level(logging.INFO)
    assert indexer.validate_extensions(paths) is True
    assert "unexpected_extensions=0" in _messages(caplog)


def _instance_with_two_repos(tmp_path):
    """Instance where extra_addons/* has more than one repo shipping variables.sh."""
    for repo in ("instance", "vauxoo"):
        folder = tmp_path / "extra_addons" / repo
        folder.mkdir(parents=True)
        # deployv's own format, so the bash and the no-bash readings run on the same input.
        (folder / "variables.sh").write_text('export MAIN_APP="%s"\nexport VERSION="12.0"\n' % repo)
    return str(tmp_path)


def test_source_variables_reads_a_real_variables_sh(tmp_path):
    """Excerpt of a real container file, the format pre_commit_vauxoo.envfile2envdict reads."""
    variables = tmp_path / "variables.sh"
    variables.write_text(
        'export BASE_IMAGE="quay.io/vauxoo/odootds-120-image"\n'
        'export VERSION="12.0"\n'
        'export MAIN_APP="vauxoo"\n'
        "export ODOORC_MAX_CRON_THREADS=4\n"
        'export EXCLUDE="axis_google_2fa_auth,galaxy,gains"\n'
        "# export COMMENTED=nope\n"
        "\n"
    )
    assert indexer.source_variables(str(variables)) == {
        "BASE_IMAGE": "quay.io/vauxoo/odootds-120-image",
        "VERSION": "12.0",
        "MAIN_APP": "vauxoo",
        "ODOORC_MAX_CRON_THREADS": "4",
        "EXCLUDE": "axis_google_2fa_auth,galaxy,gains",
    }


def test_source_variables_missing_file(tmp_path):
    assert indexer.source_variables(str(tmp_path / "nope.sh")) == {}


def test_resolve_project_uses_main_repo_path(tmp_path, monkeypatch):
    root = _instance_with_two_repos(tmp_path)
    monkeypatch.delenv("CBM_PROJECT", raising=False)
    monkeypatch.delenv("MAIN_REPO_FULL_PATH", raising=False)
    monkeypatch.setenv("MAIN_REPO_PATH", os.path.join("extra_addons", "vauxoo"))
    assert indexer._variables_files(root) == [os.path.join(root, "extra_addons", "vauxoo", "variables.sh")]
    assert indexer.resolve_project(root) == "vauxoo_12.0"


def test_resolve_project_globs_without_main_repo_path(tmp_path, monkeypatch):
    root = _instance_with_two_repos(tmp_path)
    monkeypatch.delenv("CBM_PROJECT", raising=False)
    monkeypatch.delenv("MAIN_REPO_FULL_PATH", raising=False)
    monkeypatch.delenv("MAIN_REPO_PATH", raising=False)
    assert len(indexer._variables_files(root)) == 2
    assert indexer.resolve_project(root) == "instance_12.0"


def test_resolve_project_runs_no_subprocess(tmp_path, monkeypatch):
    """variables.sh is parsed, never sourced: bash is what broke the Windows CI job."""
    root = _instance_with_two_repos(tmp_path)
    monkeypatch.delenv("CBM_PROJECT", raising=False)
    monkeypatch.delenv("MAIN_REPO_FULL_PATH", raising=False)
    monkeypatch.setenv("MAIN_REPO_PATH", os.path.join("extra_addons", "vauxoo"))

    def no_subprocess(*args, **kwargs):
        raise AssertionError("resolve_project must not shell out")

    monkeypatch.setattr(indexer.subprocess, "check_output", no_subprocess)
    monkeypatch.setattr(indexer.subprocess, "check_call", no_subprocess)
    assert indexer.resolve_project(root) == "vauxoo_12.0"


def test_resolve_project_falls_back_to_default(tmp_path, monkeypatch):
    root = _instance_with_two_repos(tmp_path)
    monkeypatch.delenv("CBM_PROJECT", raising=False)
    monkeypatch.delenv("MAIN_REPO_FULL_PATH", raising=False)
    monkeypatch.setenv("MAIN_REPO_PATH", os.path.join("extra_addons", "missing"))
    assert indexer.resolve_project(root) == indexer.DEFAULT_PROJECT


def test_check_layout_requires_odoo_bin(tmp_path):
    try:
        indexer.check_layout(str(tmp_path))
    except SystemExit as error:
        assert "odoo-bin" in str(error)
    else:
        raise AssertionError("check_layout must fail outside the container layout")
    odoo_dir = tmp_path / "odoo"
    odoo_dir.mkdir()
    (odoo_dir / "odoo-bin").write_text("")
    assert indexer.check_layout(str(tmp_path)) == os.path.join(str(tmp_path), "odoo", "odoo-bin")
