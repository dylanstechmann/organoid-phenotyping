"""Shared provider-key loader for repo-vendored agent tools (scholar_mcp, web_mcp).

Lookup order for each key (first hit wins):
  1. process environment (so `aikeys`-loaded shells and harness env overrides work)
  2. $AI_KEYS_FILE, if set to a KEY=VALUE file
  3. repo-local .env (walking up from cwd to the repo root)
  4. ~/.config/ai-keys.env — the private store (mode 600, outside every repo)

This module never prints key values. No third-party dependencies.
"""
import os

_cache = None


def _candidate_files():
    files = []
    env_file = os.environ.get("AI_KEYS_FILE")
    if env_file:
        files.append(env_file)
    d = os.getcwd()
    for _ in range(8):
        files.append(os.path.join(d, ".env"))
        if (os.path.isdir(os.path.join(d, ".git"))
                or os.path.isdir(os.path.join(d, ".agent-tools"))):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    files.append(os.path.expanduser("~/.config/ai-keys.env"))
    return files


def _load_files():
    global _cache
    if _cache is not None:
        return _cache
    merged = {}
    for path in _candidate_files():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    if key.startswith("export "):
                        key = key[len("export "):].strip()
                    val = val.strip().strip('"').strip("'")
                    if key:
                        merged.setdefault(key, val)  # earlier files win
        except OSError:
            continue
    _cache = merged
    return merged


def get_key(name, default=""):
    """Return a key: process environment first, then the file-based stores."""
    val = os.environ.get(name)
    if val:
        return val
    return _load_files().get(name, default)


def have_key(name):
    return bool(get_key(name))
