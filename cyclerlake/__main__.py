import argparse
import json
from pathlib import Path
import sqlite3

from .lake import ingest, query


def main():
    parser = argparse.ArgumentParser(description='Ingest current samples and read sealed integration facts')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    import_chunk = commands.add_parser('ingest')
    import_chunk.add_argument('source', type=Path)
    import_chunk.add_argument('--gap-ms', type=int, required=True)
    commands.add_parser('query').add_argument('run')
    args = parser.parse_args()
    try:
        if args.command == 'ingest':
            result = {'status': ingest(args.db, json.loads(args.source.read_text(encoding='utf-8')), gap_ms=args.gap_ms)}
        else:
            result = query(args.db, args.run)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
