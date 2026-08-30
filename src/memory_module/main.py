'''
Main entry point of the biomem module.

Threading Inversion architecture (Phase 5.1):
  - MainThread:         GUI Dashboard (tkinter mainloop)
  - Background Thread:  AsyncIO event loop + WS/HTTP servers + TextMemory

Startup flow:
  1. Parse arguments, logging, data_dir
  2. Initialize SettingsManager (encrypted local storage)
  3. Create the Dashboard GUI (shows IMMEDIATELY — before the model is loaded)
  4. Start the background thread with the asyncio server
  5. MainThread runs the tkinter mainloop (blocking)
  6. Shutdown: Dashboard/tray → stop asyncio → exit

Usage:
    biomem-server                  # Default configuration
    biomem-server --port 8765      # Custom port
    biomem-server --data-dir ~/.biomem  # Custom data directory
    biomem-server --debug          # Debug logging (console mode, no GUI)
    biomem-server --no-gui         # No GUI dashboard
'''
import sys
import asyncio
import signal
import logging
import argparse
import threading
from pathlib import Path
from .config import MemoryConfig
from .security import get_data_dir
from .localization import T


def setup_logging(debug: bool = False, log_dir: Path = None) -> None:
    '''Sets up logging.'''
    level = logging.DEBUG if debug else logging.INFO
    fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    date_fmt = '%Y-%m-%d %H:%M:%S'
    handlers = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / 'biomem_server.log', encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(fmt, date_fmt))
        handlers.append(file_handler)
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=level, format=fmt, datefmt=date_fmt, handlers=handlers)
    if sys.stdout is None:
        _install_windowed_excepthook()
    return None


