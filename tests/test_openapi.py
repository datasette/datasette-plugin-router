from typing import Annotated, List

import pytest
from pydantic import BaseModel

from datasette_plugin_router import (
    Body,
    Query,
    Router,
    _model_to_schema,
    _regex_to_openapi_path,
)


@pytest.mark.parametrize(
    "regex,expected",
    [
        (r"^/a/(?P<name>(?:x|y)+)$", "/a/{name}"),
        (r"^/b/(?P<f>[)\w]+)$", "/b/{f}"),
        (r"^/c/(?P<id>\d+)\.json$", "/c/{id}.json"),
        (r"^/d/(?P<a>[^/]+)/(?P<b>.*)$", "/d/{a}/{b}"),
        ("^/x$", "/x"),
        ("/plain", "/plain"),
        (r"^/-/t/(?P<id>\d+)\-(?P<slug>[^/]+)$", "/-/t/{id}-{slug}"),
        (r"^/e/(?P<x>[\)(]+)/(?P<y>[]a]+)$", "/e/{x}/{y}"),
        (r"^/f/(?:v1|v2)/(?P<id>\d+)$", "/f/(?:v1|v2)/{id}"),
    ],
)
def test_regex_to_openapi_path(regex, expected):
    assert _regex_to_openapi_path(regex) == expected


def test_nested_schema_name_collision_raises():
    def make_a():
        class Item(BaseModel):
            a: int

        return Item

    def make_b():
        class Item(BaseModel):
            b: str

        return Item

    ItemA = make_a()
    ItemB = make_b()

    class OutA(BaseModel):
        items: List[ItemA]

    class OutB(BaseModel):
        items: List[ItemB]

    router = Router()

    @router.GET(r"^/a$", output=OutA)
    async def a():
        return {}

    @router.GET(r"^/b$", output=OutB)
    async def b():
        return {}

    with pytest.raises(ValueError, match="'Item'"):
        router.openapi_document_json()


def test_same_nested_schema_reused_does_not_raise():
    class Item(BaseModel):
        a: int

    class OutA(BaseModel):
        items: List[Item]

    class OutB(BaseModel):
        item: Item

    router = Router()

    @router.GET(r"^/a$", output=OutA)
    async def a():
        return {}

    @router.GET(r"^/b$", output=OutB)
    async def b():
        return {}

    doc = router.openapi_document_json()
    assert doc["components"]["schemas"]["Item"] == {
        "properties": {"a": {"title": "A", "type": "integer"}},
        "required": ["a"],
        "title": "Item",
        "type": "object",
    }


def _ops(doc):
    return {
        (path, method): op
        for path, methods in doc["paths"].items()
        for method, op in methods.items()
    }


def test_operation_ids_from_handler_names_are_unique():
    router = Router()

    @router.GET(r"^/one$")
    async def index():
        return {}

    @router.POST(r"^/two$")
    async def index():  # noqa: F811
        return {}

    @router.POST(r"^/three$")
    async def index():  # noqa: F811
        return {}

    @router.GET(r"^/four$")
    async def other():
        return {}

    ops = _ops(router.openapi_document_json())
    assert ops[("/one", "get")]["operationId"] == "index"
    assert ops[("/two", "post")]["operationId"] == "index_post"
    assert ops[("/three", "post")]["operationId"] == "index_post_2"
    assert ops[("/four", "get")]["operationId"] == "other"


def test_summary_and_description_from_docstring():
    router = Router()

    @router.GET(r"^/documented$")
    async def documented():
        """Fetch the thing.

        Longer explanation
        over two lines.
        """
        return {}

    @router.GET(r"^/one-line$")
    async def one_line():
        """Just a summary."""
        return {}

    @router.GET(r"^/bare$")
    async def bare():
        return {}

    ops = _ops(router.openapi_document_json())
    documented_op = ops[("/documented", "get")]
    assert documented_op["summary"] == "Fetch the thing."
    assert documented_op["description"] == "Longer explanation\nover two lines."
    assert ops[("/one-line", "get")]["summary"] == "Just a summary."
    assert "description" not in ops[("/one-line", "get")]
    assert "summary" not in ops[("/bare", "get")]
    assert "description" not in ops[("/bare", "get")]
    assert ops[("/bare", "get")]["operationId"] == "bare"


VALIDATION_400 = {
    "description": "Validation error",
    "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/ValidationError"}}
    },
}


def test_error_responses():
    class Input(BaseModel):
        id: int

    router = Router()

    @router.POST(r"^/body$")
    async def with_body(params: Annotated[Input, Body()]):
        return {}

    @router.GET(r"^/str/(?P<name>[^/]+)$")
    async def with_str(name: str):
        return {}

    @router.GET(r"^/int/(?P<id>\d+)$")
    async def with_int(id: int):
        return {}

    @router.GET(r"^/query$")
    async def with_query(q: Annotated[str, Query()]):
        return {}

    @router.GET(r"^/secret$", permission="view-secret")
    async def secret():
        return {}

    doc = router.openapi_document_json()
    ops = _ops(doc)
    assert ops[("/body", "post")]["responses"]["400"] == VALIDATION_400
    assert ops[("/int/{id}", "get")]["responses"]["400"] == VALIDATION_400
    assert ops[("/query", "get")]["responses"]["400"] == VALIDATION_400
    assert set(ops[("/str/{name}", "get")]["responses"]) == {"200"}
    assert ops[("/secret", "get")]["responses"] == {
        "200": {"description": "OK"},
        "403": {"description": "Forbidden"},
    }
    assert doc["components"]["schemas"]["ValidationError"] == {
        "type": "object",
        "properties": {
            "error": {"type": "string"},
            "errors": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["error", "errors"],
    }


def test_no_validation_error_component_when_unused():
    router = Router()

    @router.GET(r"^/str/(?P<name>[^/]+)$")
    async def with_str(name: str):
        return {}

    assert "components" not in router.openapi_document_json()


def test_model_to_schema_propagates_errors():
    class Broken:
        @classmethod
        def model_json_schema(cls):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _model_to_schema(Broken)
