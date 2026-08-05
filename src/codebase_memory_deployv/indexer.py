"""Install codebase-memory-mcp and index Odoo instances living in Vauxoo containers.

The container layout is the one described by the odoo-codebase-memory-docker skill:

    /home/odoo/instance/odoo
    /home/odoo/instance/extra_addons/*

The real instance path is always indexed (never an rsync/copy) so later code
references and edits target the right files.
"""

import csv
import json
import logging
import os
import re
import shutil
import subprocess

_logger = logging.getLogger(__name__)

CBM_BIN = "codebase-memory-mcp"
INSTALL_URL = "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh"
DEFAULT_ROOT = "/home/odoo/instance"
DEFAULT_BATCH_SIZE = 1200
DEFAULT_PROJECT = "odoo_instance"
CORE_DIRS = ("odoo/odoo",)
CORE_PREFIX = "odoo/odoo/"
SOURCE_GLOBS = ("*.py", "*.xml", "*.js", "*.rst", "*.md", "*.css", "*.scss", "*.csv")
# What .cbmignore lets through, so what the graph is expected to hold: ".py", ".xml", ...
SOURCE_EXTENSIONS = tuple(sorted(pattern[1:] for pattern in SOURCE_GLOBS))
MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")
PRUNE_DIRS = {".git", ".github", "__pycache__", "node_modules", ".cache", ".tx", "dist", "build", "setup"}
CBMIGNORE_BACKUP_SUFFIX = ".before-codebase-memory-deployv"

# Fed to "odoo-bin shell" as text (it needs the shell's "self"), so it ships as a real
# module of the package instead of a string constant: black/pytest see it like any file.
LIST_INSTALLED_MODULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "list_installed_modules.py")

# Column names accepted in a modules file: an ir.module.module export uses the technical
# names when exporting fields, and the labels when exporting through the UI translations.
MODULES_FILE_NAME_COLUMNS = ("name", "technical name", "module")
MODULES_FILE_STATE_COLUMNS = ("state", "status")
INSTALLED_STATE = "installed"

# deployv writes 'export MAIN_APP="name"' lines, read when bash cannot source the file.
VARIABLES_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(MAIN_APP|VERSION)\s*=\s*(.*?)\s*$")

# query_graph refuses to return more rows than this, so a bigger result set is truncated.
QUERY_GRAPH_MAX_ROWS = 100000
NO_EXTENSION = "(none)"


def list_installed_modules_script():
    """Return the source fed to odoo-bin shell to list the installed modules."""
    with open(LIST_INSTALLED_MODULES_PATH) as script:
        return script.read()


def check_layout(root):
    """Fail fast when not running inside a container with the expected layout."""
    odoo_bin = os.path.join(root, "odoo", "odoo-bin")
    if not os.path.isfile(odoo_bin):
        raise SystemExit(
            "%s not found. This tool only runs inside a container following the "
            "/home/odoo/instance layout (odoo + extra_addons)." % odoo_bin
        )
    return odoo_bin


def find_cbm():
    """Locate the codebase-memory-mcp binary, extending PATH with common install dirs."""
    found = shutil.which(CBM_BIN)
    if found:
        return found
    for folder in (os.path.expanduser("~/.local/bin"), os.path.expanduser("~/bin"), "/usr/local/bin"):
        candidate = os.path.join(folder, CBM_BIN)
        if os.access(candidate, os.X_OK):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            return candidate
    return None


def ensure_cbm_installed():
    """Install codebase-memory-mcp in the local environment when missing."""
    found = find_cbm()
    if found:
        return found
    subprocess.check_call("curl -fsSL %s | bash" % INSTALL_URL, shell=True)
    found = find_cbm()
    if not found:
        raise SystemExit("codebase-memory-mcp still not found after running the installer")
    return found


def resolve_project(root):
    """Derive the project name from the instance variables.sh (MAIN_APP_VERSION).

    Never hardcode it, so each container/customer gets its own stable graph.
    """
    if os.environ.get("CBM_PROJECT"):
        return os.environ["CBM_PROJECT"]
    candidates = []
    main_repo = os.environ.get("MAIN_REPO_FULL_PATH")
    if main_repo:
        candidates.append(os.path.join(main_repo, "variables.sh"))
    candidates.extend(sorted(_variables_files(root)))
    for variables in candidates:
        if not os.path.isfile(variables):
            continue
        main_app, version = read_variables(variables)
        if main_app and version:
            return "%s_%s" % (main_app, version)
    return DEFAULT_PROJECT


