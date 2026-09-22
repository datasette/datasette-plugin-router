from __future__ import annotations
import inspect
import re
import types
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, TypeVar, Union, get_args, get_origin, Annotated
from dataclasses import dataclass

from datasette import Forbidden, Response
from pydantic import BaseModel, ValidationError, create_model


@dataclass
class Route:
    path: str
    method: str
    fn: Optional[Callable]
    output: Optional[type]
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    # Map from URL-var name -> annotation type (e.g. str, int). Used by
    # the OpenAPI emitter to type path parameters.
    path_param_types: Optional[Dict[str, type]] = None
    # Datasette action the actor must be allowed (via datasette.allowed())
    # before the handler runs; None means the route is public.
    permission: Optional[str] = None
    # Private Pydantic model with one field per Query() parameter (field
    # name = parameter name), or None when the handler has no Query() params.
    query_model: Optional[type] = None
    # (parameter name, query-string key, is_list) for each Query() param,
    # in signature order.
    query_fields: Optional[List[Tuple[str, str, bool]]] = None

T = TypeVar('T')


class Body:
    """Marker for request body parameters.

    Usage:
      from typing import Annotated
      
      async def view(params: Annotated[InputModel, Body()]):
          # params is properly typed as InputModel
          # and at runtime, Body() marker tells router to parse request body

    The recommended pattern is to use typing.Annotated for full type safety.
    For backwards compatibility, Body[Model] syntax is still supported.
    """

    def __init__(self, model: Optional[type[T]] = None):
        self.model = model

    def __repr__(self) -> str:  # helpful for debugging
        if self.model:
            try:
                name = getattr(self.model, "__name__", repr(self.model))
            except Exception:
                name = repr(self.model)
            return f"Body[{name}]"
        return "Body()"

    @classmethod
    def __class_getitem__(cls, item: type[T]) -> "Body":
        """Allow writing `Body[Model]` in annotations (backwards compatibility).

        Python will call this at import-time for subscription expressions
        (PEP 560). We return an instance of `Body` so that runtime code
        can continue to use `isinstance(param.annotation, Body)`.
        """
        return cls(item)


class Query:
    """Marker for query-string parameters.

    Usage:
      from typing import Annotated, Optional

      async def view(
          q: Annotated[str, Query()],                      # required
          limit: Annotated[int, Query()] = 20,             # default
          tag: Annotated[list[str], Query()] = [],         # ?tag=a&tag=b
          size: Annotated[int, Query(alias="_size")] = 10, # read from ?_size=
      ):

    Supported types: str, int, float, bool, Optional[...] of those, and
    list[...] of str/int/float. The parameter's Python default is the
    default; a parameter without one is required.
    """

    def __init__(self, *, alias: Optional[str] = None):
        self.alias = alias

    def __repr__(self) -> str:
        if self.alias is not None:
            return f"Query(alias={self.alias!r})"
        return "Query()"


_SPECIAL_PARAMS = frozenset({"request", "datasette", "scope", "receive", "send"})


class _Binding(NamedTuple):
    """One step of a view's precomputed parameter binding plan.

    kind is one of "special", "body", "str_var", "int_var", "query"; model
    is the Pydantic model class for "body" and "query" bindings and None
    otherwise. A single "query" binding (named after the first Query()
    param) binds every Query() parameter at once.
    """

    name: str
    kind: str
    model: Optional[type] = None


def _body_model_from_annotation(annotation: Any) -> Optional[type]:
    """Return the model for Annotated[Model, Body()] or legacy Body[Model], else None."""
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        if len(args) >= 2:
            # args[0] is the actual type, args[1:] are metadata
            for metadata in args[1:]:
                if isinstance(metadata, Body):
                    return args[0]
        return None
    if isinstance(annotation, Body):
        return annotation.model
    return None


def _query_marker_from_annotation(annotation: Any) -> Tuple[Optional[Query], Any]:
    """Return (Query marker, inner type) for Annotated[T, Query()], else (None, None)."""
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        for metadata in args[1:]:
            if isinstance(metadata, Query):
                return metadata, args[0]
    return None, None


