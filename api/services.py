import csv
import heapq
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from django.conf import settings


EARTH_RADIUS_MILES = 3958.7613
MAX_RANGE_MILES = 500.0
MPG = 10.0
ROUTE_MATCH_RADIUS_MILES = 25.0
ROUTE_SAMPLE_INTERVAL_MILES = 5.0


class RoutePlannerError(Exception):
    pass


@dataclass(frozen=True)
class Location:
    label: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Station:
    id: int
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Candidate:
    station: Station
    route_mile: float
    distance_from_route: float


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def fetch_json(url, timeout=30):
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode_with_maptiler(text):
    api_key = settings.MAPTILER_API_KEY
    if not api_key:
        raise RoutePlannerError("MAPTILER_API_KEY is not configured.")

    url = (
        f"https://api.maptiler.com/geocoding/{quote(text, safe='')}.json?"
        + urlencode({"key": api_key, "limit": 1, "country": "us"})
    )
    data = fetch_json(url)
    feature = (data.get("features") or [None])[0]
    if not feature:
        raise RoutePlannerError(f"Could not geocode location: {text}")

    center = feature.get("center") or []
    if len(center) != 2:
        raise RoutePlannerError(f"Geocoding response did not include coordinates: {text}")

    return Location(
        label=feature.get("place_name") or text,
        latitude=float(center[1]),
        longitude=float(center[0]),
    )


def get_route_from_geoapify(start, finish):
    api_key = settings.GEOAPIFY_API_KEY
    if not api_key:
        raise RoutePlannerError("GEOAPIFY_API_KEY is not configured.")

    params = urlencode(
        {
            "waypoints": f"{start.latitude},{start.longitude}|{finish.latitude},{finish.longitude}",
            "mode": "drive",
            "type": "balanced",
            "format": "geojson",
            "units": "imperial",
            "apiKey": api_key,
        }
    )
    data = fetch_json(f"https://api.geoapify.com/v1/routing?{params}", timeout=45)
    feature = (data.get("features") or [None])[0]
    if not feature:
        raise RoutePlannerError("Geoapify did not return a route.")

    geometry = feature.get("geometry") or {}
    coordinates = flatten_route_coordinates(geometry)
    if len(coordinates) < 2:
        raise RoutePlannerError("Geoapify route did not include enough geometry points.")

    distance = float((feature.get("properties") or {}).get("distance") or 0)
    if distance <= 0:
        distance = route_length_miles(coordinates)

    return {
        "geojson": feature,
        "coordinates": coordinates,
        "distance_miles": distance,
        "duration_seconds": (feature.get("properties") or {}).get("time"),
    }


def flatten_route_coordinates(geometry):
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "LineString":
        return [(float(lon), float(lat)) for lon, lat in coordinates]
    if geometry_type == "MultiLineString":
        return [(float(lon), float(lat)) for line in coordinates for lon, lat in line]
    return []


def route_length_miles(coordinates):
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coordinates, coordinates[1:]):
        total += haversine_miles(lat1, lon1, lat2, lon2)
    return total


