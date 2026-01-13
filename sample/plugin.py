from datasette import Response, hookimpl
from datasette_plugin_router import Router, Body
from pydantic import BaseModel
from pathlib import Path
from typing import Annotated

router = Router()


class Input(BaseModel):
    id: int
    name: str


class Output(BaseModel):
    id_negative: int
    name_upper: str


@router.POST(r"/-/demo1$", output=Output)
async def demo1(params: Annotated[Input, Body()]):
    # params is now properly typed as Input, not Body[Input]
    # Type checkers will understand params.id is int, params.name is str
    output = Output(
        id_negative=-1 * params.id,
        name_upper=params.name.upper(),
    )
    return Response.json(output.model_dump())


@router.GET(r"/-/hello/(?P<name>.*)$")
async def hello(name: str):
    return Response.html(f"<h1>Hello, {name}!</h1>")


@hookimpl
def register_routes():
    return router.routes()


@hookimpl
def extra_body_script():
    return {
        "module": True,
        "script": Path(__file__).parent.joinpath("script.js").read_text(),
    }