def _has_body_marker(annotation: Any) -> bool:
    if isinstance(annotation, Body):
        return True
    if get_origin(annotation) is Annotated:
        return any(isinstance(m, Body) for m in get_args(annotation)[1:])
    return False


_QUERY_SCALARS = (str, int, float, bool)
_QUERY_LIST_ITEMS = (str, int, float)


def _query_type_is_list(inner: Any) -> Optional[bool]:
    """For a supported Query() inner type return whether it is a list, else None."""
    if inner in _QUERY_SCALARS:
        return False
    origin = get_origin(inner)
    if origin in (Union, types.UnionType):
        members = get_args(inner)
        non_none = [a for a in members if a is not type(None)]
        if len(members) == 2 and len(non_none) == 1 and non_none[0] in _QUERY_SCALARS:
            return False
        return None
    if origin is list:
        args = get_args(inner)
        if len(args) == 1 and args[0] in _QUERY_LIST_ITEMS:
            return True
    return None


def _json_model_response(model: BaseModel) -> Response:
    # model_dump_json() so pydantic serialises datetimes, UUIDs, etc.
    return Response(
        model.model_dump_json(),
        status=200,
        content_type="application/json; charset=utf-8",
    )


def _output_mismatch(fn: Callable, entry: Route, detail: str) -> TypeError:
    name = getattr(fn, "__qualname__", repr(fn))
    return TypeError(
        f"Handler {name} for {entry.method.upper()} {entry.path} returned a value "
        f"that does not match its declared output={entry.output.__name__}: {detail}"  # type: ignore[union-attr]
    )


def _serialize_result(result: Any, output_model: Optional[type], fn: Callable, entry: Route) -> Any:
    """Turn a handler's return value into a response.

    A BaseModel instance or dict becomes a JSON response (checked against
    output_model when set); anything else, e.g. a Response, is returned as-is.
    A mismatch with output_model is a server bug, so it raises (500) rather
    than returning a 400.
    """
    if isinstance(result, BaseModel):
        if output_model is not None and not isinstance(result, output_model):
            raise _output_mismatch(
                fn, entry, f"got a {type(result).__name__} instance"
            )
        return _json_model_response(result)
    if isinstance(result, dict):
        if output_model is None:
            return Response.json(result)
        try:
            model = output_model.model_validate(result)  # type: ignore[attr-defined]
        except ValidationError as exc:
            raise _output_mismatch(fn, entry, str(exc)) from exc
        return _json_model_response(model)
    return result


