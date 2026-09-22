from __future__ import annotations

from typing import Annotated

import pytest
from datasette import Response, hookimpl
from datasette.app import Datasette
from pydantic import BaseModel

from datasette_plugin_router import Body, Router


# Module level: eval_str resolves annotations against the handler's globals,
# so the model must be importable from here (as in real plugin modules).
class In(BaseModel):
    x: int


@pytest.mark.asyncio
async def test_string_annotations_are_resolved():
    datasette = Datasette(memory=True)
    router = Router()

    @router.POST(r"^/future/body$")
    async def create(body: Annotated[In, Body()]):
        return Response.json(body.model_dump())

    @router.GET(r"^/future/item/(?P<id>\d+)$")
    async def item(id: int):
        return Response.json({"id": id})

    # The OpenAPI request body is seen through the string annotation.
    assert router._routes[0].input_schema is not None

    class TestPlugin:
        __name__ = "FutureAnnotationsTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="future-annotations-test-plugin")

        r = await datasette.client.post("/future/body", json={"x": 1})
        assert r.status_code == 200
        assert r.json() == {"x": 1}

        r = await datasette.client.get("/future/item/5")
        assert r.status_code == 200
        assert r.json() == {"id": 5}
    finally:
        datasette.pm.unregister(name="future-annotations-test-plugin")


def test_unbindable_param_still_raises_with_string_annotations():
    router = Router()
    with pytest.raises(ValueError, match="'id'.*not a named group"):

        @router.GET(r"^/future/x/(?P<slug>[^/]+)$")
        async def foo(id: int):
            return Response.text("unreachable")
