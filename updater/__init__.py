"""
Pacote de autoatualização via GitHub Releases, sanitização de ambiente PyInstaller (_MEI*)
e desinstalador completo do Telegram Cloud Drive.
"""

from updater.github_updater import (
    GitHubUpdaterMixin,
    run_uninstaller_flow,
    _find_all_desktop_and_start_menu_dirs,
)

__all__ = [
    "GitHubUpdaterMixin",
    "run_uninstaller_flow",
    "_find_all_desktop_and_start_menu_dirs",
]
