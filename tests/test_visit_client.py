"""Where a Product / Outlet visit was sent from (migration 132): the attendance app (native or PWA) or Operations."""
import pytest

from app.visit_forms import client_of


@pytest.mark.parametrize("header,ua,client", [
    (None, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)", "OPERATIONS"),
    ("attendance", "okhttp/4.12.0", "ATTENDANCE_APP"),
    ("attendance", "Expo/1017565 CFNetwork/1568 Darwin/24.0.0", "ATTENDANCE_APP"),
    ("attendance", "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile", "ATTENDANCE_PWA"),
    ("other", "okhttp/4.12.0", "OPERATIONS"),
])
def test_client_of(header, ua, client):
    assert client_of(header, ua) == client
