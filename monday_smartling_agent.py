#!/usr/bin/env python3
"""
Monday + Smartling automation agent

For subitems on board 9991668759 where ETA = yesterday:
  1. Parse the Smartling URL in the Task Link column
  2. Get string UIDs from Smartling (by tag filter) and check locale status
  3. Publish any locales that have translations in progress (not yet published)
  4. Comment on the Monday subitem tagging Sanne Heijmans with the published languages
  5. Set Task Status to Done
"""

import json
import os
import re
import sys
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import requests

DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
BOARD_ID = 9991668759

# On Sundays, look back at Friday (skip Saturday which has no deadlines)
_today = date.today()
YESTERDAY = (
    (_today - timedelta(days=2)) if _today.weekday() == 6  # Sunday → Friday
    else (_today - timedelta(days=1))
).strftime("%Y-%m-%d")
SANNE_USER_ID = 217591

# Subitem column IDs
TASK_STATUS_COL = "color_mkyf691e"
ETA_COL = "date0"
TASK_LINK_COL = "link_mkvq6z0h"

# Per-language status column IDs on subitems
LANG_STATUS_COLS = {
    "FR": "color_mkvqnrh5",
    "DE": "color_mkvqh7v2",
    "ES": "color_mkvq2vtd",
    "JA": "color_mkvqdhfp",
    "PT": "color_mkvqdcny",
    "IT": "color_mkvqej59",
    "KO": "color_mkvqb6qs",
    "NL": "color_mkvqsw28",
    "TR": "color_mkvqvac0",
    "DA": "color_mkvqas0w",
    "UK": "color_mkvqrkq4",
    "CS": "color_mkvq5mak",
    "ID": "color_mkvqc4zs",
    "NO": "color_mkvq1ws8",
    "PL": "color_mkvqqr2z",
    "RU": "color_mkvqre50",
    "SV": "color_mkvqv754",
    "TH": "color_mkvqkx6v",
    "VI": "color_mkvq5s4f",
    "ZH": "color_mkvqjhc9",
    "HI": "color_mkzj6fx9",
    "Arabic": "color_mkvq3qg1",
    "Bulgarian": "color_mkvqeba7",
    "Catalan": "color_mkvqcby7",
    "Croatian": "color_mkvqftbd",
    "Finnish": "color_mkvqtm9m",
    "Greek": "color_mkvqzksg",
    "Hebrew": "color_mkvqqcfj",
    "Hungarian": "color_mkvqdw4k",
    "Latvian": "color_mkvq54ea",
    "Lithuanian": "color_mkvqp4xd",
    "Romanian": "color_mkvqdrqd",
    "Malay": "color_mkvqdmw3",
    "Slovak": "color_mkvq5br3",
    "Slovenian": "color_mkvqbyw4",
    "Tagalog": "color_mkvqam7b",
}

# Candidate Smartling locale IDs per Monday language label
LANG_TO_LOCALES = {
    "FR": ["fr-FR"],
    "DE": ["de-DE"],
    "ES": ["es"],
    "JA": ["ja-JP"],
    "PT": ["pt-BR", "pt-PT"],
    "IT": ["it-IT"],
    "KO": ["ko-KR"],
    "NL": ["nl-NL"],
    "TR": ["tr-TR"],
    "DA": ["da-DK"],
    "UK": ["uk-UA"],
    "CS": ["cs-CZ"],
    "ID": ["id-ID"],
    "NO": ["no-NO", "nb-NO"],
    "PL": ["pl-PL"],
    "RU": ["ru-RU"],
    "SV": ["sv-SE"],
    "TH": ["th-TH"],
    "VI": ["vi-VN"],
    "ZH": ["zh-TW", "zh-CN", "zh-Hans"],
    "HI": ["hi-IN"],
    "Arabic": ["ar"],
    "Bulgarian": ["bg-BG"],
    "Catalan": ["ca-ES"],
    "Croatian": ["hr-HR"],
    "Finnish": ["fi-FI"],
    "Greek": ["el-GR"],
    "Hebrew": ["he-IL"],
    "Hungarian": ["hu-HU"],
    "Latvian": ["lv-LV"],
    "Lithuanian": ["lt-LT"],
    "Romanian": ["ro-RO"],
    "Malay": ["ms-MY"],
    "Slovak": ["sk-SK"],
    "Slovenian": ["sl-SI"],
    "Tagalog": ["tl-PH"],
}

