from typing import Annotated, List, Optional

import pytest
from datasette import Response, hookimpl
from datasette.app import Datasette
from pydantic import BaseModel

from datasette_plugin_router import Body, Query, Router


async def _call(router, path, method="get", **kwargs):
    datasette = Datasette(memory=True)

    class TestPlugin:
        __name__ = "TestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    datasette.pm.register(TestPlugin(), name="test-plugin")
    try:
        return await getattr(datasette.client, method)(path, **kwargs)
    finally:
        datasette.pm.unregister(name="test-plugin")


def _search_router():
    router = Router()

    @router.GET(r"^/-/search$")
    async def search(
        q: Annotated[str, Query()],
        limit: Annotated[int, Query()] = 20,
        lat: Annotated[Optional[float], Query()] = None,
        verbose: Annotated[bool, Query()] = False,
        tag: Annotated[list[str], Query()] = [],
        n: Annotated[List[int], Query()] = [],
        size: Annotated[int, Query(alias="_size")] = 10,
    ):
        return Response.json(
            {"q": q, "limit": limit, "lat": lat, "verbose": verbose, "tag": tag, "n": n, "size": size}
        )

    return router


@pytest.mark.asyncio
async def test_required_query_param():
    router = _search_router()
    response = await _call(router, "/-/search?q=hello")
    assert response.status_code == 200
    assert response.json()["q"] == "hello"

    response = await _call(router, "/-/search")
    assert response.status_code == 400
    data = response.json()
    assert data["errors"][0]["loc"] == ["q"]
    assert data["errors"][0]["type"] == "missing"
    assert data["error"].startswith("q: ")


@pytest.mark.asyncio
async def test_int_default_and_coercion():
    router = _search_router()
    response = await _call(router, "/-/search?q=x")
    assert response.json()["limit"] == 20

    response = await _call(router, "/-/search?q=x&limit=5")
    assert response.status_code == 200
    assert response.json()["limit"] == 5

    response = await _call(router, "/-/search?q=x&limit=abc")
    assert response.status_code == 400
    err = response.json()["errors"][0]
    assert err["loc"] == ["limit"]
    assert err["type"] == "int_parsing"


@pytest.mark.asyncio
async def test_optional_float():
    router = _search_router()
    response = await _call(router, "/-/search?q=x")
    assert response.json()["lat"] is None
    response = await _call(router, "/-/search?q=x&lat=1.5")
    assert response.json()["lat"] == 1.5


@pytest.mark.asyncio
async def test_bool():
    router = _search_router()
    response = await _call(router, "/-/search?q=x&verbose=true")
    assert response.json()["verbose"] is True
    response = await _call(router, "/-/search?q=x&verbose=0")
    assert response.json()["verbose"] is False
    response = await _call(router, "/-/search?q=x&verbose=maybe")
    assert response.status_code == 400
    assert response.json()["errors"][0]["loc"] == ["verbose"]


@pytest.mark.asyncio
async def test_list_params():
    router = _search_router()
    response = await _call(router, "/-/search?q=x&tag=a&tag=b")
    assert response.json()["tag"] == ["a", "b"]
    response = await _call(router, "/-/search?q=x")
    assert response.json()["tag"] == []
    response = await _call(router, "/-/search?q=x&n=1&n=2")
    assert response.json()["n"] == [1, 2]
    response = await _call(router, "/-/search?q=x&n=1&n=x")
    assert response.status_code == 400
    err = response.json()["errors"][0]
    assert err["loc"] == ["n", 1]
    assert err["type"] == "int_parsing"


@pytest.mark.asyncio
async def test_alias():
    router = _search_router()
    response = await _call(router, "/-/search?q=x&_size=3")
    assert response.json()["size"] == 3
    response = await _call(router, "/-/search?q=x&size=3")
    assert response.json()["size"] == 10


@pytest.mark.asyncio
async def test_query_path_and_body_together():
    router = Router()
    calls = []

    class Input(BaseModel):
        name: str

    @router.POST(r"^/-/things/(?P<id>\d+)$")
    async def update(
        id: int,
        params: Annotated[Input, Body()],
        dry_run: Annotated[bool, Query()] = False,
        request=None,
    ):
        calls.append(1)
        return Response.json({"id": id, "name": params.name, "dry_run": dry_run})

    response = await _call(router, "/-/things/7?dry_run=1", method="post", json={"name": "a"})
    assert response.status_code == 200
    assert response.json() == {"id": 7, "name": "a", "dry_run": True}
    assert calls == [1]

    # Bad query string with an invalid body: the query error wins, so the
    # body was never validated, and the handler was not called.
    response = await _call(router, "/-/things/7?dry_run=maybe", method="post", content=b"not json")
    assert response.status_code == 400
    assert response.json()["errors"][0]["loc"] == ["dry_run"]
    assert calls == [1]


def test_unsupported_query_type_raises():
    router = Router()
    with pytest.raises(ValueError, match="'filters'.*Query\\(\\) with unsupported type"):

        @router.GET(r"^/-/x$")
        async def view(filters: Annotated[dict, Query()] = {}):
            return Response.json({})


def test_body_and_query_on_same_param_raises():
    router = Router()

    class Input(BaseModel):
        name: str

    with pytest.raises(ValueError, match="'params'.*both Body\\(\\) and Query\\(\\)"):

        @router.POST(r"^/-/x$")
        async def view(params: Annotated[Input, Body(), Query()]):
            return Response.json({})


def test_underscore_query_param_name_raises():
    router = Router()
    with pytest.raises(ValueError, match="'_size'.*Query\\(alias='_size'\\)"):

        @router.GET(r"^/-/x$")
        async def view(_size: Annotated[int, Query()] = 10):
            return Response.json({})


def test_openapi_query_parameters():
    router = Router()

    @router.GET(r"^/-/things/(?P<id>\d+)$")
    async def view(
        id: int,
        q: Annotated[str, Query()],
        limit: Annotated[int, Query()] = 20,
        lat: Annotated[Optional[float], Query()] = None,
        verbose: Annotated[bool, Query()] = False,
        tag: Annotated[list[str], Query()] = [],
        size: Annotated[int, Query(alias="_size")] = 10,
    ):
        return Response.json({})

    params = router.openapi_document_json()["paths"]["/-/things/{id}"]["get"]["parameters"]
    assert params[0] == {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    by_name = {p["name"]: p for p in params[1:]}
    assert [p["name"] for p in params[1:]] == ["q", "limit", "lat", "verbose", "tag", "_size"]
    assert all(p["in"] == "query" for p in params[1:])

    assert by_name["q"] == {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}
    assert by_name["limit"] == {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "default": 20},
    }
    assert by_name["lat"]["required"] is False
    assert by_name["lat"]["schema"]["default"] is None
    assert {"type": "number"} in by_name["lat"]["schema"]["anyOf"]
    assert by_name["verbose"]["schema"] == {"type": "boolean", "default": False}
    assert by_name["tag"] == {
        "name": "tag",
        "in": "query",
        "required": False,
        "schema": {"type": "array", "items": {"type": "string"}, "default": []},
        "style": "form",
        "explode": True,
    }
    assert by_name["_size"]["schema"] == {"type": "integer", "default": 10}
    assert "size" not in by_name
