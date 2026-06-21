#!/usr/bin/env python3
"""
Smartling Screenshot Matcher
Scans all projects for keys that have screenshot context, then finds similarly-named
keys that have no screenshot and binds them to the same context (using OCR auto-binding
so Smartling highlights the correct source string in the image).

Usage:
    python main.py                 # dry-run across all projects
    python main.py --apply         # apply changes (auto-bind mode)
    python main.py --apply --manual-bind  # apply with manual binding (no OCR)
    python main.py --project <id>  # limit to one project
    python main.py --threshold 0.8 # stricter fuzzy match (default 0.7)
"""

import os
import sys
import argparse
import requests
from difflib import SequenceMatcher
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.smartling.com"
DEFAULT_THRESHOLD = 0.7  # fuzzy similarity threshold


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authenticate(user_id: str, user_secret: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/auth-api/v2/authenticate",
        json={"userIdentifier": user_id, "userSecret": user_secret},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["response"]["data"]["accessToken"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# API helpers (all paginated)
# ---------------------------------------------------------------------------

def _get_all(token: str, url: str, params: dict = None) -> list:
    """Fetch all pages from a Smartling list endpoint."""
    items = []
    offset = 0
    limit = 500
    params = dict(params or {})
    while True:
        params.update({"offset": offset, "limit": limit})
        resp = requests.get(url, headers=auth_headers(token), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()["response"]["data"]
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return items


def get_account_uid(token: str) -> str:
    resp = requests.get(
        f"{BASE_URL}/accounts-api/v2/accounts",
        headers=auth_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    accounts = resp.json()["response"]["data"]["items"]
    if not accounts:
        raise RuntimeError("No Smartling accounts found for these credentials.")
    return accounts[0]["accountUid"]


def get_projects(token: str, account_uid: str) -> list:
    return _get_all(token, f"{BASE_URL}/projects-api/v2/projects", {"accountUid": account_uid})


def get_strings(token: str, project_id: str) -> list:
    return _get_all(token, f"{BASE_URL}/strings-api/v2/projects/{project_id}/strings")


def get_bindings(token: str, project_id: str) -> list:
    return _get_all(token, f"{BASE_URL}/context-api/v2/projects/{project_id}/bindings")


# ---------------------------------------------------------------------------
# Similarity logic
# ---------------------------------------------------------------------------

def are_similar(key_a: str, key_b: str, threshold: float) -> bool:
    """Return True if two key names are 'similar' by any of the three methods."""
    if key_a == key_b:
        return False

    # Method 1: Fuzzy character-level similarity
    if SequenceMatcher(None, key_a, key_b).ratio() >= threshold:
        return True

    # Method 2: Same dot-notation namespace prefix (e.g. "home.title" ~ "home.subtitle")
    parts_a = key_a.split(".")
    parts_b = key_b.split(".")
    if len(parts_a) > 1 and len(parts_b) > 1 and parts_a[:-1] == parts_b[:-1]:
        return True

    # Method 3: One key is a substring of the other
    if key_a in key_b or key_b in key_a:
        return True

    return False


def find_best_match(target_key: str, source_keys: list[str], threshold: float) -> str | None:
    """Return the most similar source key (highest ratio), or None."""
    best_key = None
    best_score = -1.0
    for sk in source_keys:
        if are_similar(target_key, sk, threshold):
            score = SequenceMatcher(None, target_key, sk).ratio()
            if score > best_score:
                best_score = score
                best_key = sk
    return best_key


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------

def auto_bind(token: str, project_id: str, context_uid: str, hashcodes: list[str]) -> dict:
    """
    Ask Smartling to OCR the screenshot and bind the given strings to it.
    Smartling will highlight where each source string appears in the image.
    """
    resp = requests.post(
        f"{BASE_URL}/context-api/v2/projects/{project_id}/contexts/{context_uid}/auto-binding",
        headers=auth_headers(token),
        json={"stringHashcodes": hashcodes},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["response"]["data"]


def manual_bind(token: str, project_id: str, context_uid: str, hashcode: str) -> dict:
    """Manually bind a context to a string (no OCR, no bounding-box)."""
    resp = requests.post(
        f"{BASE_URL}/context-api/v2/projects/{project_id}/bindings",
        headers=auth_headers(token),
        json={"bindings": [{"contextUid": context_uid, "stringHashcode": hashcode}]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["response"]["data"]


# ---------------------------------------------------------------------------
# Per-project processing
# ---------------------------------------------------------------------------

def process_project(
    token: str,
    project: dict,
    threshold: float,
    apply: bool,
    use_auto_bind: bool,
) -> int:
    project_id = project["projectId"]
    project_name = project.get("projectName", project_id)
    print(f"\n{'='*64}")
    print(f"  Project: {project_name}  ({project_id})")
    print(f"{'='*64}")

    print("  Fetching strings...", end=" ", flush=True)
    strings = get_strings(token, project_id)
    print(f"{len(strings)} found")

    print("  Fetching context bindings...", end=" ", flush=True)
    bindings = get_bindings(token, project_id)
    print(f"{len(bindings)} found")

    # Build hashcode -> set[contextUid] from existing bindings
    hashcode_to_contexts: dict[str, set[str]] = {}
    for b in bindings:
        hc = b.get("stringHashcode")
        ctx = b.get("contextUid")
        if hc and ctx:
            hashcode_to_contexts.setdefault(hc, set()).add(ctx)

    # Build key -> string record
    key_to_string: dict[str, dict] = {}
    for s in strings:
        for k_entry in s.get("keys", []):
            key_to_string[k_entry["key"]] = s

    keys_with_ctx = {
        k: s for k, s in key_to_string.items()
        if s.get("hashcode") in hashcode_to_contexts
    }
    keys_without_ctx = {
        k: s for k, s in key_to_string.items()
        if s.get("hashcode") not in hashcode_to_contexts
    }

    print(f"  Keys with screenshots   : {len(keys_with_ctx)}")
    print(f"  Keys without screenshots: {len(keys_without_ctx)}")

    if not keys_with_ctx or not keys_without_ctx:
        print("  Nothing to match.")
        return 0

    source_keys = list(keys_with_ctx.keys())
    matches_applied = 0

    for target_key, target_str in keys_without_ctx.items():
        best = find_best_match(target_key, source_keys, threshold)
        if best is None:
            continue

        source_str = keys_with_ctx[best]
        source_contexts = hashcode_to_contexts[source_str["hashcode"]]
        target_hashcode = target_str.get("hashcode", "")
        target_text = (target_str.get("stringText") or "")[:80]

        print(f"\n  MATCH  '{target_key}'")
        print(f"  <- FROM '{best}'")
        print(f"  String : {target_text!r}")
        print(f"  Contexts to add: {len(source_contexts)}")

        for ctx_uid in source_contexts:
            if apply:
                try:
                    if use_auto_bind:
                        auto_bind(token, project_id, ctx_uid, [target_hashcode])
                        print(f"  [AUTO-BOUND] context {ctx_uid}")
                    else:
                        manual_bind(token, project_id, ctx_uid, target_hashcode)
                        print(f"  [BOUND]      context {ctx_uid}")
                    matches_applied += 1
                except requests.HTTPError as exc:
                    print(f"  [ERROR] {exc.response.status_code}: {exc.response.text[:200]}")
            else:
                mode_label = "AUTO-BIND" if use_auto_bind else "BIND"
                print(f"  [DRY-RUN/{mode_label}] would bind context {ctx_uid}")
                matches_applied += 1

    return matches_applied


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Copy Smartling screenshot contexts from keys to similarly-named keys that lack them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to Smartling (default is dry-run).",
    )
    parser.add_argument(
        "--manual-bind",
        action="store_true",
        help="Use simple manual binding instead of OCR auto-binding.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="0.0-1.0",
        help=f"Fuzzy similarity threshold (default: {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--project",
        metavar="PROJECT_ID",
        help="Limit processing to a single project ID.",
    )
    args = parser.parse_args()

    use_auto_bind = not args.manual_bind

    user_id = os.getenv("SMARTLING_USER_ID")
    user_secret = os.getenv("SMARTLING_USER_SECRET")
    account_uid = os.getenv("SMARTLING_ACCOUNT_UID")  # optional

    if not user_id or not user_secret:
        sys.exit(
            "ERROR: SMARTLING_USER_ID and SMARTLING_USER_SECRET must be set in .env\n"
            "Copy .env.example to .env and fill in your credentials."
        )

    print("Authenticating with Smartling...")
    token = authenticate(user_id, user_secret)
    print("OK")

    if not account_uid:
        print("Fetching account UID...")
        account_uid = get_account_uid(token)
        print(f"Account UID: {account_uid}")

    print("Fetching projects...")
    projects = get_projects(token, account_uid)
    print(f"Found {len(projects)} project(s)")

    if args.project:
        projects = [p for p in projects if p["projectId"] == args.project]
        if not projects:
            sys.exit(f"ERROR: Project '{args.project}' not found in this account.")

    if not args.apply:
        print("\n*** DRY-RUN — no changes will be made (re-run with --apply to apply) ***")
    else:
        mode = "OCR auto-binding" if use_auto_bind else "manual binding"
        print(f"\n*** APPLY MODE ({mode}) — writing changes to Smartling ***")

    total = 0
    for project in projects:
        try:
            total += process_project(token, project, args.threshold, args.apply, use_auto_bind)
        except requests.HTTPError as exc:
            print(f"\n[SKIP] Project error: {exc}")

    print(f"\n{'='*64}")
    action = "matches found" if not args.apply else "bindings applied"
    print(f"Total {action}: {total}")
    print("Done.")


if __name__ == "__main__":
    main()
