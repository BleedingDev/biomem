r"""
Automatic start of the biomem server at user login.

Windows:
    - Registry key HKCU\Software\Microsoft\Windows\CurrentVersion\Run
    - Or a shortcut in the Startup folder (fallback)

Linux:
    - systemd user service (~/.config/systemd/user/bdbm-server.service)
    - XDG autostart (~/.config/autostart/bdbm-server.desktop) as fallback
"""
import os
import sys
import platform
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger('bdbm.autostart')

APP_NAME = 'biomem-server'
APP_DESCRIPTION = 'biomem memory module – Cognitive Memory Server'


def get_executable_path() -> str:
    """
    Returns the path to the biomem server executable.

    Detects whether we run as:
    - PyInstaller frozen exe → sys.executable
    - Installed Python package → 'biomem-server' script
    - Dev mode → 'python -m memory_module.main'
    """
    if getattr(sys, 'frozen', False):
        return sys.executable
    bdbm_path = shutil.which('biomem-server')
    if bdbm_path:
        return bdbm_path
    return f'"{sys.executable}" -m memory_module.main'


def register_autostart(enable: bool = True, executable_path: Optional[str] = None) -> bool:
    """
    Registers/unregisters biomem for automatic start.

    Detects the platform and uses the corresponding mechanism.

    Args:
        enable: True = register, False = unregister
        executable_path: Path to the executable (auto-detected if None)

    Returns:
        True if the operation succeeded
    """
    if executable_path is None:
        executable_path = get_executable_path()

    system = platform.system()

    if system == 'Windows':
        return _autostart_windows(enable, executable_path)

    if system == 'Linux':
        return _autostart_linux(enable, executable_path)

    logger.warning(f'Auto-start is not supported on platform: {system}')
    return False


def is_autostart_enabled() -> bool:
    """Checks whether auto-start is active."""
    system = platform.system()

    if system == 'Windows':
        return _check_autostart_windows()

    if system == 'Linux':
        return _check_autostart_linux()

    return False


def _autostart_windows(enable: bool, executable_path: str) -> bool:
    """Windows: Registry HKCU Run key."""
    try:
        import winreg

        key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'

        if enable:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, executable_path)
            logger.info(f'Auto-start registered: {executable_path}')
            return True
        else:
            # Nested try: the Exception Table covers FileNotFoundError (270–338)
            # only for the disable branch — i.e. not both branches at once.
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, APP_NAME)
                logger.info('Auto-start unregistered')
                return True
            except FileNotFoundError:
                logger.info('Auto-start was not registered')
                return True

    except ImportError:
        logger.warning('winreg unavailable, trying the Startup folder...')
        return _autostart_windows_startup_folder(enable, executable_path)
    except OSError as e:
        logger.error(f'Error registering auto-start: {e}')
        return False


def _autostart_windows_startup_folder(enable: bool, executable_path: str) -> bool:
    """Windows fallback: Shortcut in the Startup folder."""
    try:
        startup_dir = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
        shortcut_path = startup_dir / f'{APP_NAME}.bat'

        if enable:
            startup_dir.mkdir(parents=True, exist_ok=True)
            shortcut_path.write_text(f'@echo off\r\nstart "" /min {executable_path}\r\n', encoding='utf-8')
            logger.info(f'Auto-start shortcut created: {shortcut_path}')
            return True
        else:
            if shortcut_path.exists():
                shortcut_path.unlink()
                logger.info('Auto-start shortcut removed')
            return True

    except OSError as e:
        logger.error(f'Startup folder error: {e}')
        return False


def _check_autostart_windows() -> bool:
    """Checks whether auto-start is active on Windows."""
    try:
        import winreg

        key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True

    except (ImportError, FileNotFoundError, OSError):
        # Fallback: check the Startup folder
        startup_dir = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
        return (startup_dir / f'{APP_NAME}.bat').exists()