def read_variables(path):
    """Return (MAIN_APP, VERSION) from a variables.sh.

    Sourcing it with bash is the accurate reading, because a value may reference another
    variable, so that is tried first. The plain assignment parse is the fallback for a
    platform without a usable bash: deployv writes literal 'export KEY="value"' lines,
    and it is also what keeps this testable outside the Linux containers.
    """
    try:
        output = subprocess.check_output(
            ["bash", "-c", '. "%s" >/dev/null 2>&1; printf "%%s|%%s" "$MAIN_APP" "$VERSION"' % path],
            universal_newlines=True,
        )
        main_app, _, version = output.partition("|")
        if main_app and version:
            return main_app, version
    except (subprocess.CalledProcessError, OSError):
        pass
    values = {}
    with open(path) as variables:
        for line in variables:
            match = VARIABLES_ASSIGNMENT.match(line)
            if match:
                values[match.group(1)] = match.group(2).strip("\"'")
    return values.get("MAIN_APP", ""), values.get("VERSION", "")


def _variables_files(root):
    """variables.sh of the main repo, falling back to every repo under extra_addons.

    deployv exports MAIN_REPO_PATH (e.g. "extra_addons/vauxoo") in the container, so it
    points at the one repo that owns MAIN_APP/VERSION. Globbing extra_addons/* is
    ambiguous: more than one repo can ship its own variables.sh and the sorted order
    would pick an arbitrary project name.
    """
    import glob

    main_repo_path = os.environ.get("MAIN_REPO_PATH")
    if main_repo_path:
        return [os.path.join(root, main_repo_path, "variables.sh")]
    return glob.glob(os.path.join(root, "extra_addons", "*", "variables.sh"))


def run_odoo_shell(root, script):
    """Feed a script to odoo-bin shell and return its combined output ("" on failure)."""
    odoo_bin = os.path.join(root, "odoo", "odoo-bin")
    env = dict(os.environ)
    env["CBM_ROOT"] = root
    try:
        proc = subprocess.run(
            [odoo_bin, "shell", "--no-http", "--stop-after-init", "--log-level=warn"],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=env,
            cwd=root,
        )
    except OSError:
        return ""
    return proc.stdout or ""


def installed_modules(root):
    """Return installed module paths via odoo-bin shell, or None when no database is reachable."""
    output = run_odoo_shell(root, list_installed_modules_script())
    if "cbm_done=1" not in output:
        return None
    paths = set()
    for line in output.splitlines():
        if line.startswith("cbm_module_path="):
            paths.add(line.split("=", 1)[1].strip())
        elif line.startswith("cbm_import_error"):
            _logger.warning("%s", line)
    return sorted(paths)


def discover_modules(root):
    """Return every addon module found on disk (no database mode), excluding test_* addons.

    The odoo/odoo core tree is skipped here because .cbmignore always includes it completely.
    """
    core = os.path.join(root, "odoo", "odoo")
    found = set()
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in PRUNE_DIRS and not name.startswith(".")]
        if current == core:
            dirs[:] = []
            continue
        if any(name in files for name in MANIFEST_NAMES):
            dirs[:] = []
            if os.path.basename(current).startswith("test_"):
                continue
            found.add(os.path.relpath(current, root).replace(os.sep, "/"))
    return sorted(found)


def _columns(row):
    """Indexes of the name/state columns when the row is a header, else (None, None)."""
    cells = [cell.strip().strip('"').lower() for cell in row]
    name_index = next((index for index, cell in enumerate(cells) if cell in MODULES_FILE_NAME_COLUMNS), None)
    if name_index is None:
        return None, None
    state_index = next((index for index, cell in enumerate(cells) if cell in MODULES_FILE_STATE_COLUMNS), None)
    return name_index, state_index


def read_modules_file(path):
    """Return the module names listed in an ir.module.module export (csv or plain list).

    Production usually has modules installed by hand that the local docker container does
    not install, so its module list can be exported and passed instead of asking the local
    database. A "state" column keeps only the installed rows; without one the file is
    assumed to be already filtered, which is what a manual export looks like.
    """
    with open(path) as handler:
        rows = list(csv.reader(line for line in handler if line.strip() and not line.lstrip().startswith("#")))
    if not rows:
        return []
    name_index, state_index = _columns(rows[0])
    if name_index is None:
        # Headerless: one module name per line, optionally followed by its state.
        name_index, state_index = 0, 1 if len(rows[0]) > 1 else None
    else:
        rows = rows[1:]
    names = set()
    for row in rows:
        if len(row) <= name_index:
            continue
        if state_index is not None:
            state = row[state_index].strip().lower() if len(row) > state_index else ""
            if state != INSTALLED_STATE:
                continue
        name = row[name_index].strip()
        if name and not name.startswith("test_"):
            names.add(name)
    return sorted(names)