class Router:
    """Minimal router to simplify Datasette plugin route registration and OpenAPI export."""

    def __init__(self, title: str = "API", version: str = "0.0.0", server_url: str = "http://localhost:8001") -> None:
        self._routes: List[Route] = []
        self.title = title
        self.version = version
        self.server_url = server_url

    def POST(self, path: str, *, output: Optional[type] = None, permission: Optional[str] = None):
        return self._add_route("post", path, output=output, permission=permission)

    def GET(self, path: str, *, output: Optional[type] = None, permission: Optional[str] = None):
        return self._add_route("get", path, output=output, permission=permission)

    def PUT(self, path: str, *, output: Optional[type] = None, permission: Optional[str] = None):
        return self._add_route("put", path, output=output, permission=permission)

    def DELETE(self, path: str, *, output: Optional[type] = None, permission: Optional[str] = None):
        return self._add_route("delete", path, output=output, permission=permission)

    def PATCH(self, path: str, *, output: Optional[type] = None, permission: Optional[str] = None):
        return self._add_route("patch", path, output=output, permission=permission)

    def _add_route(self, method: str, path: str, *, output: Optional[type], permission: Optional[str] = None):
        def decorator(fn: Callable):
            # create route entry and compute/store input/output schemas now so
            # we don't need to keep references to the original function
            entry = Route(path=path, output=output, method=method, fn=None, permission=permission)
            # Walk the handler signature once, at decoration time, producing
            # both the OpenAPI path-param types and the per-request binding
            # plan. The view wrapper below only iterates the plan, so no
            # signature/typing introspection happens on the request path.
            path_param_names = set(_extract_named_groups(path))
            param_types: Dict[str, type] = {}
            plan: List[_Binding] = []
            input_model = None
            input_model_found = False
            signature_error: Optional[Exception] = None
            # (param name, reason) for required params that cannot be bound
            # (and for invalid Query() params, with or without a default);
            # raised below, outside the try, so it is never swallowed.
            unbindable: List[Tuple[str, str]] = []
            # (param name, query key, is_list, inner type, default) per Query() param.
            query_params: List[Tuple[str, str, bool, Any, Any]] = []
            try:
                # Resolve string annotations (from __future__ import annotations);
                # fall back to raw ones if a forward reference can't be evaluated.
                try:
                    signature = inspect.signature(fn, eval_str=True)
                except Exception:
                    signature = inspect.signature(fn)
                for pname, pparam in signature.parameters.items():
                    if pparam.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                        continue
                    annotation = pparam.annotation
                    if pname in path_param_names and isinstance(annotation, type):
                        param_types[pname] = annotation
                    query_marker, query_inner = _query_marker_from_annotation(annotation)
                    if query_marker is not None:
                        if _has_body_marker(annotation):
                            unbindable.append((pname, "is annotated with both Body() and Query()"))
                            continue
                        is_list = _query_type_is_list(query_inner)
                        if is_list is None:
                            unbindable.append((pname, _unbindable_reason(pname, annotation)))
                            continue
                        query_params.append(
                            (pname, query_marker.alias or pname, is_list, query_inner, pparam.default)
                        )
                        continue
                    body_model = _body_model_from_annotation(annotation)
                    # The first Body parameter drives the OpenAPI requestBody
                    # (a bare legacy Body() annotation also ends the search).
                    if not input_model_found and (body_model or isinstance(annotation, Body)):
                        input_model = body_model
                        input_model_found = True
                    if pname in _SPECIAL_PARAMS:
                        plan.append(_Binding(pname, "special"))
                    elif body_model is not None:
                        plan.append(_Binding(pname, "body", body_model))
                    elif annotation is str and pname in path_param_names:
                        plan.append(_Binding(pname, "str_var"))
                    elif annotation is int and pname in path_param_names:
                        plan.append(_Binding(pname, "int_var"))
                    elif pparam.default is inspect.Parameter.empty:
                        unbindable.append((pname, _unbindable_reason(pname, annotation)))
                    # Unbindable params with a default are left to that default.
            except Exception as exc:
                # Still register the route; surface the error when it is called,
                # as the old per-request inspect.signature() call would have.
                signature_error = exc
            if unbindable:
                pname, reason = unbindable[0]
                handler = getattr(fn, "__qualname__", repr(fn))
                raise ValueError(f"Parameter {pname!r} of handler {handler} for route {path!r} {reason}")
            entry.path_param_types = param_types

            if query_params:
                fields = {
                    pname: (inner, ... if default is inspect.Parameter.empty else default)
                    for pname, _key, _is_list, inner, default in query_params
                }
                try:
                    query_model = create_model(f"{getattr(fn, '__name__', 'handler')}Query", **fields)
                except Exception as exc:
                    handler = getattr(fn, "__qualname__", repr(fn))
                    raise ValueError(
                        f"Query() parameters of handler {handler} for route {path!r} are invalid: {exc}"
                    ) from exc
                # Pydantic turns underscore-prefixed names into private
                # attributes, which would silently never be bound.
                for pname, *_rest in query_params:
                    if pname not in query_model.model_fields:
                        handler = getattr(fn, "__qualname__", repr(fn))
                        raise ValueError(
                            f"Parameter {pname!r} of handler {handler} for route {path!r} cannot be a "
                            "Query() parameter name (names starting with an underscore are not "
                            f"supported; use a plain name with Query(alias={pname!r}))"
                        )
                entry.query_model = query_model
                entry.query_fields = [(pname, key, is_list) for pname, key, is_list, _i, _d in query_params]
                # One step binds every Query() param; it runs before any body
                # step so a bad query string never reads the request body.
                query_binding = _Binding(query_params[0][0], "query", query_model)
                body_index = next((i for i, b in enumerate(plan) if b.kind == "body"), len(plan))
                plan.insert(body_index, query_binding)

            if input_model is not None:
                entry.input_schema = _model_to_schema(input_model) or {"type": "object"}

            # determine output schema from explicit `output` if provided
            if entry.output is not None:
                entry.output_schema = _model_to_schema(entry.output) or {"type": "object"}

            # append entry after computing schemas
            self._routes.append(entry)

            # Returned dicts/models are only checked against output= when it
            # is a pydantic model; other classes (tolerated by the OpenAPI
            # emitter) skip validation.
            output_model = (
                output if isinstance(output, type) and issubclass(output, BaseModel) else None
            )

            # Datasette inspects this exact signature to decide what to inject,
            # so it must not change (and must not be masked via __wrapped__).
            async def view(request, datasette=None, scope=None, receive=None, send=None):
                if signature_error is not None:
                    raise signature_error
                # Check before binding so a denied request never reads or
                # validates the body. Forbidden goes through Datasette's
                # forbidden() hook, so instance-wide 403 customisation applies.
                if entry.permission is not None:
                    if datasette is None:
                        raise RuntimeError("permission= requires Datasette to inject `datasette`")
                    if not await datasette.allowed(action=entry.permission, actor=request.actor):
                        raise Forbidden(f"Permission denied: {entry.permission}")
                specials = {
                    "request": request,
                    "datasette": datasette,
                    "scope": scope,
                    "receive": receive,
                    "send": send,
                }
                kwargs = {}
                for binding in plan:
                    name = binding.name
                    kind = binding.kind
                    if kind == "special":
                        kwargs[name] = specials[name]
                    elif kind == "query":
                        args = request.args
                        query_data: Dict[str, Any] = {}
                        for pname, key, is_list in entry.query_fields:  # type: ignore[union-attr]
                            if key in args:
                                query_data[pname] = args.getlist(key) if is_list else args.get(key)
                        try:
                            query_instance = binding.model.model_validate(query_data)  # type: ignore[union-attr]
                        except ValidationError as exc:
                            return _validation_error_response(exc)
                        for pname, _key, _is_list in entry.query_fields:  # type: ignore[union-attr]
                            kwargs[pname] = getattr(query_instance, pname)
                    elif kind == "body":
                        data = await request.post_body()
                        try:
                            model_instance = binding.model.model_validate_json(data)  # type: ignore[union-attr]
                        except ValidationError as exc:
                            return _validation_error_response(exc)
                        kwargs[name] = model_instance
                    elif kind == "str_var":
                        kwargs[name] = request.url_vars[name]
                    elif kind == "int_var":
                        # int() accepts "1_0" and " 5 "; that is tolerated.
                        try:
                            kwargs[name] = int(request.url_vars[name])
                        except ValueError:
                            return _int_parsing_error_response(name)

                result = await fn(**kwargs)
                return _serialize_result(result, output_model, fn, entry)

            # Carry the handler's identity for tracing/debugging. Deliberately
            # NOT functools.wraps: that sets __wrapped__, which makes
            # inspect.signature(view) report fn's signature and breaks
            # Datasette's argument injection.
            for attr in ("__module__", "__name__", "__qualname__", "__doc__"):
                try:
                    setattr(view, attr, getattr(fn, attr))
                except AttributeError:
                    pass

            # replace the stored fn with the wrapper that Datasette should call
            entry.fn = view
            return view

        return decorator

    def routes(self) -> List[Tuple[str, Callable]]:
        """Return a list of (regex, view_fn) tuples suitable for Datasette's register_routes.

        Datasette dispatches on the path regex only, so routes are grouped by
        regex and each unique path gets one dispatcher that enforces the
        declared HTTP method(s), answering anything else with a 405.
        """
        # Preserve first-seen path order; within a path, a later registration
        # of the same method replaces an earlier one (as the OpenAPI emitter does).
        by_path: Dict[str, Dict[str, Callable]] = {}
        for entry in self._routes:
            if entry.fn is not None:
                by_path.setdefault(entry.path, {})[entry.method.upper()] = entry.fn
        return [(path, _make_dispatcher(views)) for path, views in by_path.items()]

    def openapi_document_json(self) -> Dict[str, Any]:
        """Return a minimal OpenAPI 3 document as a Python dict."""
        components_schemas: Dict[str, Any] = {}

        doc: Dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": self.title, "version": self.version},
            "servers": [{"url": self.server_url}],
            "paths": {},
        }

        # (entry, operation) pairs in registration order, for operationIds.
        emitted: List[Tuple[Route, Dict[str, Any]]] = []
        uses_validation_error = False

        for entry in self._routes:
            path = entry.path
            openapi_path = _regex_to_openapi_path(path)
            method = entry.method.lower()

            parameters: List[Dict[str, Any]] = []
            param_types = entry.path_param_types or {}
            for name in _extract_named_groups(path):
                ann = param_types.get(name, str)
                # bool is an int subclass; we don't support bool path params,
                # so fall back to "string" for anything we don't recognize.
                if ann is int:
                    schema_type = "integer"
                else:
                    schema_type = "string"
                parameters.append({"name": name, "in": "path", "required": True, "schema": {"type": schema_type}})

            if entry.query_model is not None and entry.query_fields:
                query_schema = entry.query_model.model_json_schema()
                model_fields = entry.query_model.model_fields
                for pname, key, is_list in entry.query_fields:
                    prop = dict(query_schema.get("properties", {}).get(pname, {}))
                    prop.pop("title", None)
                    if "$ref" in repr(prop):
                        if "$defs" in query_schema:
                            prop["$defs"] = query_schema["$defs"]
                        prop = _extract_defs_from_schema(prop, components_schemas)
                    param: Dict[str, Any] = {
                        "name": key,
                        "in": "query",
                        "required": model_fields[pname].is_required(),
                        "schema": prop,
                    }
                    if is_list:
                        param["style"] = "form"
                        param["explode"] = True
                    parameters.append(param)

            operation: Dict[str, Any] = {"responses": {"200": {"description": "OK"}}, "parameters": parameters}
            where = f"{method.upper()} {path}"

            # Use precomputed schemas stored on the Route entry
            if entry.input_schema is not None:
                # Extract $defs and rewrite $refs for OpenAPI 3.0 compatibility
                processed_schema = _extract_defs_from_schema(entry.input_schema, components_schemas, where)
                operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": processed_schema}}}

            if entry.output_schema is not None:
                # Extract $defs and rewrite $refs for OpenAPI 3.0 compatibility
                processed_schema = _extract_defs_from_schema(entry.output_schema, components_schemas, where)
                operation["responses"]["200"]["content"] = {"application/json": {"schema": processed_schema}}

            # The router answers bad bodies, query strings and int url vars
            # with a 400 {"error", "errors"}; permission denials with a 403.
            can_400 = (
                entry.input_schema is not None
                or any(p["in"] == "query" for p in parameters)
                or any(p["in"] == "path" and p["schema"]["type"] == "integer" for p in parameters)
            )
            if can_400:
                uses_validation_error = True
                operation["responses"]["400"] = {
                    "description": "Validation error",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ValidationError"}}},
                }
            if entry.permission is not None:
                operation["responses"]["403"] = {"description": "Forbidden"}

            doc["paths"].setdefault(openapi_path, {})[method] = operation
            emitted.append((entry, operation))

        # Assign operationIds in registration order, skipping operations a
        # later registration of the same path+method replaced. They go first
        # in each operation; replacing an existing dict key keeps its position.
        taken: set = set()
        for entry, operation in emitted:
            openapi_path = _regex_to_openapi_path(entry.path)
            method = entry.method.lower()
            if doc["paths"][openapi_path][method] is not operation:
                continue
            base = getattr(entry.fn, "__name__", None) or "handler"
            operation_id = base
            if operation_id in taken:
                operation_id = f"{base}_{method}"
                n = 2
                while operation_id in taken:
                    operation_id = f"{base}_{method}_{n}"
                    n += 1
            taken.add(operation_id)
            head: Dict[str, Any] = {"operationId": operation_id}
            doc_text = inspect.cleandoc(getattr(entry.fn, "__doc__", None) or "")
            if doc_text:
                summary, _, rest = doc_text.partition("\n")
                head["summary"] = summary.strip()
                description = rest.strip()
                if description:
                    head["description"] = description
            doc["paths"][openapi_path][method] = {**head, **operation}

        if uses_validation_error:
            if "ValidationError" in components_schemas:
                raise ValueError(
                    "OpenAPI components.schemas name collision: a route's model is named "
                    "'ValidationError', which is reserved for the router's 400 error schema"
                )
            components_schemas["ValidationError"] = dict(_VALIDATION_ERROR_SCHEMA)

        # Add components.schemas if any $defs were extracted
        if components_schemas:
            doc["components"] = {"schemas": components_schemas}

        return doc


