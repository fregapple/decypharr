#!/usr/bin/env python3
"""
Reconcile Decypharr torrents against Arr library symlinks.

How it works:
1) Reads Decypharr torrents from /api/torrents (paginated).
2) Reads Arr instances from /api/arrs.
3) For each Arr, fetches media file paths via Arr API and keeps only symlink files.
4) Resolves each symlink target and matches it to a Decypharr torrent mount path.
5) Torrents with zero symlink matches are considered orphan candidates.
6) Optional deletion via Decypharr DELETE /api/torrents.

Default behavior is dry-run (no deletion).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.request import Request, urlopen


@dataclass
class ArrInstance:
    name: str
    host: str
    token: str
    arr_type: str


@dataclass
class TorrentEntry:
    info_hash: str
    debrid_id: str
    name: str
    mount_path: str
    active_provider: str
    protocol: str
    file_name: str = ""


class HTTPErrorWithBody(RuntimeError):
    pass


VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}


def display_name_for_torrent(t: TorrentEntry) -> str:
    if t.file_name:
        return t.file_name
    if t.mount_path:
        basename = os.path.basename(os.path.normpath(t.mount_path))
        if basename:
            return basename
    return t.name


def torrent_folder_for_torrent(t: TorrentEntry) -> str:
    if t.mount_path:
        basename = os.path.basename(os.path.normpath(t.mount_path))
        if basename:
            return basename
    if t.name:
        return t.name
    return display_name_for_torrent(t)


def display_rd_id_for_torrent(t: TorrentEntry) -> str:
    return t.debrid_id or t.info_hash


def build_search_regex_from_name(name: str) -> str:
    # Build a tolerant ordered-token matcher for brittle regex search bars.
    tokens = re.findall(r"[A-Za-z0-9]+", name)
    if not tokens:
        return re.escape(name)
    return ".*".join(tokens)


def normalize_provider_name(name: str) -> str:
    compact = "".join(ch for ch in (name or "").lower() if ch.isalnum())
    aliases = {
        "rd": "realdebrid",
        "realdebrid": "realdebrid",
        "realdebridcom": "realdebrid",
        "ad": "alldebrid",
        "alldebrid": "alldebrid",
        "dl": "debridlink",
        "debridlink": "debridlink",
        "tb": "torbox",
        "torbox": "torbox",
    }
    return aliases.get(compact, compact)


# Optional built-in Arr host overrides.
# Use this when Decypharr stores internal hostnames (e.g., Docker DNS names)
# that are not resolvable from the machine running this script.
#
# Format: "arr_name": "http://LAN_IP:PORT"
ARR_HOST_OVERRIDES: Dict[str, str] = {}


# Optional path remapping for Arr file paths returned by Sonarr/Radarr.
# This is useful when Arr runs in containers and returns in-container paths
# that differ from the host paths where this script runs.
#
# Format: "source_prefix": "destination_prefix"
ARR_PATH_PREFIX_MAP: Dict[str, str] = {}


def load_config_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("config file root must be a JSON object")
    return raw


def config_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = cfg.get(key, default)
    return bool(value)


def config_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_str(cfg: Dict[str, Any], key: str, default: str = "") -> str:
    value = cfg.get(key)
    if value is None:
        return default
    return str(value)


def config_map(cfg: Dict[str, Any], key: str) -> Dict[str, str]:
    value = cfg.get(key, {})
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in value.items():
        if k is None or v is None:
            continue
        out[str(k)] = str(v)
    return out


def parse_name_url_mappings(values: List[str]) -> Dict[str, str]:
    mappings: Dict[str, str] = {}
    for raw in values:
        text = (raw or "").strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"Invalid --arr-host-map value '{raw}'. Expected NAME=URL")
        name, url = text.split("=", 1)
        name = name.strip()
        url = url.strip().rstrip("/")
        if not name or not url:
            raise ValueError(f"Invalid --arr-host-map value '{raw}'. Expected NAME=URL")
        mappings[name] = url
    return mappings


def parse_prefix_mappings(values: List[str]) -> Dict[str, str]:
    mappings: Dict[str, str] = {}
    for raw in values:
        text = (raw or "").strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"Invalid --path-map value '{raw}'. Expected SRC_PREFIX=DST_PREFIX")
        src, dst = text.split("=", 1)
        src = src.strip()
        dst = dst.strip()
        if not src or not dst:
            raise ValueError(f"Invalid --path-map value '{raw}'. Expected SRC_PREFIX=DST_PREFIX")
        mappings[src] = dst
    return mappings


def normalize_path_for_prefix(path: str) -> str:
    return (path or "").replace("\\", "/").rstrip("/")


def remap_path_prefix(path: str, prefix_map: Dict[str, str]) -> str:
    if not prefix_map:
        return path

    original = path
    normalized_path = normalize_path_for_prefix(path)
    best_src = ""
    best_dst = ""

    for src, dst in prefix_map.items():
        src_norm = normalize_path_for_prefix(src)
        if not src_norm:
            continue
        if normalized_path == src_norm or normalized_path.startswith(src_norm + "/"):
            if len(src_norm) > len(best_src):
                best_src = src_norm
                best_dst = dst

    if not best_src:
        return original

    suffix = normalized_path[len(best_src):]
    remapped = normalize_path_for_prefix(best_dst) + suffix
    return os.path.normpath(remapped)


def host_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""


def norm_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = path.lstrip("/")
    return f"{base}/{path}"


def http_json(url: str, token: str, method: str = "GET"):
    req = Request(url=url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=30) as resp:
        payload = resp.read().decode("utf-8")
        if resp.status < 200 or resp.status >= 300:
            raise HTTPErrorWithBody(f"HTTP {resp.status} for {url}: {payload[:500]}")
        if not payload:
            return None
        return json.loads(payload)


def fetch_decypharr_queue_torrents(base_url: str, token: str) -> List[TorrentEntry]:
    page = 1
    limit = 100
    torrents: List[TorrentEntry] = []

    while True:
        q = urlencode({"page": page, "limit": limit})
        url = norm_url(base_url, f"api/torrents?{q}")
        data = http_json(url, token)
        rows = data.get("torrents", []) if isinstance(data, dict) else []
        for row in rows:
            active_provider = str(row.get("active_provider", ""))
            debrid_id = ""
            providers = row.get("providers", {})
            if isinstance(providers, dict) and active_provider:
                provider_entry = providers.get(active_provider)
                if isinstance(provider_entry, dict):
                    debrid_id = str(provider_entry.get("id", ""))
            if not debrid_id:
                # Fallbacks for alternate payload shapes.
                debrid_id = str(row.get("debrid_id", "") or row.get("id", ""))

            torrents.append(
                TorrentEntry(
                    info_hash=str(row.get("info_hash", "")).lower(),
                    debrid_id=debrid_id,
                    name=str(row.get("name", "")),
                    mount_path=str(row.get("mount_path", "")),
                    active_provider=active_provider,
                    protocol=str(row.get("protocol", "")),
                )
            )

        total_pages = int(data.get("total_pages", page)) if isinstance(data, dict) else page
        if page >= total_pages:
            break
        page += 1

    return torrents


def fetch_decypharr_browse_torrents(base_url: str, token: str) -> List[TorrentEntry]:
    page = 1
    limit = 100
    torrents: List[TorrentEntry] = []

    while True:
        q = urlencode({"page": page, "limit": limit})
        url = norm_url(base_url, f"api/browse/torrents?{q}")
        data = http_json(url, token)
        rows = data.get("entries", []) if isinstance(data, dict) else []
        for row in rows:
            info_hash = str(row.get("info_hash", "") or row.get("infohash", "")).lower()
            torrents.append(
                TorrentEntry(
                    info_hash=info_hash,
                    debrid_id=str(row.get("id", "") or row.get("debrid_id", "")),
                    name=str(row.get("name", "")),
                    mount_path="",
                    active_provider=str(row.get("active_debrid", "")),
                    protocol="torrent",
                )
            )

        total_pages = int(data.get("total_pages", page)) if isinstance(data, dict) else page
        if page >= total_pages:
            break
        page += 1

    return torrents


def fetch_decypharr_torrents(base_url: str, token: str, verbose: bool = False) -> List[TorrentEntry]:
    queue_torrents = fetch_decypharr_queue_torrents(base_url, token)
    browse_torrents = fetch_decypharr_browse_torrents(base_url, token)

    by_key: Dict[str, TorrentEntry] = {}
    for source in (queue_torrents, browse_torrents):
        for t in source:
            key = t.info_hash or f"name::{t.name.lower()}::provider::{normalize_provider_name(t.active_provider)}"
            if key not in by_key:
                by_key[key] = t
                continue
            existing = by_key[key]
            if not existing.mount_path and t.mount_path:
                by_key[key] = t
                continue
            if not existing.debrid_id and t.debrid_id:
                existing.debrid_id = t.debrid_id

    merged = list(by_key.values())
    if verbose:
        print(
            f"[info] Decypharr torrent sources: queue={len(queue_torrents)}, browse={len(browse_torrents)}, merged={len(merged)}"
        )
    return merged


def current_torrent_hashes(base_url: str, token: str) -> Set[str]:
    return {t.info_hash for t in fetch_decypharr_torrents(base_url, token) if t.info_hash}


def fetch_torrent_children(base_url: str, token: str, torrent_name: str) -> List[dict]:
    url = norm_url(base_url, f"api/browse/__all__/{quote(torrent_name, safe='')}")
    data = http_json(url, token)
    rows = data.get("entries", []) if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict) and not bool(row.get("is_dir", False))]


def choose_primary_file_name(children: List[dict]) -> str:
    if not children:
        return ""

    def sort_key(row: dict):
        name = str(row.get("name", ""))
        size = int(row.get("size", 0) or 0)
        ext = os.path.splitext(name)[1].lower()
        is_video = ext in VIDEO_EXTENSIONS
        return (is_video, size, name.lower())

    return str(max(children, key=sort_key).get("name", ""))


def enrich_torrent_file_names(base_url: str, token: str, torrents: List[TorrentEntry], verbose: bool = False) -> None:
    resolved = 0
    for torrent in torrents:
        try:
            children = fetch_torrent_children(base_url, token, torrent.name)
        except Exception as exc:
            if verbose:
                print(f"[warn] failed to fetch child files for {torrent.name}: {exc}", file=sys.stderr)
            continue

        file_name = choose_primary_file_name(children)
        if file_name:
            torrent.file_name = file_name
            resolved += 1

    if verbose and torrents:
        print(f"[info] resolved primary file names for {resolved}/{len(torrents)} orphan candidates")


def infer_arr_type(name: str, arr_type: str) -> str:
    t = (arr_type or "").strip().lower()
    if t in {"sonarr", "radarr"}:
        return t
    n = (name or "").strip().lower()
    if "sonarr" in n:
        return "sonarr"
    if "radarr" in n:
        return "radarr"
    return ""


def fetch_arr_instances(base_url: str, token: str, host_overrides: Dict[str, str]) -> List[ArrInstance]:
    url = norm_url(base_url, "api/arrs")
    data = http_json(url, token)
    arrs: List[ArrInstance] = []
    if not isinstance(data, list):
        return arrs

    for row in data:
        host = str(row.get("host", "")).strip()
        arr_token = str(row.get("token", "")).strip()
        name = str(row.get("name", "")).strip()
        arr_type = infer_arr_type(name, str(row.get("type", "")))
        if name in host_overrides:
            host = host_overrides[name]
        if not host or not arr_token:
            continue
        if arr_type not in {"sonarr", "radarr"}:
            continue
        arrs.append(ArrInstance(name=name, host=host, token=arr_token, arr_type=arr_type))

    return arrs


def arr_get(arr: ArrInstance, endpoint: str):
    url = norm_url(arr.host, endpoint)
    req = Request(url=url, method="GET")
    req.add_header("X-Api-Key", arr.token)
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=30) as resp:
        payload = resp.read().decode("utf-8")
        if resp.status < 200 or resp.status >= 300:
            raise HTTPErrorWithBody(f"HTTP {resp.status} for {url}: {payload[:500]}")
        return json.loads(payload) if payload else []


def sonarr_file_paths(arr: ArrInstance) -> Iterable[str]:
    series = arr_get(arr, "api/v3/series")
    if not isinstance(series, list):
        return
    for s in series:
        series_id = s.get("id")
        if not isinstance(series_id, int):
            continue
        episode_files = arr_get(arr, f"api/v3/episodefile?seriesId={series_id}")
        if not isinstance(episode_files, list):
            continue
        for ef in episode_files:
            p = ef.get("path")
            if isinstance(p, str) and p:
                yield p


def radarr_file_paths(arr: ArrInstance) -> Iterable[str]:
    movies = arr_get(arr, "api/v3/movie")
    if not isinstance(movies, list):
        return
    for m in movies:
        mf = m.get("movieFile")
        if not isinstance(mf, dict):
            continue
        p = mf.get("path")
        if isinstance(p, str) and p:
            yield p


def to_abs_symlink_target(link_path: str) -> Optional[str]:
    try:
        if not os.path.islink(link_path):
            return None
        target = os.readlink(link_path)
    except OSError:
        return None

    if not target:
        return None

    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(link_path), target)

    return os.path.normcase(os.path.normpath(target))


def collect_symlink_targets(
    arrs: List[ArrInstance],
    path_prefix_map: Dict[str, str],
    verbose: bool = False,
) -> Set[str]:
    targets: Set[str] = set()
    scanned_total = 0
    existing_total = 0
    symlink_total = 0
    remapped_total = 0

    for a in arrs:
        if verbose:
            print(f"[info] reading files from {a.name} ({a.arr_type})")

        try:
            if a.arr_type == "sonarr":
                paths = sonarr_file_paths(a)
            elif a.arr_type == "radarr":
                paths = radarr_file_paths(a)
            else:
                continue

            local_count = 0
            for p in paths:
                local_count += 1
                scanned_total += 1
                local_path = remap_path_prefix(p, path_prefix_map)
                if local_path != p:
                    remapped_total += 1

                if os.path.exists(local_path):
                    existing_total += 1
                if os.path.islink(local_path):
                    symlink_total += 1
                t = to_abs_symlink_target(local_path)
                if t:
                    targets.add(t)

            if verbose:
                print(f"[info] {a.name}: scanned {local_count} file paths")

        except Exception as exc:
            print(
                f"[warn] failed to scan {a.name} ({a.host}): {exc}",
                file=sys.stderr,
            )

    if verbose:
        print(
            "[info] symlink stats: "
            f"scanned_paths={scanned_total}, remapped_paths={remapped_total}, existing_paths={existing_total}, symlink_paths={symlink_total}, "
            f"unique_targets={len(targets)}"
        )
        if scanned_total > 0 and symlink_total == 0:
            print(
                "[warn] zero symlinks detected from Arr file paths; this usually means the script cannot see those paths "
                "from this machine, or your setup uses hardlinks/copies instead of symlinks"
            )

    return targets


def is_target_under_mount(target: str, mount_path: str) -> bool:
    if not mount_path:
        return False
    m = os.path.normcase(os.path.normpath(mount_path))
    if target == m:
        return True
    prefix = m + os.sep
    return target.startswith(prefix)


def find_orphan_torrents(
    torrents: List[TorrentEntry],
    symlink_targets: Set[str],
    provider_filter: Optional[str],
    stop_after: int = 0,
) -> List[TorrentEntry]:
    normalized_filter = normalize_provider_name(provider_filter or "")
    orphans: List[TorrentEntry] = []

    for t in torrents:
        if t.protocol.lower() != "torrent":
            continue
        if normalized_filter and normalize_provider_name(t.active_provider) != normalized_filter:
            continue
        if not t.info_hash:
            continue

        is_linked = False
        for target in symlink_targets:
            if t.mount_path:
                is_linked = is_target_under_mount(target, t.mount_path)
            else:
                target_folder = os.path.basename(os.path.dirname(target))
                is_linked = os.path.normcase(target_folder) == os.path.normcase(t.name)

            if is_linked:
                break

        if not is_linked:
            orphans.append(t)
            if stop_after > 0 and len(orphans) >= stop_after:
                break

    return orphans


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for v in values:
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def delete_torrents(
    base_url: str,
    token: str,
    hashes: List[str],
    remove_from_debrid: bool,
    max_attempts: int = 3,
    verify_delay: int = 10,
    hash_to_rd_id: Optional[Dict[str, str]] = None,
    hash_to_folder: Optional[Dict[str, str]] = None,
    verbose: bool = False,
) -> tuple[int, List[str]]:
    """Delete torrents one-by-one with verification passes.

    Some backend failures happen after the queue entry is already removed
    (e.g., duplicate provider placement cleanup). We therefore verify against
    the live torrent list between attempts and only report hashes that remain.

    verify_delay: seconds to wait after issuing deletes before re-checking the
    live list. RD can transiently report an entry as gone immediately after
    deletion and then re-add it if the provider-side delete actually failed.
    A delay of ~10 s is enough for that failure to surface.
    """
    del remove_from_debrid  # Browse delete currently always removes placements server-side.

    pending = dedupe_preserve_order(hashes)
    if not pending:
        return 0, []

    deleted_hashes: Set[str] = set()
    attempts = max(1, max_attempts)

    for attempt in range(1, attempts + 1):
        if not pending:
            break

        if verbose:
            print(f"[info] delete pass {attempt}/{attempts}: attempting {len(pending)} hashes")

        for h in list(pending):
            url = norm_url(base_url, "api/browse/torrents/batch")
            payload = json.dumps({"ids": [h]})
            req = Request(url=url, method="DELETE", data=payload.encode("utf-8"))
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            try:
                with urlopen(req, timeout=30) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        body = resp.read().decode("utf-8", errors="replace")
                        print(f"  [warn] HTTP {resp.status} deleting {h}: {body[:200]}", file=sys.stderr)
            except Exception as e:
                print(f"  [warn] Error deleting {h}: {e}", file=sys.stderr)

        if verify_delay > 0:
            if verbose:
                print(f"[info] waiting {verify_delay}s before verifying deletions...")
            time.sleep(verify_delay)

        try:
            remaining = current_torrent_hashes(base_url, token)
        except Exception as exc:
            print(
                f"[warn] could not refresh torrent list after delete pass {attempt}: {exc}",
                file=sys.stderr,
            )
            continue

        next_pending: List[str] = []
        for h in pending:
            if h in remaining:
                next_pending.append(h)
            else:
                if h not in deleted_hashes:
                    rd_id = (hash_to_rd_id or {}).get(h, h)
                    folder = (hash_to_folder or {}).get(h, "")
                    print(f"{rd_id} | {folder} | Deleted")
                deleted_hashes.add(h)

        if verbose:
            removed_this_pass = len(pending) - len(next_pending)
            print(
                f"[info] delete pass {attempt}: removed={removed_this_pass}, still_present={len(next_pending)}"
            )

        pending = next_pending

    return len(deleted_hashes), pending


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Arr symlink usage against Decypharr torrents")
    parser.add_argument(
        "--config",
        default="",
        help=(
            "Optional JSON config file path. "
            "CLI flags override values from config."
        ),
    )
    parser.add_argument("--base-url", default="", help="Decypharr URL, e.g. http://localhost:8282")
    parser.add_argument("--api-token", default="", help="Decypharr API token")
    parser.add_argument(
        "--provider",
        default="",
        help="Provider filter (default: realdebrid). Set to empty string to include all.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions for orphan candidates (default is dry-run)",
    )
    parser.add_argument(
        "--remove-from-debrid",
        action="store_true",
        help="When deleting, also remove from debrid provider",
    )
    parser.add_argument(
        "--arr-host-map",
        action="append",
        default=[],
        metavar="NAME=URL",
        help=(
            "Override Arr host for specific Arr name. "
            "Repeat per Arr, e.g. --arr-host-map sonarr_client=http://192.168.1.44:8989"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Print progress details")
    parser.add_argument(
        "--path-map",
        action="append",
        default=[],
        metavar="SRC_PREFIX=DST_PREFIX",
        help=(
            "Remap Arr file path prefixes to local filesystem prefixes. "
            "Repeat per mapping, e.g. --path-map /data/media=/mnt/media"
        ),
    )
    parser.add_argument(
        "--allow-zero-symlink-targets",
        action="store_true",
        help=(
            "Allow processing to continue even when zero symlink targets are found. "
            "Use only when you are sure your setup intentionally has no symlinks."
        ),
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=-1,
        help=(
            "Maximum number of orphan candidates to delete when --apply is used. "
            "0 means no limit."
        ),
    )
    parser.add_argument(
        "--delete-attempts",
        type=int,
        default=0,
        help=(
            "Number of verified delete passes when --apply is used. "
            "Each pass re-checks live Decypharr state and retries only entries still present."
        ),
    )
    parser.add_argument(
        "--delete-verify-delay",
        type=int,
        default=-1,
        help=(
            "Seconds to wait after issuing deletes before re-checking the live torrent list. "
            "RD can transiently drop an entry and re-add it if the provider-side delete failed. "
            "Default: 10. Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--full-regex",
        action="store_true",
        help="Print regex for all processed orphan torrents after deletion pass.",
    )
    parser.add_argument(
        "--failed-regex",
        action="store_true",
        help="Print regex for only torrents that still failed deletion after all attempts.",
    )

    args = parser.parse_args()

    cfg: Dict[str, Any] = {}
    if args.config:
        if not os.path.exists(args.config):
            print(f"[error] config file not found: {args.config}", file=sys.stderr)
            return 2
        try:
            cfg = load_config_file(args.config)
        except Exception as exc:
            print(f"[error] failed to load config file '{args.config}': {exc}", file=sys.stderr)
            return 2

    base_url = (args.base_url or "").strip() or config_str(cfg, "base_url", "").strip()
    api_token = (args.api_token or "").strip() or config_str(cfg, "api_token", "").strip()

    provider_filter = (args.provider or "").strip()
    if not provider_filter:
        provider_filter = config_str(cfg, "provider", "realdebrid").strip()

    apply_changes = bool(args.apply or config_bool(cfg, "apply", False))
    remove_from_debrid = bool(args.remove_from_debrid or config_bool(cfg, "remove_from_debrid", False))
    verbose = bool(args.verbose or config_bool(cfg, "verbose", False))
    allow_zero_symlink_targets = bool(
        args.allow_zero_symlink_targets or config_bool(cfg, "allow_zero_symlink_targets", False)
    )
    full_regex = bool(args.full_regex or config_bool(cfg, "full_regex", False))
    failed_regex = bool(args.failed_regex or config_bool(cfg, "failed_regex", False))

    max_delete = args.max_delete if args.max_delete >= 0 else config_int(cfg, "max_delete", 0)
    delete_attempts = args.delete_attempts if args.delete_attempts > 0 else config_int(cfg, "delete_attempts", 3)
    delete_verify_delay = args.delete_verify_delay if args.delete_verify_delay >= 0 else config_int(cfg, "delete_verify_delay", 10)

    if not base_url:
        print("[error] missing base URL. Set --base-url or config key 'base_url'.", file=sys.stderr)
        return 2
    if not api_token:
        print("[error] missing API token. Set --api-token or config key 'api_token'.", file=sys.stderr)
        return 2

    if provider_filter == "*":
        provider_filter = ""
    normalized_provider_filter = normalize_provider_name(provider_filter)

    if max_delete < 0:
        print("[error] --max-delete must be >= 0", file=sys.stderr)
        return 2
    if delete_attempts < 1:
        print("[error] --delete-attempts must be >= 1", file=sys.stderr)
        return 2

    try:
        cli_host_overrides = parse_name_url_mappings(args.arr_host_map)
        cli_path_map = parse_prefix_mappings(args.path_map)
        config_host_overrides = config_map(cfg, "arr_host_map")
        config_path_map = config_map(cfg, "path_map")
        host_overrides = dict(ARR_HOST_OVERRIDES)
        path_prefix_map = dict(ARR_PATH_PREFIX_MAP)
        host_overrides.update(config_host_overrides)
        path_prefix_map.update(config_path_map)
        host_overrides.update(cli_host_overrides)
        path_prefix_map.update(cli_path_map)

        if verbose:
            if path_prefix_map:
                print("[info] active path mappings:")
                for src, dst in sorted(path_prefix_map.items(), key=lambda kv: kv[0]):
                    print(f"[info]   {src} -> {dst}")
            else:
                print("[info] active path mappings: (none)")

        torrents = fetch_decypharr_torrents(base_url, api_token, verbose=verbose)
        arrs = fetch_arr_instances(base_url, api_token, host_overrides)

        if not arrs:
            print("[warn] no supported Arr instances found (Sonarr/Radarr)")
        elif verbose:
            for a in arrs:
                print(f"[info] using Arr host for {a.name}: {a.host}")
                hostname = host_from_url(a.host)
                if hostname and "." not in hostname and hostname.lower() != "localhost":
                    print(
                        f"[info] host '{hostname}' looks like an internal DNS name; if it fails, set --arr-host-map {a.name}=http://LAN_IP:PORT"
                    )

        symlink_targets = collect_symlink_targets(arrs, path_prefix_map, verbose=verbose)

        if len(symlink_targets) == 0 and not allow_zero_symlink_targets:
            print(
                "[error] safety stop: zero symlink targets detected. "
                "Aborting to prevent mass orphan classification."
            )
            print(
                "[error] Fix Arr path visibility/path mapping first, or re-run with "
                "--allow-zero-symlink-targets if this is expected in your setup."
            )
            return 2

        candidate_limit = max_delete if max_delete > 0 else 0
        orphans = find_orphan_torrents(torrents, symlink_targets, provider_filter, stop_after=candidate_limit)
        if full_regex or failed_regex:
            enrich_torrent_file_names(base_url, api_token, orphans, verbose=verbose)

        if verbose:
            provider_counts: Dict[str, int] = {}
            torrent_count = 0
            for t in torrents:
                if t.protocol.lower() != "torrent":
                    continue
                torrent_count += 1
                p = normalize_provider_name(t.active_provider)
                provider_counts[p] = provider_counts.get(p, 0) + 1

            print(f"[info] total torrent entries from Decypharr: {torrent_count}")
            if provider_counts:
                sorted_counts = sorted(provider_counts.items(), key=lambda kv: (-kv[1], kv[0]))
                print("[info] torrent providers: " + ", ".join(f"{k}={v}" for k, v in sorted_counts))
            if normalized_provider_filter:
                print(f"[info] provider filter (normalized): {normalized_provider_filter}")

        scoped_torrents = [
            t
            for t in torrents
            if t.protocol.lower() == "torrent"
            and (
                not normalized_provider_filter
                or normalize_provider_name(t.active_provider) == normalized_provider_filter
            )
        ]

        print(f"Scanned torrents: {len(scoped_torrents)}")
        if candidate_limit > 0 and len(orphans) >= candidate_limit:
            print(f"Orphan candidates: {len(orphans)} (search stopped early at max-delete limit)")
        else:
            print(f"Symlink targets found: {len(symlink_targets)}")
        print(f"Orphan candidates: {len(orphans)}")

        for t in orphans:
            print(f" - {display_rd_id_for_torrent(t)} | {torrent_folder_for_torrent(t)}")

        if not apply_changes:
            if full_regex:
                full_names = [display_name_for_torrent(t) for t in orphans]
                full_pattern = "|".join(build_search_regex_from_name(name) for name in full_names if name)
                print("\nFULL REGEX (orphan candidates):")
                print(full_pattern)
                print()
                print(f"Printed regex for {len(full_names)} orphan candidates.")
            if failed_regex:
                print("[warn] --failed-regex is only available with --apply (it requires delete results).")
            print("Dry run complete. Re-run with --apply to delete orphans and print the DMM cleanup regex.")
            return 0

        selected_orphans = orphans
        total_candidates = len(orphans)

        hashes = [t.info_hash for t in selected_orphans if t.info_hash]
        hashes = dedupe_preserve_order(hashes)
        hash_to_rd_id: Dict[str, str] = {}
        hash_to_folder: Dict[str, str] = {}
        hash_to_regex_name: Dict[str, str] = {}
        for t in selected_orphans:
            if t.info_hash and t.info_hash not in hash_to_folder:
                hash_to_rd_id[t.info_hash] = display_rd_id_for_torrent(t)
                hash_to_folder[t.info_hash] = torrent_folder_for_torrent(t)
                hash_to_regex_name[t.info_hash] = display_name_for_torrent(t)

        print(f"\nDeleting {len(hashes)} torrents from Decypharr...")
        deleted, remaining = delete_torrents(
            base_url,
            api_token,
            hashes,
            remove_from_debrid=remove_from_debrid,
            max_attempts=delete_attempts,
            verify_delay=delete_verify_delay,
            hash_to_rd_id=hash_to_rd_id,
            hash_to_folder=hash_to_folder,
            verbose=verbose,
        )
        remaining_set = set(remaining)
        for h in hashes:
            if h in remaining_set:
                print(f"{hash_to_rd_id.get(h, h)} | {hash_to_folder.get(h, '')} | Failed")

        print(f"Deleted {deleted} of {len(hashes)} torrents.")
        if remaining:
            print(f"[warn] {len(remaining)} torrents still present after {delete_attempts} delete passes.")
            for h in remaining:
                print(f"  [warn] still present: {h}")

        all_names = [hash_to_regex_name.get(h, "") for h in hashes]
        all_pattern = "|".join(build_search_regex_from_name(name) for name in all_names if name)
        failed_names = [hash_to_regex_name.get(h, "") for h in hashes if h in remaining_set]
        failed_pattern = "|".join(build_search_regex_from_name(name) for name in failed_names if name)

        regex_lines_printed = 0
        if full_regex:
            print("\nFULL REGEX (all processed):")
            print(all_pattern)
            print()
            regex_lines_printed += 1

        if failed_regex:
            print("FAILED-ONLY REGEX:")
            print(failed_pattern)
            print()
            regex_lines_printed += 1

        if regex_lines_printed > 0:
            print(
                f"Printed regex for {len(all_names)} processed orphan torrents; "
                f"failed-only regex covers {len(failed_names)} torrents "
                f"(from {total_candidates} candidates)"
            )
        return 0

    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
