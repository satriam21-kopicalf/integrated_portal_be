"""Stage 3 (2026-11-23): the HRMS punch of a COTR employee against the pool."""
from app.cotr_attendance import distance_m, where_of


def test_distance_and_radius():
    pool = {"latitude": -6.18, "longitude": 106.84, "radius_m": 150}
    assert round(distance_m(-6.18, 106.84, -6.18, 106.84)) == 0
    m, near = where_of({"lat": -6.1810, "lng": 106.84}, pool)  # ~111 m south
    assert 100 < m < 120 and near is True
    m, near = where_of({"lat": -6.1830, "lng": 106.84}, pool)  # ~333 m
    assert near is False


def test_no_coordinates_is_unknown():
    assert where_of({"lat": None, "lng": None}, {"latitude": -6.18, "longitude": 106.84}) == (None, None)
    assert where_of({"lat": -6.18, "lng": 106.84}, {}) == (None, None)
