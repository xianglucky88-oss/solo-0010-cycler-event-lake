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


def note_of(material):
    note = material.get('note')
    if note is not None and (not isinstance(note, str) or len(note) > 200):
        raise ValueError('invalid note')
    return note


def _boolean(value, field):
    if type(value) is not bool:
        raise ValueError(f'{field} must be a boolean')
    return value


def _digest(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest(), raw


def source_of(material):
    return key(material.get('device', 'primary')), integer(material.get('boot', 1), 'boot')


def decode(chunk):
    run, name = key(chunk['run']), key(chunk['chunk'])
    device, boot = source_of(chunk)
    seal = integer(chunk['seal_ms'], 'seal_ms', -1)
    if not isinstance(chunk['samples'], list):
        raise ValueError('samples must be an array')
    samples = []
    for row, item in enumerate(chunk['samples'], 1):
        samples.append((row, integer(item['ms'], 'ms'), integer(item['rev'], 'rev', 1),
                        integer(item['current_ma'], 'current_ma', -10**9, 10**9)))
    digest, raw = _digest(chunk)
    return run, name, device, boot, seal, samples, digest, raw


def decode_priority(material):
    run = key(material['run'])
    items = material['priorities']
    if not isinstance(items, list) or not items:
        raise ValueError('priorities must be a non-empty array')
    ranks = []
    for item in items:
        device, boot = source_of(item)
        ranks.append((device, boot, integer(item['rank'], 'rank', 0, 10**6)))
    return run, ranks


def decode_anchor(material):
    run = key(material['run'])
    device, boot = source_of(material)
    local = integer(material['local_ms'], 'local_ms')
    canonical = integer(material['canonical_ms'], 'canonical_ms')
    start = integer(material['valid_from_ms'], 'valid_from_ms')
    end = integer(material['valid_to_ms'], 'valid_to_ms')
    if start > end or not start <= local <= end:
        raise ValueError('anchor point outside its validity range')
    offset = canonical - local
    if start + offset < 0:
        raise ValueError('anchor maps its validity range before time zero')
    return run, device, boot, start, end, offset


def decode_availability(material):
    run = key(material['run'])
    device, boot = source_of(material)
    start = integer(material['from_ms'], 'from_ms')
    end = integer(material['to_ms'], 'to_ms')
    if start >= end:
        raise ValueError('availability window must be non-empty')
    return run, device, boot, start, end, _boolean(material['available'], 'available')


def decode_seam(material):
    run = key(material['run'])
    at = integer(material['at_ms'], 'at_ms')
    left, right = source_of(material['left']), source_of(material['right'])
    if left == right:
        raise ValueError('seam must join two different sources')
    return run, at, left, right


def decode_correction(material):
    run = key(material['run'])
    cid, receipt = key(material['id']), key(material['receipt'])
    items = material['items']
    if not isinstance(items, list) or not items:
        raise ValueError('items must be a non-empty array')
    out, seen = [], set()
    for item in items:
        device, boot = source_of(item)
        point = (device, boot, integer(item['ms'], 'ms'))
        if point in seen:
            raise ValueError('duplicate correction item')
        seen.add(point)
        out.append((device, boot, point[2], integer(item['rev'], 'rev', 1),
                    integer(item['current_ma'], 'current_ma', -10**9, 10**9)))
    digest, raw = _digest(material)
    return run, cid, receipt, note_of(material), out, raw
