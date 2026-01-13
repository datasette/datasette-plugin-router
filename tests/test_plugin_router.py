from datasette.app import Datasette
import pytest
from datasette_plugin_router import Router, Body
from pydantic import BaseModel
from datasette import hookimpl, Response
from typing import List, Annotated

@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
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
    async def test_endpoint(params: Body[Input]):
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