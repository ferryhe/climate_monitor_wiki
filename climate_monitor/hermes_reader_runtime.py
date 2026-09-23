"""Pinned reader's immutable tool selection, separate from mutable job state."""
import hashlib
import json
import os
from pathlib import Path


def collect(root, engine, *, capture=None, commitments=None, bound_files=None):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('unsupported reader runtime; start a fresh run')
    fd = engine.open_file(root, directory=True)
    try:
        st = os.fstat(fd); engine.check(st, directory=True)
        identity = list(engine.signature(st)[:5])
    finally:
        os.close(fd)
    paths = []; absent = []
    for name in ('tools', 'browser-runtimes'):
        path = root / name
        if os.path.lexists(path):
            engine.check(path.lstat(), directory=True)
            paths.append(str(path))
        else:
            absent.append(name)
    value = engine.inventory(paths, capture=capture, commitments=commitments,
                             bound_files=bound_files, scoped=True)
    # The deployed source supports host Playwright. Container adapters require
    # a separately bound container/runtime contract, not a host directory hash.
    for config in (root / 'tools').glob('*/*/*/runtime.json'):
        fd = engine.open_file(config)
        try:
            raw = os.read(fd, 65537)
            if len(raw) > 65536: raise ValueError('unsupported reader runtime')
        finally:
            os.close(fd)
        item = json.loads(raw)
        if item.get('container_image'):
            raise ValueError('unsupported container reader runtime; start a fresh run')
        python = Path(item['python'])
        expected = root / 'browser-runtimes/playwright/bin/python'
        if python != expected or item.get('sdk') != 'playwright':
            raise ValueError('unsupported reader runtime; start a fresh run')
        for name in ('python', 'browser'):
            if str(Path(item[name])) not in (capture if capture is not None else commitments or {}):
                # During the two source passes, inventory has already validated
                # the complete subtree; forbid pointers outside that subtree.
                if not Path(item[name]).is_relative_to(root / 'browser-runtimes/playwright'):
                    raise ValueError('unsupported reader runtime; start a fresh run')
    if list(engine.signature(root.lstat())[:5]) != identity:
        raise ValueError('reader runtime changed; start a fresh run')
    return {'root': str(root), 'identity': identity, 'absent': absent, 'inventory': value}


def verify(root, payload, read, engine, *, bound_files=None):
    commitments = engine.decode_commitments(read(root / 'bootstrap/reader-commitments.zlib'))
    expected = payload['reader_runtime']
    if collect(expected['root'], engine, commitments=commitments, bound_files=bound_files) != expected:
        raise ValueError('reader runtime changed; start a fresh run')


def verify_attempt(root, home, payload, read_private, runtime_inventory):
    """Verify the immutable attempt authority shared by worker and adapter."""
    seal = json.loads(read_private(home / 'attempt-policy.json'))
    binding_path = Path(seal['binding_path'])
    if (seal['schema_version'] != 'climate-acquisition-attempt-policy.v1'
            or binding_path.parent != root.parent
            or seal['snapshot'] != {'schema_version': payload['schema_version'], 'sha256': hashlib.sha256(read_private(root / 'manifest.json')).hexdigest()}
            or home.name != 'attempt-' + str(seal['attempt'])
            or hashlib.sha256(read_private(home / 'config.yaml')).hexdigest() != seal['configuration_sha256']):
        raise ValueError()
    fd = runtime_inventory.open_file(binding_path)
    try:
        before = os.fstat(fd); runtime_inventory.check(before)
        if before.st_size > 4 * 1024 * 1024: raise ValueError()
        binding_raw = os.read(fd, before.st_size + 1)
        if (len(binding_raw) != before.st_size
                or runtime_inventory.signature(os.fstat(fd)) != runtime_inventory.signature(before)):
            raise ValueError()
    finally:
        os.close(fd)
    binding = json.loads(binding_raw)
    if (hashlib.sha256(binding_raw).hexdigest() != seal['binding_sha256']
            or binding['hermes_snapshot'] != seal['snapshot']
            or binding['run_id'] != seal['run_id'] or binding['attempt'] != seal['attempt']
            or payload['source'] != 'climate-acquisition-' + binding['run_id']):
        raise ValueError()
