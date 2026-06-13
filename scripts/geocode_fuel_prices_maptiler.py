#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen


MAPTILER_URL = "https://api.maptiler.com/geocoding/{query}.json?key={key}&limit=1&country=us"


def location_key(row):
    return " | ".join(
        [
            row["Address"].strip(),
            row["City"].strip(),
            row["State"].strip(),
        ]
    )


def geocode_location(api_key, query):
    url = MAPTILER_URL.format(query=quote(query, safe=""), key=api_key)
    with urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    feature = data.get("features", [None])[0]
    if not feature:
        return {
            "latitude": "",
            "longitude": "",
            "geocode_place_name": "",
            "geocode_place_type": "",
            "geocode_relevance": "",
            "geocode_status": "no_result",
        }

    center = feature.get("center") or ["", ""]
    place_type = ",".join(feature.get("place_type") or [])
    return {
        "latitude": center[1],
        "longitude": center[0],
        "geocode_place_name": feature.get("place_name", ""),
        "geocode_place_type": place_type,
        "geocode_relevance": feature.get("relevance", ""),
        "geocode_status": "ok",
    }


def load_cache(path):
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_cache(path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2, sort_keys=True)


def write_output(path, rows, cache):
    extra_fields = [
        "latitude",
        "longitude",
        "geocode_place_name",
        "geocode_place_type",
        "geocode_relevance",
        "geocode_status",
    ]
    fieldnames = list(rows[0].keys()) + extra_fields

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            output_row = dict(row)
            output_row.update(cache.get(location_key(row), {}))
            writer.writerow(output_row)


def main():
    parser = argparse.ArgumentParser(description="Geocode fuel-prices.csv with MapTiler.")
    parser.add_argument("--input", default="fuel-prices.csv")
    parser.add_argument("--output", default="fuel-prices-geocoded.csv")
    parser.add_argument("--cache", default=".cache/maptiler-geocode-cache.json")
    parser.add_argument("--api-key", default=os.getenv("MAPTILER_API_KEY"))
    parser.add_argument("--delay", type=float, default=0.25, help="Delay between API calls in seconds.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max new lookups for testing.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.api_key:
        raise SystemExit("Missing MapTiler API key. Set MAPTILER_API_KEY or pass --api-key.")

    input_path = Path(args.input)
    output_path = Path(args.output)
    cache_path = Path(args.cache)

    with input_path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    cache = load_cache(cache_path)
    unique_locations = []
    seen = set()
    for row in rows:
        key = location_key(row)
        if key not in seen:
            seen.add(key)
            unique_locations.append((key, f"{row['Address']}, {row['City']}, {row['State']}, USA"))

    pending = [
        (key, query)
        for key, query in unique_locations
        if key not in cache or str(cache[key].get("geocode_status", "")).startswith("error:")
    ]
    if args.limit:
        pending = pending[: args.limit]

    logging.info("Rows: %s", len(rows))
    logging.info("Unique locations: %s", len(unique_locations))
    logging.info("Cached locations: %s", len(cache))
    logging.info("New lookups this run: %s", len(pending))

    attempted = 0
    for key, query in pending:
        try:
            cache[key] = geocode_location(args.api_key, query)
            attempted += 1
            status = cache[key]["geocode_status"]
            logging.info("[%s/%s] %s: %s", attempted, len(pending), status, query)
        except Exception as exc:
            attempted += 1
            cache[key] = {
                "latitude": "",
                "longitude": "",
                "geocode_place_name": "",
                "geocode_place_type": "",
                "geocode_relevance": "",
                "geocode_status": f"error: {exc}",
            }
            logging.exception("[%s/%s] failed: %s", attempted, len(pending), query)

        if attempted % 25 == 0:
            save_cache(cache_path, cache)
            write_output(output_path, rows, cache)
            logging.info("Saved progress to %s", output_path)

        time.sleep(args.delay)

    save_cache(cache_path, cache)
    write_output(output_path, rows, cache)
    logging.info("Done. Wrote %s", output_path)


if __name__ == "__main__":
    main()
