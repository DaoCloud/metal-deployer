"""Small helpers shared by mtest modules."""

import os
import re

import yaml


class ConfigError(Exception):
    """A configuration file cannot be read at all."""


class Problems:
    """Collects validation errors and warnings instead of stopping at the first one."""

    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @property
    def ok(self):
        return not self.errors


def natural_key(value):
    """Sort key so that R2 < R10 and t2 < t10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(value))]


def dig(data, path, default=None):
    """Read a nested value: dig(cfg, "a.b.c")."""
    cur = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def deep_merge(base, override):
    """Return a new dict where nested dicts are merged and other values from override win."""
    out = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_yaml(path):
    if not os.path.isfile(path):
        raise ConfigError("{}: file not found".format(path))
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError("{}: invalid YAML: {}".format(path, e))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("{}: top level must be a mapping".format(path))
    return data


def unknown_keys(data, schema, path, free_paths, out):
    """Collect keys in data that the schema (a sample document) does not have.

    Paths in free_paths hold user-defined keys (env maps, test cases) and are not checked.
    """
    if path in free_paths or not isinstance(data, dict) or not isinstance(schema, dict):
        return
    for key, value in data.items():
        sub = "{}.{}".format(path, key) if path else str(key)
        if key not in schema:
            out.append(sub)
        else:
            unknown_keys(value, schema[key], sub, free_paths, out)


def env_str(value):
    """YAML may give numbers or booleans for env values; containers need strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)
