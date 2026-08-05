"""Command line entry point: install codebase-memory-mcp and index the Odoo instance by parts."""

import argparse
import logging
import os
import sys

from . import __version__
from .indexer import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_ROOT,
    check_layout,
    cumulative_batches,
    discover_modules,
    ensure_cbm_installed,
    index_repository,
    installed_modules,
    modules_from_names,
    read_modules_file,
    render_cbmignore,
    resolve_project,
    validate_scope,
    write_cbmignore,
)

_logger = logging.getLogger(__name__)
PACKAGE_LOGGER = __name__.rsplit(".", 1)[0]


def setup_logging(verbose=False):
    """Send this package's logs to stdout as bare messages (the output is key=value).

    The handler is rebuilt on every call so a second main() in the same process logs to
    the current sys.stdout instead of a stream captured by the first one.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def build_parser():
    parser = argparse.ArgumentParser(
        prog="codebase-memory-deployv",
        description=(
            "Install codebase-memory-mcp and index an Odoo instance from %s. "
            "The module list comes from --modules-file when given, else from the local database "
            "(via odoo-bin shell), else from every module found on disk." % DEFAULT_ROOT
        ),
    )
    parser.add_argument("--repo-path", default=DEFAULT_ROOT, help="instance root to index (default: %(default)s)")
    parser.add_argument(
        "--project", default=None, help="graph name; default derives MAIN_APP_VERSION from variables.sh"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="modules per cumulative indexing pass; 0 indexes one-shot (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "installed", "all"],
        default="auto",
        help="auto: installed modules when a database answers, everything otherwise",
    )
    parser.add_argument(
        "--modules-file",
        default=None,
        help=(
            "ir.module.module export (csv 'name,state' or one module name per line) used instead of "
            "the local database; production usually has more modules installed than a local container. "
            "Without a state column the file is assumed to list installed modules only. Ignored by --mode=all"
        ),
    )
    parser.add_argument("--skip-install", action="store_true", help="do not install codebase-memory-mcp when missing")
    parser.add_argument("--skip-validate", action="store_true", help="do not validate graph scope after indexing")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug messages too")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def _modules_from_file(root, modules_file):
    """Module paths listed by an ir.module.module export, reporting what is not on disk."""
    names = read_modules_file(modules_file)
    modules, unknown = modules_from_names(root, names)
    _logger.info(
        "modules_file=%s names=%d on_disk=%d not_on_disk=%d", modules_file, len(names), len(modules), len(unknown)
    )
    for name in unknown[:50]:
        _logger.warning("not_on_disk %s", name)
    if not modules:
        raise SystemExit("%s does not list any module available under %s" % (modules_file, root))
    return modules


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    root = os.path.abspath(args.repo_path)
    check_layout(root)
    if not args.skip_install:
        ensure_cbm_installed()
    project = args.project or resolve_project(root)
    _logger.info("project=%s", project)

    mode = args.mode
    modules = None
    if mode in ("auto", "installed") and args.modules_file:
        modules = _modules_from_file(root, args.modules_file)
        mode = "file"
    elif mode in ("auto", "installed"):
        modules = installed_modules(root)
        if modules is None:
            if mode == "installed":
                raise SystemExit("No database reachable through odoo-bin shell; use --mode=all to index everything")
            _logger.warning("database=not-reachable")
            mode = "all"
        else:
            mode = "installed"
    elif args.modules_file:
        _logger.warning("modules_file=ignored (--mode=all indexes every module on disk)")
    if mode == "all":
        modules = discover_modules(root)
    _logger.info("mode=%s modules=%d", mode, len(modules))

    for number, selected in enumerate(cumulative_batches(modules, args.batch_size), 1):
        _logger.info(
            "=== batch=%d modules_rendered=%d/%d project=%s ===", number, len(selected), len(modules), project
        )
        write_cbmignore(root, render_cbmignore(selected))
        index_repository(root, project)

    if not args.skip_validate:
        if not validate_scope(project, modules):
            raise SystemExit("Validation failed: fix .cbmignore, re-index and re-run validation")
        _logger.info("validation=ok")
    return 0
