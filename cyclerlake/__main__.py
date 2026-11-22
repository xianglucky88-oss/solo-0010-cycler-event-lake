import argparse
import json
from pathlib import Path
import sqlite3

from .lake import ingest, query
from .settlement import configure, history, report, sign
from .lake import _database
from .settlement import query_block


def _settlement_block(path, run, gap_ms, base):
    """Attach settlement classification to a run query when steps exist.

    The runtime still answers what was sampled; settlement adds the separate
    distinction between observed integration, confirmed missing
    measurement, meter-backed-but-unsigned charge and signed bills.
    """
    with _database(path) as conn:
        conn.execute('BEGIN')
        block = query_block(conn, run, gap_ms=gap_ms)
    if block is None:
        return base
    signed = [entry['step'] for entry in block['steps'] if 'bill' in entry]
    return dict(base, settlement={
        'steps': block['steps'],
        'links': block['links'],
        'signed_steps': signed,
    })


def main():
    parser = argparse.ArgumentParser(description='Ingest current samples and read sealed integration facts')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)

    import_chunk = commands.add_parser('ingest')
    import_chunk.add_argument('source', type=Path)
    import_chunk.add_argument('--gap-ms', type=int, required=True)

    read = commands.add_parser('query')
    read.add_argument('run')
    read.add_argument('--gap-ms', type=int, default=12)

    setup = commands.add_parser('configure')
    setup.add_argument('source', type=Path)

    settle = commands.add_parser('settle')
    settle.add_argument('--step')
    settle.add_argument('--gap-ms', type=int, default=12)

    bill = commands.add_parser('sign')
    bill.add_argument('step')
    bill.add_argument('--mode', choices=['pre', 'group'], default='pre')
    bill.add_argument('--gap-ms', type=int, default=12)

    audit = commands.add_parser('history')
    audit.add_argument('step')

    args = parser.parse_args()
    try:
        if args.command == 'ingest':
            result = {'status': ingest(args.db, json.loads(args.source.read_text(encoding='utf-8')),
                                       gap_ms=args.gap_ms)}
        elif args.command == 'query':
            result = _settlement_block(args.db, args.run, args.gap_ms, query(args.db, args.run))
        elif args.command == 'configure':
            result = configure(args.db, json.loads(args.source.read_text(encoding='utf-8')))
        elif args.command == 'settle':
            result = report(args.db, gap_ms=args.gap_ms, step=args.step)
        elif args.command == 'sign':
            result = sign(args.db, args.step, mode=args.mode, gap_ms=args.gap_ms)
        else:
            result = history(args.db, args.step)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
