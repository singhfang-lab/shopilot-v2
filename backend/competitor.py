import os
import httpx

PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_DETAIL_URL = "https://places.googleapis.com/v1/places/{place_id}"


def _maps_key() -> str:
    return os.environ.get("GOOGLE_MAPS_KEY", "")


async def search_competitors(
    address: str,
    business_type: str,
    radius_meters: int = 1000,
    limit: int = 5,
    language: str = "en",
) -> dict:
    """Search nearby competitors using Google Maps Places API (Nearby Search v2).

    First geocodes the address, then searches for nearby places of the given type.
    """
    lat, lng = await _geocode(address)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _maps_key(),
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.rating,places.userRatingCount,places.priceLevel,"
            "places.regularOpeningHours,places.location"
        ),
    }
    body = {
        "includedTypes": [_map_business_type(business_type)],
        "maxResultCount": min(limit, 20),
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_meters),
            }
        },
        "languageCode": language,
    }

    resp = await _post(PLACES_NEARBY_URL, body, headers)

    places = resp.get("places", [])
    competitors = [_parse_place(p) for p in places]
    return {"competitors": competitors}


async def get_competitor_details(place_id: str, language: str = "en") -> dict:
    """Fetch detailed info for a single place: hours, popular times, top reviews."""
    headers = {
        "X-Goog-Api-Key": _maps_key(),
        "X-Goog-FieldMask": (
            "id,displayName,formattedAddress,rating,userRatingCount,priceLevel,"
            "regularOpeningHours,reviews"
        ),
    }
    url = PLACES_DETAIL_URL.format(place_id=place_id)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers, params={"languageCode": language})
        resp.raise_for_status()
        data = resp.json()

    return _parse_place(data, include_reviews=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _post(url: str, body: dict, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _geocode(address: str) -> tuple[float, float]:
    """Convert address string to (lat, lng) via Geocoding API."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": _maps_key()}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "OK" or not data.get("results"):
        raise ValueError(f"Geocoding failed for address '{address}': {data.get('status')}")

    loc = data["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]


def _map_business_type(business_type: str) -> str:
    """Map human-readable business type to Google Places type string."""
    mapping = {
        "餐厅": "restaurant",
        "restaurant": "restaurant",
        "咖啡": "cafe",
        "cafe": "cafe",
        "咖啡厅": "cafe",
        "奶茶": "cafe",
        "茶饮": "cafe",
        "便利店": "convenience_store",
        "超市": "supermarket",
        "药店": "pharmacy",
        "美容": "beauty_salon",
        "健身": "gym",
        "酒吧": "bar",
        "bar": "bar",
        "面包": "bakery",
        "bakery": "bakery",
        "快餐": "fast_food_restaurant",
        "快食": "fast_food_restaurant",
    }
    key = business_type.lower().strip()
    return mapping.get(key, key)


def _parse_place(place: dict, include_reviews: bool = False) -> dict:
    """Normalize a Places API v2 place object into our output schema."""
    price_map = {
        "PRICE_LEVEL_FREE": 0,
        "PRICE_LEVEL_INEXPENSIVE": 1,
        "PRICE_LEVEL_MODERATE": 2,
        "PRICE_LEVEL_EXPENSIVE": 3,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4,
    }

    hours_text = None
    oh = place.get("regularOpeningHours", {})
    if oh.get("weekdayDescriptions"):
        hours_text = " | ".join(oh["weekdayDescriptions"])

    loc = place.get("location", {})
    result = {
        "place_id": place.get("id", ""),
        "name": place.get("displayName", {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "rating": place.get("rating"),
        "reviews": place.get("userRatingCount"),
        "price_level": price_map.get(place.get("priceLevel", ""), None),
        "opening_hours": hours_text,
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "top_reviews": [],
    }

    if include_reviews:
        raw_reviews = place.get("reviews", [])[:5]
        result["top_reviews"] = [
            r.get("text", {}).get("text", "") for r in raw_reviews if r.get("text")
        ]

    return result