def modules_from_names(root, names):
    """Map module names to their paths on disk; return (module paths, names not found)."""
    paths_by_name = {}
    for rel in discover_modules(root):
        paths_by_name.setdefault(os.path.basename(rel), []).append(rel)
    paths = set()
    unknown = []
    for name in names:
        found = paths_by_name.get(name)
        if not found:
            unknown.append(name)
            continue
        # Several repos may ship the same module name; without the database the effective
        # addons_path order is unknown, so keep every copy rather than guessing one.
        paths.update(found)
    return sorted(paths), unknown


def cumulative_batches(module_paths, batch_size):
    """Split sorted module paths into cumulative selections: [0:n], [0:2n], ...

    Cumulative on purpose so modules indexed in earlier passes stay in scope.
    A non-positive batch_size means a single one-shot pass.
    """
    ordered = sorted(module_paths)
    if batch_size <= 0 or batch_size >= len(ordered):
        return [ordered]
    return [ordered[:end] for end in range(batch_size, len(ordered) + batch_size, batch_size)]


def render_cbmignore(module_rel_paths):
    """Render a .cbmignore keeping only the Odoo core tree plus the given modules."""
    patterns = []
    seen = set()

    def add(line):
        if line not in seen:
            seen.add(line)
            patterns.append(line)

    def include_tree(rel):
        parts = rel.split("/")
        cur = ""
        for part in parts:
            cur = part if not cur else cur + "/" + part
            add("!%s/" % cur)
        add("!%s/**/" % rel)
        for glob_pattern in SOURCE_GLOBS:
            add("!%s/**/%s" % (rel, glob_pattern))

    add("# Generated by codebase-memory-deployv from Odoo modules.")
    add("# Keep repo_path=%s and project name stable, e.g. --name=$PROJECT." % DEFAULT_ROOT)
    add("")
    add("*")
    add("")
    add("# Odoo core package. Keep it complete for ORM/http/sql/api internals.")
    for rel in CORE_DIRS:
        include_tree(rel)
    add("")
    add("# Drop Odoo test addon modules, but keep unittest folders inside real modules.")
    for line in [
        "odoo/odoo/addons/test_*/",
        "odoo/odoo/addons/test_*/**",
        "odoo/addons/test_*/",
        "odoo/addons/test_*/**",
    ]:
        add(line)
    add("")
    add("# Selected Odoo addons.")
    for rel in sorted(module_rel_paths):
        include_tree(rel)
    add("")
    add("# Heavy/generated/vendor paths.")
    for line in [
        ".git/",
        "**/.git/",
        ".github/",
        "**/.github/",
        "__pycache__/",
        "**/__pycache__/",
        "node_modules/",
        "**/node_modules/",
        ".cache/",
        "**/.cache/",
        ".tx/",
        "**/.tx/",
        "dist/",
        "**/dist/",
        "build/",
        "**/build/",
        "**/static/lib/",
        "**/static/lib/**",
        "**/static/tests/",
        "**/static/tests/**",
        "**/*.min.js",
        "**/*-min.js",
    ]:
        add(line)
    return "\n".join(patterns).rstrip() + "\n"


def write_cbmignore(root, content):
    """Write .cbmignore in the instance root, backing up any pre-existing file once."""
    path = os.path.join(root, ".cbmignore")
    backup = path + CBMIGNORE_BACKUP_SUFFIX
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copyfile(path, backup)
    with open(path, "w") as fh:
        fh.write(content)
    return path


def index_repository(root, project):
    """Run one indexing pass reusing the same project name for every pass."""
    subprocess.check_call(
        [find_cbm() or CBM_BIN, "cli", "index_repository", "--repo_path", root, "--name=%s" % project]
    )


def query_graph(project, query):
    """Return the rows of a Cypher query against the project graph.

    Not "search_code --mode=files": that tool greps the indexed files and hard-caps the
    scan at 500 matched *lines* whatever --limit says, so on a real instance (Odoo 19
    alone ships 533 manifests) it silently drops the tail of the module list and every
    module in it gets reported as missing. query_graph reads the graph itself instead.
    """
    cmd = [find_cbm() or CBM_BIN, "cli", "query_graph", "--project", project, "--query", query]
    raw = subprocess.check_output(cmd, universal_newlines=True)
    data = json.loads(raw.strip().splitlines()[-1])
    return data.get("rows") or []