_SYSTEMD_SERVICE_TEMPLATE = '[Unit]\nDescription={description}\nAfter=network.target\n\n[Service]\nType=simple\nExecStart={executable}\nRestart=on-failure\nRestartSec=5\nEnvironment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=default.target\n'
_XDG_DESKTOP_TEMPLATE = '[Desktop Entry]\nType=Application\nName={name}\nComment={description}\nExec={executable}\nHidden=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\nTerminal=false\n'


def _autostart_linux(enable: bool, executable_path: str) -> bool:
    """Linux: systemd user service (primary) + XDG autostart (fallback)."""
    systemd_ok = _autostart_linux_systemd(enable, executable_path)
    xdg_ok = _autostart_linux_xdg(enable, executable_path)
    return systemd_ok or xdg_ok


def _autostart_linux_systemd(enable: bool, executable_path: str) -> bool:
    """Linux: systemd user service."""
    try:
        service_dir = Path.home() / '.config' / 'systemd' / 'user'
        service_file = service_dir / 'bdbm-server.service'

        if enable:
            service_dir.mkdir(parents=True, exist_ok=True)
            service_content = _SYSTEMD_SERVICE_TEMPLATE.format(description=APP_DESCRIPTION, executable=executable_path)
            service_file.write_text(service_content, encoding='utf-8')

            os.system('systemctl --user daemon-reload')
            os.system('systemctl --user enable bdbm-server.service')

            logger.info(f'systemd service created: {service_file}')
            logger.info('   To start immediately: systemctl --user start bdbm-server')
            return True

        else:
            os.system('systemctl --user stop bdbm-server.service 2>/dev/null')
            os.system('systemctl --user disable bdbm-server.service 2>/dev/null')

            if service_file.exists():
                service_file.unlink()
                os.system('systemctl --user daemon-reload')

            logger.info('systemd service removed')
            return True

    except OSError as e:
        logger.warning(f'systemd setup failed: {e}')
        return False


def _autostart_linux_xdg(enable: bool, executable_path: str) -> bool:
    """Linux fallback: XDG autostart desktop entry."""
    try:
        autostart_dir = Path.home() / '.config' / 'autostart'
        desktop_file = autostart_dir / 'bdbm-server.desktop'

        if enable:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_content = _XDG_DESKTOP_TEMPLATE.format(
                name=APP_NAME,
                description=APP_DESCRIPTION,
                executable=executable_path
            )
            desktop_file.write_text(desktop_content, encoding='utf-8')
            logger.info(f'XDG autostart created: {desktop_file}')
            return True

        else:
            if desktop_file.exists():
                desktop_file.unlink()
            logger.info('XDG autostart removed')
            return True

    except OSError as e:
        logger.warning(f'XDG autostart setup failed: {e}')
        return False


def _check_autostart_linux() -> bool:
    """Checks whether auto-start is active on Linux."""
    service_file = Path.home() / '.config' / 'systemd' / 'user' / 'bdbm-server.service'
    if service_file.exists():
        return True

    desktop_file = Path.home() / '.config' / 'autostart' / 'bdbm-server.desktop'
    return desktop_file.exists()


def main():
    """CLI for managing auto-start."""
    import argparse

    parser = argparse.ArgumentParser(
        prog='biomem-autostart',
        description='Manage auto-start of the biomem server.'
    )
    parser.add_argument(
        'action',
        choices=['enable', 'disable', 'status'],
        help='Action: enable/disable/status'
    )
    parser.add_argument(
        '--path',
        default=None,
        help='Path to the biomem executable (auto-detect if not given)'
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if args.action == 'status':
        if is_autostart_enabled():
            print('Auto-start is ACTIVE')
            return
        print('Auto-start is INACTIVE')
        return

    if args.action == 'enable':
        success = register_autostart(True, args.path)
        if not success:
            sys.exit(1)
        return

    if args.action == 'disable':
        success = register_autostart(False)
        if not success:
            sys.exit(1)


if __name__ == '__main__':
    main()
