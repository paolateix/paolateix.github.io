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


REVIEWED_TASKS_ITEM_ID = 12180635204  # "Reviewed tasks" parent item

def get_subitems_overdue():
    """
    Fetch subitems directly from the 'Reviewed tasks' parent item (12180635204),
    then filter locally for ETA < today and status != Done.
    Much faster than scanning the whole subitems board.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    results = []

    q = """
    {
      items(ids: [12180635204]) {
        subitems {
          id name
          board { id }
          column_values { id type text value }
        }
      }
    }
    """
    data = monday_query(q)
    subitems = data["items"][0]["subitems"] if data["items"] else []
    print(f"[debug] {len(subitems)} subitems fetched from parent item, today={today}")

    for sub in subitems:
        cv_map = {cv["id"]: cv for cv in sub["column_values"]}

        # Filter by ETA < today
        eta_cv = cv_map.get(ETA_COL, {})
        eta_text = (eta_cv.get("text") or "").strip()
        if not eta_text:
            print(f"[debug] skip '{sub['name']}': no ETA")
            continue
        try:
            eta_date = date.fromisoformat(eta_text)
        except ValueError:
            print(f"[debug] skip '{sub['name']}': bad ETA '{eta_text}'")
            continue
        if eta_date >= today:
            print(f"[debug] skip '{sub['name']}': ETA {eta_text} not overdue")
            continue

        # Filter out already Done
        status_cv = cv_map.get(TASK_STATUS_COL, {})
        status_text = (status_cv.get("text") or "").strip().lower()
        if status_text == "done":
            print(f"[debug] skip '{sub['name']}': status=done")
            continue

        results.append({
            "subitem_id": sub["id"],
            "board_id": sub["board"]["id"],
            "subitem_name": sub["name"],
            "parent_name": "Reviewed tasks",
            "cv_map": cv_map,
        })

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


def set_task_status_done(subitem_id, board_id):
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
        "board_id": str(board_id),
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
    """Return (project_id, job_id, tags, key_names) from a Smartling URL.
    Supports both job URLs (/jobs/<id>) and strings filter URLs.
    """
    parsed = urlparse(url)
    m = re.search(r"/projects/([^/]+)/", parsed.path)
    if not m:
        return None, None, [], []
    project_id = m.group(1)

    # Detect job URL: /strings/jobs/<job_uid>
    job_m = re.search(r"/jobs/([^/?]+)", parsed.path)
    job_id = job_m.group(1) if job_m else None

    params = parse_qs(parsed.query)

    # Also detect job UIDs from translationJobsFilter query param
    if not job_id:
        job_uids = params.get("translationJobsFilter.translationJobUids[]", [])
        if job_uids:
            job_id = unquote(job_uids[0])  # use first job UID

    tags = [unquote(t) for t in params.get("tagsFilter.keywords[]", [])]
    keys = [unquote(k) for k in params.get("keyVariantFilter.keyword[]", [])]
    file_uris = [unquote(u) for u in params.get("urlsFilter.urls", [])]
    return project_id, job_id, tags, keys, file_uris


_tag_uid_cache = {}

def get_string_uids_by_tag(project_id, tag):
    """Return all string hashcodes in a project matching a tag via the translations API."""
    cache_key = (project_id, tag)
    if cache_key in _tag_uid_cache:
        return _tag_uid_cache[cache_key]
    # Use the translations endpoint with tagName filter to discover hashcodes
    # We sample one locale to get the hashcodes (they're the same across locales)
    locales = get_project_locale_ids(project_id)
    if not locales:
        return []
    sample_locale = sorted(locales)[0]
    uids, offset = [], 0
    while True:
        r = requests.get(
            f"https://api.smartling.com/strings-api/v2/projects/{project_id}/translations",
            headers={"Authorization": f"Bearer {smartling_token()}"},
            params={"targetLocaleId": sample_locale, "tagName": tag, "limit": 500, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()["response"]["data"]
        items = data.get("items", [])
        uids.extend(item["hashcode"] for item in items if "hashcode" in item)
        if len(items) < 500:
            break
        offset += 500
    _tag_uid_cache[cache_key] = uids
    return uids


def _resolve_file_uri(project_id, file_uri):
    """Return the exact Smartling file URI, using prefix search if the exact name has a timestamp suffix."""
    r = requests.get(
        f"https://api.smartling.com/files-api/v2/projects/{project_id}/files/list",
        headers={"Authorization": f"Bearer {smartling_token()}"},
        params={"uriMask": file_uri, "limit": 10},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json()["response"]["data"].get("items", [])
    if items:
        return items[0]["fileUri"]
    return file_uri  # fall back to original


def get_string_uids_by_file_uri(project_id, file_uri):
    """Return hashcodes for all strings in a project with a matching file URI."""
    cache_key = (project_id, "file:" + file_uri)
    if cache_key in _tag_uid_cache:
        return _tag_uid_cache[cache_key]
    locales = get_project_locale_ids(project_id)
    if not locales:
        return []
    locale = sorted(locales)[0]
    exact_uri = _resolve_file_uri(project_id, file_uri)
    uids, offset = [], 0
    while True:
        r = requests.get(
            f"https://api.smartling.com/strings-api/v2/projects/{project_id}/translations",
            headers={"Authorization": f"Bearer {smartling_token()}"},
            params={"targetLocaleId": locale, "fileUri": exact_uri, "limit": 500, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()["response"]["data"]
        items = data.get("items", [])
        uids.extend(item["hashcode"] for item in items if "hashcode" in item)
        if len(items) < 500:
            break
        offset += 500
    _tag_uid_cache[cache_key] = uids
    return uids


def get_string_uids_by_key(project_id, key_name):
    """Return hashcodes for strings matching a key name (keyVariantFilter)."""
    cache_key = (project_id, "key:" + key_name)
    if cache_key in _tag_uid_cache:
        return _tag_uid_cache[cache_key]
    locales = get_project_locale_ids(project_id)
    if not locales:
        return []
    locale = sorted(locales)[0]
    uids, offset = [], 0
    while True:
        r = requests.get(
            f"https://api.smartling.com/strings-api/v2/projects/{project_id}/translations",
            headers={"Authorization": f"Bearer {smartling_token()}"},
            params={"targetLocaleId": locale, "keyName": key_name, "limit": 500, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()["response"]["data"]
        items = data.get("items", [])
        uids.extend(item["hashcode"] for item in items if "hashcode" in item)
        if len(items) < 500:
            break
        offset += 500
    _tag_uid_cache[cache_key] = uids
    return uids


def get_string_uids_by_job(project_id, job_id):
    """Return all string hashcodes associated with a Smartling job."""
    cache_key = (project_id, job_id)
    if cache_key in _tag_uid_cache:
        return _tag_uid_cache[cache_key]
    uids, offset = [], 0
    while True:
        try:
            data = sl_get(
                f"/jobs-api/v3/projects/{project_id}/jobs/{job_id}/strings",
                {"limit": 500, "offset": offset},
            )
            items = data.get("items", [])
            uids.extend(item["hashcode"] for item in items if "hashcode" in item)
            if len(items) < 500:
                break
            offset += 500
        except Exception as e:
            print(f"    Warning: could not fetch job strings: {e}")
            break
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


def publish_job_directly(project_id, job_id, target_locales):
    """Publish a Smartling job for specific locales. Returns list of locale_ids published."""
    try:
        body = {}
        if target_locales:
            body["localeWorkflows"] = [{"targetLocaleId": loc} for loc in target_locales]
        sl_post(f"/jobs-api/v3/projects/{project_id}/jobs/{job_id}/publish", body)
        return list(target_locales) if target_locales else []
    except Exception as e:
        print(f"    Warning: job publish failed: {e}")
        return []


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
            params = [("targetLocaleId", locale_id)]
            for uid in batch:
                params.append(("hashcodes[]", uid))
            r = requests.get(
                f"https://api.smartling.com/strings-api/v2/projects/{project_id}/translations",
                headers={"Authorization": f"Bearer {smartling_token()}"},
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            trans_data = r.json()["response"]["data"].get("items", [])

            # Publishable = has a translation that isn't already published
            publishable = [
                t for t in trans_data
                if t.get("translationState") not in ("PUBLISHED", None)
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
    subitems = get_subitems_overdue()

    if not subitems:
        print("Nothing to do.")
        return

    if dry_run:
        print(f"Dry run — {len(subitems)} overdue task(s) found:\n")

    smartling_token()  # authenticate early

    locale_to_lang = {}
    for lang, locs in LANG_TO_LOCALES.items():
        for loc in locs:
            locale_to_lang[loc] = lang

    for sub in subitems:
        name = sub["subitem_name"]

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
            print(f"[debug] skip '{name}': no Smartling URL")
            continue  # no Smartling link — skip silently

        project_id, job_id, tags, keys, file_uris = parse_smartling_url(task_link_url)
        print(f"[debug] '{name}': project={project_id} job={job_id} tags={tags} keys={keys} file_uris={file_uris}")
        if not project_id:
            print(f"[debug] skip '{name}': could not parse project_id from URL")
            continue

        # Resolve string UIDs
        string_uids = []
        if job_id:
            string_uids = get_string_uids_by_job(project_id, job_id)
        elif tags:
            for tag in tags:
                string_uids.extend(get_string_uids_by_tag(project_id, tag))
        elif file_uris:
            for furi in file_uris:
                string_uids.extend(get_string_uids_by_file_uri(project_id, furi))
            if not string_uids and keys:
                # file URI lookup failed, try key-based lookup as fallback
                for key in keys:
                    string_uids.extend(get_string_uids_by_key(project_id, key))
        elif keys:
            for key in keys:
                string_uids.extend(get_string_uids_by_key(project_id, key))
        print(f"[debug] '{name}': {len(string_uids)} string UIDs, in_progress will be checked next")

        project_locale_ids = get_project_locale_ids(project_id)

        if not string_uids and not job_id:
            # No way to identify strings — just mark done
            if dry_run:
                print(f"• {name}\n  → Mark as Done (no Smartling strings found)")
            else:
                set_task_status_done(sub["subitem_id"], sub["board_id"])
            continue

        if dry_run:
            if job_id:
                try:
                    progress = sl_get(f"/jobs-api/v3/projects/{project_id}/jobs/{job_id}/progress")
                    unpublished_locales = []
                    for item in progress.get("contentProgressReport", []):
                        loc = item.get("targetLocaleId", "")
                        has_unpublished = False
                        for workflow in item.get("workflowProgressReportList", []):
                            for step in workflow.get("workflowStepSummaryReportItemList", []):
                                step_name = (step.get("workflowStepName") or "").lower()
                                if "publish" in step_name:
                                    continue
                                if any(isinstance(v, (int, float)) and v > 0 for v in step.values()):
                                    has_unpublished = True
                                    break
                        if has_unpublished:
                            unpublished_locales.append(locale_to_lang.get(loc, loc))
                    if unpublished_locales:
                        print(f"• {name}\n  → Publish: {', '.join(sorted(set(unpublished_locales)))}")
                    else:
                        print(f"• {name}\n  → Mark as Done (all strings already published)")
                except Exception as e:
                    print(f"• {name}\n  → Publish via job (could not fetch progress: {e})")
            elif string_uids:
                print(f"• {name}\n  → Publish ({len(string_uids)} strings via tags)")
            else:
                print(f"• {name}\n  → Mark as Done (no Smartling strings found)")
        else:
            published_locales = []

            if job_id:
                published_locales = publish_job_directly(project_id, job_id, list(project_locale_ids))
            elif string_uids:
                published_locales = publish_locales_for_strings(project_id, string_uids, list(project_locale_ids))

            if published_locales:
                published_lang_names = sorted({locale_to_lang.get(loc, loc) for loc in published_locales})
                post_monday_comment(sub["subitem_id"], published_lang_names)
            set_task_status_done(sub["subitem_id"], sub["board_id"])

    print("\nDone.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
