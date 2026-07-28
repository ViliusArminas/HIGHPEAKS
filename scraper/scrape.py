#!/usr/bin/env python3
"""
Scraper for triatlonotaure.lt results and upcoming-race registrations.

Three API endpoints are used (all discovered via browser devtools):

1) Stage list, per season:
   GET https://www.triatlonotaure.lt/api/stages?season={season_id}
   Returns real stage names, dates, addresses, a `has_results` flag, and a
   `distances` array - the actual set of distance codes offered at that
   stage (varies per race: od, sd, trifun, vaikai, splash, relay "-est"
   variants, ssd, etc). Some entries in this list are NOT individual races -
   they're season-wide aggregates (e.g. "Klubų įskaita" / club standings, or
   per-age-group combined results) and are marked `global_results_stage: true`
   / `show_in_main_window: false`. Those are skipped here since they don't
   represent a single race someone competed in.

2) Results for a stage that already happened (has_results: true):
   GET https://www.triatlonotaure.lt/api/stages/{stage_id}/results
       ?page=0&size=100&query=&distance={code}
   {code} is one of that stage's own `distances[].code` values from (1) -
   NOT a hardcoded od/sd, since which distances exist varies per race.
   Each record: {"id":..., "distance":"od", "row":[...], "titles":[...], "stageId":...}
   "row"/"titles" line up positionally and are zipped into a flat dict.

3) Participants/registrations for a stage that hasn't happened yet (has_results: false):
   GET https://www.triatlonotaure.lt/api/stages/{stage_id}/participants
       ?page=0&size=100&query=&participantType=all&distance=
   Each record is already a flat object (no row/titles unpacking needed), with
   "club", "displayName", nested "distance": {"name": ...}, and "payed" (bool),
   plus "payment": {"payed": bool, "price": "45.00"}.

4) Club standings ("Klubų įskaita"), one per season:
   GET https://www.triatlonotaure.lt/api/results
   Returns every stage across every season, flat, including the season-wide
   aggregate entries skipped in (1). The club-standings aggregate for a season
   is the entry with `global_results_stage: true` and name "Klubų įskaita";
   its `id` is then used as the {stage_id} below:
   GET https://www.triatlonotaure.lt/api/stages/{stage_id}/results
       ?page=0&size=20&query=&distance=klubai
   Same row/titles shape as (2), but columns are: Vieta, Klubas, RANDOM DATE,
   one column per real stage that season (points earned there, 0 if the stage
   hasn't happened yet), Taškai (season total), Dalyvių skaičius (participants).
"""

import argparse
import json
import re
import time
import sys
from pathlib import Path

import requests

# Project root = one level up from this script (scraper/scrape.py -> project root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

STAGES_LIST_URL = "https://www.triatlonotaure.lt/api/stages"
ALL_STAGES_URL = "https://www.triatlonotaure.lt/api/results"
RESULTS_URL = "https://www.triatlonotaure.lt/api/stages/{stage_id}/results"
PARTICIPANTS_URL = "https://www.triatlonotaure.lt/api/stages/{stage_id}/participants"

CLUB_STANDINGS_NAME = "klubų įskaita"
CLUB_STANDINGS_TOP_N = 10

FALLBACK_DISTANCES = ["od", "sd"]  # used only if a stage's own `distances` list is missing
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.4  # be polite to their server
TIMEOUT = 15
MAX_CONSECUTIVE_EMPTY_SEASONS = 3  # stop season scan after this many empty seasons in a row


