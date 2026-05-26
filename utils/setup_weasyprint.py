"""
setup_weasyprint.py — Windows GTK3 / WeasyPrint bootstrap (portable-first, no admin).

Preferred path: download the official GTK runtime archive and **extract** it into
``./gtk3/runtime`` (no elevation). Silent ``.exe`` install is only attempted when
portable extraction fails **and** the process is already elevated.

Used by ``run_demo.bat``, ``app.py``, and ``/setup/weasyprint``.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("jobdoc.weasyprint")

# Official Tom Schoonjans GTK3 runtime (64-bit) — same family WeasyPrint docs recommend.
GTK_INSTALLER_VERSION_TAG = "2022-01-04"
GTK_INSTALLER_FILENAME = "gtk3-runtime-3.24.31-2022-01-04-ts-win64.exe"
GTK_INSTALLER_URL = (
    "https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/"
    f"releases/download/{GTK_INSTALLER_VERSION_TAG}/{GTK_INSTALLER_FILENAME}"
)
GTK_MANUAL_RELEASE_URL = (
    "https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/"
    f"releases/tag/{GTK_INSTALLER_VERSION_TAG}"
)

PANGO_DLL_MARKERS = (
    "libpangoft2-1.0-0.dll",
    "libpango-1.0-0.dll",
    "libcairo-2.dll",
)

SYSTEM_GTK_DIR_NAMES = (
    "GTK3-Runtime Win64",
    "GTK3-Runtime",
)

_weasyprint_import_ok: bool | None = None
_weasyprint_import_error: str = ""


@dataclass
class WeasyPrintStatus:
    """Readiness probe for /health and the setup page."""

    ready: bool
    platform: str
    message: str
    gtk_bin_dirs: list[str] = field(default_factory=list)
    gtk_runtime_path: str = ""
    auto_install_attempted: bool = False
    auto_install_succeeded: bool = False
    portable_extract_attempted: bool = False
    portable_extract_succeeded: bool = False
    installer_attempted: bool = False
    is_elevated: bool = False
    prefer_portable: bool = True
    install_hint: str = ""
    docs_url: str = "https://doc.courtbouillon.org/weasyprint/stable/first_steps.html"
    manual_release_url: str = GTK_MANUAL_RELEASE_URL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_root(start: Path | None = None) -> Path:
    return (start or Path(__file__).resolve().parent.parent).resolve()


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_process_elevated() -> bool:
    """True when the current Windows process has administrator rights."""
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def gtk_project_dir(root: Path | None = None) -> Path:
    return project_root(root) / "gtk3"


def gtk_runtime_dir(root: Path | None = None) -> Path:
    return gtk_project_dir(root) / "runtime"


def gtk_download_cache(root: Path | None = None) -> Path:
    return gtk_project_dir(root) / "downloads"


def gtk_extract_staging_dir(root: Path | None = None) -> Path:
    return gtk_project_dir(root) / "_extract_staging"


def _dir_has_pango_dll(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    names = {p.name.lower() for p in directory.glob("*.dll")}
    return any(marker.lower() in names for marker in PANGO_DLL_MARKERS)


def discover_gtk_bin_dirs(root: Path | None = None) -> list[Path]:
    """Find directories that contain Pango/Cairo DLLs (project-local then system)."""
    root = project_root(root)
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        key = str(resolved).lower()
        if key in seen:
            return
        if _dir_has_pango_dll(resolved):
            seen.add(key)
            found.append(resolved)

    base = gtk_runtime_dir(root)
    if base.is_dir():
        add(base / "bin")
        add(base)
        for child in base.rglob("bin"):
            if child.is_dir():
                add(child)

    if is_windows():
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base_pf = os.environ.get(env_name)
            if not base_pf:
                continue
            for name in SYSTEM_GTK_DIR_NAMES:
                add(Path(base_pf) / name / "bin")

    existing = os.environ.get("WEASYPRINT_DLL_DIRECTORIES", "")
    if existing:
        for part in existing.split(os.pathsep):
            part = part.strip()
            if part:
                add(Path(part))

    return found


def apply_gtk_env(bin_dirs: list[Path]) -> None:
    """Set env vars so WeasyPrint/cffi can load GTK DLLs (current process)."""
    if not bin_dirs:
        return
    str_dirs = [str(p.resolve()) for p in bin_dirs]
    os.environ["WEASYPRINT_DLL_DIRECTORIES"] = os.pathsep.join(str_dirs)
    path_prefix = os.pathsep.join(str_dirs)
    current_path = os.environ.get("PATH", "")
    if path_prefix and path_prefix not in current_path:
        os.environ["PATH"] = path_prefix + (os.pathsep + current_path if current_path else "")


def probe_weasyprint_import() -> tuple[bool, str]:
    """Try importing WeasyPrint after ``apply_gtk_env`` (cached)."""
    global _weasyprint_import_ok, _weasyprint_import_error
    if _weasyprint_import_ok is not None:
        return _weasyprint_import_ok, _weasyprint_import_error
    try:
        import weasyprint  # noqa: F401

        _weasyprint_import_ok = True
        _weasyprint_import_error = ""
    except Exception as exc:  # noqa: BLE001
        _weasyprint_import_ok = False
        _weasyprint_import_error = str(exc)
    return _weasyprint_import_ok, _weasyprint_import_error


def reset_weasyprint_probe_cache() -> None:
    global _weasyprint_import_ok, _weasyprint_import_error
    _weasyprint_import_ok = None
    _weasyprint_import_error = ""


def _prefer_portable_install() -> bool:
    val = os.environ.get("JOBDOC_PREFER_PORTABLE_GTK", "1").strip().lower()
    return val in ("1", "true", "yes", "on")


def _allow_silent_installer() -> bool:
    """Silent NSIS install needs elevation on many PCs — opt-in unless already admin."""
    if is_process_elevated():
        return True
    return os.environ.get("JOBDOC_ALLOW_GTK_INSTALLER", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def find_7zip_executable() -> Path | None:
    """Locate 7-Zip ``7z.exe`` (common install path or PATH)."""
    if not is_windows():
        return None
    which = shutil.which("7z") or shutil.which("7z.exe")
    if which:
        return Path(which)
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        candidate = Path(base) / "7-Zip" / "7z.exe"
        if candidate.is_file():
            return candidate
    return None


def _download_gtk_installer(root: Path, verbose: bool = False) -> Path:
    cache_dir = gtk_download_cache(root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / GTK_INSTALLER_FILENAME
    if target.is_file() and target.stat().st_size > 1_000_000:
        if verbose:
            print(f"  Using cached package: {target}")
        logger.info("Using cached GTK package at %s", target)
        return target

    if verbose:
        print(f"  Downloading GTK3 runtime (~49 MB, one-time)...")
        print(f"  {GTK_INSTALLER_URL}")
    logger.info("Downloading GTK package from %s", GTK_INSTALLER_URL)

    tmp = target.with_suffix(".exe.part")
    try:
        urllib.request.urlretrieve(GTK_INSTALLER_URL, tmp)  # noqa: S310
        tmp.replace(target)
    except (urllib.error.URLError, OSError) as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Download failed: {exc}") from exc

    if verbose:
        print(f"  Saved to {target}")
    logger.info("GTK package saved to %s", target)
    return target


def _normalize_extracted_tree(staging: Path, runtime_root: Path, verbose: bool = False) -> bool:
    """
    Move extracted GTK files into ``gtk3/runtime``.

    The NSIS/7z layout varies; we copy the first tree that contains Pango DLLs.
    """
    runtime_root.mkdir(parents=True, exist_ok=True)

    # If staging already has bin/ with DLLs, use it directly.
    candidates: list[Path] = []
    if _dir_has_pango_dll(staging / "bin"):
        candidates.append(staging)
    for bin_dir in staging.rglob("bin"):
        if bin_dir.is_dir() and _dir_has_pango_dll(bin_dir):
            candidates.append(bin_dir.parent)

    if not candidates:
        return False

    # Prefer shallowest match (usually $INSTDIR at top of extract).
    candidates.sort(key=lambda p: len(p.parts))
    source = candidates[0]

    if verbose:
        print(f"  Installing portable GTK from {source} -> {runtime_root}")

    if runtime_root.exists():
        shutil.rmtree(runtime_root, ignore_errors=True)
    runtime_root.parent.mkdir(parents=True, exist_ok=True)

    if source.resolve() == runtime_root.resolve():
        return True

    shutil.copytree(source, runtime_root, dirs_exist_ok=True)
    logger.info("Portable GTK copied to %s", runtime_root)
    return bool(discover_gtk_bin_dirs(runtime_root.parent.parent))


def extract_gtk_portable(
    installer: Path,
    root: Path,
    *,
    verbose: bool = False,
) -> bool:
    """
    Extract GTK DLLs from the downloaded runtime package without running the installer.

    Uses 7-Zip if available, otherwise ``py7zr`` (pure Python, no elevation).
    """
    staging = gtk_extract_staging_dir(root)
    runtime_root = gtk_runtime_dir(root)

    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    seven_z = find_7zip_executable()
    if seven_z:
        if verbose:
            print(f"  Extracting with 7-Zip ({seven_z}) — no administrator required...")
        logger.info("Portable extract via 7z: %s", seven_z)
        cmd = [
            str(seven_z),
            "x",
            "-y",
            f"-o{staging}",
            str(installer),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
        except OSError as exc:
            if verbose:
                print(f"  7-Zip failed: {exc}")
            logger.warning("7z extract failed: %s", exc)
        else:
            if proc.returncode == 0 and _normalize_extracted_tree(staging, runtime_root, verbose):
                shutil.rmtree(staging, ignore_errors=True)
                return True
            if verbose and proc.returncode != 0:
                print(f"  7-Zip exit code {proc.returncode}")

    try:
        import py7zr
    except ImportError:
        if verbose:
            print("  py7zr not installed — run: pip install py7zr")
        logger.info("py7zr not available for portable extract")
    else:
        if verbose:
            print("  Extracting with py7zr (portable, no admin)...")
        logger.info("Portable extract via py7zr")
        try:
            with py7zr.SevenZipFile(installer, mode="r") as archive:
                archive.extractall(path=staging)
            if _normalize_extracted_tree(staging, runtime_root, verbose):
                shutil.rmtree(staging, ignore_errors=True)
                return True
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  py7zr extract failed: {exc}")
            logger.warning("py7zr extract failed: %s", exc)

    shutil.rmtree(staging, ignore_errors=True)
    return False


def _run_silent_gtk_installer(installer: Path, root: Path, *, verbose: bool = False) -> bool:
    """NSIS silent install — may require elevation."""
    install_root = gtk_runtime_dir(root)
    if install_root.exists():
        if verbose:
            print(f"  Removing previous install at {install_root}")
        shutil.rmtree(install_root, ignore_errors=True)
    install_root.parent.mkdir(parents=True, exist_ok=True)

    dest = str(install_root.resolve())
    cmd = [
        str(installer),
        "/sideeffects=no",
        "/dllpath=root",
        "/translations=no",
        "/S",
        f"/D={dest}",
    ]
    if verbose:
        print(f"  Running silent installer to {dest} (administrator may be required)...")

    logger.info("Running silent GTK installer to %s", dest)
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        if verbose:
            print("  ERROR: GTK installer timed out.")
        return False
    except OSError as exc:
        if verbose:
            print(f"  ERROR: Could not run installer: {exc}")
        return False

    combined = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
    if proc.returncode != 0:
        logger.warning("GTK installer exit %s: %s", proc.returncode, combined[:400])
        if verbose:
            print(f"  Installer exit code {proc.returncode}")
            if "elevation" in combined or "administrator" in combined:
                print("  The installer needs Administrator rights (see run_demo.bat Option A).")

    if discover_gtk_bin_dirs(root):
        return True

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    default_bin = Path(pf) / "GTK3-Runtime Win64" / "bin"
    if _dir_has_pango_dll(default_bin):
        if verbose:
            print(f"  Found system GTK at {default_bin}")
        apply_gtk_env([default_bin])
        return True
    return False


def ensure_gtk_runtime_windows(root: Path | None = None, *, verbose: bool = False) -> dict[str, bool]:
    """
    Download GTK if needed; prefer portable extract, then optional silent install.

    Returns flags: ``portable_ok``, ``installer_ok``, ``ready``.
    """
    flags = {
        "portable_attempted": False,
        "portable_ok": False,
        "installer_attempted": False,
        "installer_ok": False,
        "ready": False,
    }
    if not is_windows():
        return flags

    root = project_root(root)
    if discover_gtk_bin_dirs(root):
        flags["ready"] = True
        if verbose:
            print(f"  GTK already present: {discover_gtk_bin_dirs(root)[0]}")
        return flags

    try:
        installer = _download_gtk_installer(root, verbose=verbose)
    except RuntimeError as exc:
        if verbose:
            print(f"  ERROR: {exc}")
        logger.error("GTK download failed: %s", exc)
        return flags

    if _prefer_portable_install():
        flags["portable_attempted"] = True
        if verbose:
            print("  Trying portable extraction (no administrator required)...")
        flags["portable_ok"] = extract_gtk_portable(installer, root, verbose=verbose)
        if flags["portable_ok"]:
            flags["ready"] = True
            logger.info("Portable GTK extract succeeded")
            return flags
        if verbose:
            print("  Portable extract did not produce required DLLs.")

    if _allow_silent_installer():
        flags["installer_attempted"] = True
        flags["installer_ok"] = _run_silent_gtk_installer(installer, root, verbose=verbose)
        flags["ready"] = flags["installer_ok"]
        if flags["installer_ok"]:
            logger.info("Silent GTK installer succeeded")
        return flags

    if verbose:
        print("  Skipping silent installer (not elevated).")
        print("  Option A: Right-click run_demo.bat -> Run as administrator")
        print("  Option B: Install 7-Zip, then run run_demo.bat again for portable extract")
        print(f"  Option C: Manual download — {GTK_MANUAL_RELEASE_URL}")
    return flags


def check_weasyprint_status(
    root: Path | None = None,
    *,
    auto_install: bool = False,
) -> WeasyPrintStatus:
    """Full readiness check; optionally download/extract GTK on Windows."""
    root = project_root(root)
    elevated = is_process_elevated()
    prefer_portable = _prefer_portable_install()

    if not is_windows():
        apply_gtk_env(discover_gtk_bin_dirs(root))
        ok, err = probe_weasyprint_import()
        if ok:
            return WeasyPrintStatus(
                ready=True,
                platform=sys.platform,
                message="WeasyPrint is available.",
                gtk_bin_dirs=[str(p) for p in discover_gtk_bin_dirs(root)],
                is_elevated=elevated,
                prefer_portable=prefer_portable,
            )
        return WeasyPrintStatus(
            ready=False,
            platform=sys.platform,
            message="WeasyPrint is not installed or system libraries are missing.",
            install_hint=err or "Install Pango/Cairo per WeasyPrint docs (macOS: brew, Linux: apt).",
            is_elevated=elevated,
            prefer_portable=prefer_portable,
        )

    bin_dirs = discover_gtk_bin_dirs(root)
    apply_gtk_env(bin_dirs)
    ok, err = probe_weasyprint_import()

    runtime_path = str(gtk_runtime_dir(root)) if gtk_runtime_dir(root).is_dir() else ""

    if ok:
        return WeasyPrintStatus(
            ready=True,
            platform=sys.platform,
            message="WeasyPrint and GTK3 libraries are ready.",
            gtk_bin_dirs=[str(p) for p in bin_dirs],
            gtk_runtime_path=runtime_path,
            is_elevated=elevated,
            prefer_portable=prefer_portable,
        )

    status = WeasyPrintStatus(
        ready=False,
        platform=sys.platform,
        message="PDF export needs GTK3 runtime libraries (libpango, Cairo).",
        gtk_bin_dirs=[str(p) for p in bin_dirs],
        gtk_runtime_path=runtime_path,
        install_hint=err,
        is_elevated=elevated,
        prefer_portable=prefer_portable,
    )

    if auto_install:
        status.auto_install_attempted = True
        flags = ensure_gtk_runtime_windows(root, verbose=False)
        status.portable_extract_attempted = flags["portable_attempted"]
        status.portable_extract_succeeded = flags["portable_ok"]
        status.installer_attempted = flags["installer_attempted"]
        status.auto_install_succeeded = flags["ready"]

        if flags["ready"]:
            reset_weasyprint_probe_cache()
            bin_dirs = discover_gtk_bin_dirs(root)
            apply_gtk_env(bin_dirs)
            ok, err = probe_weasyprint_import()
            status.gtk_bin_dirs = [str(p) for p in bin_dirs]
            status.gtk_runtime_path = str(gtk_runtime_dir(root))
            if ok:
                status.ready = True
                if flags["portable_ok"]:
                    status.message = (
                        "GTK3 was extracted into gtk3/runtime (portable); WeasyPrint is ready."
                    )
                else:
                    status.message = "GTK3 was installed; WeasyPrint is ready."
                return status
            status.install_hint = err or "GTK present but WeasyPrint still cannot load DLLs."
        else:
            status.install_hint = (
                "GTK setup did not complete. "
                "(A) Right-click run_demo.bat → Run as administrator (one-time), or "
                "(B) install 7-Zip and run again for portable extract into gtk3/runtime, or "
                f"(C) install manually from {GTK_MANUAL_RELEASE_URL}"
            )

    return status


def ensure_weasyprint_deps(
    root: Path | None = None,
    *,
    auto_install: bool | None = None,
    verbose: bool = False,
) -> WeasyPrintStatus:
    """
    Apply GTK paths and verify WeasyPrint.

    ``auto_install`` defaults to env ``JOBDOC_AUTO_INSTALL_GTK=1`` on Windows.
    """
    root = project_root(root)
    if auto_install is None:
        auto_install = is_windows() and os.environ.get("JOBDOC_AUTO_INSTALL_GTK", "0").strip() in (
            "1",
            "true",
            "yes",
        )
    apply_gtk_env(discover_gtk_bin_dirs(root))
    status = check_weasyprint_status(root, auto_install=auto_install)
    if verbose:
        _print_status(status)
    return status


def ensure_weasyprint_import_ready(root: Path | None = None) -> None:
    """Call before ``from weasyprint import ...`` in PDF code paths."""
    status = ensure_weasyprint_deps(root, auto_install=False)
    if not status.ready:
        raise RuntimeError(
            status.install_hint or status.message or "WeasyPrint is not available on this machine."
        )


def bootstrap_weasyprint_for_app(root: Path | None = None) -> WeasyPrintStatus:
    """Called once from ``app.py`` startup: apply known GTK paths."""
    root = project_root(root)
    apply_gtk_env(discover_gtk_bin_dirs(root))
    auto = is_windows() and os.environ.get("JOBDOC_AUTO_INSTALL_GTK", "0").strip() in (
        "1",
        "true",
        "yes",
    )
    return ensure_weasyprint_deps(root, auto_install=auto, verbose=False)


def emit_env_lines_for_batch(root: Path | None = None) -> list[str]:
    """
    Return ``set VAR=...`` lines for ``run_demo.bat`` to apply GTK paths to child processes.
    """
    root = project_root(root)
    bin_dirs = discover_gtk_bin_dirs(root)
    if not bin_dirs:
        return []
    apply_gtk_env(bin_dirs)
    lines: list[str] = []
    wp = os.environ.get("WEASYPRINT_DLL_DIRECTORIES", "")
    if wp:
        lines.append(f'set "WEASYPRINT_DLL_DIRECTORIES={wp}"')
    prefix = os.pathsep.join(str(p) for p in bin_dirs)
    lines.append(f'set "PATH={prefix};%PATH%"')
    return lines


def _print_status(status: WeasyPrintStatus) -> None:
    if status.ready:
        print("[JobDoc] WeasyPrint OK — PDF export is enabled.")
        for d in status.gtk_bin_dirs:
            print(f"         GTK: {d}")
        return
    print("[JobDoc] WeasyPrint NOT ready — PDF export will be blocked until GTK is installed.")
    print(f"         {status.message}")
    if status.install_hint:
        print(f"         Detail: {status.install_hint[:400]}")
    if status.auto_install_attempted:
        print(
            f"         Auto-install: {'succeeded' if status.auto_install_succeeded else 'failed'}"
        )
        if status.portable_extract_attempted:
            print(
                f"         Portable extract: "
                f"{'ok' if status.portable_extract_succeeded else 'failed'}"
            )


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m utils.setup_weasyprint --ensure-gtk``."""
    parser = argparse.ArgumentParser(description="JobDoc WeasyPrint / GTK setup (Windows)")
    parser.add_argument(
        "--ensure-gtk",
        action="store_true",
        help="On Windows, download/extract GTK3 if missing, then verify WeasyPrint.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check status only (no download).",
    )
    parser.add_argument(
        "--auto-install",
        action="store_true",
        help="Allow automatic GTK download/setup on Windows.",
    )
    parser.add_argument(
        "--emit-env",
        action="store_true",
        help="Print set commands for run_demo.bat (WEASYPRINT_DLL_DIRECTORIES, PATH).",
    )
    parser.add_argument(
        "--allow-installer",
        action="store_true",
        help="Allow silent .exe install when portable extract fails (needs admin).",
    )
    args = parser.parse_args(argv)

    if args.emit_env:
        for line in emit_env_lines_for_batch():
            print(line)
        return 0

    if args.allow_installer:
        os.environ["JOBDOC_ALLOW_GTK_INSTALLER"] = "1"

    auto = args.auto_install or args.ensure_gtk or (
        os.environ.get("JOBDOC_AUTO_INSTALL_GTK", "0").strip() in ("1", "true", "yes")
    )
    if args.ensure_gtk:
        os.environ["JOBDOC_AUTO_INSTALL_GTK"] = "1"
        os.environ.setdefault("JOBDOC_PREFER_PORTABLE_GTK", "1")
        auto = True

    if args.check_only:
        status = ensure_weasyprint_deps(auto_install=False, verbose=True)
        return 0 if status.ready else 1

    status = ensure_weasyprint_deps(auto_install=auto, verbose=True)
    if status.ready:
        return 0
    if args.ensure_gtk and is_windows() and not status.ready:
        return 2
    return 1 if not status.ready else 0


if __name__ == "__main__":
    raise SystemExit(main())
