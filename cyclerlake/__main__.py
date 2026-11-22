import argparse
import json
from pathlib import Path
import sqlite3

from . import lake


def _load(source):
    return json.loads(source.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description='Ingest current samples and read sealed integration facts')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    load_chunk = commands.add_parser('ingest')
    load_chunk.add_argument('source', type=Path)
    load_chunk.add_argument('--gap-ms', type=int, required=True)
    for name in ('priority', 'anchor', 'availability', 'seam', 'correct'):
        commands.add_parser(name).add_argument('source', type=Path)
    sign = commands.add_parser('sign')
    sign.add_argument('run')
    sign.add_argument('start_ms', type=int)
    sign.add_argument('end_ms', type=int)
    sign.add_argument('--receipt')
    sign.add_argument('--note')
    commands.add_parser('query').add_argument('run')
    commands.add_parser('settle').add_argument('run')
    args = parser.parse_args()
    try:
        if args.command == 'ingest':
            result = {'status': lake.ingest(args.db, _load(args.source), gap_ms=args.gap_ms)}
        elif args.command == 'priority':
            result = {'status': lake.declare_priority(args.db, _load(args.source))}
        elif args.command == 'anchor':
            result = {'status': lake.add_anchor(args.db, _load(args.source))}
        elif args.command == 'availability':
            result = {'status': lake.add_availability(args.db, _load(args.source))}
        elif args.command == 'seam':
            result = {'status': lake.add_seam(args.db, _load(args.source))}
        elif args.command == 'correct':
            result = lake.correct(args.db, _load(args.source))
        elif args.command == 'sign':
            result = lake.sign(args.db, args.run, args.start_ms, args.end_ms,
                               receipt_id=args.receipt, note=args.note)
        elif args.command == 'query':
            result = lake.query(args.db, args.run)
        else:
            result = lake.settle(args.db, args.run)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