def cumulative_route_miles(coordinates):
    cumulative = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(coordinates, coordinates[1:]):
        cumulative.append(cumulative[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return cumulative


def sample_route(coordinates, cumulative):
    samples = []
    next_mile = 0.0
    for coord, mile in zip(coordinates, cumulative):
        if mile >= next_mile:
            samples.append((coord[0], coord[1], mile))
            next_mile += ROUTE_SAMPLE_INTERVAL_MILES
    if samples[-1][2] != cumulative[-1]:
        lon, lat = coordinates[-1]
        samples.append((lon, lat, cumulative[-1]))
    return samples


@lru_cache(maxsize=1)
def load_stations():
    csv_path = Path(settings.FUEL_PRICES_CSV)
    stations = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
        for row_number, row in enumerate(csv.DictReader(file), start=1):
            if not row.get("latitude") or not row.get("longitude"):
                continue
            try:
                stations.append(
                    Station(
                        id=row_number,
                        opis_id=row["OPIS Truckstop ID"],
                        name=row["Truckstop Name"].strip(),
                        address=row["Address"].strip(),
                        city=row["City"].strip(),
                        state=row["State"].strip(),
                        price=float(row["Retail Price"]),
                        latitude=float(row["latitude"]),
                        longitude=float(row["longitude"]),
                    )
                )
            except (KeyError, ValueError):
                continue
    return stations


def find_route_candidates(coordinates, route_distance_miles):
    # cumulative is distance between point1 -> point2 -> point3
    # this is roughly calculated by measuring the straight distance between 2 points
    # cumulative = [0, 5, 11, 18, 25]
    cumulative = cumulative_route_miles(coordinates)

    # raw_total = 25
    # totalt calculated by us, route_distance_miles as a fallback
    raw_total = cumulative[-1] or route_distance_miles

    # distances calculated by us can differ from Geoapify’s
    # so we create a scale factor

    # Geoapify distance = 792 miles
    # Our calculated distance = 760 miles

    # scale = 792 / 760
    # scale = 1.042
    scale = route_distance_miles / raw_total if raw_total else 1.0

    # sample_route picks route points roughly every 5 miles 
    # checking every fuel station against every route point would be slow.
    samples = sample_route(coordinates, cumulative)

    candidates = []

    # goes through the fuel stations from the geocoded CSV and check whether it is close to the route.
    for station in load_stations():
        best_distance = None
        best_mile = None
        for lon, lat, mile in samples:
            distance = haversine_miles(station.latitude, station.longitude, lat, lon)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_mile = mile

        if best_distance is not None and best_distance <= ROUTE_MATCH_RADIUS_MILES:
            candidates.append(
                Candidate(
                    station=station,
                    route_mile=min(best_mile * scale, route_distance_miles),
                    distance_from_route=best_distance,
                )
            )

    candidates.sort(key=lambda candidate: candidate.route_mile)
    return candidates


def choose_fuel_stops(candidates, total_miles):
    if total_miles <= MAX_RANGE_MILES:
        return [], 0.0
    
    # visualization
    # stop_points = [
    #     {"kind": "start", "mile": 0},
    #     {"kind": "station", "mile": 220},
    #     {"kind": "station", "mile": 410},
    #     {"kind": "station", "mile": 620},
    #     {"kind": "finish", "mile": 790},
    # ]
    stop_points = [{"kind": "start", "mile": 0.0, "station": None}]
    stop_points.extend({"kind": "station", "mile": c.route_mile, "station": c} for c in candidates)
    stop_points.append({"kind": "finish", "mile": total_miles, "station": None})

    # Plan cost is (fuel_cost, stop_count). Fuel cost is optimized first.
    # best_plan_cost_to_stop_point starts as "nothing is reachable yet," then fills in as valid routes are found.
    best_plan_cost_to_stop_point = [(math.inf, math.inf)] * len(stop_points)

    # previous_stop_point_index remembers the path, so after finding the best plan, we can reconstruct which stations were chosen.
    previous_stop_point_index = [None] * len(stop_points)

    # we know how to reach the start:
    # $0 cost, 0 stops.
    # this assumes the tank is full at the start
    best_plan_cost_to_stop_point[0] = (0.0, 0)

    # queue is the list of places still to explore.
    # at the beginning, the only place we know we can explore from is the start.
    stop_points_to_explore = [((0.0, 0), 0)]

    # the loop repeatedly picks the cheapest route state we know so far, then explores where we can go from there.
    while stop_points_to_explore:
        current_plan_cost, current_stop_point_index = heapq.heappop(stop_points_to_explore)
        current_stop_point = stop_points[current_stop_point_index]

        # ignore the queue entries which are not the best
        if current_plan_cost > best_plan_cost_to_stop_point[current_stop_point_index]:
            continue
        if current_stop_point["kind"] == "finish":
            break
        
        # loop to check from current location, which future locations can be reached within 500 miles?
        for next_stop_point_index in range(current_stop_point_index + 1, len(stop_points)):
            next_stop_point = stop_points[next_stop_point_index]
            segment_miles = next_stop_point["mile"] - current_stop_point["mile"]

            # if out of range then stop
            if segment_miles > MAX_RANGE_MILES:
                break
            added_stop_count = 1 if next_stop_point["kind"] == "station" else 0
            added_fuel_cost = 0.0
            if current_stop_point["kind"] == "station":
                added_fuel_cost = (segment_miles / MPG) * current_stop_point["station"].station.price
            next_plan_cost = (
                current_plan_cost[0] + added_fuel_cost,
                current_plan_cost[1] + added_stop_count,
            )
            if next_plan_cost < best_plan_cost_to_stop_point[next_stop_point_index]:
                best_plan_cost_to_stop_point[next_stop_point_index] = next_plan_cost
                previous_stop_point_index[next_stop_point_index] = current_stop_point_index
                heapq.heappush(stop_points_to_explore, (next_plan_cost, next_stop_point_index))

    finish_stop_point_index = len(stop_points) - 1
    if math.isinf(best_plan_cost_to_stop_point[finish_stop_point_index][0]):
        raise RoutePlannerError(
            "No reachable fuel plan found. Try increasing route match radius or using more station data."
        )

    path = []
    stop_point_index = finish_stop_point_index
    while stop_point_index is not None:
        path.append(stop_point_index)
        stop_point_index = previous_stop_point_index[stop_point_index]
    path.reverse()

    fuel_stops = []
    for path_index, selected_stop_point_index in enumerate(path[1:-1], start=1):
        next_stop_point = stop_points[path[path_index + 1]]
        candidate = stop_points[selected_stop_point_index]["station"]
        station = candidate.station
        segment_miles = next_stop_point["mile"] - stop_points[selected_stop_point_index]["mile"]
        gallons = segment_miles / MPG
        fuel_stops.append(
            {
                "station": station,
                "route_mile": candidate.route_mile,
                "distance_from_route_miles": candidate.distance_from_route,
                "gallons": gallons,
                "cost": gallons * station.price,
                "covers_miles": segment_miles,
            }
        )

    return fuel_stops, best_plan_cost_to_stop_point[finish_stop_point_index][0]


def route_bbox(coordinates):
    lons = [lon for lon, _lat in coordinates]
    lats = [lat for _lon, lat in coordinates]
    return [min(lons), min(lats), max(lons), max(lats)]


def plan_route(start_text, finish_text):
    start = geocode_with_maptiler(start_text)
    finish = geocode_with_maptiler(finish_text)
    route = get_route_from_geoapify(start, finish)
    # get fuel stop candidates
    candidates = find_route_candidates(route["coordinates"], route["distance_miles"])
    fuel_stops, total_cost = choose_fuel_stops(candidates, route["distance_miles"])

    return {
        "start": start,
        "finish": finish,
        "route": route,
        "candidate_count": len(candidates),
        "fuel_stops": fuel_stops,
        "total_cost": total_cost,
    }