# Shape of the router's 400 responses (see _validation_error_response).
_VALIDATION_ERROR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["error", "errors"],
}


def _make_dispatcher(views: Dict[str, Callable]) -> Callable:
    """Build one view that routes a request to views[METHOD], else returns 405."""
    handlers = dict(views)
    # HEAD is served by the GET handler; the ASGI server drops the body.
    if "GET" in handlers and "HEAD" not in handlers:
        handlers["HEAD"] = handlers["GET"]
    allow = ", ".join(sorted(handlers))

    # Same exact signature as the per-route view: Datasette inspects it to
    # decide what to inject (and it must not be masked via __wrapped__).
    async def dispatch(request, datasette=None, scope=None, receive=None, send=None):
        view = handlers.get(request.method.upper())
        if view is None:
            return Response.json(
                {"error": "Method not allowed"},
                status=405,
                headers={"Allow": allow},
            )
        return await view(request, datasette=datasette, scope=scope, receive=receive, send=send)

    # Carry handler identity for tracing, manually (no functools.wraps).
    registered = list(views.values())
    if len(registered) == 1:
        for attr in ("__module__", "__name__", "__qualname__", "__doc__"):
            try:
                setattr(dispatch, attr, getattr(registered[0], attr))
            except AttributeError:
                pass
    else:
        name = "_or_".join(getattr(v, "__name__", "view") for v in registered)
        dispatch.__name__ = name
        dispatch.__qualname__ = name
        dispatch.__doc__ = None
    return dispatch


