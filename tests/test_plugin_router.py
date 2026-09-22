from datasette.app import Datasette
import pytest
from datasette_plugin_router import Router, Body
from pydantic import BaseModel
from datasette import hookimpl, Response
from pydantic import field_validator
from typing import List, Annotated

@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    data = response.json()
    # datasette >= 1.0a36 wraps the list in {"ok": ..., "plugins": [...]}
    plugins = data["plugins"] if isinstance(data, dict) else data
    installed_plugins = {p["name"] for p in plugins}
    assert "datasette-plugin-router" in installed_plugins



@pytest.mark.asyncio
async def test_spec(snapshot):
    datasette = Datasette(memory=True)
    class Input(BaseModel):
        id: int

    class Output(BaseModel):
        id_negative: int

    router = Router(title="Test API", version="1.2.3", server_url="http://example.com")

    @router.POST("/test", output=Output)
    async def test_endpoint(params: Annotated[Input, Body()]):
        return Response.json(Output(id_negative=-1 * params.id).model_dump())
    
    @router.GET(r"/hello/(?P<name>.*)$")
    async def hello(name: str):
        return Response.html(f"<h1>Hello, {name}!</h1>")
    
    assert router.openapi_document_json() == snapshot(name="router spec")
    
    class TestPlugin:
        __name__ = "TestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()
    
    try:
        datasette.pm.register(TestPlugin(), name="test-plugin")

        result = await datasette.client.post("/test", json={"id": 42})
        assert result.status_code == 200
        assert result.json() == {"id_negative": -42}

    finally:
        datasette.pm.unregister(name="test-plugin")


