# Source Generated with Decompyle++
# File: cli.pyc (Python 3.11)

'''
Command Line Interface for the Memory Module.

Usage:
    biomem store "key" "value"
    biomem recall "query"
    biomem stats
    biomem interactive
'''
import argparse
import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4
from .text_memory import TextMemory
from .config import MemoryConfig
from .localization import T, Localization
from .security import get_data_dir
from .settings_manager import SettingsManager

_CLI_SESSION_ID = f'cli:{uuid4()}'


def _cli_provenance() -> dict:
    '''Returns local-only provenance shared by writes in this CLI process.'''
    return {
        'source_class': 'cli',
        'origin': 'local-cli',
        'session_id': _CLI_SESSION_ID,
    }


def create_parser() -> argparse.ArgumentParser:
    '''Creates the argument parser.'''
    parser = argparse.ArgumentParser(prog='biomem', description=T('cli.desc'))
    parser.add_argument('--state-file', '-f', default='memory_state.pt', help=T('cli.help_state'))
    parser.add_argument('--device', '-d', choices=['cpu', 'cuda'], default=None, help=T('cli.help_device'))
    parser.add_argument('--no-load', action='store_true', help=T('cli.help_no_load'))
    subparsers = parser.add_subparsers(dest='command', help=T('cli.cmd_help'))
    store_parser = subparsers.add_parser('store', help=T('cli.cmd_store'))
    store_parser.add_argument('key', help=T('cli.arg_key'))
    store_parser.add_argument('value', help=T('cli.arg_value'))
    store_parser.add_argument('--emotion', '-e', choices=['neutral', 'positive', 'negative', 'curious', 'social'], default='neutral', help=T('cli.arg_emotion'))
    store_parser.add_argument('--intensity', '-i', type=float, default=1.0, help=T('cli.arg_intensity'))
    recall_parser = subparsers.add_parser('recall', help=T('cli.cmd_recall'))
    recall_parser.add_argument('query', help=T('cli.arg_query'))
    recall_parser.add_argument('--top-k', '-k', type=int, default=5, help=T('cli.arg_top_k'))
    recall_parser.add_argument('--verbose', '-v', action='store_true', help=T('cli.help_verbose'))
    subparsers.add_parser('stats', help=T('cli.cmd_stats'))
    subparsers.add_parser('consolidate', help=T('cli.cmd_consolidate'))
    reset_parser = subparsers.add_parser('reset', help=T('cli.cmd_reset'))
    reset_parser.add_argument('--confirm', action='store_true', help=T('cli.help_confirm'))
    interactive_parser = subparsers.add_parser('interactive', help=T('cli.cmd_interactive'))
    interactive_parser.add_argument('--auto-step', action='store_true', help=T('cli.help_auto_step'))
    batch_parser = subparsers.add_parser('batch', help=T('cli.cmd_batch'))
    batch_parser.add_argument('file', help=T('cli.arg_file'))
    batch_parser.add_argument('--separator', '-s', default='\t', help=T('cli.arg_separator'))
    list_parser = subparsers.add_parser('list', help=T('cli.cmd_list'))
    list_parser.add_argument('--source', '-s', choices=['ltm', 'stm', 'both'], default='both', help=T('cli.arg_source'))
    list_parser.add_argument('--limit', '-n', type=int, default=20, help=T('cli.arg_limit'))
    forget_parser = subparsers.add_parser('forget', help=T('cli.cmd_forget'))
    forget_parser.add_argument('pattern', help=T('cli.arg_pattern'))
    forget_parser.add_argument('--exact', action='store_true', help=T('cli.arg_exact'))
    forget_parser.add_argument('--source', '-s', choices=['ltm', 'stm', 'both'], default='both', help=T('cli.arg_source'))
    edit_parser = subparsers.add_parser('edit', help=T('cli.cmd_edit'))
    edit_parser.add_argument('old_value', help=T('cli.arg_old_val'))
    edit_parser.add_argument('new_value', help=T('cli.arg_new_val'))
    edit_parser.add_argument('--source', '-s', choices=['ltm', 'stm', 'both'], default='both', help=T('cli.arg_source'))
    search_parser = subparsers.add_parser('search', help=T('cli.cmd_search'))
    search_parser.add_argument('query', help=T('cli.arg_search_query'))
    search_parser.add_argument('--top-k', '-k', type=int, default=10, help=T('cli.arg_search_limit'))
    search_parser.add_argument('--source', '-s', choices=['ltm', 'stm', 'both'], default='both', help=T('cli.arg_source'))
    return parser


