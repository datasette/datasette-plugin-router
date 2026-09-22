# datasette-plugin-router

[![PyPI](https://img.shields.io/pypi/v/datasette-plugin-router.svg)](https://pypi.org/project/datasette-plugin-router/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-plugin-router?include_prereleases&label=changelog)](https://github.com/datasette/datasette-plugin-router/releases)
[![Tests](https://github.com/datasette/datasette-plugin-router/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-plugin-router/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-plugin-router/blob/main/LICENSE)

WIP router for Datasette plugins

Datasette plugins that have a lot of [custom API endpoints](https://docs.datasette.io/en/stable/plugin_hooks.html#register-routes-datasette) can get tiresome to write by hand.  `datasette-plugin-router` aims to be a small Python library that adds a FastAPI-like API for defining custom Datasette plugin endpoints.

- Define routes with familiar `GET`, `POST`, `PUT`, `PATCH` and `DELETE` decorators
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
    return Output(
        id_negative=-1 * params.id,
        name_upper=params.name.upper(),
    )


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

Five decorators are available: `@router.GET`, `@router.POST`, `@router.PUT`,
`@router.PATCH` and `@router.DELETE`, all with the same
`(path, *, output=None, permission=None)` signature. For example:

```python
@router.DELETE(r"^/-/things/(?P<id>\d+)$")
async def delete_thing(id: int):
    return Response.json({"deleted": id})
```

## Handler parameters

Handler parameters are bound by name and annotation when the route is
registered. String annotations (`from __future__ import annotations`) are
resolved at registration.

- `request`, `datasette`, `scope`, `receive` and `send` get the Datasette values.
- `Annotated[Model, Body()]` (or legacy `Body[Model]`) gets the validated request body.
- `Annotated[Model, Form()]` gets a validated form-encoded body (see "Form bodies" below).
- `Annotated[T, Query()]` gets a typed query-string value (see "Query parameters" below).
- A parameter annotated `str`, `int`, `float`, `uuid.UUID` or `datetime.date`
  gets the URL var of the same name, converted to that type; its name must be
  a named group in the route regex (e.g. `(?P<id>\d+)`). A `date` is parsed
  with `date.fromisoformat`, i.e. `YYYY-MM-DD`.
- Any other parameter without a default (no annotation, an unsupported type such
  as `bool`, or a name missing from the regex) raises `ValueError` at
  import time, naming the parameter and route. Parameters with defaults, and
  `*args`/`**kwargs`, are left alone.
- A value that fails to convert for a typed path parameter gets a **400** in
  the same `{"error", "errors"}` shape, with an `errors[0]["type"]` of
  `int_parsing`, `float_parsing`, `uuid_parsing` or `date_parsing`
  (`{"error": "id: value is not a valid integer", "errors": [{"type": "int_parsing", ...}]}`).

## Query parameters

Annotate a parameter with `Query()` to bind it from the query string:

```python
from typing import Annotated, Optional
from datasette_plugin_router import Router, Query

@router.GET(r"^/-/search$")
async def search(
    q: Annotated[str, Query()],                       # required
    limit: Annotated[int, Query()] = 20,              # optional with default
    lat: Annotated[Optional[float], Query()] = None,  # optional
    verbose: Annotated[bool, Query()] = False,
    tag: Annotated[list[str], Query()] = [],          # multi-value ?tag=a&tag=b
    size: Annotated[int, Query(alias="_size")] = 10,  # read from ?_size=
):
    ...
```

- Supported types: `str`, `int`, `float`, `bool`, `Optional[...]` of those,
  and `list[...]` / `List[...]` of `str`, `int` or `float`. Any other type, or
  a parameter marked with both `Body()` and `Query()`, raises `ValueError` at
  import time.
- A parameter without a default is required; the Python default is used when
  the key is absent from the query string.
- Scalars use the first value if the key is repeated; `list[...]` parameters
  collect every value (`?tag=a&tag=b` gives `["a", "b"]`, absent gives the
  default).
- `Query(alias="_size")` reads `?_size=` instead of the parameter name.
- Values are coerced with Pydantic's lax mode, so `bool` accepts Pydantic's
  usual spellings (`true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`, ...).
- A missing required value or one that fails coercion gets a **400** in the
  same shape as request body errors (see "Request body validation errors"
  below), with `loc` set to the parameter name, e.g.
  `{"error": "limit: Input should be a valid integer, ...", "errors": [{"type": "int_parsing", "loc": ["limit"], ...}]}`.
  Query parameters are validated before the request body is read.
- Query parameters appear in the OpenAPI document as `in: query` parameters
  (named by their alias, if any) with `required`, a JSON schema `type` and any
  `default`, after the route's path parameters.

## Returning models

A handler can return a Pydantic model (or a `dict`) instead of building a
`Response` itself:

```python
@router.GET(r"^/-/things/(?P<id>\d+)$", output=Output)
async def get_thing(id: int) -> Output:
    return Output(id_negative=-id, name_upper="THING")
```

The router turns the return value into a response:

- A `Response` (or anything that is not a `BaseModel` or `dict`) is returned
  untouched, whether or not `output=` is set.
- A `BaseModel` instance becomes a **200** `application/json` response,
  serialised with `model_dump_json()` (so `datetime`, `UUID` etc. become
  JSON strings). If `output=` is set, the instance must be an instance of
  that class; it is not re-validated.
- A `dict` is validated with `output.model_validate()` when `output=` is set,
  then serialised the same way; with no `output=` it is sent as JSON as-is.
- `output=` validation only applies when it is a Pydantic `BaseModel`
  subclass; other classes are only used for the OpenAPI document.

Existing handlers that `return Response.json(...)` are unaffected: validation
only happens when a handler returns a dict or a model. A returned dict or
model that does not match `output=` is a bug in the handler, so it raises a
`TypeError` (naming the handler, the route and the Pydantic errors) and the
client gets a **500**, not a 400.

## Form bodies

`Body()` parses JSON. To accept a plain HTML `<form method="post">`
submission instead, annotate the parameter with `Form()`:

```python
from typing import Annotated
from pydantic import BaseModel
from datasette_plugin_router import Router, Form

class Signup(BaseModel):
    name: str
    count: int = 1
    tags: list[str] = []

@router.POST(r"^/-/signup$")
async def signup(params: Annotated[Signup, Form()]):
    ...
```

- The body may be `application/x-www-form-urlencoded` or
  `multipart/form-data`; it is read with Datasette's `request.form()`.
- Each model field is read from the form field of the same name (or its
  alias). Scalars take the first value; `list[...]` / `List[...]` fields
  collect every value of a repeated key (`tags=a&tags=b` gives
  `["a", "b"]`). An absent key falls back to the model default.
- Values are validated with `model_validate()`, so `"5"` coerces to an
  `int` field. A missing or invalid field gets the usual **400** (see
  "Request body validation errors" below), e.g.
  `{"errors": [{"type": "missing", "loc": ["name"], ...}]}`.
- A request with any other content type (e.g. a JSON body) or none gets a
  **400**:
  `{"error": "body: expected application/x-www-form-urlencoded or multipart/form-data", "errors": [{"type": "form_parsing", "loc": ["body"], "msg": "..."}]}`.
- A route may use `Body()` or `Form()`, not both; mixing them (on one
  parameter or across parameters) raises `ValueError` at import time.
- File uploads are out of scope: `request.form()` discards file parts by
  default, so a file field is never bound. Take `request` and call
  `await request.form(files=True)` yourself to handle uploads.
- In the OpenAPI document the route's `requestBody` is keyed
  `application/x-www-form-urlencoded` instead of `application/json`.

Datasette's CSRF check is origin-based, so a form post needs no hidden
`csrftoken` field (see "CSRF and browser clients" below).

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

## OpenAPI export

`router.openapi_document_json()` returns an OpenAPI 3.0 document as a dict.

- Each operation's `operationId` is the handler's function name. Duplicates get
  `_<method>` appended (`index`, then `index_post`), then `_2`, `_3`, ... in
  registration order.
- The handler docstring's first line becomes `summary`; the rest, if any,
  becomes `description`.
- Operations with a `Body()`, a `Query()` parameter or an `int` path parameter
  declare a **400** response (`#/components/schemas/ValidationError`, the
  `{"error", "errors"}` shape above); routes with `permission=` declare a **403**.
- Two different nested models with the same class name raise `ValueError`
  instead of silently sharing one `components.schemas` entry.
- Adding `operationId`s changes the method names that client generators such
  as `@hey-api/openapi-ts` produce, so regenerate clients once after upgrading.