# Status texts on Monday that mean "not needed / already done"
SKIP_STATUSES = {"done", "no need", "n/a", ""}


def load_env():
    env = {}
    with open(DOTENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# ── Monday helpers ────────────────────────────────────────────────────────────

def monday_query(query, variables=None):
    env = load_env()
    headers = {
        "Authorization": env["MONDAY_API_TOKEN"],
        "Content-Type": "application/json",
        "API-Version": "2024-01",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post("https://api.monday.com/v2", json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data["data"]


SUBITEMS_BOARD_ID = 9991673115  # "Subitems of Product Localization Tasks"


def get_subitems_overdue():
    """
    Query the subitems board for all subitems where ETA <= today (overdue or due today).
    Uses 'lower_than_or_equal' operator so it catches yesterday AND any older missed tasks.
    Excludes subitems already marked Done.
    """
    results = []
    cursor = None
    today_str = date.today().strftime("%Y-%m-%d")

    while True:
        if cursor:
            q = """
            query($cursor: String!) {
              boards(ids: [9991673115]) {
                items_page(limit: 200, cursor: $cursor) {
                  cursor
                  items {
                    id name
                    parent_item { id name }
                    column_values { id type text value }
                  }
                }
              }
            }
            """
            data = monday_query(q, {"cursor": cursor})
        else:
            q = """
            {
              boards(ids: [9991673115]) {
                items_page(limit: 200, query_params: {
                  rules: [
                    {column_id: "date0", compare_value: ["%s"], operator: lower_than_or_equal},
                    {column_id: "color_mkyf691e", compare_value: ["Done"], operator: not_any_of}
                  ]
                  operator: and
                }) {
                  cursor
                  items {
                    id name
                    parent_item { id name }
                    column_values { id type text value }
                  }
                }
              }
            }
            """ % today_str
            data = monday_query(q)

        page = data["boards"][0]["items_page"]
        for sub in page["items"]:
            cv_map = {cv["id"]: cv for cv in sub["column_values"]}
            parent = sub.get("parent_item") or {}
            results.append({
                "subitem_id": sub["id"],
                "subitem_name": sub["name"],
                "parent_name": parent.get("name", "(unknown)"),
                "cv_map": cv_map,
            })

        cursor = page.get("cursor")
        if not cursor:
            break

    return results


def get_in_progress_languages(cv_map):
    """Return Monday language labels whose status isn't Done / No Need / N/A."""
    langs = []
    for lang, col_id in LANG_STATUS_COLS.items():
        cv = cv_map.get(col_id) or {}
        status = (cv.get("text") or "").strip().lower()
        if status not in SKIP_STATUSES:
            langs.append(lang)
    return langs


def post_monday_comment(subitem_id, language_names):
    """Create an update on the subitem mentioning Sanne Heijmans."""
    lang_list = ", ".join(language_names)
    body = (
        f'I published the languages {lang_list} that were due. '
        f'<p><a href="https://wix.monday.com/users/{SANNE_USER_ID}" '
        f'data-mention-id="{SANNE_USER_ID}" data-mention-type="user" '
        f'class="mention">@Sanne Heijmans</a></p>'
    )
    q = """
    mutation($item_id: ID!, $body: String!) {
      create_update(item_id: $item_id, body: $body) { id }
    }
    """
    monday_query(q, {"item_id": subitem_id, "body": body})


def set_task_status_done(subitem_id):
    """Set Task Status column to Done on the subitem."""
    q = """
    mutation($board_id: ID!, $item_id: ID!, $column_id: String!, $value: JSON!) {
      change_column_value(
        board_id: $board_id
        item_id: $item_id
        column_id: $column_id
        value: $value
      ) { id }
    }
    """
    monday_query(q, {
        "board_id": str(BOARD_ID),
        "item_id": subitem_id,
        "column_id": TASK_STATUS_COL,
        "value": json.dumps({"label": "Done"}),
    })


# ── Smartling helpers ─────────────────────────────────────────────────────────

_sl_token = None


def smartling_token():
    global _sl_token
    if _sl_token:
        return _sl_token
    env = load_env()
    r = requests.post(
        "https://api.smartling.com/auth-api/v2/authenticate",
        json={"userIdentifier": env["SMARTLING_USER_ID"],
              "userSecret": env["SMARTLING_USER_SECRET"]},
        timeout=30,
    )
    r.raise_for_status()
    _sl_token = r.json()["response"]["data"]["accessToken"]
    return _sl_token


def sl_get(path, params=None):
    r = requests.get(
        f"https://api.smartling.com{path}",
        headers={"Authorization": f"Bearer {smartling_token()}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["response"]["data"]


def sl_post(path, body):
    r = requests.post(
        f"https://api.smartling.com{path}",
        headers={"Authorization": f"Bearer {smartling_token()}",
                 "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["response"]["data"]


def parse_smartling_url(url):
    """Return (project_id, tags, key_names) from a Smartling strings filter URL."""
    m = re.search(r"/projects/([^/]+)/", urlparse(url).path)
    if not m:
        return None, [], []
    project_id = m.group(1)
    params = parse_qs(urlparse(url).query)
    tags = [unquote(t) for t in params.get("tagsFilter.keywords[]", [])]
    keys = [unquote(k) for k in params.get("keyVariantFilter.keyword[]", [])]
    return project_id, tags, keys


_tag_uid_cache = {}

def get_string_uids_by_tag(project_id, tag):
    """Return all string hashcodes in a project matching a tag (cached per project+tag)."""
    cache_key = (project_id, tag)
    if cache_key in _tag_uid_cache:
        return _tag_uid_cache[cache_key]
    uids, offset = [], 0
    while True:
        data = sl_get(f"/strings-api/v2/projects/{project_id}",
                      {"tag": tag, "limit": 500, "offset": offset})
        items = data.get("items", [])
        uids.extend(item["hashcode"] for item in items)
        if len(items) < 500:
            break
        offset += 500
    _tag_uid_cache[cache_key] = uids
    return uids


_locale_cache = {}

def get_project_locale_ids(project_id):
    """Return the set of locale IDs configured on the project (cached per project)."""
    if project_id in _locale_cache:
        return _locale_cache[project_id]
    try:
        data = sl_get(f"/projects-api/v2/projects/{project_id}")
        result = {loc["localeId"] for loc in data.get("targetLocales", [])}
        _locale_cache[project_id] = result
        return result
    except Exception as e:
        print(f"    Warning: could not fetch project locales: {e}")
        return set()


def publish_locales_for_strings(project_id, string_uids, locale_ids):
    """
    For each locale_id, check whether any of the strings are in a publishable
    state (translation exists but not yet PUBLISHED). Publish those that are.
    Returns list of locale_ids that were actually published.
    """
    if not string_uids:
        return []

    published = []
    # Smartling limits hashcodes[] to ~500 per request; batch if needed
    batch = string_uids[:500]

    for locale_id in locale_ids:
        try:
            params = [("localeId", locale_id)]
            for uid in batch:
                params.append(("hashcodes[]", uid))
            r = requests.get(
                f"https://api.smartling.com/strings-api/v2/projects/{project_id}/translations",
                headers={"Authorization": f"Bearer {smartling_token()}"},
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            trans_data = r.json()["response"]["data"].get("translationData", [])

            # Publishable = has a translation that isn't already PUBLISHED
            publishable = [
                t for t in trans_data
                if t.get("translationState") not in ("PUBLISHED", "WAITING_FOR_AUTHORIZATION", None)
            ]
            if not publishable:
                print(f"    {locale_id}: nothing to publish (already done or not started)")
                continue

            sl_post(
                f"/strings-api/v2/projects/{project_id}/translations/publish",
                {"stringUids": batch, "localeIds": [locale_id]},
            )
            print(f"    {locale_id}: published {len(publishable)} translation(s)")
            published.append(locale_id)

        except Exception as e:
            print(f"    {locale_id}: error — {e}")

    return published


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run=False):
    mode = "[DRY RUN] " if dry_run else ""
    print(f"=== Monday + Smartling Agent {mode}===")
    print(f"Looking for subitems with ETA = {YESTERDAY}\n")

    subitems = get_subitems_overdue()
    print(f"Found {len(subitems)} subitem(s) with ETA = {YESTERDAY}\n")

    if not subitems:
        print("Nothing to do.")
        return

    # Authenticate Smartling eagerly so failures surface early
    smartling_token()
    print("Smartling: authenticated\n")

    for sub in subitems:
        print(f"─── {sub['subitem_name']} (id={sub['subitem_id']}) ───")
        print(f"    Parent item: {sub['parent_name']}")

        # Get Smartling URL
        tl_cv = sub["cv_map"].get(TASK_LINK_COL) or {}
        task_link_url = None
        if tl_cv.get("value"):
            try:
                task_link_url = json.loads(tl_cv["value"]).get("url")
            except Exception:
                pass
        if not task_link_url:
            task_link_url = tl_cv.get("text") or ""

        if "dashboard.smartling.com" not in task_link_url:
            print("    No Smartling link found — skipping.\n")
            continue

        print(f"    Smartling URL: {task_link_url[:90]}...")
        project_id, tags, keys = parse_smartling_url(task_link_url)
        if not project_id:
            print("    Could not parse project ID — skipping.\n")
            continue
        print(f"    Project: {project_id} | Tags: {tags} | Keys count: {len(keys)}")

        # Resolve string UIDs
        string_uids = []
        if tags:
            for tag in tags:
                uids = get_string_uids_by_tag(project_id, tag)
                print(f"    Tag '{tag}' → {len(uids)} string(s)")
                string_uids.extend(uids)
        elif keys:
            print("    Key-based URL detected. Will use Monday language status columns to target locales.")

        # Determine which languages are in progress
        in_progress_langs = get_in_progress_languages(sub["cv_map"])
        print(f"    In-progress languages (Monday status): {in_progress_langs or 'none'}")

        if not in_progress_langs and not string_uids:
            print(f"    No in-progress languages and no strings found.")
            if dry_run:
                print("    [DRY RUN] Would set Task Status → Done.\n")
            else:
                print("    Setting Task Status → Done.\n")
                set_task_status_done(sub["subitem_id"])
            continue

        # Map Monday language labels → Smartling locale IDs
        project_locale_ids = get_project_locale_ids(project_id)
        print(f"    Project locales in Smartling: {sorted(project_locale_ids)}")

        target_locales = []
        for lang in in_progress_langs:
            for candidate in LANG_TO_LOCALES.get(lang, []):
                if candidate in project_locale_ids:
                    target_locales.append(candidate)
                    break

        if not target_locales and in_progress_langs:
            print("    Warning: could not map any in-progress languages to Smartling locales.")

        # Check / publish in Smartling
        published_locales = []
        if string_uids and target_locales:
            if dry_run:
                print(f"    [DRY RUN] Would publish {len(string_uids)} string(s) for: {target_locales}")
            else:
                print(f"    Publishing {len(string_uids)} string(s) for {target_locales}...")
                published_locales = publish_locales_for_strings(project_id, string_uids, target_locales)

        elif string_uids and not target_locales:
            if dry_run:
                print(f"    [DRY RUN] No specific in-progress langs; would check all project locales.")
            else:
                print(f"    No specific in-progress langs; checking all project locales...")
                published_locales = publish_locales_for_strings(
                    project_id, string_uids, list(project_locale_ids)
                )
        else:
            print("    No string UIDs available (key-based URL) — Smartling publish not possible via API.")

        # Build human-readable language names
        locale_to_lang = {}
        for lang, locs in LANG_TO_LOCALES.items():
            for loc in locs:
                locale_to_lang[loc] = lang

        if dry_run:
            if target_locales:
                lang_names = [locale_to_lang.get(loc, loc) for loc in target_locales]
                print(f"    [DRY RUN] Would post Monday comment tagging Sanne Heijmans:")
                print(f"             \"I published the languages {', '.join(lang_names)} that were due.\"")
            print(f"    [DRY RUN] Would set Task Status → Done.\n")
        else:
            if published_locales:
                published_lang_names = [locale_to_lang.get(loc, loc) for loc in published_locales]
                print(f"    Posting Monday comment (published: {published_lang_names})...")
                post_monday_comment(sub["subitem_id"], published_lang_names)
                print("    Comment posted.")
            else:
                print("    No locales were published — skipping comment.")

            print(f"    Setting Task Status → Done...")
            set_task_status_done(sub["subitem_id"])
            print(f"    Done!\n")

    print("=== Agent finished ===")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