def _validation_error_response(exc: ValidationError) -> Response:
    """Turn a Pydantic ValidationError into a 400 {"error": ..., "errors": [...]} response."""
    # ctx can hold a non-serializable ValueError, so drop it (and url/input noise).
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    parts: List[str] = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "Invalid input")
        parts.append(f"{loc}: {msg}" if loc else msg)
    message = "; ".join(parts) if parts else "Invalid request body"
    return Response.json({"error": message, "errors": errors}, status=400)


def _int_parsing_error_response(name: str) -> Response:
    """400 in the same shape as _validation_error_response, for a bad int url var."""
    msg = "value is not a valid integer"
    return Response.json(
        {"error": f"{name}: {msg}", "errors": [{"type": "int_parsing", "loc": [name], "msg": msg}]},
        status=400,
    )


def _unbindable_reason(name: str, annotation: Any) -> str:
    """Explain why a required handler parameter cannot be bound (for the ValueError)."""
    query_marker, query_inner = _query_marker_from_annotation(annotation)
    if query_marker is not None:
        return (
            f"is annotated Query() with unsupported type {query_inner!r} (expected str, int, "
            "float, bool, Optional[...] of those, or list[...] of str/int/float)"
        )
    if annotation is str or annotation is int:
        return (
            f"is annotated {annotation.__name__} but {name!r} is not a named group in the route regex"
        )
    if isinstance(annotation, Body):
        return "is annotated with a bare Body() that has no model to validate against"
    if annotation is bool:
        return (
            "is annotated bool, which the router cannot bind (bool url vars are not supported; "
            "expected str/int url var, Body(), or request/datasette/scope/receive/send)"
        )
    return (
        "has no annotation the router can bind (expected str/int url var, Body(), "
        "or request/datasette/scope/receive/send)"
    )


