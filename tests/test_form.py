from typing import Annotated

import pytest
from datasette import Response, hookimpl
from datasette.app import Datasette
from pydantic import BaseModel

from datasette_plugin_router import Body, Form, Router


class Signup(BaseModel):
    name: str
    count: int = 1
    tags: list[str] = []


def _form_router():
    router = Router()

    @router.POST(r"^/-/form-test$")
    async def signup(params: Annotated[Signup, Form()]):
        return Response.json(params.model_dump())

    @router.POST(r"^/-/json-test$")
    async def signup_json(params: Annotated[Signup, Body()]):
        return Response.json(params.model_dump())

    return router


@pytest.fixture
def form_datasette():
    router = _form_router()

    class TestPlugin:
        __name__ = "FormTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    datasette = Datasette(memory=True)
    datasette.pm.register(TestPlugin(), name="form-test-plugin")
    try:
        yield datasette
    finally:
        datasette.pm.unregister(name="form-test-plugin")


@pytest.mark.asyncio
async def test_urlencoded_post_is_validated(form_datasette):
    r = await form_datasette.client.post("/-/form-test", data={"name": "alex", "count": "5"})
    assert r.status_code == 200
    assert r.json() == {"name": "alex", "count": 5, "tags": []}


@pytest.mark.asyncio
async def test_missing_required_field(form_datasette):
    r = await form_datasette.client.post("/-/form-test", data={"count": "5"})
    assert r.status_code == 400
    body = r.json()
    assert body["errors"][0]["loc"] == ["name"]
    assert body["errors"][0]["type"] == "missing"


@pytest.mark.asyncio
async def test_invalid_field(form_datasette):
    r = await form_datasette.client.post("/-/form-test", data={"name": "a", "count": "x"})
    assert r.status_code == 400
    assert r.json()["errors"][0]["loc"] == ["count"]
    assert r.json()["errors"][0]["type"] == "int_parsing"


@pytest.mark.asyncio
async def test_repeated_keys_map_to_list_field(form_datasette):
    r = await form_datasette.client.post("/-/form-test", data={"name": "a", "tags": ["a", "b"]})
    assert r.status_code == 200
    assert r.json()["tags"] == ["a", "b"]

    r = await form_datasette.client.post("/-/form-test", data={"name": "a", "tags": "only"})
    assert r.json()["tags"] == ["only"]

    r = await form_datasette.client.post("/-/form-test", data={"name": "a"})
    assert r.json()["tags"] == []


@pytest.mark.asyncio
async def test_json_body_to_form_route_is_400(form_datasette):
    r = await form_datasette.client.post("/-/form-test", json={"name": "alex"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "body: expected application/x-www-form-urlencoded or multipart/form-data"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["type"] == "form_parsing"
    assert body["errors"][0]["loc"] == ["body"]
    assert body["errors"][0]["msg"] == (
        "Unsupported Content-Type: application/json. "
        "Expected application/x-www-form-urlencoded or multipart/form-data"
    )


@pytest.mark.asyncio
async def test_missing_content_type_is_400(form_datasette):
    r = await form_datasette.client.post("/-/form-test", content=b"name=alex")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "body: expected application/x-www-form-urlencoded or multipart/form-data"
    assert body["errors"][0]["type"] == "form_parsing"


@pytest.mark.asyncio
async def test_multipart_post_without_files(form_datasette):
    # (None, value) tuples are plain multipart fields, not file parts.
    r = await form_datasette.client.post(
        "/-/form-test",
        files={"name": (None, "alex"), "count": (None, "3")},
    )
    assert r.request.headers["content-type"].startswith("multipart/form-data")
    assert r.status_code == 200
    assert r.json() == {"name": "alex", "count": 3, "tags": []}


@pytest.mark.asyncio
async def test_multipart_file_parts_are_ignored(form_datasette):
    # request.form() discards file parts by default, so a file sent for a
    # model field is simply absent.
    r = await form_datasette.client.post(
        "/-/form-test",
        data={"count": "2"},
        files={"name": ("name.txt", b"alex", "text/plain")},
    )
    assert r.status_code == 400
    assert r.json()["errors"][0]["loc"] == ["name"]
    assert r.json()["errors"][0]["type"] == "missing"


@pytest.mark.asyncio
async def test_body_route_still_parses_json(form_datasette):
    r = await form_datasette.client.post("/-/json-test", json={"name": "alex"})
    assert r.status_code == 200
    assert r.json() == {"name": "alex", "count": 1, "tags": []}


def test_body_and_form_on_one_param_raises():
    router = Router()
    with pytest.raises(ValueError, match=r"both Body\(\) and Form\(\)"):

        @router.POST(r"^/-/x$")
        async def x(params: Annotated[Signup, Body(), Form()]):
            return {}


def test_body_and_form_params_on_one_route_raises():
    router = Router()
    with pytest.raises(ValueError, match=r"Body\(\) or Form\(\), not both"):

        @router.POST(r"^/-/x$")
        async def x(a: Annotated[Signup, Body()], b: Annotated[Signup, Form()]):
            return {}


def test_form_with_non_pydantic_model_raises():
    class NotAModel:
        name: str

    router = Router()
    with pytest.raises(ValueError, match="not a pydantic model"):

        @router.POST(r"^/-/x$")
        async def x(params: Annotated[NotAModel, Form()]):
            return {}


def test_openapi_form_request_body():
    doc = _form_router().openapi_document_json()
    form_op = doc["paths"]["/-/form-test"]["post"]
    assert list(form_op["requestBody"]["content"]) == ["application/x-www-form-urlencoded"]
    assert (
        form_op["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
        == Signup.model_json_schema()
    )
    assert "400" in form_op["responses"]

    json_body = doc["paths"]["/-/json-test"]["post"]["requestBody"]
    assert list(json_body["content"]) == ["application/json"]
    assert json_body["content"]["application/json"]["schema"] == Signup.model_json_schema()


def test_form_repr():
    assert repr(Form()) == "Form()"
