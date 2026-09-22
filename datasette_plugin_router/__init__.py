from __future__ import annotations
import inspect
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, TypeVar, get_args, get_origin, Annotated
from dataclasses import dataclass

from datasette import Response
from pydantic import ValidationError


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

_SPECIAL_PARAMS = frozenset({"request", "datasette", "scope", "receive", "send"})


class _Binding(NamedTuple):
    """One step of a view's precomputed parameter binding plan.

    kind is one of "special", "body", "str_var", "int_var"; model is the
    Pydantic model class for "body" bindings and None otherwise.
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


class Router:
    """Minimal router to simplify Datasette plugin route registration and OpenAPI export."""

    def __init__(self, title: str = "API", version: str = "0.0.0", server_url: str = "http://localhost:8001") -> None:
        self._routes: List[Route] = []
        self.title = title
        self.version = version
        self.server_url = server_url

    def POST(self, path: str, *, output: Optional[type] = None):
        return self._add_route("post", path, output=output)

    def GET(self, path: str, *, output: Optional[type] = None):
        return self._add_route("get", path, output=output)

    def _add_route(self, method: str, path: str, *, output: Optional[type]):
        def decorator(fn: Callable):
            # create route entry and compute/store input/output schemas now so
            # we don't need to keep references to the original function
            entry = Route(path=path, output=output, method=method, fn=None)
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
            # (param name, reason) for required params that cannot be bound;
            # raised below, outside the try, so it is never swallowed.
            unbindable: List[Tuple[str, str]] = []
            try:
                for pname, pparam in inspect.signature(fn).parameters.items():
                    if pparam.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                        continue
                    annotation = pparam.annotation
                    if pname in path_param_names and isinstance(annotation, type):
                        param_types[pname] = annotation
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

            if input_model is not None:
                entry.input_schema = _model_to_schema(input_model) or {"type": "object"}

            # determine output schema from explicit `output` if provided
            if entry.output is not None:
                entry.output_schema = _model_to_schema(entry.output) or {"type": "object"}

            # append entry after computing schemas
            self._routes.append(entry)

            # Datasette inspects this exact signature to decide what to inject,
            # so it must not change (and must not be masked via __wrapped__).
            async def view(request, datasette=None, scope=None, receive=None, send=None):
                if signature_error is not None:
                    raise signature_error
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

                return await fn(**kwargs)

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

            operation: Dict[str, Any] = {"responses": {"200": {"description": "OK"}}, "parameters": parameters}

            # Use precomputed schemas stored on the Route entry
            if entry.input_schema is not None:
                # Extract $defs and rewrite $refs for OpenAPI 3.0 compatibility
                processed_schema = _extract_defs_from_schema(entry.input_schema, components_schemas)
                operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": processed_schema}}}

            if entry.output is not None:
                schema = _model_to_schema(entry.output) or {"type": "object"}
                # Extract $defs and rewrite $refs for OpenAPI 3.0 compatibility
                processed_schema = _extract_defs_from_schema(schema, components_schemas)
                operation["responses"]["200"]["content"] = {"application/json": {"schema": processed_schema}}

            doc["paths"].setdefault(openapi_path, {})[method] = operation

        # Add components.schemas if any $defs were extracted
        if components_schemas:
            doc["components"] = {"schemas": components_schemas}

        return doc

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
    if model is None:
        return None
    mjs = getattr(model, "model_json_schema", None)
    if callable(mjs):
        try:
            return mjs()  # type: ignore[no-any-return]
        except Exception:
            pass
    schema_fn = getattr(model, "schema", None)
    if callable(schema_fn):
        try:
            return schema_fn()  # type: ignore[no-any-return]
        except Exception:
            pass
    ann = getattr(model, "__annotations__", None)
    if isinstance(ann, dict):
        return {"type": "object", "properties": {k: {"type": "string"} for k in ann.keys()}}
    return None


def _extract_defs_from_schema(schema: Dict[str, Any], components_schemas: Dict[str, Any]) -> Dict[str, Any]:
    """Extract $defs from a schema, add them to components_schemas, and rewrite $refs.

    Pydantic's model_json_schema() generates JSON Schema 2020-12 style with $defs
    for nested model references. OpenAPI 3.0 expects schemas under #/components/schemas/.
    This function extracts $defs, moves them to components_schemas, and rewrites
    $ref values from #/$defs/ModelName to #/components/schemas/ModelName.
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
    try:
        path = regex
        if path.startswith("^"):
            path = path[1:]
        if path.endswith("$"):
            path = path[:-1]
        path = re.sub(r"\(\?P<([^>]+)>[^)]+\)", r"{\1}", path)
        return path
    except Exception:
        return regex