def cmd_store(memory: TextMemory, args) -> int:
    '''Stores a key-value pair.'''
    n_new = memory.store(
        key=args.key,
        value=args.value,
        emotion=args.emotion,
        intensity=args.intensity,
        provenance=_cli_provenance(),
    )
    memory.save()
    print(T('cli.stored_success', args.key, args.value))
    print(T('cli.new_centers', n_new))
    return 0


def cmd_recall(memory: TextMemory, args) -> int:
    '''Recalls text from memory.'''
    result = memory.recall(args.query, top_k=args.top_k)
    if result.source == 'EMPTY':
        print(T('cli.not_found'))
        return 1
    memory.save()
    print(T('cli.result', result.text))
    if args.verbose:
        print(T('cli.key', result.key_text))
        print(T('cli.confidence', result.confidence))
        print(T('cli.source', result.source))
    return 0


def cmd_stats(memory: TextMemory, args) -> int:
    '''Shows statistics.'''
    stats = memory.get_stats()
    print(T('cli.stats_title'))
    print(T('cli.ltm_stats', stats['ltm_active'], stats['ltm_total'], stats['ltm_texts']))
    print(T('cli.stm_stats', stats['stm_active'], stats['stm_total'], stats['stm_texts']))
    print(T('cli.writes', stats['writes']))
    print(T('cli.reads', stats['reads']))
    print(T('cli.consolidations', stats['consolidations']))
    print(T('cli.steps', stats['steps']))
    print(T('cli.fatigue', stats['fatigue']))
    print(T('cli.device', stats['device']))
    return 0


def cmd_consolidate(memory: TextMemory, args) -> int:
    '''Forces consolidation.'''
    stats = memory.consolidate()
    memory.save()
    print(T('cli.consolidate_done'))
    print(T('cli.transferred', stats.get('consolidated_centers', 0)))
    print(T('cli.new_ltm', stats.get('new_ltm_centers', 0)))
    print(T('cli.integrated', stats.get('integrated_centers', 0)))
    return 0


def cmd_reset(memory: TextMemory, args) -> int:
    '''Resets memory.'''
    if not args.confirm:
        print(T('cli.reset_warning'))
        return 1
    memory.reset()
    memory.save()
    print(T('cli.reset_done'))
    return 0


def cmd_interactive(memory: TextMemory, args) -> int:
    '''Interactive mode.'''
    print(T('cli.interactive_welcome'))
    print(T('cli.interactive_help'))
    print()

    try:
        while True:
            try:
                line = input('memory> ').strip()
            except (EOFError, KeyboardInterrupt):
                print(f'''\n{T('cli.goodbye')}''')
                break

            if not line:
                continue
            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ''
            if cmd == 'quit' or cmd == 'exit':
                print(T('cli.goodbye'))
                break
            elif cmd == 'store':
                if '|' in rest:
                    (key, value) = rest.split('|', 1)
                    n = memory.store(
                        key.strip(),
                        value.strip(),
                        provenance=_cli_provenance(),
                    )
                    print(T('cli.stored_success_short', n))
                else:
                    print(T('cli.interactive_format_err'))
            elif cmd == 'recall':
                result = memory.recall(rest)
                if result.source == 'EMPTY':
                    print(T('cli.not_found_err'))
                else:
                    print(f'''Result: {result.text}''')
                    print(f'''   (confidence={result.confidence:.3f}, source={result.source})''')
            elif cmd == 'stats':
                cmd_stats(memory, args)
            elif cmd == 'consolidate':
                cmd_consolidate(memory, args)
            elif cmd == 'save':
                memory.save()
            elif cmd == 'step':
                memory.step()
                print(T('cli.step_done'))
            else:
                result = memory.recall(line)
                if result.source != 'EMPTY':
                    print(f'''Result: {result.text}''')
                else:
                    print(T('cli.cmd_unknown', cmd))
            if args.auto_step:
                memory.step()
    finally:
        memory.save()

    return 0


