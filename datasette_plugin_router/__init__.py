from __future__ import annotations
import inspect
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class Route:
    path: str
    method: str
    fn: Optional[Callable]
    output: Optional[type]
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None

T = None


class Body:
    """Marker for request body parameters.

    Usage:
      async def view(params: Body[InputModel]):
          # params is validated instance of InputModel

    At runtime `Body[InputModel]` returns an instance of `Body` which
    stores the model in `model`. This makes it friendly for static
    checkers that forbid call expressions in type contexts (e.g.
    `Body(Input)`), while keeping the existing runtime handling that
    uses `isinstance(annotation, Body)`.
    """

    def __init__(self, model: type[Any]):
        self.model = model

    def __repr__(self) -> str:  # helpful for debugging
        try:
            name = getattr(self.model, "__name__", repr(self.model))
        except Exception:
            name = repr(self.model)
        return f"Body[{name}]"

    @classmethod
    def __class_getitem__(cls, item: Any) -> "Body":
        """Allow writing `Body[Model]` in annotations.

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
            # inspect the handler's annotations for Body[...] parameters
            try:
                for _, param in inspect.signature(fn).parameters.items():
                    if isinstance(param.annotation, Body):
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
                    
                    if isinstance(param.annotation, Body):
                        data = await request.post_body()
                        model_instance = param.annotation.model.model_validate_json(data)
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
            out.append((entry.path, entry.fn))
        return out

    def openapi_document_json(self) -> Dict[str, Any]:
        """Return a minimal OpenAPI 3 document as a Python dict."""
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
                operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": entry.input_schema}}}

            if entry.output is not None:
                schema = _model_to_schema(entry.output) or {"type": "object"}
                operation["responses"]["200"]["content"] = {"application/json": {"schema": schema}}

            doc["paths"].setdefault(openapi_path, {})[method] = operation

        return doc

def _model_to_schema(model: type) -> Optional[Dict[str, Any]]:
    if model is None:
        return None
    mjs = getattr(model, "model_json_schema", None)
    if callable(mjs):
        try:
            return mjs()
        except Exception:
            pass
    schema_fn = getattr(model, "schema", None)
    if callable(schema_fn):
        try:
            return schema_fn()
        except Exception:
            pass
    ann = getattr(model, "__annotations__", None)
    if isinstance(ann, dict):
        return {"type": "object", "properties": {k: {"type": "string"} for k in ann.keys()}}
    return None


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
