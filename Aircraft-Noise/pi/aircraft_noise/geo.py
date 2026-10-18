"""Distances between the receiver and an aircraft."""

import math

EARTH_RADIUS_KM = 6371.0
FEET_TO_M = 0.3048


def ground_km(a, b):
    """Great-circle distance between two (lat, lon) points in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def slant_km(receiver, lat, lon, alt_ft, receiver_alt_m=0.0):
    """Straight-line distance from the receiver to the aircraft in km.

    With no altitude this falls back to ground distance, which can only under-estimate.
    """
    ground = ground_km(receiver, (lat, lon))
    if alt_ft is None:
        return ground
    height_km = (alt_ft * FEET_TO_M - receiver_alt_m) / 1000
    return math.hypot(ground, height_km)
