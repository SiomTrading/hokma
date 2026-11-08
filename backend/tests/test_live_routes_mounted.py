"""The engine mounts the live price channel and Connections (a placeholder file name kept from the first run; the
content is a real check of the routes the app exposes)."""
from hokma.core.app import create_app


def test_the_app_exposes_the_live_channel_and_the_connections_routes():
    paths = {route.path for route in create_app().routes}
    assert "/ws/market" in paths and "/api/connections" in paths
    assert "/api/connections/{provider_id}/test" in paths and "/api/market/candles" in paths
