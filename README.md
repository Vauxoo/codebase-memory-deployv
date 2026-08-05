# codebase-memory-deployv

Install [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) and index Odoo
instances **by parts** inside containers following the Vauxoo layout:

```text
/home/odoo/instance/odoo
/home/odoo/instance/extra_addons/*
```

This tool only runs inside such a container (or SSH session) as user `odoo`; it refuses to start
when `/home/odoo/instance/odoo/odoo-bin` is missing. It always indexes the real instance path —
never an rsync/copy — so later code references and edits target the right files.

## Install

```bash
pip install codebase-memory-deployv
```

## Usage

```bash
codebase-memory-deployv
```

That single command:

1. Installs `codebase-memory-mcp` in the local environment when it is not available yet.
2. Resolves a stable project name from `${MAIN_REPO_FULL_PATH}/variables.sh`, or from
   `${MAIN_REPO_PATH}/variables.sh` relative to `--repo-path`
   (`PROJECT=${MAIN_APP}_${VERSION}`, e.g. `vauxoo_12.0`), so each container/customer gets its
   own graph and every pass reuses the same `--name`. Both variables are exported by deployv and
   point at the one repo that owns `MAIN_APP`/`VERSION`; only when neither is set does it fall
   back to globbing `extra_addons/*/variables.sh`, which is ambiguous when several repos ship one.
3. Picks the module list, in this order:
   - **`--modules-file` given** → the modules listed there (see below).
   - **Database reachable** through `odoo-bin shell` → only installed modules from
     `ir.module.module` (no `test_*` addon modules, keeping backend unit tests inside real
     modules), listed by `list_installed_modules.py` which is fed to the shell.
   - **Neither** → every addon module found on disk.
4. Renders `.cbmignore` (Odoo core `odoo/odoo` complete + selected modules, dropping
   `static/lib`, `static/tests`, minified JS, caches and `.git`) in **cumulative batches of 25
   modules**, running `codebase-memory-mcp cli index_repository` after each batch so memory-killed
   one-shot indexing is never a problem.
5. Validates the graph scope: no `test_*` addon modules indexed, no missing and no extra module
   roots compared to the expected module list, and **every indexed file carrying one of the
   configured extensions** (`SOURCE_GLOBS`: `.py .xml .js .rst .md .css .scss .csv`) — anything
   else means `.cbmignore` did not apply. Counts per extension are logged, unexpected ones marked
   `(!)`:

   ```text
   extensions_configured=.css,.csv,.js,.md,.py,.rst,.scss,.xml
   extensions_indexed=5 files=12057
   extension .py      files=4949
   extension .xml     files=3276
   extension .json    files=39 (!)
   unexpected_extensions=1 files=39 (not in SOURCE_GLOBS: .json)
   ```

   Both checks read the graph with `query_graph` over its `File` nodes, not with `search_code`:
   that one greps the indexed files and hard-caps the scan at 500 matched lines whatever
   `--limit` says, which reports the tail of a real instance (Odoo 19 alone ships 533 manifests)
   as missing.

## Options

```text
--repo-path PATH    instance root to index (default: /home/odoo/instance)
--project NAME      graph name; default derives MAIN_APP_VERSION from variables.sh
--batch-size N      modules per cumulative indexing pass; 0 indexes one-shot (default: 25)
--mode MODE         auto (default), installed, or all
--modules-file PATH ir.module.module export listing the modules to index
--skip-install      do not install codebase-memory-mcp when missing
--skip-validate     do not validate graph scope after indexing
```

## Indexing the modules installed in production

A local docker container only installs the modules of the main app, while production usually has
more modules installed by hand. Export `ir.module.module` from production and pass the file, so
the graph covers the same code production runs:

```bash
codebase-memory-deployv --modules-file modules_installed.csv
```

The file is an `ir.module.module` export, either a csv or one module name per line
(`#` comments and blank lines are ignored):

```csv
name,state
sale,installed
purchase,uninstalled
```

- With a `state` column (`state`/`status`), only the `installed` rows are used.
- Without one, the file is assumed to be already filtered to the installed modules.
- Column headers may be the technical names (`name`, `state`) or the exported labels
  (`Technical Name`, `Status`); a headerless file is read as `name[,state]`.
- Modules listed but missing on disk are reported as `not_on_disk` and skipped — they simply are
  not in this container's repos.

## Development

```bash
tox -e py-multi   # tests in parallel
tox -e lint       # pre-commit checks
tox -e build      # release rehearsal (sdist+wheel, twine check, install smoke test)
```
