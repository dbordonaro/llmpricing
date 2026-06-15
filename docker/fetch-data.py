#!/usr/bin/env python3
"""
fetch-data.py — Fetch LLM pricing from OpenRouter API and merge into elo.csv.

Usage:
  python3 fetch-data.py [--repo-dir PATH] [--commit]

By default reads/writes elo.csv in the current directory.
Use --commit to git commit and push changes to the fork.
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone

# ─── Config ─────────────────────────────────────────────────────────────────
OPENROUTER_API = "https://openrouter.ai/api/v1/models"
CSV_FILENAME = "elo.csv"
HEADERS = ["model", "overall", "hard", "coding", "cpmi", "launch", "end", "source"]

# ─── Helpers ────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def fetch_json(url, retries=3):
    """Fetch JSON from a URL with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "llmpricing-fetcher/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            log(f"  Fetch failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def normalize_model_name(or_id: str) -> str:
    """
    Convert an OpenRouter model ID to elo.csv naming convention.
    Examples:
      'anthropic/claude-opus-4.7'        -> 'claude-opus-4-7'
      'openai/gpt-5.5'                   -> 'gpt-5-5'
      'deepseek/deepseek-v4-pro'         -> 'deepseek-v4-pro'
      'google/gemini-3.5-flash'          -> 'gemini-3-5-flash'
      'qwen/qwen3.7-plus'                -> 'qwen3-7-plus'
    """
    name = or_id

    # Remove :suffix variants (e.g. ':free', ':extended')
    name = re.sub(r':.*$', '', name)

    # Remove provider prefix (e.g. 'anthropic/', 'openai/', 'google/')
    name = re.sub(r'^[^/]+/', '', name)

    # If the name still has a slash (unlikely after stripping prefix), replace it
    name = name.replace('/', '-')

    # Normalize dots to hyphens (but be careful with version numbers)
    # elo.csv uses: claude-opus-4-7 (not claude-opus-4.7)
    name = name.replace('.', '-')

    # Lowercase
    name = name.lower()

    # Strip trailing garbage
    name = name.strip('-')

    return name


def or_price_to_cpmi(price_per_token: float) -> float | None:
    """
    Convert OpenRouter per-token price to CPMI (cost per million input tokens).
    If price is -1 (unlisted), NaN, or None, return None.
    """
    if price_per_token is None or price_per_token < 0:
        return None
    if price_per_token == 0:
        return 0.0
    # OpenRouter returns price per token; CPMI = price_per_token * 1_000_000
    cpmi = price_per_token * 1_000_000
    # Round to reasonable precision
    return round(cpmi, 3)


def parse_cpmi(val: str) -> float | None:
    """Parse CPMI string from CSV to float."""
    val = val.strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def format_cpmi(val: float | None) -> str:
    """Format CPMI for CSV output."""
    if val is None:
        return ""
    # Remove trailing zeros: 5.0 -> 5, 0.5 -> 0.5, 0.435 -> 0.435
    s = f"{val:.4f}".rstrip('0').rstrip('.')
    return s


def price_to_str(val: float | None) -> str:
    """Format a price-per-token value for CSV source field."""
    if val is None:
        return ""
    return f"{val:.8f}"


# ─── Model Name Overrides ───────────────────────────────────────────────────
# Some OpenRouter IDs don't map cleanly via the normalization heuristic.
NAME_OVERRIDES = {
    "openai/gpt-4o": "gpt-4o-2024-05-13",
    "openai/gpt-4o-mini": "gpt-4o-mini-2024-07-18",
    "openai/gpt-4-turbo": "gpt-4-turbo-2024-04-09",
    "openai/o1": "o1-2024-12-17",
    "openai/o1-mini": "o1-mini-2024-09-12",
    "openai/o1-preview": "o1-preview-2024-09-12",
    "openai/o3-mini": "o3-mini-2025-01-31",
    "google/gemini-2.5-pro": "gemini-2-5-pro-exp-03-25",
    "google/gemini-2.0-flash": "gemini-2-0-flash",
    "google/gemini-2.0-flash-lite-001": "gemini-2-0-flash-lite-preview-02-05",
    "anthropic/claude-sonnet-4-20250514": "claude-sonnet-4-20250514",
    "anthropic/claude-haiku-3-5": "claude-3-5-haiku-20241022",
    "cohere/command-r": "command-r-2024-08-04",
    "cohere/command-r-plus": "command-r-plus-2024-08-04",
    "meta-llama/llama-3.1-8b-instruct": "llama-3-1-8b-instruct",
    "meta-llama/llama-3.1-70b-instruct": "llama-3-1-70b-instruct",
    "meta-llama/llama-3.1-405b-instruct": "llama-3-1-405b-instruct",
}


