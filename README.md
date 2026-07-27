# HighPeaks Lietuva — Race Results Tracker

Scrapes results for the club **HighPeaks Lietuva** from
[triatlonotaure.lt](https://www.triatlonotaure.lt/stages), stores them as JSON,
and shows them in a small static dashboard. Runs entirely on free tiers:
GitHub (code + scheduled scraping via Actions) + GitHub Pages (hosting).

## How it works

```
scraper/scrape.py   →  1. walks season IDs, calling the stage-list API to discover
                          every real race (with its actual name/date/address),
                          split into completed (has_results: true) and
                          upcoming (has_results: false)
                       2. for completed races: calls the results API for both
                          distances (od = Olimpinė, sd = Sprinto), filters rows
                          where CLUB == "HighPeaks Lietuva"
                       3. for upcoming races: calls the participants API,
                          filters registrants by club, and records payment status
                       4. calls the all-stages endpoint to find each season's
                          "Klubų įskaita" (club standings) aggregate stage, then
                          fetches its club leaderboard (top 10, with per-stage points)
                       │
                       ├─ data/all_results_raw.json   (every athlete, every club — full archive)
                       ├─ docs/data.json              (just HighPeaks Lietuva results)
                       ├─ docs/stage_names.json       (stage id -> real name/date label)
                       ├─ docs/upcoming.json          (per upcoming stage: HighPeaks
                       │                                registrants + paid/unpaid status)
                       └─ docs/club_standings.json    (per season: top 10 clubs, total +
                                                        per-stage points, from "Klubų įskaita")

docs/index.html      →  static dashboard, fetches all of the above directly
                         (same folder, no CORS issues)

.github/workflows/scrape.yml → runs the scraper on a schedule and commits updated data automatically
```

Four API endpoints, all found via browser devtools (not publicly documented):

```
GET https://www.triatlonotaure.lt/api/stages?season={season_id}
    → real stage names, dates, addresses, and a has_results flag for that season

GET https://www.triatlonotaure.lt/api/stages/{stage_id}/results?page=0&size=100&query=&distance=od
    → od = Olimpinė distancija, sd = Sprinto distancija (for races that already happened)

GET https://www.triatlonotaure.lt/api/stages/{stage_id}/participants?page=0&size=100&query=&participantType=all&distance=
    → registration list for a race that hasn't happened yet, including "payed" (bool)
      and "payment": {"payed": bool, "price": "45.00"}

GET https://www.triatlonotaure.lt/api/results
    → flat list of every stage across every season, including the season-wide
      aggregate entries (global_results_stage: true). The one named "Klubų įskaita"
      is that season's club-standings leaderboard; its id is then used as
      {stage_id} in:
GET https://www.triatlonotaure.lt/api/stages/{stage_id}/results?page=0&size=20&query=&distance=klubai
    → club standings for that season: place, club name, points per real stage
      (0 for stages that haven't happened yet), season total, and participant count
```

Each result row already carries its own `"distance"` field, so even if a
distance code ever turns out wrong, you can re-slice the raw archive by that
field instead of re-scraping.

**One quirk worth knowing about the stage list**: some entries returned by
`/api/stages` aren't individual races — they're season-wide aggregates like
overall age-group standings or "Klubų įskaita" (club standings), flagged with
`global_results_stage: true` / `show_in_main_window: false`. The scraper skips
those automatically since they don't represent a single event someone raced in.

## Running it yourself (first time)

You need Python 3.9+ locally (or use GitHub Actions — see below — instead of running anything on your own machine).

```bash
cd scraper
pip install -r requirements.txt
python scrape.py --club "HighPeaks Lietuva"
```

This walks every season it can find, discovers every race, and writes
`data/all_results_raw.json`, `docs/data.json`, and `docs/stage_names.json`.
Open `docs/index.html` in a browser to check it — or run a local static
server from `docs/` (`python -m http.server`) since opening the file directly
via `file://` can occasionally block `fetch()` in some browsers.

Useful flags:
- `--club "Other Club Name"` — track a different club instead
- `--max-seasons 15` — how far back to look; the scraper stops early once it
  hits a few empty seasons in a row, so a generous number here is harmless

## Putting it on GitHub + free hosting

1. Create a new GitHub repo and push this folder to it.
2. In the repo settings → **Pages**, set the source to the `docs/` folder on
   your default branch. GitHub will give you a free URL like
   `https://yourusername.github.io/highpeaks-triathlon/`.
3. The included workflow (`.github/workflows/scrape.yml`) runs nightly
   and re-commits fresh data automatically. You can also trigger it manually
   from the **Actions** tab (`Run workflow` button) any time — e.g. right
   after this first push, so you don't have to wait for the next scheduled run.
4. No API keys or secrets needed — it only calls a public read-only endpoint.

## Known caveats / things worth double-checking

- **Season range**: the scraper walks season IDs from 1 upward and stops
  after 3 empty seasons in a row. This wasn't independently verified against
  every possible season boundary, so if a known past season is ever missing
  from the archive, bump `--max-seasons` and re-run.
- **Politeness**: the scraper waits ~0.4s between requests. Please don't
  remove that — it's a small public results site, not a CDN.
- **Terms of use**: this pulls from a public results page for personal,
  non-commercial tracking. Worth a quick look at the site's terms if you
  ever plan to publish this more widely or scrape much more aggressively.

## Extending it

- Swap the flat JSON for SQLite if you want to run real SQL queries later —
  the `record_to_dict()` output is already a flat, consistent dict per row,
  so loading it into a `results` table is a ~10 line script.
- Add more clubs by running the scraper with `--club "Other Club Name"` into
  a different output file, and add another dropdown option in the frontend.
- The segmented swim/bike/run bar and the athlete progress chart are both in
  `docs/index.html` — everything is one file, so it's easy to tweak styling
  or add new columns/metrics without a build step.

