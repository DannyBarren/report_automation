"""
setup_utils.py — backward-compatible re-exports.

Prefer ``utils.setup_weasyprint`` for new code and ``run_demo.bat``.
"""

from utils.setup_weasyprint import (  # noqa: F401
    GTK_INSTALLER_URL,
    WeasyPrintStatus,
    apply_gtk_env,
    bootstrap_weasyprint_for_app,
    check_weasyprint_status,
    discover_gtk_bin_dirs,
    ensure_gtk_runtime_windows,
    ensure_weasyprint_deps,
    ensure_weasyprint_import_ready,
    gtk_runtime_dir,
    is_windows,
    probe_weasyprint_import,
    project_root,
    reset_weasyprint_probe_cache,
)


def main(argv: list[str] | None = None) -> int:
    from utils.setup_weasyprint import main as _main

    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
