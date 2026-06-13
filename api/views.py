import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .services import MAX_RANGE_MILES, MPG, RoutePlannerError, plan_route


def serialize_location(location):
    return {
        "label": location.label,
        "latitude": location.latitude,
        "longitude": location.longitude,
    }


def serialize_station_stop(stop):
    station = stop["station"]
    return {
        "opis_id": station.opis_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "retail_price_per_gallon": round(station.price, 4),
        "latitude": station.latitude,
        "longitude": station.longitude,
        "route_mile": round(stop["route_mile"], 2),
        "distance_from_route_miles": round(stop["distance_from_route_miles"], 2),
        "fuel_needed_gallons": round(stop["gallons"], 2),
        "fuel_cost": round(stop["cost"], 2),
        "covers_next_miles": round(stop["covers_miles"], 2),
    }


@csrf_exempt
@require_http_methods(["POST"])
def route_plan(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Request body must be valid JSON."}, status=400)

    start = str(payload.get("start", "")).strip()
    finish = str(payload.get("finish", "")).strip()
    if not start or not finish:
        return JsonResponse({"error": "Both 'start' and 'finish' are required."}, status=400)

    try:
        result = plan_route(start, finish)
    except RoutePlannerError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": "Route planning failed.", "detail": str(exc)}, status=502)

    fuel_stops = [serialize_station_stop(stop) for stop in result["fuel_stops"]]
    route = result["route"]

    return JsonResponse(
        {
            "start": serialize_location(result["start"]),
            "finish": serialize_location(result["finish"]),
            "vehicle": {
                "max_range_miles": MAX_RANGE_MILES,
                "miles_per_gallon": MPG,
                "start_with_full_tank": True,
            },
            "route": {
                "distance_miles": round(route["distance_miles"], 2),
                "duration_seconds": route["duration_seconds"],
                "geojson": route["geojson"],
            },
            "fuel": {
                "candidate_station_count": result["candidate_count"],
                "stops": fuel_stops,
                "total_spent": round(result["total_cost"], 2),
            },
            "map": {
                "provider": "MapTiler",
                "style_url": (
                    f"https://api.maptiler.com/maps/streets/style.json?key={settings.MAPTILER_API_KEY}"
                    if settings.MAPTILER_API_KEY
                    else ""
                ),
                "fuel_stop_markers": [
                    {
                        "latitude": stop["latitude"],
                        "longitude": stop["longitude"],
                        "label": stop["name"],
                        "price": stop["retail_price_per_gallon"],
                    }
                    for stop in fuel_stops
                ],
            },
        }
    )


@require_http_methods(["GET"])
def health(request):
    return JsonResponse({"status": "ok"})


@require_http_methods(["GET"])
def route_map(request):
    return render(
        request,
        "api/route_map.html",
        {
            "maptiler_style_url": (
                f"https://api.maptiler.com/maps/streets/style.json?key={settings.MAPTILER_API_KEY}"
                if settings.MAPTILER_API_KEY
                else ""
            )
        },
    )