# ─── Main ────────────────────────────────────────────────────────────────────

def read_csv(path: str) -> OrderedDict:
    """Read elo.csv into an OrderedDict keyed by model name."""
    rows = OrderedDict()
    if not os.path.exists(path):
        log(f"  {path} not found, starting fresh")
        return rows

    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            log("  Empty CSV, no headers found")
            return rows
        actual_headers = [h.strip().lower() for h in reader.fieldnames]
        log(f"  CSV headers: {actual_headers}")
        for row in reader:
            model = row.get("model", "").strip()
            if not model or model.startswith("#"):
                continue
            rows[model] = OrderedDict(
                (h.strip(), (row.get(h, "") or "").strip())
                for h in HEADERS
            )
    log(f"  Read {len(rows)} existing models")
    return rows


def write_csv(path: str, rows: OrderedDict):
    """Write elo.csv from OrderedDict."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        for model, row in rows.items():
            writer.writerow([row.get(h, "") for h in HEADERS])
    log(f"  Wrote {len(rows)} models to {path}")


def fetch_openrouter_models() -> list[dict]:
    """Fetch model list from OpenRouter API."""
    log(f"Fetching OpenRouter models from {OPENROUTER_API}...")
    data = fetch_json(OPENROUTER_API)
    if not data:
        log("  Failed to fetch OpenRouter data")
        return []

    models = data.get("data", data if isinstance(data, list) else [])
    log(f"  Got {len(models)} models from OpenRouter")
    return models


def build_model_map(or_models: list[dict], existing: OrderedDict) -> dict:
    """
    Build a mapping: elo.csv model name -> {cpmi, source, or_id}
    from OpenRouter models.
    """
    mapped = {}

    for m in or_models:
        or_id = m.get("id", "")
        if not or_id:
            continue

        # Skip routing/aggregator models
        if or_id.startswith("openrouter/"):
            continue

        pricing = m.get("pricing", {})
        prompt_price = pricing.get("prompt")
        try:
            prompt_price_f = float(prompt_price)
        except (TypeError, ValueError):
            prompt_price_f = -1

        source_url = f"https://openrouter.ai/{or_id}"

        # Try override first
        if or_id in NAME_OVERRIDES:
            name = NAME_OVERRIDES[or_id]
            cpmi = or_price_to_cpmi(prompt_price_f)
            if cpmi is not None:
                mapped[name] = {"cpmi": cpmi, "source": source_url, "or_id": or_id}
            continue

        # Try normalization
        name = normalize_model_name(or_id)
        if not name:
            continue

        cpmi = or_price_to_cpmi(prompt_price_f)
        if cpmi is not None:
            mapped[name] = {"cpmi": cpmi, "source": source_url, "or_id": or_id}

    log(f"  Matched {len(mapped)} models from OpenRouter to elo naming")
    return mapped


def merge_data(existing: OrderedDict, or_models: list[dict], or_map: dict) -> tuple[OrderedDict, dict]:
    """
    Merge OpenRouter pricing into existing elo.csv data.
    Returns (updated_rows, stats).
    """
    stats = {
        "updated": 0,
        "added": 0,
        "unchanged": 0,
        "no_match": 0,
        "skipped_or_router": 0,
    }

    # Track OR models we've matched
    matched_or_ids = set()

    # Update existing models
    for model_name in list(existing.keys()):
        if model_name in or_map:
            info = or_map[model_name]
            old_cpmi = parse_cpmi(existing[model_name].get("cpmi", ""))
            new_cpmi = info["cpmi"]

            if old_cpmi != new_cpmi:
                existing[model_name]["cpmi"] = format_cpmi(new_cpmi)
                existing[model_name]["source"] = info["source"]
                stats["updated"] += 1
                log(f"    UPDATE {model_name}: cpmi {format_cpmi(old_cpmi) or '?'} -> {format_cpmi(new_cpmi)}")
            else:
                stats["unchanged"] += 1

            matched_or_ids.add(info["or_id"])

    # Add new models from OpenRouter that aren't already in elo.csv
    for m in or_models:
        or_id = m.get("id", "")
        if not or_id or or_id.startswith("openrouter/"):
            stats["skipped_or_router"] += 1
            continue

        pricing = m.get("pricing", {})
        prompt_price = pricing.get("prompt")
        try:
            prompt_price_f = float(prompt_price)
        except (TypeError, ValueError):
            continue

        if prompt_price_f < 0:
            continue

        # Use override or normalize
        if or_id in NAME_OVERRIDES:
            name = NAME_OVERRIDES[or_id]
        else:
            name = normalize_model_name(or_id)

        if not name:
            continue

        # Skip if already in existing (already matched/updated above)
        if name in existing:
            continue

        # Skip free/beta/nightly variants that are noise
        if any(tag in or_id.lower() for tag in [":free", ":beta", "nightly", "dev-"]):
            continue

        cpmi = or_price_to_cpmi(prompt_price_f)
        if cpmi is None:
            continue

        source = m.get("id", "openrouter")
        source_url = f"https://openrouter.ai/{source}"

        this_month = datetime.now(timezone.utc).strftime("%Y-%m")

        existing[name] = OrderedDict(
            (h, "") for h in HEADERS
        )
        existing[name]["model"] = name
        existing[name]["cpmi"] = format_cpmi(cpmi)
        existing[name]["launch"] = this_month
        existing[name]["source"] = source_url
        stats["added"] += 1
        log(f"    ADD {name}: cpmi={format_cpmi(cpmi)} from {or_id}")

    stats["no_match"] = len(or_models) - len(matched_or_ids) - stats["skipped_or_router"]

    return existing, stats


def main():
    repo_dir = "."
    do_commit = False

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--repo-dir" and i + 1 < len(args):
            repo_dir = args[i + 1]
        elif arg == "--commit":
            do_commit = True

    csv_path = os.path.join(repo_dir, CSV_FILENAME)

    log("=" * 50)
    log("Fetching LLM pricing data...")
    log(f"  Repo dir: {repo_dir}")
    log(f"  CSV path: {csv_path}")

    # Step 1: Read the actual elo.csv from the repo
    existing = read_csv(csv_path)

    # Step 2: Fetch OpenRouter models
    or_models = fetch_openrouter_models()
    if not or_models:
        log("ERROR: No OpenRouter data fetched, aborting")
        return 1

    # Step 3: Build model name mapping
    or_map = build_model_map(or_models, existing)

    # Step 4: Merge data
    updated, stats = merge_data(existing, or_models, or_map)

    # Step 5: Write updated CSV
    write_csv(csv_path, updated)

    # Step 6: Summary
    log("-" * 50)
    log(f"Summary:")
    log(f"  Models in elo.csv: {len(updated)}")
    log(f"  Prices updated:    {stats['updated']}")
    log(f"  New models added:  {stats['added']}")
    log(f"  Unchanged:         {stats['unchanged']}")
    log(f"  OpenRouter models: {len(or_models)}")
    log(f"  Unmatched (no elo): {stats['no_match']}")
    log("=" * 50)

    # Step 7: Git commit if requested
    if do_commit:
        has_changes = stats["updated"] > 0 or stats["added"] > 0
        if has_changes:
            log("Committing changes...")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            change_summary = f"+{stats['added']} ~{stats['updated']}"
            msg = f"chore(pricing): weekly data refresh from OpenRouter ({today}) [{change_summary}]"
            os.chdir(repo_dir)
            os.system(f"git add {CSV_FILENAME}")
            exit_code = os.system(f"git commit -m '{msg}'")
            if exit_code == 0:
                log("Pushing to fork...")
                os.system("git push origin master")
                log("  Pushed successfully")
            else:
                log("  No new changes to commit (already up to date)")
        else:
            log("No changes to commit")

    return 0


if __name__ == "__main__":
    sys.exit(main())
