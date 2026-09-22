import datetime

import pytest
from datasette import Response, hookimpl
from datasette.app import Datasette
from pydantic import BaseModel

from datasette_plugin_router import Router, _serialize_result


class Output(BaseModel):
    id: int
    name: str


class Other(BaseModel):
    something: str


class Stamped(BaseModel):
    at: datetime.datetime


async def _get(router, path):
    datasette = Datasette(memory=True)

    class TestPlugin:
        __name__ = "TestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    datasette.pm.register(TestPlugin(), name="test-plugin")
    try:
        return await datasette.client.get(path)
    finally:
        datasette.pm.unregister(name="test-plugin")


@pytest.mark.asyncio
async def test_model_with_matching_output():
    router = Router()

    @router.GET(r"^/-/m$", output=Output)
    async def handler():
        return Output(id=1, name="a")

    response = await _get(router, "/-/m")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == Output(id=1, name="a").model_dump()


@pytest.mark.asyncio
async def test_model_without_output():
    router = Router()

    @router.GET(r"^/-/m$")
    async def handler():
        return Output(id=2, name="b")

    response = await _get(router, "/-/m")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"id": 2, "name": "b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, Output])
async def test_response_returned_unchanged(output):
    router = Router()

    # Body deliberately does not match Output: Responses are never validated.
    @router.GET(r"^/-/r$", output=output)
    async def handler():
        return Response.json({"anything": True}, status=201)

    response = await _get(router, "/-/r")
    assert response.status_code == 201
    assert response.json() == {"anything": True}


@pytest.mark.asyncio
async def test_dict_validated_against_output():
    router = Router()

    @router.GET(r"^/-/ok$", output=Output)
    async def ok():
        return {"id": "3", "name": "c"}

    @router.GET(r"^/-/bad$", output=Output)
    async def bad():
        return {"id": "not-an-int"}

    response = await _get(router, "/-/ok")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"id": 3, "name": "c"}

    response = await _get(router, "/-/bad")
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_dict_without_output():
    router = Router()

    @router.GET(r"^/-/d$")
    async def handler():
        return {"free": ["form", 1]}

    response = await _get(router, "/-/d")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"free": ["form", 1]}


@pytest.mark.asyncio
async def test_wrong_model_type_is_server_error():
    router = Router()

    @router.GET(r"^/-/w$", output=Output)
    async def handler():
        return Other(something="x")

    response = await _get(router, "/-/w")
    assert response.status_code == 500


def test_mismatch_error_message():
    router = Router()

    @router.GET(r"^/-/bad$", output=Output)
    async def bad():
        return {}

    entry = router._routes[0]
    with pytest.raises(TypeError) as excinfo:
        _serialize_result({"id": "x"}, Output, bad, entry)
    message = str(excinfo.value)
    assert "bad" in message
    assert "GET ^/-/bad$" in message
    assert "output=Output" in message
    assert "int_parsing" in message or "valid integer" in message


@pytest.mark.asyncio
async def test_datetime_serialised_as_iso_string():
    router = Router()

    @router.GET(r"^/-/t$", output=Stamped)
    async def handler():
        return Stamped(at=datetime.datetime(2024, 1, 2, 3, 4, 5))

    response = await _get(router, "/-/t")
    assert response.status_code == 200
    assert response.json() == {"at": "2024-01-02T03:04:05"}


@pytest.mark.asyncio
async def test_non_basemodel_output_skips_validation():
    class Plain:
        x: int

    router = Router()

    @router.GET(r"^/-/p$", output=Plain)
    async def handler():
        return {"not": "validated"}

    response = await _get(router, "/-/p")
    assert response.status_code == 200
    assert response.json() == {"not": "validated"}
