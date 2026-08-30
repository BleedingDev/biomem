'''
Startup script for the biomem server entry point (PyInstaller build).
Ensures the memory_module package is importable so relative imports work.
'''
import sys
import tempfile
import os
import multiprocessing

multiprocessing.freeze_support()


def _crash_report(msg: str) -> None:
    crash_log = os.path.join(tempfile.gettempdir(), 'bdbm_crash.log')
    try:
        with open(crash_log, 'w', encoding='utf-8') as f:
            f.write(f'CRITICAL ERROR: {msg}\n')
    except OSError:
        pass
    print(f'CRITICAL ERROR: {msg}', file=sys.stderr)
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f'Critical error during startup.\nMore information in:\n{crash_log}\n\nError: {msg}',
                'BDBM Server Error', 16)
        except Exception:
            pass


def main() -> None:
    try:
        from memory_module import main as server_main
        server_main.main()
    except ImportError as e:
        _crash_report(f'Failed to import memory_module: {e}\n'
                      'Ensure you are running this from the correct environment.')
        sys.exit(1)
    except Exception as e:
        import traceback
        crash_log = os.path.join(tempfile.gettempdir(), 'bdbm_crash.log')
        try:
            with open(crash_log, 'w', encoding='utf-8') as f:
                traceback.print_exc(file=f)
        except OSError:
            pass
        _crash_report(str(e))
        sys.exit(1)


if __name__ == '__main__':
    main()