@pytest.mark.asyncio
async def test_nested_pydantic_models_openapi():
    """Test that nested Pydantic models generate valid OpenAPI with components.schemas."""
    
    class DocumentListItem(BaseModel):
        id: int
        title: str
    
    class DocumentListOutput(BaseModel):
        documents: List[DocumentListItem]
        total: int

    router = Router(title="Nested API", version="1.0.0", server_url="http://example.com")

    @router.GET("/documents", output=DocumentListOutput)
    async def list_documents():
        return Response.json({"documents": [], "total": 0})
    
    spec = router.openapi_document_json()
    
    # Verify that $defs was extracted and moved to components.schemas
    assert "components" in spec, "Should have components section"
    assert "schemas" in spec["components"], "Should have schemas in components"
    assert "DocumentListItem" in spec["components"]["schemas"], "Should have DocumentListItem in schemas"
    
    # Verify the nested model schema is correct
    item_schema = spec["components"]["schemas"]["DocumentListItem"]
    assert item_schema["type"] == "object"
    assert "id" in item_schema["properties"]
    assert "title" in item_schema["properties"]
    
    # Verify the response schema uses the correct $ref
    response_schema = spec["paths"]["/documents"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$defs" not in response_schema, "Should not have $defs in inline schema"
    
    # Verify the $ref points to components/schemas
    docs_property = response_schema["properties"]["documents"]
    assert docs_property["items"]["$ref"] == "#/components/schemas/DocumentListItem"


@pytest.mark.asyncio
async def test_int_url_var_param():
    """Test that int-annotated URL params are cast to int before being passed."""
    datasette = Datasette(memory=True)

    router = Router(title="Int API", version="1.0.0", server_url="http://example.com")

    captured = {}

    @router.GET(r"/items/(?P<item_id>\d+)$")
    async def item(item_id: int):
        captured["item_id"] = item_id
        captured["type"] = type(item_id).__name__
        return Response.json({"id": item_id, "type": type(item_id).__name__})

    class TestPlugin:
        __name__ = "IntUrlVarTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="int-url-var-test-plugin")

        result = await datasette.client.get("/items/42")
        assert result.status_code == 200
        assert result.json() == {"id": 42, "type": "int"}
        assert captured == {"item_id": 42, "type": "int"}

        # Verify OpenAPI emits integer schema for int-annotated path params
        spec = router.openapi_document_json()
        params = spec["paths"]["/items/{item_id}"]["get"]["parameters"]
        assert params == [
            {"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}}
        ]
    finally:
        datasette.pm.unregister(name="int-url-var-test-plugin")


@pytest.mark.asyncio
async def test_annotated_body_syntax():
    """Test that Annotated[Model, Body()] syntax works for type-safe parameters."""
    datasette = Datasette(memory=True)
    
    class Input(BaseModel):
        id: int
        name: str

    class Output(BaseModel):
        id_negative: int
        name_upper: str

    router = Router(title="Annotated API", version="1.0.0", server_url="http://example.com")

    # Using Annotated[Model, Body()] for full type safety
    @router.POST("/annotated-test", output=Output)
    async def test_endpoint(params: Annotated[Input, Body()]):
        # params is now properly typed as Input, not Body[Input]
        # Type checkers understand params.id is int, params.name is str
        return Response.json(Output(
            id_negative=-1 * params.id,
            name_upper=params.name.upper()
        ).model_dump())
    
    class TestPlugin:
        __name__ = "AnnotatedTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()
    
    try:
        datasette.pm.register(TestPlugin(), name="annotated-test-plugin")

        # Test the endpoint works correctly
        result = await datasette.client.post("/annotated-test", json={"id": 42, "name": "hello"})
        assert result.status_code == 200
        assert result.json() == {"id_negative": -42, "name_upper": "HELLO"}
        
        # Verify OpenAPI spec is generated correctly
        spec = router.openapi_document_json()
        assert "/annotated-test" in spec["paths"]
        post_spec = spec["paths"]["/annotated-test"]["post"]
        
        # Should have request body schema
        assert "requestBody" in post_spec
        assert post_spec["requestBody"]["required"] is True
        request_schema = post_spec["requestBody"]["content"]["application/json"]["schema"]
        assert "properties" in request_schema
        assert "id" in request_schema["properties"]
        assert "name" in request_schema["properties"]
        
        # Should have response schema
        response_schema = post_spec["responses"]["200"]["content"]["application/json"]["schema"]
        assert "properties" in response_schema
        assert "id_negative" in response_schema["properties"]
        assert "name_upper" in response_schema["properties"]

    finally:
        datasette.pm.unregister(name="annotated-test-plugin")


@pytest.mark.asyncio
async def test_body_validation_returns_400():
    """A bad Body() request body yields a 400 with {"error", "errors"}, not a 500."""
    datasette = Datasette(memory=True)

    class Input(BaseModel):
        id: int
        name: str

        @field_validator("name")
        @classmethod
        def name_not_empty(cls, v):
            if not v:
                raise ValueError("name must not be empty")
            return v

    router = Router(title="Validation API", version="1.0.0", server_url="http://example.com")

    @router.POST("/validate-test")
    async def validate_endpoint(params: Annotated[Input, Body()]):
        return Response.json({"id": params.id, "name": params.name})

    class TestPlugin:
        __name__ = "ValidationTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="validation-test-plugin")

        # valid body is injected and the handler runs normally
        ok = await datasette.client.post("/validate-test", json={"id": 1, "name": "x"})
        assert ok.status_code == 200
        assert ok.json() == {"id": 1, "name": "x"}

        # wrong type
        bad_type = await datasette.client.post(
            "/validate-test", json={"id": "not-an-int", "name": "x"}
        )
        assert bad_type.status_code == 400
        body = bad_type.json()
        assert isinstance(body["error"], str)
        assert body["errors"][0]["loc"] == ["id"]

        # missing required field
        missing = await datasette.client.post("/validate-test", json={"id": 1})
        assert missing.status_code == 400
        assert "name" in missing.json()["error"]

        # custom validator message passes through verbatim
        custom = await datasette.client.post("/validate-test", json={"id": 1, "name": ""})
        assert custom.status_code == 400
        assert "name must not be empty" in custom.json()["error"]

        # empty body
        empty = await datasette.client.post(
            "/validate-test", content=b"", headers={"content-type": "application/json"}
        )
        assert empty.status_code == 400
        assert "error" in empty.json()

        # non-JSON body
        non_json = await datasette.client.post(
            "/validate-test", content=b"this is not json",
            headers={"content-type": "application/json"},
        )
        assert non_json.status_code == 400
        assert "error" in non_json.json()

    finally:
        datasette.pm.unregister(name="validation-test-plugin")


@pytest.mark.asyncio
async def test_body_oversize_returns_413():
    """A Body() request larger than Datasette's max_post_body_bytes gets a 413.

    Datasette >= 1.0a36 enforces a 2MB default cap in request.post_body(),
    which Body() relies on, before the router ever sees the payload.
    """
    datasette = Datasette(memory=True)

    class Input(BaseModel):
        id: int

    router = Router(title="Oversize API", version="1.0.0", server_url="http://example.com")

    @router.POST("/oversize-test")
    async def oversize_endpoint(params: Annotated[Input, Body()]):
        return Response.json({"id": params.id})

    class TestPlugin:
        __name__ = "OversizeTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="oversize-test-plugin")

        result = await datasette.client.post(
            "/oversize-test",
            content=b"x" * (3 * 1024 * 1024),
            headers={"content-type": "application/json"},
        )
        assert result.status_code == 413

    finally:
        datasette.pm.unregister(name="oversize-test-plugin")


def test_view_carries_handler_identity():
    router = Router()

    @router.GET(r"/ident$")
    async def my_handler():
        """Handler docstring."""
        return Response.text("ok")

    # The decorator returns the per-route view, so `my_handler` is the view
    # here; routes() hands Datasette a method dispatcher that wraps it. Both
    # must carry the handler's identity.
    dispatcher = router.routes()[0][1]
    for view in (my_handler, dispatcher):
        assert view.__name__ == "my_handler"
        assert view.__doc__ == "Handler docstring."
        assert view.__qualname__.endswith("my_handler")
        # functools.wraps would set __wrapped__, which makes inspect.signature()
        # report the handler's signature and breaks Datasette's injection.
        assert not hasattr(view, "__wrapped__")


@pytest.mark.asyncio
async def test_request_datasette_and_url_vars_injected():
    datasette = Datasette(memory=True)
    router = Router()
    captured = {}

    @router.GET(r"/things/(?P<slug>[a-z]+)/(?P<n>\d+)$")
    async def thing(request, datasette, slug: str, n: int):
        """Thing handler."""
        captured.update(request=request, datasette=datasette, slug=slug, n=n)
        return Response.json({"slug": slug, "n": n})

    class TestPlugin:
        __name__ = "InjectionTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="injection-test-plugin")

        result = await datasette.client.get("/things/abc/7")
        assert result.status_code == 200
        assert result.json() == {"slug": "abc", "n": 7}
        assert captured["datasette"] is datasette
        assert captured["request"].path == "/things/abc/7"
        assert type(captured["slug"]) is str
        assert type(captured["n"]) is int
    finally:
        datasette.pm.unregister(name="injection-test-plugin")


@pytest.mark.asyncio
async def test_legacy_body_subscript_syntax():
    datasette = Datasette(memory=True)

    class Input(BaseModel):
        id: int

    router = Router()

    @router.POST(r"/legacy$")
    async def legacy(params: Body[Input]):  # type: ignore[valid-type]
        assert isinstance(params, Input)
        return Response.json({"id": params.id})

    class TestPlugin:
        __name__ = "LegacyBodyTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="legacy-body-test-plugin")

        result = await datasette.client.post("/legacy", json={"id": 5})
        assert result.status_code == 200
        assert result.json() == {"id": 5}

        bad = await datasette.client.post("/legacy", json={"id": "nope"})
        assert bad.status_code == 400
    finally:
        datasette.pm.unregister(name="legacy-body-test-plugin")


def _method_test_router():
    router = Router()

    @router.POST(r"/delete-thing$")
    async def delete_thing():
        return Response.json({"deleted": 7})

    @router.GET(r"/read-thing$")
    async def read_thing():
        return Response.json({"read": True})

    @router.GET(r"/items$")
    async def list_items():
        return Response.text("GET handler")

    @router.POST(r"/items$")
    async def create_item():
        return Response.text("POST handler")

    return router


@pytest.mark.asyncio
async def test_http_method_dispatch():
    datasette = Datasette(memory=True)
    router = _method_test_router()

    class TestPlugin:
        __name__ = "MethodDispatchTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    try:
        datasette.pm.register(TestPlugin(), name="method-dispatch-test-plugin")

        # GET on a POST-declared route is rejected
        r = await datasette.client.get("/delete-thing")
        assert r.status_code == 405
        assert r.headers["allow"] == "POST"
        assert "error" in r.json()

        # PUT on a GET-declared route is rejected; HEAD is advertised
        r = await datasette.client.put("/read-thing")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD"

        # GET and POST on the same regex each reach their own handler
        r = await datasette.client.get("/items")
        assert r.status_code == 200
        assert r.text == "GET handler"
        r = await datasette.client.post("/items")
        assert r.status_code == 200
        assert r.text == "POST handler"
        r = await datasette.client.request("DELETE", "/items")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD, POST"

        # HEAD on a GET route is served by the GET handler
        r = await datasette.client.head("/read-thing")
        assert r.status_code == 200
    finally:
        datasette.pm.unregister(name="method-dispatch-test-plugin")


def test_routes_one_tuple_per_path():
    router = _method_test_router()
    routes = router.routes()
    assert [path for path, _ in routes] == [r"/delete-thing$", r"/read-thing$", r"/items$"]
    views = dict(routes)
    assert views[r"/items$"].__name__ == "list_items_or_create_item"
    assert views[r"/items$"].__qualname__ == "list_items_or_create_item"
    assert views[r"/items$"].__doc__ is None
    assert views[r"/read-thing$"].__name__ == "read_thing"
    assert not hasattr(views[r"/items$"], "__wrapped__")