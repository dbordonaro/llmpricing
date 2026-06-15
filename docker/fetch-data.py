#!/usr/bin/env python3
"""
fetch-data.py — Fetch ELO scores from Arena AI + pricing from OpenRouter.

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
ARENA_URL = "https://arena.ai/leaderboard?tab=overall"
OPENROUTER_API = "https://openrouter.ai/api/v1/models"
CSV_FILENAME = "elo.csv"
HEADERS = ["model", "overall", "hard", "coding", "cpmi", "launch", "end", "source"]

STALE_MONTHS = 3  # remove launch/end for models with no pricing update in N months

# ─── Helpers ────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def fetch_with_retries(url, retries=3):
    """Fetch a URL with retries and browser-like headers."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log(f"  Fetch failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


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


# ─── Arena AI ELO Scraper ───────────────────────────────────────────────────

def fetch_arena_elo():
    """
    Fetch ELO scores from Arena AI by extracting data from Next.js RSC payload.
    Returns dict: model_name -> {"overall": float}
    """
    log("Fetching ELO scores from Arena AI...")
    html = fetch_with_retries(ARENA_URL)
    if not html:
        log("  FAILED to fetch Arena AI page")
        return {}

    # Extract all __next_f.push([...]) chunks
    chunks = re.findall(r'self\.__next_f\.push\((\[.*?\])\s*\)</script>', html, re.DOTALL)
    if not chunks:
        log("  No RSC chunks found in Arena AI page")
        return {}

    # Decode all chunks and concatenate
    all_text = ""
    for chunk_str in chunks:
        try:
            data = json.loads(chunk_str)
            if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], str):
                all_text += data[1]
        except Exception:
            continue

    if not all_text:
        log("  No decoded text from RSC chunks")
        return {}

    # Find the text/overall entries array
    # Structure: "arenaSlug":"text","leaderboardSlug":"overall",...,"entries":[...]
    for m in re.finditer(r'"entries"', all_text):
        start = m.start()
        before = all_text[max(0, start - 400):start]
        arena_slug = re.search(r'"arenaSlug"\s*:\s*"([^"]+)"', before)
        lb_slug = re.search(r'"leaderboardSlug"\s*:\s*"([^"]+)"', before)

        if arena_slug and lb_slug and arena_slug.group(1) == 'text' and lb_slug.group(1) == 'overall':
            # Find the entries array
            bracket = all_text.find('[', start + 10)
            if bracket == -1:
                continue

            # Match closing bracket
            depth = 0
            end = bracket
            for pos in range(bracket, len(all_text)):
                c = all_text[pos]
                if c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break

            try:
                entries = json.loads(all_text[bracket:end])
                result = {}
                for entry in entries:
                    model = entry.get('modelDisplayName', '')
                    rating = entry.get('rating', '')
                    if model and rating:
                        result[model] = {"overall": float(rating)}
                log(f"  Found {len(result)} models with ELO scores from Arena AI")
                return result
            except Exception as e:
                log(f"  Failed to parse entries: {e}")
                return {}

    log("  text/overall entries not found in Arena AI payload")
    return {}


# ─── OpenRouter Pricing ─────────────────────────────────────────────────────

def fetch_openrouter_models():
    """Fetch model list from OpenRouter API."""
    log("Fetching pricing from OpenRouter API...")
    data = fetch_json(OPENROUTER_API)
    if not data or "data" not in data:
        log("  FAILED to fetch OpenRouter data")
        return []
    log(f"  Received {len(data['data'])} models from OpenRouter")
    return data["data"]


def normalize_model_name(or_id: str) -> str:
    """Convert an OpenRouter model ID to elo.csv naming convention."""
    name = or_id.replace("/", "-").lower().strip()

    # Remove common prefixes
    for prefix in [
        "anthropic-", "openai-", "google-", "meta-", "mistralai-",
        "deepseek-", "cohere-", "qwen-", "alibaba-",
    ]:
        if name.startswith(prefix):
            base = name[len(prefix):]
            if len(base) > 4:
                name = base

    # Map specific model names back to canonical form
    # (the reverse of what OpenRouter shows)
    name = re.sub(r"^claude-(sonnet|opus|haiku|fable)-(\d)", r"claude-\1-\2", name)

    return name


def or_price_to_cpmi(prompt_price: float) -> float | None:
    """Convert OpenRouter prompt price (per token) to cpmi ($ per million tokens)."""
    if prompt_price < 0:
        return None
    return round(prompt_price * 1_000_000, 1)


def parse_cpmi(val: str) -> float | None:
    """Parse cpmi string to float, returning None if invalid."""
    try:
        v = float(val)
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    return None


def format_cpmi(val: float | None) -> str:
    """Format cpmi value as string."""
    if val is None:
        return ""
    return f"{val:.1f}"


# ─── Model name overrides ───────────────────────────────────────────────────
NAME_OVERRIDES = {
    "mistralai/mistral-large-2411": "mistral-large-3",
    "mistralai/mistral-small-2501": "mistral-small-3",
    "cohere/command-r7b-12-2024": "command-r7b",
    "cohere/command-r-08-2024": "command-r",
}


# ─── Main merge logic ───────────────────────────────────────────────────────

def load_existing_csv(path):
    """Load existing elo.csv, return OrderedDict keyed by model name."""
    existing = OrderedDict()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("model", "").strip()
                if name:
                    existing[name] = OrderedDict((h, row.get(h, "")) for h in HEADERS)
        log(f"  Loaded {len(existing)} existing models")
    except FileNotFoundError:
        log(f"  No existing {CSV_FILENAME} found, starting fresh")
    return existing