def cmd_batch(memory: TextMemory, args) -> int:
    '''Batch import from a file.'''
    path = Path(args.file)
    if not path.exists():
        print(T('cli.file_not_found', path))
        return 1
    count = 0

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(args.separator, 1)
            if len(parts) != 2:
                continue
            key, value = parts[0].strip(), parts[1].strip()
            memory.store(key, value, provenance=_cli_provenance())
            count += 1

    memory.save()
    print(T('cli.batch_loaded', count, path))
    return 0


def cmd_list(memory: TextMemory, args) -> int:
    '''Lists stored memories.'''
    memories = memory.list_memories(source=args.source, limit=args.limit)
    if not memories:
        print(T('cli.empty'))
        return 0
    print(T('cli.found_n', len(memories)))
    print()
    for i, mem in enumerate(memories, 1):
        print(f'''{i:3}. [{mem['layer']}] {mem['key_text'][:50]}''')
        print(f'''     -> {mem['value_text'][:60]}''')
        print(f'''     (intensity={mem['intensity']:.3f}, age={mem['age']:.0f})''')
    return 0


def cmd_forget(memory: TextMemory, args) -> int:
    '''Forgets memories.'''
    n = memory.forget(key_pattern=args.pattern, exact_match=args.exact, source=args.source)
    if n > 0:
        memory.save()
    else:
        print(T('cli.not_found_err'))
    return 0 if n > 0 else 1


def cmd_edit(memory: TextMemory, args) -> int:
    '''Edits a memory value.'''
    n = memory.edit(old_value=args.old_value, new_value=args.new_value, source=args.source)
    if n > 0:
        memory.save()
    else:
        print(T('cli.not_found_err'))
    return 0 if n > 0 else 1


def cmd_search(memory: TextMemory, args) -> int:
    '''Searches memories.'''
    results = memory.search(query=args.query, top_k=args.top_k, source=args.source)
    if not results:
        print(T('cli.not_found'))
        return 1
    print(T('cli.search_results', len(results)))
    print()
    for i, res in enumerate(results, 1):
        print(f'''{i:3}. [{res['source']}] {res['key'][:50]}''')
        print(f'''     -> {res['value'][:60]}''')
        print(f'''     (similarity={res['similarity']:.3f})''')
    return 0


def main():
    '''Main CLI entry point.'''

    try:
        data_dir = get_data_dir()
        settings_mgr = SettingsManager(data_dir)
        Localization.set_language(settings_mgr.get_ui_language())
    except Exception:
        pass

    parser = create_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0
    memory = TextMemory(state_file=args.state_file, device=args.device, auto_load=not (args.no_load))
    if args.command == 'store':
        return cmd_store(memory, args)
    if args.command == 'recall':
        return cmd_recall(memory, args)
    if args.command == 'stats':
        return cmd_stats(memory, args)
    if args.command == 'consolidate':
        return cmd_consolidate(memory, args)
    if args.command == 'reset':
        return cmd_reset(memory, args)
    if args.command == 'interactive':
        return cmd_interactive(memory, args)
    if args.command == 'batch':
        return cmd_batch(memory, args)
    if args.command == 'list':
        return cmd_list(memory, args)
    if args.command == 'forget':
        return cmd_forget(memory, args)
    if args.command == 'edit':
        return cmd_edit(memory, args)
    if args.command == 'search':
        return cmd_search(memory, args)
    parser.print_help()
    return 1

if __name__ == '__main__':
    sys.exit(main())
