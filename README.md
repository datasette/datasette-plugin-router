# datasette-plugin-router

[![PyPI](https://img.shields.io/pypi/v/datasette-plugin-router.svg)](https://pypi.org/project/datasette-plugin-router/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-plugin-router?include_prereleases&label=changelog)](https://github.com/datasette/datasette-plugin-router/releases)
[![Tests](https://github.com/datasette/datasette-plugin-router/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-plugin-router/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-plugin-router/blob/main/LICENSE)

WIP router for Datasette plugins

Datasette plugins that have a lot of [custom API endpoints](https://docs.datasette.io/en/stable/plugin_hooks.html#register-routes-datasette) can get tiresome to write by hand.  `datasette-plugin-router` aims to be a small Python library that adds a FastAPI-like API for defining custom Datasette plugin endpoints.

- Define routes with familiar GET/POST decorators
- Define Pydantic-backed input/output schemas on JSON endpoints
- `register_routes()` compatability
- export to OpenAPI schema for codegen'ing clients

## Supported Datasette versions

Requires Datasette 1.0a36 or later. That release adds a `max_post_body_bytes`
setting (default 2MB, returning a **413** on oversize requests), which caps
the `Body()` request-body reads this router does, and switches CSRF
protection to Origin/`Sec-Fetch-Site` checks so JSON `fetch()` requests work
without a token.

Sample usage:

```python
from datasette import Response, hookimpl
from datasette_plugin_router import Router, Body
from pydantic import BaseModel
from markupsafe import escape

router = Router()

class Input(BaseModel):
    id: int
    name: str

class Output(BaseModel):
    id_negative: int
    name_upper: str

@router.POST(r"/-/demo1$", output=Output)
async def demo1(params: Body[Input]) -> Output:
    output = Output(
        id_negative=-1 * params.id,
        name_upper=params.name.upper(),
    )
    return Response.json(output.model_dump())


@router.GET(r"/-/hello/(?P<name>.*)$")
async def hello(name: str):
    return Response.html(f"<h1>Hello, {escape(name)}!</h1>")


@hookimpl
def register_routes():
    return router.routes()

```

## HTTP method dispatch

Routes only answer the HTTP method they were declared with. A `HEAD` request
to a `GET` route is served by the `GET` handler; any other method gets a
**405** with an `Allow` header listing the registered methods (e.g.
`Allow: GET, HEAD, POST`) and a `{"error": "Method not allowed"}` JSON body.
Registering `@router.GET` and `@router.POST` on the same path regex is
supported: `router.routes()` returns one entry per path, which dispatches each
request to the handler for its method.

## Handler parameters

Handler parameters are bound by name and annotation when the route is
registered. String annotations (`from __future__ import annotations`) are
resolved at registration.

- `request`, `datasette`, `scope`, `receive` and `send` get the Datasette values.
- `Annotated[Model, Body()]` (or legacy `Body[Model]`) gets the validated request body.
- A `str`- or `int`-annotated parameter gets the URL var of the same name; its
  name must be a named group in the route regex (e.g. `(?P<id>\d+)`).
- Any other parameter without a default (no annotation, an unsupported type such
  as `bool`, or a `str`/`int` name missing from the regex) raises `ValueError` at
  import time, naming the parameter and route. Parameters with defaults, and
  `*args`/`**kwargs`, are left alone.
- A value that `int()` cannot parse for an `int` parameter gets a **400**
  (`{"error": "id: value is not a valid integer", "errors": [{"type": "int_parsing", ...}]}`).

## Permissions

Routes are public unless you pass `permission=` to `GET` or `POST`:

```python
@router.POST(r"^/-/things$", permission="my-plugin-access")
async def create_thing(params: Annotated[Input, Body()]):
    ...
```

Before binding any parameters the router calls
`await datasette.allowed(action="my-plugin-access", actor=request.actor)`.
The check runs before the request body is read, so a denied request gets a
403 even when its body is invalid.

A denial raises Datasette's `Forbidden`, so any `forbidden()` plugin hook on
the instance can customise the response. With Datasette's default hook:

- JSON clients (path ending in `.json`, an `Accept` header containing
  `application/json`, or a `Content-Type: application/json` request) get a
  **403** `application/json` response:
  `{"ok": false, "error": "Permission denied: my-plugin-access", "errors": ["Permission denied: my-plugin-access"], "status": 403}`
- Everything else gets a **403** HTML error page with the same message.

The action has to be registered with Datasette's
[`register_actions`](https://docs.datasette.io/en/latest/plugin_hooks.html#register-actions)
hook. `datasette.allowed()` raises `ValueError("Unknown action: ...")` for an
action name that was never registered, so the request fails with a 500
instead of a 403.

```python
from datasette.permissions import Action

@hookimpl
def register_actions(datasette):
    return [Action(name="my-plugin-access", description="Use my-plugin")]
```

Grant the action like any other, for example in `datasette.yaml`:

```yaml
permissions:
  my-plugin-access:
    id: alice
```

## CSRF and browser clients

Routes registered through the router are ordinary Datasette routes, so
Datasette's cross-origin protection applies to them. `POST` (and any other
non-safe method) is checked; `GET`, `HEAD` and `OPTIONS` are never checked, so
a mutation must not be hidden behind a `GET` route (see "HTTP method
dispatch" above).

A same-origin browser `fetch()` sending JSON just works: browsers set
`Sec-Fetch-Site: same-origin` on same-origin requests, and that alone lets
the request through — no token or extra header is needed. Legacy
`x-csrftoken` headers do nothing here and can be dropped from old frontends.

A cross-site request that carries cookies gets a **403** by design; that's
the protection working, not a bug.

For API clients: send `Authorization: Bearer <token>` **without** a `Cookie`
header — bearer auth with no cookie is exempt from the check. Non-browser
clients that send neither `Origin` nor `Sec-Fetch-Site` (curl, most HTTP
libraries) also pass through unchecked.

This describes Datasette >= 1.0a27's Origin/`Sec-Fetch-Site` based check,
which is within this package's supported range (Datasette >= 1.0a36, see
above).

## Request body validation errors

If a `Body()`-injected request body fails Pydantic validation — including an empty
body or malformed JSON — the router returns a **400** instead of a 500:

```json
{
  "error": "id: Input should be a valid integer, unable to parse string as an integer",
  "errors": [{"type": "int_parsing", "loc": ["id"], "msg": "..."}]
}
```

- `error` joins each field error as `"<loc>: <msg>"` (just `<msg>` when there is no
  location, e.g. malformed JSON) with `"; "`.
- `errors` is `ValidationError.errors()` minus the `url`/`ctx`/`input` fields.
- Messages are Pydantic's `msg` verbatim, so custom validator messages pass through.