def _install_windowed_excepthook():
    '''Route unhandled Python exceptions to the bdbm logger (windowed build).'''
    import traceback
    _exc_log = logging.getLogger('bdbm.crash')

    def _hook(exc_type, exc_value, exc_tb):
        _exc_log.critical('Unhandled exception:\n%s', ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    sys.excepthook = _hook
    try:
        import threading

        def _thread_hook(args):
            if args.exc_type is SystemExit:
                return
            _exc_log.critical('Unhandled exception in thread %s:\n%s', args.thread, ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

        threading.excepthook = _thread_hook
    except AttributeError:
        pass
    return None


def parse_args() -> argparse.Namespace:
    '''Parses command line arguments.'''
    parser = argparse.ArgumentParser(prog='biomem-server', description=T('cli.desc'))
    parser.add_argument('--host', default='127.0.0.1', help=T('cli.host'))
    parser.add_argument('--port', type=int, default=8765, help=T('cli.port'))
    parser.add_argument('--data-dir', default='', help=T('cli.data_dir'))
    parser.add_argument('--state-file', default='', help=T('cli.state_file'))
    parser.add_argument('--debug', action='store_true', help=T('cli.debug'))
    parser.add_argument('--no-tray', action='store_true', help=T('cli.no_tray'))
    parser.add_argument('--no-gui', action='store_true', help=T('cli.no_gui'))
    parser.add_argument('--version', action='store_true', help=T('cli.version'))
    return parser.parse_args()


def hide_console_window() -> None:
    '''Hides the console window on Windows (CMD window).'''
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
            return None
        return None
    except Exception:
        return None


def _check_already_running(host: str, port: int) -> None:
    '''
    Checks whether another instance is already running on the given port.
    If so, shows a warning and exits the program.
    '''
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect((host, port))
        sock.close()
        msg = T('cli.already_running', host, port)
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, T('tray.title'), 64)
            except Exception:
                print(msg)
        else:
            print(msg)
        sys.exit(0)
    except (ConnectionRefusedError, OSError, socket.timeout):
        pass
    finally:
        sock.close()
    return None


def _resolve_state_file(args, data_dir: Path, config: MemoryConfig, logger) -> str:
    '''Resolves the state file path with .bdbm/.pt fallback logic.'''
    if args.state_file:
        return args.state_file
    bdbm_path = str(data_dir / config.state_file)
    pt_path = str(data_dir / config.legacy_state_file)
    if Path(bdbm_path).exists():
        return bdbm_path
    if Path(pt_path).exists():
        logger.info(T('cli.legacy_found', pt_path))
        return pt_path
    return bdbm_path


def _run_background_server(config: MemoryConfig, args: argparse.Namespace, state_file: str, data_dir: Path, settings_mgr, dashboard=None, tray=None, conv_handler=None):
    '''
    Runs in a background daemon thread.

    Initializes TextMemory, the WS server and the asyncio loop.
    Communicates with the Dashboard GUI via a thread-safe queue.
    '''
    logger = logging.getLogger('bdbm.background')
    loop = None
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if dashboard:
            dashboard.set_async_loop(loop)
        if dashboard:
            from .dashboard import MSG_STATUS_UPDATE
            dashboard.post_message(MSG_STATUS_UPDATE, {
                'text': T('ui.loading_model'),
                'detail': T('ui.loading_model_detail'),
                'color': '#e0e0e0',
            })
        from .ws_server import BDBMServer
        server = BDBMServer(config=config, host=args.host, port=args.port, state_file=state_file, settings_manager=settings_mgr)
        _ = server.memory.embedder.model
        logger.info(T('cli.model_ready'))
        if dashboard:
            dashboard.set_command_handler(server.handler)
        if conv_handler:
            conv_handler.set_command_handler(server.handler)
            if dashboard:
                from .dashboard import MSG_CONV_HANDLER_READY
                dashboard.post_message(MSG_CONV_HANDLER_READY, {})
        try:
            from . import __version__
            from .update_checker import check_for_update_async
            check_for_update_async(__version__, tray, backup_callback=server.memory.backup)
        except Exception:
            pass
        if state_file.endswith('.pt') and Path(state_file).exists():
            bdbm_target = str(data_dir / config.state_file)
            try:
                server.memory.save(bdbm_target)
                server.memory.state_file = bdbm_target
                logger.info(T('cli.migration_done', bdbm_target))
            except Exception as e:
                logger.warning(T('cli.migration_failed', str(e)))
        if dashboard:
            from .dashboard import fetch_news_async
            fetch_news_async(settings_mgr, dashboard)

        async def _stats_reporter():
            '''Sends memory stats to the dashboard every 5 seconds.'''
            from .dashboard import MSG_MEMORY_STATS
            while True:
                try:
                    stats = server.memory.get_stats()
                    dashboard.post_message(MSG_MEMORY_STATS, {'ltm_active': stats.get('ltm_active', 0), 'ltm_total': stats.get('ltm_total', 1), 'stm_active': stats.get('stm_active', 0), 'stm_total': stats.get('stm_total', 1), 'writes': stats.get('writes', 0), 'reads': stats.get('reads', 0), 'fatigue_pct': stats.get('fatigue', 0.0) * 100.0})
                except Exception:
                    pass
                await asyncio.sleep(5)

        from .telemetry import TelemetryClient

        def _telemetry_stats():
            mem = server.memory.get_stats()
            model = 'unknown'
            if dashboard is not None:
                try:
                    model = dashboard._get_selected_model()
                except Exception:
                    pass
            return {'stm': mem.get('stm_active', 0), 'ltm': mem.get('ltm_active', 0), 'model': model, 'bdbm_status': 'connected'}

        telemetry = TelemetryClient(
            get_stats_fn=_telemetry_stats,
            get_session_hash_fn=settings_mgr.get_session_hash,
        )

        async def _run_all():
            auxiliary_tasks = []
            server_task = asyncio.create_task(server.start(), name='bdbm-server')
            if dashboard and hasattr(dashboard, 'set_server_task'):
                dashboard.set_server_task(server_task)
            try:
                while not server.is_running:
                    if server_task.done():
                        await server_task
                    await asyncio.sleep(0.01)
                if dashboard:
                    from .dashboard import MSG_SERVER_READY
                    dashboard.post_message(MSG_SERVER_READY, {})
                    auxiliary_tasks.append(asyncio.create_task(
                        _stats_reporter(), name='bdbm-dashboard-stats'))
                auxiliary_tasks.append(asyncio.create_task(
                    telemetry.start(), name='bdbm-telemetry'))
                await server_task
            finally:
                for task in auxiliary_tasks:
                    task.cancel()
                if auxiliary_tasks:
                    await asyncio.gather(*auxiliary_tasks, return_exceptions=True)
                if dashboard and hasattr(dashboard, 'set_server_task'):
                    dashboard.set_server_task(None)

        loop.run_until_complete(_run_all())
    except asyncio.CancelledError:
        logger.info('Background server stopped by dashboard request')
    except Exception as e:
        logger.error(T('cli.error_fatal', str(e)), exc_info=True)
        if dashboard:
            from .dashboard import MSG_STATUS_UPDATE
            dashboard.post_message(MSG_STATUS_UPDATE, {
                'text': T('cli.error_start'),
                'detail': str(e),
                'color': '#ef476f',
            })
    finally:
        if loop is not None and not loop.is_closed():
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
    return None


def main() -> None:
    '''
    Main entry point of the biomem module.

    Threading Inversion (Phase 5.1):
    - MainThread: Dashboard GUI (tkinter mainloop)
    - Background: AsyncIO + WS/HTTP servers + TextMemory
    '''
    args = parse_args()
    if args.version:
        from . import __version__
        print(f'biomem memory module v{__version__}')
        sys.exit(0)
    _check_already_running(args.host or '127.0.0.1', args.port)
    data_dir = get_data_dir(args.data_dir)
    setup_logging(debug=args.debug, log_dir=data_dir / 'logs')
    logger = logging.getLogger('bdbm.main')
    logger.info('============================================================')
    logger.info('🧠 biomem memory module')
    logger.info('============================================================')
    logger.info(f'Data: {data_dir}')
    config = MemoryConfig()
    config.data_dir = str(data_dir)
    config.ws_host = args.host
    config.ws_port = args.port
    state_file = _resolve_state_file(args, data_dir, config, logger)
    from .settings_manager import SettingsManager
    from .localization import Localization
    settings_mgr = SettingsManager(data_dir)
    Localization.set_language(settings_mgr.get_ui_language())
    logger.info('Language: %s', settings_mgr.get_ui_language())
    config.stm_new_center_threshold = settings_mgr.get_stm_threshold()
    config.ltm_new_center_threshold = settings_mgr.get_ltm_threshold()
    config.max_associations = settings_mgr.get_max_associations()
    logger.info(f"⚙️  Thresholds: stm={config.stm_new_center_threshold:.3f}, ltm={config.ltm_new_center_threshold:.3f}, max_associations={config.max_associations}")
    use_gui = not args.no_gui and not args.debug
    if use_gui:
        _run_with_dashboard(args, config, state_file, data_dir, settings_mgr, logger)
        return None
    _run_headless(args, config, state_file, data_dir, settings_mgr, logger)
    return None


def _run_with_dashboard(args, config, state_file, data_dir, settings_mgr, logger):
    '''
    Run with the GUI Dashboard (production mode).

    MainThread = PyQt6, Background = asyncio server.
    '''
    try:
        from .dashboard import BDBMDashboard
    except ImportError as e:
        logger.warning(f'Dashboard unavailable (PyQt6?): {e}')
        logger.info(T('cli.headless_switch'))
        return _run_headless(args, config, state_file, data_dir, settings_mgr, logger)
    try:
        from .thread_store import ThreadStore
        from .llm_client import LLMClient
        from .conversation_handler import ConversationHandler
        thread_store = ThreadStore(data_dir=data_dir, aes_key=settings_mgr._aes_key, hmac_key=settings_mgr._hmac_key)
        llm_client = LLMClient(get_key_fn=settings_mgr.get_llm_key, get_model_fn=settings_mgr.get_llm_model_name, get_ollama_timeout_min_fn=settings_mgr.get_ollama_timeout_min)
        conv_handler = ConversationHandler(command_handler=None, llm_client=llm_client, settings_manager=settings_mgr, thread_store=thread_store)
        logger.info('💬 ConversationHandler initialized.')
    except Exception as e:
        logger.warning(f'ConversationHandler unavailable: {e}')
        conv_handler = None

    tray = None
    _bg_thread = None

    def quit_module():
        '''Quits the whole process. Must run in the Qt main thread.'''
        logger.info(T('msg.shutdown_title'))
        if tray:
            try:
                tray.stop()
            except Exception:
                pass
        dashboard.request_server_shutdown()
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.quit()

    dashboard = BDBMDashboard(settings_manager=settings_mgr, on_quit=quit_module, conversation_handler=conv_handler)
    dashboard.show()
    if not args.no_tray:
        try:
            from .tray_icon import BDBMTrayIcon

            def show_dashboard_from_tray():
                '''Shows the Dashboard from the tray menu — thread-safe via Qt signal.'''
                dashboard.post_message('__show__', {})

            def quit_from_tray():
                '''Quits the module from the tray — thread-safe via Qt signal.'''
                dashboard.post_message('__quit__', {})

            tray = BDBMTrayIcon(on_quit=quit_from_tray, on_show_dashboard=show_dashboard_from_tray)
            tray.start()
            dashboard.set_tray_icon(tray)
            hide_console_window()
        except Exception as e:
            logger.warning(f'Failed to start tray icon: {e}')
    _bg_thread = threading.Thread(target=_run_background_server, args=(config, args, state_file, data_dir, settings_mgr, dashboard, tray, conv_handler), daemon=True, name='bdbm-async-server')
    _bg_thread.start()
    logger.info('🚀 Background server thread started')
    try:
        dashboard.mainloop()
    except KeyboardInterrupt:
        logger.info('⏹️ Quit by user (Ctrl+C)')
    finally:
        if tray:
            tray.stop()
        dashboard.request_server_shutdown()
        if _bg_thread and _bg_thread.is_alive():
            _bg_thread.join(timeout=5)
    return None


def _run_headless(args, config, state_file, data_dir, settings_mgr, logger):
    '''
    Run without GUI (debug/headless mode).

    Design note: asyncio runs on the main thread.
    '''
    tray = None
    if not args.no_tray and not args.debug:
        try:
            from .tray_icon import BDBMTrayIcon

            def quit_from_tray():
                logger.info('⏹️ Quit from tray menu')
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.call_soon_threadsafe(loop.stop)
                        return None
                    return None
                except Exception:
                    import os
                    os._exit(0)

            tray = BDBMTrayIcon(on_quit=quit_from_tray)
            tray.start()
            hide_console_window()
        except Exception as e:
            logger.warning(f'Failed to start tray icon: {e}')

    try:
        from .ws_server import BDBMServer
        server = BDBMServer(config=config, host=args.host, port=args.port, state_file=state_file, settings_manager=settings_mgr)
        _ = server.memory.embedder.model
    except ImportError as e:
        logger.error(T('cli.error_init', str(e)))
        logger.error(T('cli.install_deps'))
        sys.exit(1)
    except Exception as e:
        logger.error(T('cli.error_init', str(e)))
        sys.exit(1)

    try:
        from . import __version__
        from .update_checker import check_for_update_async
        check_for_update_async(__version__, tray, backup_callback=server.memory.backup)
    except Exception:
        pass
    if state_file.endswith('.pt') and Path(state_file).exists():
        bdbm_target = str(data_dir / config.state_file)
        try:
            server.memory.save(bdbm_target)
            server.memory.state_file = bdbm_target
            logger.info(T('cli.migration_done', bdbm_target))
        except Exception as e:
            logger.warning(T('cli.migration_failed', str(e)))
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info('⏹️ Quit by user (Ctrl+C)')
    except Exception as e:
        logger.error(f'❌ Fatal error: {e}')
        sys.exit(1)
    finally:
        if tray:
            tray.stop()
    return None


if __name__ == '__main__':
    main()