def indexed_files(project):
    """Every file path in the graph, the one File property that can be trusted.

    Neither f.name nor f.extension is usable: the indexer stores the basename on some
    File nodes and the whole relative path on others (leaving extension empty). On a real
    instance that split 1238 of the manifests one way and the rest the other, so a
    "WHERE f.name = '__manifest__.py'" query reported perfectly indexed modules as
    missing. Read f.file_path once and derive names/extensions here.
    """
    rows = query_graph(project, "MATCH (f:File) RETURN f.file_path")
    paths = [row[0] for row in rows if row and row[0]]
    if len(paths) >= QUERY_GRAPH_MAX_ROWS:
        _logger.warning("indexed_files=%d hit the query_graph row ceiling; results are truncated", len(paths))
    return paths


def indexed_manifest_files(paths):
    """Manifest paths among the given indexed file paths."""
    return [path for path in paths if path.rpartition("/")[2] in MANIFEST_NAMES]


def indexed_extensions(paths):
    """Return {extension: file count} for the given indexed file paths."""
    counts = {}
    for path in paths:
        extension = os.path.splitext(path)[1] or NO_EXTENSION
        counts[extension] = counts.get(extension, 0) + 1
    return counts


def validate_extensions(paths):
    """Prove the graph only holds the extensions .cbmignore lets through.

    Anything else means .cbmignore did not apply (stale file, pattern typo, indexing run
    from another root), which silently bloats the graph with vendored/generated content.
    """
    counts = indexed_extensions(paths)
    unexpected = {ext: total for ext, total in counts.items() if ext not in SOURCE_EXTENSIONS}
    _logger.info("extensions_configured=%s", ",".join(SOURCE_EXTENSIONS))
    _logger.info("extensions_indexed=%d files=%d", len(counts), sum(counts.values()))
    for extension, total in sorted(counts.items(), key=lambda item: -item[1]):
        _logger.info("extension %-8s files=%d%s", extension, total, "" if extension in SOURCE_EXTENSIONS else " (!)")
    if unexpected:
        _logger.error(
            "unexpected_extensions=%d files=%d (not in SOURCE_GLOBS: %s)",
            len(unexpected),
            sum(unexpected.values()),
            ",".join(sorted(unexpected)),
        )
        return False
    _logger.info("unexpected_extensions=0")
    return True


def outermost_module_roots(module_rel_paths):
    """Drop module roots nested inside another one.

    Odoo ships manifests *inside* real modules: the base_import_module test fixture, the
    point_of_sale posbox overwrite_after_init overlay. Those are files of their parent
    module, not modules of their own — discover_modules prunes at the first manifest for
    the same reason, so the comparison has to prune the graph side too.
    """
    kept = []
    for rel in sorted(module_rel_paths):
        if kept and rel.startswith(kept[-1] + "/"):
            continue
        kept.append(rel)
    return set(kept)


def _is_test_addon(module_rel_path):
    """True for Odoo test addon modules ("<...>/addons/test_*"), not for test folders."""
    parent, _, name = module_rel_path.rpartition("/")
    return name.startswith("test_") and (parent == "addons" or parent.endswith("/addons"))


def validate_scope(project, expected_module_paths):
    """Prove the graph scope matches the expected modules; return True when clean.

    Addons are enumerated through their manifests: every addon has exactly one, so the
    manifests in the graph are the modules in the graph. The odoo/odoo core tree is
    excluded from the comparison: it is included completely by design.
    """
    ok = True
    paths = indexed_files(project)
    indexed_roots = outermost_module_roots(path.rpartition("/")[0] for path in indexed_manifest_files(paths))
    leaked_tests = sorted(rel for rel in indexed_roots if _is_test_addon(rel))
    if leaked_tests:
        ok = False
        _logger.error("test_addon_modules_indexed=%d (must be 0)", len(leaked_tests))
        for rel in leaked_tests[:20]:
            _logger.error("leaked %s", rel)
    indexed_cmp = {rel for rel in indexed_roots if not rel.startswith(CORE_PREFIX)}
    expected_cmp = {rel for rel in expected_module_paths if not rel.startswith(CORE_PREFIX)}
    missing = sorted(expected_cmp - indexed_cmp)
    extra = sorted(indexed_cmp - expected_cmp)
    _logger.info("module_roots_expected=%d", len(expected_cmp))
    _logger.info("module_roots_indexed=%d", len(indexed_cmp))
    _logger.info("missing_module_roots=%d", len(missing))
    _logger.info("extra_indexed_module_roots=%d", len(extra))
    for rel in missing[:50]:
        _logger.error("missing %s", rel)
    for rel in extra[:50]:
        _logger.error("extra %s", rel)
    # Extensions are checked last: the module comparison is the headline, this one tells
    # whether .cbmignore really applied to whatever did get indexed.
    return validate_extensions(paths) and ok and not missing and not extra