def write_csv(path, models):
    """Write models to CSV."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for row in models.values():
            writer.writerow(row)
    log(f"  Wrote {len(models)} models to {path}")


def run(repo_dir=".", do_commit=False):
    """Main fetch and merge logic."""
    csv_path = os.path.join(repo_dir, CSV_FILENAME) if repo_dir else CSV_FILENAME

    # Step 1: Fetch ELO scores from Arena AI
    arena_elo = fetch_arena_elo()

    # Step 2: Fetch pricing from OpenRouter
    or_models = fetch_openrouter_models()

    # Step 3: Build OpenRouter lookup map
    or_map = {}
    for m in or_models:
        or_id = m.get("id", "")
        if not or_id or or_id.startswith("openrouter/"):
            continue

        pricing = m.get("pricing", {})
        try:
            prompt_price = float(pricing.get("prompt", -1))
        except (TypeError, ValueError):
            continue
        if prompt_price < 0:
            continue

        # Determine model name
        name = NAME_OVERRIDES.get(or_id) or normalize_model_name(or_id)
        if not name:
            continue

        if any(tag in or_id.lower() for tag in [":free", ":beta", "nightly", "dev-"]):
            continue

        source_url = f"https://openrouter.ai/{or_id}"
        created = m.get("created", 0)
        if created:
            launch = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m")
        else:
            launch = datetime.now(timezone.utc).strftime("%Y-%m")

        or_map[name] = {
            "or_id": or_id,
            "cpmi": or_price_to_cpmi(prompt_price),
            "source": source_url,
            "launch": launch,
        }

    log(f"  Mapped {len(or_map)} OpenRouter models to canonical names")

    # Step 4: Load existing CSV
    existing = load_existing_csv(csv_path)

    stats = {
        "updated_elo": 0,
        "updated_price": 0,
        "added": 0,
        "unchanged": 0,
    }

    # Step 5: Update ELO scores from Arena AI
    for name, elo_info in arena_elo.items():
        if name in existing:
            old_overall = existing[name].get("overall", "")
            new_overall = f"{elo_info['overall']:.0f}"
            if old_overall != new_overall:
                existing[name]["overall"] = new_overall
                existing[name]["hard"] = ""
                existing[name]["coding"] = ""
                stats["updated_elo"] += 1
        else:
            # New model with ELO score — add it
            existing[name] = OrderedDict((h, "") for h in HEADERS)
            existing[name]["model"] = name
            existing[name]["overall"] = f"{elo_info['overall']:.0f}"
            # Use OpenRouter data if available
            if name in or_map:
                existing[name]["cpmi"] = format_cpmi(or_map[name]["cpmi"])
                existing[name]["launch"] = or_map[name]["launch"]
                existing[name]["source"] = or_map[name]["source"]
            stats["added"] += 1

    # Step 6: Update pricing from OpenRouter
    now = datetime.now(timezone.utc)
    this_month = now.strftime("%Y-%m")

    for name, info in or_map.items():
        if name in existing:
            old_cpmi = parse_cpmi(existing[name].get("cpmi", ""))
            new_cpmi = info["cpmi"]
            if old_cpmi != new_cpmi:
                existing[name]["cpmi"] = format_cpmi(new_cpmi)
                existing[name]["source"] = info["source"]
                stats["updated_price"] += 1
        else:
            # New model from OpenRouter
            existing[name] = OrderedDict((h, "") for h in HEADERS)
            existing[name]["model"] = name
            existing[name]["cpmi"] = format_cpmi(new_cpmi := info["cpmi"])
            existing[name]["launch"] = info["launch"]
            existing[name]["source"] = info["source"]
            stats["added"] += 1
            log(f"    ADD {name}: cpmi={format_cpmi(new_cpmi)}")

    # Step 7: Log summary
    total = len(existing)
    with_elo = sum(1 for r in existing.values() if r.get("overall", "").strip())
    with_pricing = sum(1 for r in existing.values() if r.get("cpmi", "").strip())
    log(f"Summary: {total} models ({with_elo} with ELO, {with_pricing} with pricing)")
    log(f"  ELO updates: {stats['updated_elo']} | Price updates: {stats['updated_price']} | Added: {stats['added']}")

    # Step 8: Write CSV
    write_csv(csv_path, existing)

    # Step 9: Commit and push if requested
    if do_commit:
        log("Committing and pushing changes...")
        os.chdir(repo_dir)
        result = os.popen("git add elo.csv").read()
        # Check if there are changes
        status = os.popen("git status --porcelain elo.csv").read().strip()
        if not status:
            log("  No changes to commit")
            return

        # Build commit message
        parts = []
        if stats["updated_elo"] > 0:
            parts.append(f"ELO+{stats['updated_elo']}")
        if stats["updated_price"] > 0:
            parts.append(f"price+{stats['updated_price']}")
        if stats["added"] > 0:
            parts.append(f"new+{stats['added']}")

        if not parts:
            parts.append("no-change")

        msg = f"chore(data): weekly refresh ({this_month}) [{', '.join(parts)}]"
        os.system(f'git commit -m "{msg}"')
        os.system("git push origin master 2>&1")
        log(f"  Committed: {msg}")


# ─── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch LLM pricing data")
    parser.add_argument("--repo-dir", default=".", help="Directory containing elo.csv")
    parser.add_argument("--commit", action="store_true", help="Commit and push changes")
    args = parser.parse_args()

    run(repo_dir=args.repo_dir, do_commit=args.commit)