def _model_to_schema(model: type) -> Optional[Dict[str, Any]]:
    """JSON schema for a model class.

    Errors from model_json_schema()/schema() propagate (at registration) so a
    broken model never ships a silently wrong spec. Classes with neither get a
    string-typed object built from __annotations__.
    """
    if model is None:
        return None
    mjs = getattr(model, "model_json_schema", None)
    if callable(mjs):
        return mjs()  # type: ignore[no-any-return]
    schema_fn = getattr(model, "schema", None)
    if callable(schema_fn):
        return schema_fn()  # type: ignore[no-any-return]
    ann = getattr(model, "__annotations__", None)
    if isinstance(ann, dict):
        return {"type": "object", "properties": {k: {"type": "string"} for k in ann.keys()}}
    return None


def _extract_defs_from_schema(
    schema: Dict[str, Any], components_schemas: Dict[str, Any], where: Optional[str] = None
) -> Dict[str, Any]:
    """Extract $defs from a schema, add them to components_schemas, and rewrite $refs.

    Pydantic's model_json_schema() generates JSON Schema 2020-12 style with $defs
    for nested model references. OpenAPI 3.0 expects schemas under #/components/schemas/.
    This function extracts $defs, moves them to components_schemas, and rewrites
    $ref values from #/$defs/ModelName to #/components/schemas/ModelName.

    A $defs name already in components_schemas with a different definition
    (two distinct classes sharing a name) raises ValueError; `where` names the
    operation being processed in that message.
    """
    if not isinstance(schema, dict):
        return schema

    # Make a copy to avoid mutating the original
    schema = dict(schema)

    # Extract $defs and add to components_schemas
    if "$defs" in schema:
        defs = schema.pop("$defs")
        for name, definition in defs.items():
            # Recursively process nested $defs in definitions
            processed_def = _rewrite_refs(definition)
            existing = components_schemas.get(name)
            if existing is not None and existing != processed_def:
                context = f" (while processing {where})" if where else ""
                raise ValueError(
                    "OpenAPI components.schemas name collision: two different models are "
                    f"named {name!r}{context}; rename one of them"
                )
            components_schemas[name] = processed_def

    # Rewrite $refs in the schema
    return _rewrite_refs(schema)


