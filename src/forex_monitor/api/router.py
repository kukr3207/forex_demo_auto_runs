"""Typed path routing, middleware, scope checks, and stable error envelopes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, Optional, Pattern, Tuple

from forex_monitor.api.models import ApiRequest, ApiResponse, HttpMethod
from forex_monitor.errors import ForexMonitorError, NotFoundError, ValidationError


Handler = Callable[[ApiRequest, Mapping[str, str]], ApiResponse]
Middleware = Callable[[ApiRequest, Handler], ApiResponse]


@dataclass(frozen=True)
class Route:
    name: str
    method: HttpMethod
    template: str
    pattern: Pattern[str]
    parameter_names: Tuple[str, ...]
    handler: Handler
    required_scopes: Tuple[str, ...]


class ApiRouter:
    def __init__(self) -> None:
        self._routes: Dict[Tuple[HttpMethod, str], Route] = {}
        self._middleware: list[Middleware] = []

    def add(
        self,
        name: str,
        method: HttpMethod,
        template: str,
        handler: Handler,
        *,
        required_scopes: Iterable[str] = (),
    ) -> Route:
        method_value = HttpMethod(method)
        pattern, names = self._compile(template)
        key = (method_value, template)
        if key in self._routes:
            raise ValidationError("route method and template already exist")
        if any(item.name == name for item in self._routes.values()):
            raise ValidationError("route name already exists")
        route = Route(name, method_value, template, pattern, names, handler, tuple(sorted(set(item.lower() for item in required_scopes))))
        self._routes[key] = route
        return route

    def use(self, middleware: Middleware) -> None:
        self._middleware.append(middleware)

    def dispatch(self, request: ApiRequest) -> ApiResponse:
        request_id_header = ("x-request-id", request.request_id)
        try:
            route, parameters = self._match(request.method, request.path)
            if route.required_scopes:
                if request.identity is None:
                    return ApiResponse(401, {"error": "authentication required", "code": "unauthorized"}, (request_id_header,))
                missing = set(route.required_scopes) - set(request.identity.scopes)
                if missing:
                    return ApiResponse(403, {"error": "insufficient scope", "code": "forbidden", "requiredScopes": sorted(missing)}, (request_id_header,))
            handler = lambda incoming, _: route.handler(incoming, parameters)
            for middleware in reversed(self._middleware):
                next_handler = handler
                handler = lambda incoming, _, middleware=middleware, next_handler=next_handler: middleware(incoming, lambda req, params={}: next_handler(req, params))
            response = handler(request, parameters)
            return response.with_header("x-request-id", request.request_id)
        except NotFoundError as exc:
            return ApiResponse(404, exc.as_dict(), (request_id_header,))
        except ValidationError as exc:
            return ApiResponse(400, exc.as_dict(), (request_id_header,))
        except ForexMonitorError as exc:
            return ApiResponse(409, exc.as_dict(), (request_id_header,))
        except Exception:
            return ApiResponse(500, {"error": "internal server error", "code": "internal_error"}, (request_id_header,))

    def routes(self) -> Tuple[Route, ...]:
        return tuple(sorted(self._routes.values(), key=lambda item: (item.template, item.method.value)))

    def _match(self, method: HttpMethod, path: str) -> Tuple[Route, Mapping[str, str]]:
        method_matches = []
        for route in self._routes.values():
            match = route.pattern.fullmatch(path)
            if match:
                if route.method == method:
                    return route, match.groupdict()
                method_matches.append(route.method.value)
        if method_matches:
            raise ValidationError("method not allowed")
        raise NotFoundError("API route does not exist")

    @staticmethod
    def _compile(template: str) -> Tuple[Pattern[str], Tuple[str, ...]]:
        if not template.startswith("/"):
            raise ValidationError("route template must be absolute")
        names = []
        parts = []
        for part in template.strip("/").split("/"):
            if part.startswith("{") and part.endswith("}"):
                name = part[1:-1]
                if not name.isidentifier() or name in names:
                    raise ValidationError("route parameter name is invalid")
                names.append(name)
                parts.append("(?P<%s>[^/]+)" % name)
            else:
                parts.append(re.escape(part))
        return re.compile("/" + "/".join(parts)), tuple(names)
