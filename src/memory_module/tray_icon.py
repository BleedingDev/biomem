# Source Generated with Decompyle++
# File: tray_icon.pyc (Python 3.11)

'''
System tray icon for the biomem memory module.

Shows a hippocampus icon in the system panel.
Right click → context menu with a quit option.

Uses pystray (cross-platform: Windows, macOS, Linux).
'''
import sys
import logging
import threading
from pathlib import Path
from typing import Callable, Optional
from .localization import T

logger = logging.getLogger('bdbm.tray')
_pystray = None
_PILImage = None

def _ensure_imports():
    '''Lazy import pystray a PIL.'''
    global _pystray, _PILImage
    if _pystray is not None:
        return None

    try:
        import pystray
        from PIL import Image
        _pystray = pystray
        _PILImage = Image
        return None
    except ImportError as e:
        logger.warning(f'''System tray unavailable (pystray/Pillow missing): {e}. The module will run without a tray icon.''')
        raise


def _find_icon_path() -> Optional[Path]:
    '''Finds the hippocampus icon – searches several locations.'''
    candidates = [
        Path(getattr(sys, '_MEIPASS', '.')) / 'icon.ico',
        Path(sys.executable).parent / 'icon.ico',
        Path(__file__).parent.parent / 'installer' / 'icon.ico']
    for path in candidates:
        if path.exists():
            logger.debug(f'''Tray icon found: {path}''')
            return path
    logger.warning('Tray icon not found, using default.')
    return None


def _create_icon_image():
    '''Loads the icon or creates a simple placeholder icon.'''
    icon_path = _find_icon_path()
    if icon_path:

        try:
            return _PILImage.open(str(icon_path))
        except Exception as e:
            logger.warning(f'''Cannot load icon {icon_path}: {e}''')

    img = _PILImage.new('RGBA', (64, 64), (46, 139, 87, 255))

    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype('arial.ttf', 40)
        except (IOError, OSError):
            font = ImageFont.load_default()

        draw.text((16, 8), 'H', fill='white', font=font)
    except ImportError:
        pass

    return img


class BDBMTrayIcon:
    '''
    System tray icon for the biomem module.

    Usage:
        tray = BDBMTrayIcon(on_quit=shutdown_callback)
        tray.start()  # runs in a new thread
        ...
        tray.stop()    # stops (called automatically on quit)
    '''

    def __init__(self, on_quit: Callable = None, on_show_dashboard: Callable = None):
        '''
        Args:
            on_quit: Callback called when "Quit" is clicked in the context menu.
                     Should stop the async server and exit the program.
            on_show_dashboard: Callback for showing the Dashboard window.
        '''
        self._on_quit = on_quit
        self._on_show_dashboard = on_show_dashboard
        self._icon = None
        self._thread = None


    def start(self):
        '''Starts the tray icon in a separate thread (non-blocking).'''

        try:
            _ensure_imports()
        except ImportError:
            return None

        self._thread = threading.Thread(target=self._run, daemon=True, name='bdbm-tray')
        self._thread.start()
        logger.info('System tray icon started')
        return None


    def _run(self):
        '''Main loop of the tray icon (runs in a separate thread).'''
        image = _create_icon_image()
        menu_items = [
            _pystray.MenuItem(lambda item: T('tray.title'), None, enabled=False),
            _pystray.Menu.SEPARATOR]
        if self._on_show_dashboard:
            menu_items.append(_pystray.MenuItem(lambda item: T('tray.dashboard'), self._handle_show_dashboard))
            menu_items.append(_pystray.Menu.SEPARATOR)
        menu_items.append(_pystray.MenuItem(lambda item: T('tray.quit'), self._handle_quit))
        menu = _pystray.Menu(*menu_items)
        self._icon = _pystray.Icon(name='bdbm-server', icon=image, title=T('tray.running'), menu=menu)
        self._icon.run()
        return None


    def _handle_quit(self, icon, item):
        """Handler for 'Quit' in the context menu."""
        logger.info('Quitting from tray menu...')
        self.stop()
        if self._on_quit:
            self._on_quit()
            return None
        return None


    def _handle_show_dashboard(self, icon, item):
        """Handler for 'Dashboard' in the context menu."""
        if self._on_show_dashboard:
            self._on_show_dashboard()
            return None
        return None


    def update_title(self):
        '''Updates the icon tooltip according to the current language.'''
        if self._icon:
            self._icon.title = T('tray.running')
            return None
        return None


    def stop(self):
        '''Stops the tray icon.'''
        if self._icon:

            try:
                self._icon.stop()
            except Exception:
                pass

            self._icon = None
            return None
        return None
