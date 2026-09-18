"""Typed, versioned loaders for external provider and storage configuration.

``providers.yaml`` and ``storage.yaml`` are the target source of truth for
non-secret provider/storage parameters (PRD-F015, Issue #87).  This module
loads, validates and resolves those files.  Credential values are never
accepted from the files — only environment-variable references — and the
resolved snapshot carries only non-secret provenance.

Migration boundary (PRD-F015 §6): the SQLite model-settings store and the
external files are never silently merged.  When the external files are
present they take precedence; the SQLite store is read-only during the bounded
migration period.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .model_settings import (CAPABILITIES, LEGACY_CAPABILITIES, PROTOCOLS,
                              validate_profile, apply_route_defaults)
from .tos_adapter import validate_storage_store
from .validation import load_document


PROVIDERS_SCHEMA_VERSION = 1
STORAGE_SCHEMA_VERSION = 1


def _validate_env_name(value, label):
    if not isinstance(value, str) or not value or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', value):
        raise ValueError(f'{label} must be a valid environment-variable name')


def load_providers_config(path):
    """Load and validate ``providers.yaml``.

    Returns a resolved dict with ``schema_version``, ``profiles`` and
    ``routes``.  Profiles are validated through ``validate_profile`` so the
    external-file format and the SQLite format share the same contract.
    """
    raw = load_document(path)
    if not isinstance(raw, dict):
        raise ValueError('providers config must be a mapping')
    allowed = {'schema_version', 'profiles', 'routes'}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f'providers config has unsupported fields: {sorted(extra)}')
    version = raw.get('schema_version')
    if type(version) is not int or version != PROVIDERS_SCHEMA_VERSION:
        raise ValueError(f'providers schema_version must be {PROVIDERS_SCHEMA_VERSION}')
    profiles = raw.get('profiles')
    if not isinstance(profiles, list) or not profiles:
        raise ValueError('providers config must declare at least one profile')
    if len(profiles) > 50:
        raise ValueError('providers config may declare at most 50 profiles')
    validated = [validate_profile(p) for p in profiles]
    by_id = {p['id']: p for p in validated}
    if len(by_id) != len(validated):
        raise ValueError('profile ids must be unique')
    routes = raw.get('routes')
    if not isinstance(routes, dict) or set(routes) - set(CAPABILITIES):
        raise ValueError('routes must be a mapping of capability names')
    for role in LEGACY_CAPABILITIES:
        if role not in routes:
            raise ValueError(f'routes must configure the {role} capability')
    routes = {role: routes.get(role) for role in CAPABILITIES}
    for role, selected in routes.items():
        if selected is not None and (not isinstance(selected, str) or selected not in by_id
                or not by_id[selected]['enabled'] or role not in by_id[selected]['capabilities']):
            raise ValueError(f'route {role} references a non-existent, disabled or incompatible profile')
    document = {'schema_version': version, 'profiles': validated, 'routes': routes}
    document, _added = apply_route_defaults(document)
    return document


def load_storage_config(path):
    """Load and validate ``storage.yaml``.

    Returns a resolved dict with ``schema_version`` and ``stores``.  Each
    store is validated through ``validate_storage_store``; credential values
    are rejected — only ``*_env`` references are accepted.
    """
    raw = load_document(path)
    if not isinstance(raw, dict):
        raise ValueError('storage config must be a mapping')
    allowed = {'schema_version', 'stores'}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f'storage config has unsupported fields: {sorted(extra)}')
    version = raw.get('schema_version')
    if type(version) is not int or version != STORAGE_SCHEMA_VERSION:
        raise ValueError(f'storage schema_version must be {STORAGE_SCHEMA_VERSION}')
    stores = raw.get('stores')
    if not isinstance(stores, list) or not stores:
        raise ValueError('storage config must declare at least one store')
    if len(stores) > 20:
        raise ValueError('storage config may declare at most 20 stores')
    validated = [validate_storage_store(s) for s in stores]
    by_id = {s['id']: s for s in validated}
    if len(by_id) != len(validated):
        raise ValueError('storage store ids must be unique')
    return {'schema_version': version, 'stores': validated}


def resolve_credentials(providers_config):
    """Resolve credential values from the environment for each profile.

    A profile with no ``credential_env`` gets an empty string (e.g. a local
    model).  The resolved values stay in memory and never enter the snapshot.
    """
    keys = {}
    for profile in providers_config['profiles']:
        env_name = profile.get('credential_env', '')
        keys[profile['id']] = os.environ.get(env_name, '') if env_name else ''
    return keys


def _default_providers_path():
    return os.environ.get('AIVOICEBENCH_PROVIDERS_CONFIG', '/etc/aivoicebench/providers.yaml')


def _default_storage_path():
    return os.environ.get('AIVOICEBENCH_STORAGE_CONFIG', '/etc/aivoicebench/storage.yaml')


def resolve_external_config(providers_path=None, storage_path=None):
    """Resolve external provider/storage configuration if it is active.

    Returns ``(providers_config, storage_config, source)`` when the providers
    file exists and is readable, or ``None`` when no external config is active.

    The storage file is optional: it is only required when a File ASR profile
    needs object-storage transport.  When it is absent, ``storage_config`` is
    ``None`` and inline transport remains available for eligible recordings.

    ``source`` is a human-readable label for the active configuration source,
    used for migration diagnostics.
    """
    p_path = Path(providers_path or _default_providers_path())
    if not p_path.is_file():
        return None
    providers_config = load_providers_config(p_path)
    s_path = Path(storage_path or _default_storage_path())
    storage_config = load_storage_config(s_path) if s_path.is_file() else None
    source = f'external:{p_path}'
    # Cross-reference: if a profile references a storage store, the store must
    # exist in storage.yaml (when storage.yaml is loaded).
    if storage_config is not None:
        store_ids = {s['id'] for s in storage_config['stores']}
        for profile in providers_config['profiles']:
            ref = profile['parameters'].get('object_storage_ref', '')
            if ref and ref not in store_ids:
                raise ValueError(
                    f"profile '{profile['id']}' references storage store '{ref}' "
                    f'which is not declared in storage.yaml')
    return providers_config, storage_config, source


def resolve_runtime(providers_path=None, storage_path=None):
    """Resolve external config and build RunProviders.

    Returns ``(snapshot, RunProviders)`` mirroring ``ModelSettings.capture()``
    but sourced entirely from the external files.  Used by CLI and API entry
    points that opt into the file-based configuration.
    """
    from .model_settings import build_run_providers
    external = resolve_external_config(providers_path, storage_path)
    if external is None:
        raise FileNotFoundError('No external providers configuration is active')
    providers_config, storage_config, source = external
    keys = resolve_credentials(providers_config)
    doc, providers = build_run_providers(providers_config, keys, storage_config)
    doc['config_source'] = source
    return doc, providers
