from datasette.app import Datasette
import pytest
from datasette_plugin_router import Router, Body
from pydantic import BaseModel
from datasette import hookimpl, Response

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