def _rewrite_refs(obj: Any) -> Any:
    """Recursively rewrite $ref values from #/$defs/X to #/components/schemas/X."""
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                # Rewrite the ref to point to components/schemas
                model_name = value[len("#/$defs/"):]
                result[key] = f"#/components/schemas/{model_name}"
            else:
                result[key] = _rewrite_refs(value)
        return result
    elif isinstance(obj, list):
        return [_rewrite_refs(item) for item in obj]
    else:
        return obj


def _extract_named_groups(regex: str) -> List[str]:
    pattern = re.compile(regex)
    return list(pattern.groupindex.keys())

def _regex_to_openapi_path(regex: str) -> str:
    """Convert a route regex to an OpenAPI path template.

    Each top-level named group (?P<name>...) becomes {name}; backslash-escaped
    literals outside groups are unescaped (\\. -> .). Everything else,
    including unnamed or non-capturing groups, is copied through verbatim.
    """
    path = regex
    if path.startswith("^"):
        path = path[1:]
    if path.endswith("$") and not path.endswith("\\$"):
        path = path[:-1]
    out: List[str] = []
    i = 0
    n = len(path)
    while i < n:
        c = path[i]
        if c == "\\" and i + 1 < n:
            out.append(path[i + 1])
            i += 2
        elif path.startswith("(?P<", i) and ">" in path[i:]:
            close = path.index(">", i)
            out.append("{" + path[i + 4 : close] + "}")
            i = _skip_group(path, close + 1)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _skip_group(regex: str, i: int) -> int:
    """Return the index just past the ')' closing the group whose body starts at i.

    Nested groups are counted; parens inside [...] or escaped are ignored.
    """
    depth = 1
    in_class = False
    n = len(regex)
    while i < n:
        c = regex[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            # A ']' right after '[' or '[^' is a literal, not the class end.
            if regex.startswith("^", i + 1):
                i += 1
            if regex.startswith("]", i + 1):
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n
