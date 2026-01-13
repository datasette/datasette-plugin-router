from __future__ import annotations
import inspect
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, get_args, get_origin, Annotated
from dataclasses import dataclass


@dataclass
class Route:
    path: str
    method: str
    fn: Optional[Callable]
    output: Optional[type]
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None

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
            input_model = None
            # inspect the handler's annotations for Body[...] parameters or Annotated[..., Body()]
            try:
                for _, param in inspect.signature(fn).parameters.items():
                    # Check for Annotated[Model, Body()] pattern
                    if get_origin(param.annotation) is Annotated:
                        args = get_args(param.annotation)
                        if len(args) >= 2:
                            # args[0] is the actual type, args[1:] are metadata
                            for metadata in args[1:]:
                                if isinstance(metadata, Body):
                                    input_model = args[0]
                                    break
                        if input_model:
                            break
                    # Check for backwards-compatible Body[Model] pattern
                    elif isinstance(param.annotation, Body):
                        input_model = param.annotation.model
                        break
            except Exception:
                input_model = None

            if input_model is not None:
                entry.input_schema = _model_to_schema(input_model) or {"type": "object"}

            # determine output schema from explicit `output` if provided
            if entry.output is not None:
                entry.output_schema = _model_to_schema(entry.output) or {"type": "object"}

            # append entry after computing schemas
            self._routes.append(entry)

            async def view(request, datasette=None, scope=None, receive=None, send=None):
                declared_kwargs = inspect.signature(fn).parameters
                kwargs = {}
                for name, param in declared_kwargs.items():
                    if name == "request":
                        kwargs["request"] = request
                        continue
                    elif name == "datasette":
                        kwargs["datasette"] = datasette
                        continue
                    elif name == "scope":
                        kwargs["scope"] = scope
                        continue
                    elif name == "receive":
                        kwargs["receive"] = receive
                        continue
                    elif name == "send":
                        kwargs["send"] = send
                        continue
                    
                    # Check for Annotated[Model, Body()] pattern
                    body_model = None
                    if get_origin(param.annotation) is Annotated:
                        args = get_args(param.annotation)
                        if len(args) >= 2:
                            for metadata in args[1:]:
                                if isinstance(metadata, Body):
                                    body_model = args[0]
                                    break
                    # Check for backwards-compatible Body[Model] pattern
                    elif isinstance(param.annotation, Body):
                        body_model = param.annotation.model
                    
                    if body_model is not None:
                        data = await request.post_body()
                        model_instance = body_model.model_validate_json(data)  # type: ignore[attr-defined]
                        kwargs[name] = model_instance
                        continue
                    
                    # see if the str parameter exists in `request.url_vars`.
                    if param.annotation is str:
                        kwargs[name] = request.url_vars[name]
                        continue

                return await fn(**kwargs)

            # replace the stored fn with the wrapper that Datasette should call
            entry.fn = view
            return view

        return decorator

    def routes(self) -> List[Tuple[str, Callable]]:
        """Return a list of (regex, view_fn) tuples suitable for Datasette's register_routes."""
        out: List[Tuple[str, Callable]] = []
        for entry in self._routes:
            if entry.fn is not None:
                out.append((entry.path, entry.fn))
        return out

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
            for name in _extract_named_groups(path):
                parameters.append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})

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
