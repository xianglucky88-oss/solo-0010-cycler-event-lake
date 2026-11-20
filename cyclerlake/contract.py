import hashlib
import json


def integer(value, field, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{field} outside supported integer range')
    return value


def key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError('invalid run or chunk identifier')
    return value


def decode(chunk):
    run, name = key(chunk['run']), key(chunk['chunk'])
    seal = integer(chunk['seal_ms'], 'seal_ms', -1)
    if not isinstance(chunk['samples'], list):
        raise ValueError('samples must be an array')
    samples = []
    for row, item in enumerate(chunk['samples'], 1):
        samples.append((row, integer(item['ms'], 'ms'), integer(item['rev'], 'rev', 1),
                        integer(item['current_ma'], 'current_ma', -10**9, 10**9)))
    raw = json.dumps(chunk, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return run, name, seal, samples, hashlib.sha256(raw.encode()).hexdigest(), raw