def fetch_season_stages(session, season_id):
    """Fetch the stage list for one season. Returns [] on any failure or empty season."""
    try:
        resp = session.get(STAGES_LIST_URL, params={"season": season_id}, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  [season {season_id}] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def discover_all_stages(session, max_seasons=30):
    """
    Walk season IDs 1..max_seasons, collecting real races (skip aggregate entries).
    Returns (completed_stages, upcoming_stages), both {stage_id: info_dict}.
    """
    completed = {}
    upcoming = {}
    consecutive_empty = 0

    for season_id in range(1, max_seasons + 1):
        season_stages = fetch_season_stages(session, season_id)
        if not season_stages:
            consecutive_empty += 1
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY_SEASONS:
                print(f"Stopping season scan after {MAX_CONSECUTIVE_EMPTY_SEASONS} empty seasons in a row.")
                break
            continue
        consecutive_empty = 0

        usable_count = 0
        for stage in season_stages:
            if not stage.get("show_in_main_window", True):
                continue  # season-wide aggregate (club standings, combined age groups, etc), not a real race
            info = {
                "name": stage.get("name"),
                "date": stage.get("stage_date"),
                "address": stage.get("address"),
                "season_name": (stage.get("season") or {}).get("name"),
                "distances": [
                    {"code": d.get("code"), "name": d.get("name")}
                    for d in (stage.get("distances") or [])
                    if d.get("code")
                ],
            }
            if stage.get("has_results"):
                completed[stage["id"]] = info
            else:
                upcoming[stage["id"]] = info
            usable_count += 1
        print(f"Season {season_id}: found {len(season_stages)} entries ({usable_count} real races)")
        time.sleep(REQUEST_DELAY_SECONDS)

    return completed, upcoming


def fetch_stage_distance(session, stage_id, distance):
    """Fetch all pages of results for one stage + distance combo."""
    records = []
    page = 0
    while True:
        params = {"page": page, "size": PAGE_SIZE, "query": "", "distance": distance}
        try:
            resp = session.get(RESULTS_URL.format(stage_id=stage_id), params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"  [stage {stage_id} / {distance}] request failed: {e}", file=sys.stderr)
            break

        if resp.status_code != 200:
            break
        try:
            data = resp.json()
        except ValueError:
            break

        rows = data.get("result", [])
        if not rows:
            break
        records.extend(rows)

        total_pages = data.get("total_pages", 1)
        page += 1
        if page >= total_pages:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    return records


def fetch_stage_participants(session, stage_id):
    """Fetch all pages of registrations/participants for one stage (all distances at once)."""
    records = []
    page = 0
    while True:
        params = {"page": page, "size": PAGE_SIZE, "query": "", "participantType": "all", "distance": ""}
        try:
            resp = session.get(PARTICIPANTS_URL.format(stage_id=stage_id), params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"  [stage {stage_id} participants] request failed: {e}", file=sys.stderr)
            break

        if resp.status_code != 200:
            break
        try:
            data = resp.json()
        except ValueError:
            break

        rows = data.get("result", [])
        if not rows:
            break
        records.extend(rows)

        total_pages = data.get("total_pages", 1)
        page += 1
        if page >= total_pages:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    return records


def record_to_dict(record):
    """Turn a {row, titles, ...} results record into a flat dict keyed by column name."""
    titles = record.get("titles", [])
    row = record.get("row", [])
    flat = dict(zip(titles, row))
    flat["_id"] = record.get("id")
    flat["_stage_id"] = record.get("stageId")
    flat["_distance"] = record.get("distance")
    return flat


def participant_to_dict(record):
    """Flatten a participants-endpoint record into the fields the frontend needs."""
    distance = record.get("distance") or {}
    payment = record.get("payment") or {}
    return {
        "displayName": record.get("displayName"),
        "first_name": record.get("first_name"),
        "last_name": record.get("last_name"),
        "city": record.get("city"),
        "club": record.get("club"),
        "distance_name": distance.get("name"),
        "distance_code": distance.get("code"),
        "payed": record.get("payed", payment.get("payed")),
        "price": payment.get("price"),
        "_stage_id": record.get("stageId"),
    }


def stage_label(info):
    name = info.get("name") or "Unknown stage"
    date = (info.get("date") or "")[:10]  # trim to YYYY-MM-DD
    return f"{name} ({date})" if date else name


def fetch_all_stage_meta(session):
    """Fetch the flat, all-seasons stage list used to locate club-standings aggregates."""
    try:
        resp = session.get(ALL_STAGES_URL, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  [all-stages list] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def discover_club_standings_stages(session):
    """Find the 'Klubų įskaita' (club standings) aggregate stage id for each season."""
    standings_stages = {}
    for item in fetch_all_stage_meta(session):
        if not item.get("global_results_stage"):
            continue
        name = (item.get("name") or "").strip().lower()
        if CLUB_STANDINGS_NAME not in name:
            continue
        season = item.get("season") or {}
        match = re.search(r"(\d{4})", season.get("name") or "")
        season_year = match.group(1) if match else str(season.get("id") or item.get("seasonId"))
        standings_stages[item["id"]] = season_year
    return standings_stages


def fetch_club_standings_rows(session, stage_id):
    """Fetch the club-standings ('klubai') leaderboard rows for one aggregate stage."""
    params = {"page": 0, "size": 20, "query": "", "distance": "klubai"}
    try:
        resp = session.get(RESULTS_URL.format(stage_id=stage_id), params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  [club standings stage {stage_id}] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return data.get("result", [])


def build_club_standings(session):
    """Build {season_year: {stage_labels, clubs}} club-standings leaderboards."""
    standings_stages = discover_club_standings_stages(session)
    output = {}
    for stage_id, season_year in sorted(standings_stages.items(), key=lambda kv: kv[1]):
        rows = fetch_club_standings_rows(session, stage_id)
        if not rows:
            print(f"Club standings - season {season_year} (stage {stage_id}): no data returned")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        # Columns: Vieta, Klubas, RANDOM DATE, <one per real stage that season>, Taskai, Dalyviu skaicius
        stage_labels = (rows[0].get("titles") or [])[3:-2]
        clubs = []
        for r in rows:
            row = r.get("row") or []
            if len(row) < 5:
                continue
            clubs.append({
                "place": row[0],
                "club": row[1],
                "stage_points": row[3:-2],
                "total": row[-2],
                "participants": row[-1],
            })
        clubs.sort(key=lambda c: int(c["place"]) if str(c["place"]).isdigit() else 999)

        output[season_year] = {
            "stage_labels": stage_labels,
            "clubs": clubs[:CLUB_STANDINGS_TOP_N],
        }
        print(f"Club standings - season {season_year} (stage {stage_id}): {len(clubs)} clubs")
        time.sleep(REQUEST_DELAY_SECONDS)
    return output


def scrape(club_name, out_raw_path, out_filtered_path, out_stage_names_path,
           out_upcoming_path, out_club_standings_path, out_distance_names_path, max_seasons):
    session = requests.Session()
    session.headers.update({"User-Agent": "highpeaks-results-scraper/1.0"})

    print("Discovering stages across seasons...")
    print(f"  raw archive     -> {Path(out_raw_path).resolve()}")
    print(f"  filtered data   -> {Path(out_filtered_path).resolve()}")
    print(f"  stage names     -> {Path(out_stage_names_path).resolve()}")
    print(f"  upcoming/regs   -> {Path(out_upcoming_path).resolve()}")
    print(f"  club standings  -> {Path(out_club_standings_path).resolve()}")
    print(f"  distance names  -> {Path(out_distance_names_path).resolve()}\n")

    completed_stages, upcoming_stages = discover_all_stages(session, max_seasons=max_seasons)
    print(f"\nFound {len(completed_stages)} completed races and "
          f"{len(upcoming_stages)} upcoming races.\n")

    # --- Completed races: pull results, one call per distance the stage actually offered ---
    all_records = []
    distance_names = {}
    for stage_id, info in sorted(completed_stages.items()):
        distance_codes = [d["code"] for d in info.get("distances", []) if d.get("code")] or FALLBACK_DISTANCES
        for d in info.get("distances", []):
            if d.get("code") and d.get("name"):
                distance_names[d["code"]] = d["name"]

        stage_had_data = False
        for distance in distance_codes:
            records = fetch_stage_distance(session, stage_id, distance)
            if records:
                stage_had_data = True
                all_records.extend(record_to_dict(r) for r in records)
            time.sleep(REQUEST_DELAY_SECONDS)
        print(f"Results - stage {stage_id} ({stage_label(info)}): "
              f"{'OK' if stage_had_data else 'no results returned'} "
              f"[{', '.join(distance_codes)}]")

    Path(out_raw_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_raw_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {len(all_records)} total result rows to {out_raw_path}")

    club_name_lower = club_name.strip().lower()
    filtered = [r for r in all_records if (r.get("CLUB") or "").strip().lower() == club_name_lower]
    Path(out_filtered_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_filtered_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(filtered)} result rows for club '{club_name}' to {out_filtered_path}")

    # --- Upcoming races: pull registrations/participants ---
    upcoming_output = {}
    for stage_id, info in sorted(upcoming_stages.items()):
        for d in info.get("distances", []):
            if d.get("code") and d.get("name"):
                distance_names[d["code"]] = d["name"]
        participants = fetch_stage_participants(session, stage_id)
        club_participants = [
            participant_to_dict(p) for p in participants
            if (p.get("club") or "").strip().lower() == club_name_lower
        ]
        upcoming_output[str(stage_id)] = {
            "stage_name": info.get("name"),
            "stage_date": info.get("date"),
            "address": info.get("address"),
            "total_participants": len(participants),
            "club_registrations": club_participants,
        }
        print(f"Registrations - stage {stage_id} ({stage_label(info)}): "
              f"{len(club_participants)} from '{club_name}' out of {len(participants)} total")
        time.sleep(REQUEST_DELAY_SECONDS)

    Path(out_upcoming_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_upcoming_path, "w", encoding="utf-8") as f:
        json.dump(upcoming_output, f, ensure_ascii=False, indent=2)
    print(f"Saved upcoming-race registrations for {len(upcoming_output)} stages to {out_upcoming_path}")

    # --- Stage names (covers both completed and upcoming, for frontend labels) ---
    all_stages = {**completed_stages, **upcoming_stages}
    stage_names = {str(sid): stage_label(info) for sid, info in all_stages.items()}
    Path(out_stage_names_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_stage_names_path, "w", encoding="utf-8") as f:
        json.dump(stage_names, f, ensure_ascii=False, indent=2)
    print(f"Saved names for {len(stage_names)} stages to {out_stage_names_path}")

    # --- Distance code -> display name map (varies per race: od, sd, trifun, vaikai...) ---
    Path(out_distance_names_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_distance_names_path, "w", encoding="utf-8") as f:
        json.dump(distance_names, f, ensure_ascii=False, indent=2)
    print(f"Saved names for {len(distance_names)} distance codes to {out_distance_names_path}")

    # --- Club standings ("Klubų įskaita"), one leaderboard per season ---
    print("\nFetching club standings ('Klubų įskaita') per season...")
    club_standings = build_club_standings(session)
    Path(out_club_standings_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_club_standings_path, "w", encoding="utf-8") as f:
        json.dump(club_standings, f, ensure_ascii=False, indent=2)
    print(f"Saved club standings for {len(club_standings)} seasons to {out_club_standings_path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape triatlonotaure.lt results and registrations for a club.")
    parser.add_argument("--club", default="HighPeaks Lietuva", help="Exact club name to filter for")
    parser.add_argument("--max-seasons", type=int, default=15,
                         help="Upper bound on season IDs to try (stops early once seasons come back empty)")
    parser.add_argument("--out-raw", default=str(PROJECT_ROOT / "data" / "all_results_raw.json"),
                         help="Path for the unfiltered results archive")
    parser.add_argument("--out-filtered", default=str(PROJECT_ROOT / "docs" / "data.json"),
                         help="Path for the filtered results the frontend reads")
    parser.add_argument("--out-stage-names", default=str(PROJECT_ROOT / "docs" / "stage_names.json"),
                         help="Path for the stage id -> label map")
    parser.add_argument("--out-upcoming", default=str(PROJECT_ROOT / "docs" / "upcoming.json"),
                         help="Path for upcoming-race registrations (payment status etc)")
    parser.add_argument("--out-club-standings", default=str(PROJECT_ROOT / "docs" / "club_standings.json"),
                         help="Path for the per-season club standings ('Klubų įskaita') leaderboards")
    parser.add_argument("--out-distance-names", default=str(PROJECT_ROOT / "docs" / "distance_names.json"),
                         help="Path for the distance code -> display name map")
    args = parser.parse_args()

    scrape(args.club, args.out_raw, args.out_filtered, args.out_stage_names,
           args.out_upcoming, args.out_club_standings, args.out_distance_names, args.max_seasons)


if __name__ == "__main__":
    main()
