from datasette.app import Datasette
import pytest
from datasette_plugin_router import Router, Body
from datasette import hookimpl, Response
from pydantic import BaseModel
from typing import Annotated


def _csrf_router():
    class In(BaseModel):
        value: int

    router = Router()

    @router.POST(r"^/-/csrf-test$")
    async def create_thing(params: Annotated[In, Body()]):
        return Response.json({"value": params.value})

    @router.GET(r"^/-/csrf-get-test$")
    async def read_thing():
        return Response.json({"ok": True})

    return router


@pytest.fixture
def csrf_datasette():
    router = _csrf_router()

    class TestPlugin:
        __name__ = "CsrfTestPlugin"

        @hookimpl
        def register_routes(datasette):
            return router.routes()

    datasette = Datasette(memory=True)
    datasette.pm.register(TestPlugin(), name="csrf-test-plugin")
    try:
        yield datasette
    finally:
        datasette.pm.unregister(name="csrf-test-plugin")


def _cookie(datasette):
    return {"ds_actor": datasette.sign({"a": {"id": "alice"}}, "actor")}


@pytest.mark.asyncio
async def test_same_origin_post_passes(csrf_datasette):
    datasette = csrf_datasette
    r = await datasette.client.post(
        "/-/csrf-test",
        json={"value": 1},
        cookies=_cookie(datasette),
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200
    assert r.json() == {"value": 1}


@pytest.mark.asyncio
async def test_cross_site_post_forbidden(csrf_datasette):
    datasette = csrf_datasette
    r = await datasette.client.post(
        "/-/csrf-test",
        json={"value": 1},
        cookies=_cookie(datasette),
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_mismatched_origin_forbidden(csrf_datasette):
    datasette = csrf_datasette
    r = await datasette.client.post(
        "/-/csrf-test",
        json={"value": 1},
        cookies=_cookie(datasette),
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_matching_origin_passes(csrf_datasette):
    datasette = csrf_datasette
    # datasette.client rewrites "/" paths to "http://localhost/..." (see
    # DatasetteClient._fix in datasette/app.py), so that's the Host the
    # middleware sees the request arriving on.
    r = await datasette.client.post(
        "/-/csrf-test",
        json={"value": 1},
        cookies=_cookie(datasette),
        headers={"Origin": "http://localhost"},
    )
    assert r.status_code == 200
    assert r.json() == {"value": 1}


@pytest.mark.asyncio
async def test_no_origin_no_sec_fetch_site_passes(csrf_datasette):
    datasette = csrf_datasette
    r = await datasette.client.post(
        "/-/csrf-test",
        json={"value": 1},
        cookies=_cookie(datasette),
    )
    assert r.status_code == 200
    assert r.json() == {"value": 1}


@pytest.mark.asyncio
async def test_safe_method_bypasses_check(csrf_datasette):
    datasette = csrf_datasette
    r = await datasette.client.get(
        "/-/csrf-get-test",
        cookies=_cookie(datasette),
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
