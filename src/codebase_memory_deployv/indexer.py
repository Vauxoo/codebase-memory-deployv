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
import urllib.request

_logger = logging.getLogger(__name__)

CBM_BIN = "codebase-memory-mcp"
INSTALL_URL = "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh"
# The 0.9.0 discovery walk drops every subdirectory past 512 pending siblings *silently*
# (fixed upstream by 03dc9c91 "grow the walk stack", first shipped in v0.9.1-rc.1):
# extra_addons/enterprise alone holds ~790 sibling modules, so the "latest" stable the
# installer picks indexes exactly 512 of them and reports the rest missing. The pin is
# a floor, not a ceiling: a newer *stable* release that is not known bad wins over it
# (see latest_stable_download_url), and a CBM_DOWNLOAD_URL already in the environment
# wins over both.
CBM_DOWNLOAD_URL_TMPL = "https://github.com/DeusData/codebase-memory-mcp/releases/download/%s"
CBM_PINNED_VERSION = "v0.9.1-rc.1"
CBM_PINNED_DOWNLOAD_URL = CBM_DOWNLOAD_URL_TMPL % CBM_PINNED_VERSION
CBM_LATEST_RELEASE_API = "https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest"
KNOWN_BAD_CBM_VERSIONS = ("0.9.0",)
DEFAULT_ROOT = "/home/odoo/instance"
DEFAULT_BATCH_SIZE = 1200
DEFAULT_PROJECT = "odoo_instance"
CORE_DIRS = ("odoo/odoo",)
CORE_PREFIX = "odoo/odoo/"
SOURCE_GLOBS = ("*.py", "*.xml", "*.js", "*.rst", "*.md", "*.css", "*.scss", "*.csv", "*.sql")
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

# Same expression pre_commit_vauxoo.envfile2envdict uses to read the Vauxoo standard
# variables.sh, kept identical on purpose: it is long proven against the real files.
RE_EXPORT = re.compile(
    r"^(?P<export>export|EXPORT)( )+"
    r"(?P<variable>[\w]*)[ ]*[\=][ ]*[\"\']{0,1}"
    r"(?P<value>[\w\.\-\_/\$\{\}\:,\(\)\#\* ]*)[\"\']{0,1}",
    re.MULTILINE,
)

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


def cbm_version(binary):
    """Return the installed codebase-memory-mcp version, or "" when it cannot be read."""
    try:
        raw = subprocess.check_output([binary, "--version"], universal_newlines=True)
    except (OSError, subprocess.CalledProcessError):
        return ""
    words = raw.strip().splitlines()[-1].split()
    return words[-1] if words else ""


def version_key(version):
    """Sort key for release tags: numeric parts first, then a final release beats its rc.

    "v0.9.1" > "v0.9.1-rc.2" > "v0.9.1-rc.1" > "v0.9.0". Good enough for these tags;
    not a full semver implementation on purpose (no build metadata, no alpha/beta order).
    """
    release, _, prerelease = version.lstrip("v").partition("-")
    numbers = tuple(int(part) for part in release.split(".") if part.isdigit())
    rc = int("".join(char for char in prerelease if char.isdigit()) or 0)
    return (numbers, 0 if prerelease else 1, rc)


def latest_stable_download_url():
    """Return the download URL of the newest usable stable release, or None to use the pin.

    "Usable" means: a real release (the GitHub /releases/latest endpoint never returns
    prereleases), strictly newer than the pinned version, and not a known-bad version.
    Any failure (offline registry, API change) falls back to the pin — indexing must
    keep working without network access to the GitHub API.
    """
    try:
        with urllib.request.urlopen(CBM_LATEST_RELEASE_API, timeout=10) as response:
            release = json.load(response)
    except (OSError, ValueError):
        return None
    tag = release.get("tag_name") or ""
    if not tag or release.get("prerelease"):
        return None
    if tag.lstrip("v") in KNOWN_BAD_CBM_VERSIONS:
        return None
    if version_key(tag) <= version_key(CBM_PINNED_VERSION):
        return None
    return CBM_DOWNLOAD_URL_TMPL % tag


def run_cbm_installer():
    """Run the upstream installer against the newest usable release (pin as fallback)."""
    env = dict(os.environ)
    if "CBM_DOWNLOAD_URL" not in env:
        env["CBM_DOWNLOAD_URL"] = latest_stable_download_url() or CBM_PINNED_DOWNLOAD_URL
    _logger.info("installing codebase-memory-mcp from %s", env["CBM_DOWNLOAD_URL"])
    subprocess.check_call("curl -fsSL %s | bash" % INSTALL_URL, shell=True, env=env)


def ensure_cbm_installed():
    """Install codebase-memory-mcp when missing or when the installed version is known bad.

    A known-bad version is worse than a missing one: it indexes most of the instance and
    silently drops the rest, so the graph looks healthy until validation compares it
    against the disk. Reinstall over it instead of trusting whatever is on PATH.
    """
    found = find_cbm()
    if found:
        version = cbm_version(found)
        if version not in KNOWN_BAD_CBM_VERSIONS:
            return found
        _logger.warning("codebase-memory-mcp %s silently drops wide directories; reinstalling %s", version, CBM_PINNED_DOWNLOAD_URL)
    run_cbm_installer()
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
    for variables_file in candidates:
        variables = source_variables(variables_file)
        main_app, version = variables.get("MAIN_APP"), variables.get("VERSION")
        if main_app and version:
            return "%s_%s" % (main_app, version)
    return DEFAULT_PROJECT


def source_variables(path):
    """Simulate "source variables.sh" and return {variable: value}.

    Same reading pre_commit_vauxoo.envfile2envdict does, so both tools agree on what the
    file means. Parsing beats shelling out to "bash -c source": no subprocess per
    candidate file, and bash is neither guaranteed to exist nor able to take a Windows
    path, which is what made the CI matrix fail there.
    """
    variables = {}
    if not os.path.isfile(path):
        _logger.debug("Skipping 'source %s': not found", path)
        return variables
    with open(path) as source_file:
        for line in source_file:
            match = RE_EXPORT.match(line)
            if match:
                variables[match.group("variable")] = match.group("value")
    return variables


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
    return parse_query_graph_output(raw)


def parse_query_graph_output(raw):
    """Parse both CLI output formats of query_graph into a list of rows.

    codebase-memory-mcp <= 0.9.0 prints one JSON object ({"columns", "rows", "total"});
    0.9.1 renders a human table instead:

        rows: 2  (cols: f.file_path)
          odoo/addons/sale/__manifest__.py
          "1448"
        total: 2
        hint: "..."            (only on empty results)

    Values in the table are space-separated per column, so only single-column queries
    are unambiguous — every query this tool issues returns one column. Numbers come
    back double-quoted; strip one level of quotes to match the JSON format.
    """
    lines = raw.strip().splitlines()
    if not lines:
        return []
    try:
        data = json.loads(lines[-1])
        return data.get("rows") or []
    except ValueError:
        pass
    rows = []
    in_rows = False
    for line in lines:
        if re.match(r"rows: \d+ ", line):
            in_rows = True
            continue
        if line.startswith("total:"):
            break
        if in_rows and line.startswith("  "):
            value = line[2:]
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            rows.append([value])
    if not in_rows:
        raise ValueError("unrecognized query_graph output: %s" % lines[-1][:200])
    return rows


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
