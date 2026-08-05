"""Snippet executed inside "odoo-bin shell" to print the installed module paths.

It is read as text by indexer.list_installed_modules_script() and fed to the shell's
stdin, where the Odoo environment provides "self". It is never imported by odoo-bin,
so the guard at the bottom keeps importing this file (pytest, linters) side-effect free.

Blocks stay contiguous and are followed by a blank line: odoo-bin shell may replay the
script through an interactive console, which closes an indented block on a blank line.
"""

import os

DEFAULT_ROOT = "/home/odoo/instance"


def cbm_list_installed_modules(env, root):
    """Print "cbm_module_path=<relative path>" for every installed module under root."""
    modules = env["ir.module.module"].search([("state", "=", "installed")], order="name")
    for module in modules.mapped("name"):
        if module.startswith("test_"):
            continue
        try:
            module_import = __import__("odoo.addons.%s" % module, fromlist=[""])
            module_path = os.path.dirname(module_import.__file__)
        except Exception as exc:  # noqa: BLE001 - a broken addon must not abort the listing
            print("cbm_import_error %s %r" % (module, exc))
            continue
        if module_path.startswith(root + os.sep):
            print("cbm_module_path=%s" % os.path.relpath(module_path, root).replace(os.sep, "/"))
    print("cbm_done=1")


if "self" in dir():  # only true inside odoo-bin shell
    cbm_list_installed_modules(self.env, os.environ.get("CBM_ROOT", DEFAULT_ROOT))  # noqa: F821
