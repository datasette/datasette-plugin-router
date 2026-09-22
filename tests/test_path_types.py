from datetime import date
from uuid import UUID, uuid4

import pytest
from datasette import Response, hookimpl
from datasette.app import Datasette

from datasette_plugin_router import Router


@pytest.mark.asyncio
async def test_float_path_param():
    datasette = Datasette(memory=True)
    router = Router()
    captured = {}

    @router.GET(r"^/-/f/(?P<v>[^/]+)$")
    async def f(v: float):
        captured["v"] = v
        captured["type"] = type(v).__name__
        return Response.json({"v": v})

    class TestPlugin:
        __name__ = "FloatPathParamTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="float-path-param-test-plugin")

        r = await datasette.client.get("/-/f/1.5")
        assert r.status_code == 200
        assert r.json() == {"v": 1.5}
        assert captured == {"v": 1.5, "type": "float"}

        r = await datasette.client.get("/-/f/abc")
        assert r.status_code == 400
        body = r.json()
        assert body["error"] == "v: value is not a valid number"
        assert body["errors"][0]["type"] == "float_parsing"
        assert body["errors"][0]["loc"] == ["v"]
    finally:
        datasette.pm.unregister(name="float-path-param-test-plugin")


@pytest.mark.asyncio
async def test_uuid_path_param():
    datasette = Datasette(memory=True)
    router = Router()
    captured = {}

    @router.GET(r"^/-/u/(?P<v>[^/]+)$")
    async def u(v: UUID):
        captured["v"] = v
        return Response.json({"v": str(v)})

    class TestPlugin:
        __name__ = "UuidPathParamTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="uuid-path-param-test-plugin")

        value = uuid4()
        r = await datasette.client.get(f"/-/u/{value}")
        assert r.status_code == 200
        assert r.json() == {"v": str(value)}
        assert isinstance(captured["v"], UUID)
        assert captured["v"] == value

        r = await datasette.client.get("/-/u/not-a-uuid")
        assert r.status_code == 400
        body = r.json()
        assert body["error"] == "v: value is not a valid UUID"
        assert body["errors"][0]["type"] == "uuid_parsing"
        assert body["errors"][0]["loc"] == ["v"]
    finally:
        datasette.pm.unregister(name="uuid-path-param-test-plugin")


@pytest.mark.asyncio
async def test_date_path_param():
    datasette = Datasette(memory=True)
    router = Router()
    captured = {}

    @router.GET(r"^/-/d/(?P<v>[^/]+)$")
    async def d(v: date):
        captured["v"] = v
        return Response.json({"v": v.isoformat()})

    class TestPlugin:
        __name__ = "DatePathParamTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="date-path-param-test-plugin")

        r = await datasette.client.get("/-/d/2026-09-22")
        assert r.status_code == 200
        assert r.json() == {"v": "2026-09-22"}
        assert captured["v"] == date(2026, 9, 22)

        for bad in ("2026-13-01", "yesterday"):
            r = await datasette.client.get(f"/-/d/{bad}")
            assert r.status_code == 400
            body = r.json()
            assert body["error"] == "v: value is not a valid date (expected YYYY-MM-DD)"
            assert body["errors"][0]["type"] == "date_parsing"
            assert body["errors"][0]["loc"] == ["v"]
    finally:
        datasette.pm.unregister(name="date-path-param-test-plugin")


@pytest.mark.asyncio
async def test_int_path_param_still_works_regression():
    """int path params: unchanged behavior/message after adding the type table."""
    datasette = Datasette(memory=True)
    router = Router()

    @router.GET(r"^/-/i/(?P<v>[^/]+)$")
    async def i(v: int):
        return Response.json({"v": v})

    class TestPlugin:
        __name__ = "IntPathParamRegressionTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="int-path-param-regression-test-plugin")

        r = await datasette.client.get("/-/i/5")
        assert r.status_code == 200
        assert r.json() == {"v": 5}

        r = await datasette.client.get("/-/i/abc")
        assert r.status_code == 400
        body = r.json()
        assert body["error"] == "v: value is not a valid integer"
        assert body["errors"][0]["type"] == "int_parsing"
        assert body["errors"][0]["loc"] == ["v"]
    finally:
        datasette.pm.unregister(name="int-path-param-regression-test-plugin")


def test_float_param_not_in_regex_raises_at_decoration():
    router = Router()
    route = r"^/x/(?P<slug>[^/]+)$"
    with pytest.raises(ValueError) as excinfo:

        @router.GET(route)
        async def foo(v: float):
            return Response.text("unreachable")

    message = str(excinfo.value)
    assert "'v'" in message
    assert route in message
    assert "foo" in message
    assert "not a named group" in message
    assert router.routes() == []


def test_bool_path_param_still_raises_at_decoration():
    router = Router()
    with pytest.raises(ValueError, match="'flag'.*annotated bool"):

        @router.GET(r"^/b/(?P<flag>[^/]+)$")
        async def b(flag: bool):
            return Response.text("unreachable")


def test_openapi_typed_path_params():
    router = Router()

    @router.GET(r"^/-/f/(?P<v>[^/]+)$")
    async def f(v: float):
        return Response.json({"v": v})

    @router.GET(r"^/-/u/(?P<v>[^/]+)$")
    async def u(v: UUID):
        return Response.json({"v": str(v)})

    @router.GET(r"^/-/d/(?P<v>[^/]+)$")
    async def d(v: date):
        return Response.json({"v": v.isoformat()})

    @router.GET(r"^/-/s/(?P<v>[^/]+)$")
    async def s(v: str):
        return Response.json({"v": v})

    spec = router.openapi_document_json()
    paths = spec["paths"]

    float_op = paths["/-/f/{v}"]["get"]
    assert float_op["parameters"] == [
        {"name": "v", "in": "path", "required": True, "schema": {"type": "number"}}
    ]
    assert "400" in float_op["responses"]

    uuid_op = paths["/-/u/{v}"]["get"]
    assert uuid_op["parameters"] == [
        {
            "name": "v",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        }
    ]
    assert "400" in uuid_op["responses"]

    date_op = paths["/-/d/{v}"]["get"]
    assert date_op["parameters"] == [
        {
            "name": "v",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "date"},
        }
    ]
    assert "400" in date_op["responses"]

    str_op = paths["/-/s/{v}"]["get"]
    assert str_op["parameters"] == [
        {"name": "v", "in": "path", "required": True, "schema": {"type": "string"}}
    ]
    assert "400" not in str_op["responses"]
