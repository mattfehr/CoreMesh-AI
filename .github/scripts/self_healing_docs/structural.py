"""AST-backed extraction and diffing for documentation-relevant structures.

System role:
    Converts changed Python, Go, and Compose source snapshots into normalized
    API/schema/service adjustments while ignoring implementation-only edits.
Dependencies:
    Python ``ast``, Tree-sitter with the Go grammar, and PyYAML safe loading.
Side effects:
    None; parsing operates on source strings read by the Git boundary.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

import yaml

from .models import SourceFileDelta, StructuralChange, StructuralUnit


class StructuralParseError(ValueError):
    """Raised when a changed source snapshot cannot be parsed safely."""


_ROUTE_METHODS = {
    "api_route",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "route",
    "trace",
    "websocket",
    "websocket_route",
}
_BOUNDED_KINDS = {
    "compose_service",
    "go_field",
    "go_function",
    "go_interface_method",
    "go_route",
    "python_constant",
    "python_field",
    "python_function",
    "python_service",
}


def extract_structural_changes(
    deltas: Iterable[SourceFileDelta],
) -> list[StructuralChange]:
    """Parse and compare every changed source file in deterministic order."""

    changes: list[StructuralChange] = []
    for delta in sorted(deltas, key=lambda item: item.display_path):
        old_units = (
            extract_units(
                language=delta.language,
                path=delta.old_path or delta.display_path,
                source=delta.old_text,
            )
            if delta.old_text is not None
            else []
        )
        new_units = (
            extract_units(
                language=delta.language,
                path=delta.new_path or delta.display_path,
                source=delta.new_text,
            )
            if delta.new_text is not None
            else []
        )
        changes.extend(_diff_units(delta, old_units, new_units))
    return sorted(changes, key=lambda item: (item.path, item.start_line, item.change_id))


def extract_units(*, language: str, path: str, source: str) -> list[StructuralUnit]:
    """Dispatch one source snapshot to its language-specific parser."""

    try:
        if language == "python":
            return extract_python_units(path, source)
        if language == "go":
            return extract_go_units(path, source)
        if language == "compose":
            return extract_compose_units(path, source)
    except StructuralParseError:
        raise
    except Exception as exc:
        raise StructuralParseError(f"failed to parse {path}: {exc}") from exc
    raise StructuralParseError(f"unsupported structural language {language!r}")


def extract_python_units(path: str, source: str) -> list[StructuralUnit]:
    """Extract normalized Python functions, classes, fields, routes, and services."""

    try:
        module = ast.parse(source, filename=path, type_comments=True)
    except SyntaxError as exc:
        raise StructuralParseError(
            f"invalid Python syntax in {path}:{exc.lineno}: {exc.msg}"
        ) from exc

    units: list[StructuralUnit] = []
    _extract_python_scope(
        path=path,
        body=module.body,
        units=units,
        parents=(),
        class_name=None,
    )
    return sorted(units, key=lambda item: (item.start_line, item.identity))


def _extract_python_scope(
    *,
    path: str,
    body: list[ast.stmt],
    units: list[StructuralUnit],
    parents: tuple[str, ...],
    class_name: str | None,
) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = ".".join((*parents, node.name))
            decorator_texts = tuple(_safe_unparse(item) for item in node.decorator_list)
            is_route = any(_python_decorator_is_route(item) for item in node.decorator_list)
            if not (_is_public_python_name(node.name) or is_route or node.name == "__init__"):
                continue
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            arguments = _safe_unparse(node.args)
            returns = f" -> {_safe_unparse(node.returns)}" if node.returns else ""
            header = f"{prefix} {qualified}({arguments}){returns}"
            representation = "\n".join(
                [*(f"@{item}" for item in decorator_texts), header]
            )
            terms = {
                node.name,
                qualified,
                *parents,
                *_python_literal_terms(node.decorator_list),
            }
            units.append(
                StructuralUnit(
                    path=path,
                    language="python",
                    kind="python_function",
                    identity=f"python_function:{qualified}",
                    name=qualified,
                    representation=representation,
                    context=_first_doc_line(ast.get_docstring(node, clean=True)),
                    search_terms=_clean_terms(terms),
                    start_line=_python_start_line(node),
                    end_line=getattr(node, "end_lineno", node.lineno),
                    modification_is_bounded=True,
                )
            )
            continue

        if isinstance(node, ast.ClassDef):
            qualified = ".".join((*parents, node.name))
            bases = [_safe_unparse(base) for base in node.bases]
            bases.extend(
                f"{keyword.arg}={_safe_unparse(keyword.value)}"
                for keyword in node.keywords
                if keyword.arg
            )
            representation = (
                f"class {qualified}({', '.join(bases)})" if bases else f"class {qualified}"
            )
            units.append(
                StructuralUnit(
                    path=path,
                    language="python",
                    kind="python_class",
                    identity=f"python_class:{qualified}",
                    name=qualified,
                    representation=representation,
                    context=_first_doc_line(ast.get_docstring(node, clean=True)),
                    search_terms=_clean_terms({node.name, qualified, *bases}),
                    start_line=_python_start_line(node),
                    end_line=getattr(node, "end_lineno", node.lineno),
                    modification_is_bounded=False,
                )
            )
            _extract_python_scope(
                path=path,
                body=node.body,
                units=units,
                parents=(*parents, node.name),
                class_name=qualified,
            )
            continue

        if class_name and isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                units.append(
                    _python_field_unit(
                        path=path,
                        class_name=class_name,
                        field_name=node.target.id,
                        annotation=_safe_unparse(node.annotation),
                        value=_safe_unparse(node.value) if node.value is not None else None,
                        node=node,
                    )
                )
            continue

        if class_name and isinstance(node, ast.Assign):
            value = _safe_unparse(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_public_python_name(target.id):
                    units.append(
                        _python_field_unit(
                            path=path,
                            class_name=class_name,
                            field_name=target.id,
                            annotation=None,
                            value=value,
                            node=node,
                        )
                    )
            continue

        if not parents and isinstance(node, (ast.Assign, ast.AnnAssign)):
            units.extend(_python_module_assignment_units(path, node))


def _python_field_unit(
    *,
    path: str,
    class_name: str,
    field_name: str,
    annotation: str | None,
    value: str | None,
    node: ast.AST,
) -> StructuralUnit:
    qualified = f"{class_name}.{field_name}"
    representation = qualified
    if annotation:
        representation += f": {annotation}"
    if value is not None:
        representation += f" = {value}"
    return StructuralUnit(
        path=path,
        language="python",
        kind="python_field",
        identity=f"python_field:{qualified}",
        name=qualified,
        representation=representation,
        context=f"Field on {class_name}",
        search_terms=_clean_terms({field_name, qualified, annotation or "", value or ""}),
        start_line=getattr(node, "lineno", 1),
        end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        modification_is_bounded=True,
    )


def _python_module_assignment_units(
    path: str,
    node: ast.Assign | ast.AnnAssign,
) -> list[StructuralUnit]:
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
        value_node = node.value
        annotation = _safe_unparse(node.annotation)
    else:
        targets = list(node.targets)
        value_node = node.value
        annotation = None

    result: list[StructuralUnit] = []
    for target in targets:
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        is_service = (
            isinstance(value_node, ast.Call)
            and _call_name(value_node.func).split(".")[-1] in {"FastAPI", "Flask"}
        )
        if not is_service and not name.isupper():
            continue
        value = _safe_unparse(value_node) if value_node is not None else "<no default>"
        representation = name
        if annotation:
            representation += f": {annotation}"
        representation += f" = {value}"
        kind = "python_service" if is_service else "python_constant"
        result.append(
            StructuralUnit(
                path=path,
                language="python",
                kind=kind,
                identity=f"{kind}:{name}",
                name=name,
                representation=representation,
                context="Python application definition" if is_service else "Module constant",
                search_terms=_clean_terms(
                    {name, annotation or "", value, *_python_literal_terms([value_node])}
                ),
                start_line=getattr(node, "lineno", 1),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                modification_is_bounded=True,
            )
        )
    return result


def extract_go_units(path: str, source: str) -> list[StructuralUnit]:
    """Extract normalized Go declarations and HTTP registration calls."""

    try:
        import tree_sitter_go
        from tree_sitter import Language, Parser
    except ImportError as exc:
        raise StructuralParseError(
            "Go parsing requires tree-sitter and tree-sitter-go"
        ) from exc

    language = Language(tree_sitter_go.language())
    parser = Parser(language)
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    if tree.root_node.has_error:
        point = _first_error_point(tree.root_node)
        raise StructuralParseError(
            f"invalid Go syntax in {path}:{point[0]}:{point[1]}"
        )

    units: list[StructuralUnit] = []
    route_index = 0
    for node in _walk_tree(tree.root_node):
        if node.type in {"function_declaration", "method_declaration"}:
            unit = _go_function_unit(path, source_bytes, node)
            if unit is not None:
                units.append(unit)
        elif node.type == "type_declaration":
            units.extend(_go_type_units(path, source_bytes, node))
        elif node.type == "call_expression":
            route = _go_route_unit(path, source_bytes, node, route_index)
            if route is not None:
                units.append(route)
                route_index += 1
    return sorted(
        _dedupe_units(units), key=lambda item: (item.start_line, item.identity)
    )


def _go_function_unit(path: str, source: bytes, node: Any) -> StructuralUnit | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _node_text(source, name_node)
    if not (name[:1].isupper() or name == "main"):
        return None
    receiver_node = node.child_by_field_name("receiver")
    receiver = (
        _normalize_space(_node_text(source, receiver_node))
        if receiver_node is not None
        else ""
    )
    receiver_type = (
        _go_receiver_type(source, receiver_node) if receiver_node is not None else ""
    )
    receiver_signature = f"({receiver_type})" if receiver_type else ""
    qualified = f"{receiver_signature}.{name}" if receiver_signature else name
    body = node.child_by_field_name("body")
    end_byte = body.start_byte if body is not None else node.end_byte
    representation = _normalize_space(source[node.start_byte:end_byte].decode("utf-8"))
    if receiver and receiver_signature:
        representation = representation.replace(receiver, receiver_signature, 1)
    return StructuralUnit(
        path=path,
        language="go",
        kind="go_function",
        identity=f"go_function:{qualified}",
        name=qualified,
        representation=representation,
        context="Exported Go method or function",
        search_terms=_clean_terms(
            {
                name,
                qualified,
                receiver_type,
                receiver_type.lstrip("*[]"),
            }
        ),
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        modification_is_bounded=True,
    )


def _go_type_units(path: str, source: bytes, declaration: Any) -> list[StructuralUnit]:
    units: list[StructuralUnit] = []
    for node in _walk_tree(declaration):
        if node.type != "type_spec":
            continue
        name_node = node.child_by_field_name("name")
        type_node = node.child_by_field_name("type")
        if name_node is None or type_node is None:
            continue
        name = _node_text(source, name_node)
        if not name[:1].isupper():
            continue
        type_kind = type_node.type
        kind = {
            "struct_type": "go_struct",
            "interface_type": "go_interface",
        }.get(type_kind, "go_type")
        base_representation = f"type {name} {type_kind.removesuffix('_type')}"
        if kind == "go_type":
            base_representation = f"type {name} {_normalize_space(_node_text(source, type_node))}"
        units.append(
            StructuralUnit(
                path=path,
                language="go",
                kind=kind,
                identity=f"{kind}:{name}",
                name=name,
                representation=base_representation,
                context="Exported Go type",
                search_terms=_clean_terms({name, type_kind}),
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                modification_is_bounded=False,
            )
        )
        if kind == "go_struct":
            units.extend(_go_struct_field_units(path, source, name, type_node))
        elif kind == "go_interface":
            units.extend(_go_interface_method_units(path, source, name, type_node))
    return units


def _go_receiver_type(source: bytes, receiver_node: Any) -> str:
    """Return a receiver's type without its implementation-only variable name."""

    for node in _walk_tree(receiver_node):
        if node.type != "parameter_declaration":
            continue
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return _normalize_space(_node_text(source, type_node))
    rendered = _normalize_space(_node_text(source, receiver_node)).strip("() ")
    return rendered.rsplit(" ", 1)[-1] if rendered else ""


def _go_struct_field_units(
    path: str,
    source: bytes,
    type_name: str,
    type_node: Any,
) -> list[StructuralUnit]:
    result: list[StructuralUnit] = []
    ordinal = 0
    for field in _walk_tree(type_node):
        if field.type != "field_declaration":
            continue
        name_node = field.child_by_field_name("name")
        type_field = field.child_by_field_name("type")
        if name_node is not None:
            field_name = _node_text(source, name_node)
        elif type_field is not None:
            field_name = _normalize_space(_node_text(source, type_field))
        else:
            field_name = f"field_{ordinal}"
        ordinal += 1
        if not field_name[:1].isupper():
            continue
        qualified = f"{type_name}.{field_name}"
        representation = _normalize_space(_node_text(source, field))
        result.append(
            StructuralUnit(
                path=path,
                language="go",
                kind="go_field",
                identity=f"go_field:{qualified}",
                name=qualified,
                representation=f"{qualified}: {representation}",
                context=f"Field on Go struct {type_name}",
                search_terms=_clean_terms({type_name, field_name, qualified, representation}),
                start_line=field.start_point.row + 1,
                end_line=field.end_point.row + 1,
                modification_is_bounded=True,
            )
        )
    return result


def _go_interface_method_units(
    path: str,
    source: bytes,
    type_name: str,
    type_node: Any,
) -> list[StructuralUnit]:
    result: list[StructuralUnit] = []
    for element in _walk_tree(type_node):
        if element.type != "method_elem":
            continue
        name_node = element.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = _node_text(source, name_node)
        qualified = f"{type_name}.{method_name}"
        representation = _normalize_space(_node_text(source, element))
        result.append(
            StructuralUnit(
                path=path,
                language="go",
                kind="go_interface_method",
                identity=f"go_interface_method:{qualified}",
                name=qualified,
                representation=f"{qualified}: {representation}",
                context=f"Method on Go interface {type_name}",
                search_terms=_clean_terms(
                    {type_name, method_name, qualified, representation}
                ),
                start_line=element.start_point.row + 1,
                end_line=element.end_point.row + 1,
                modification_is_bounded=True,
            )
        )
    return result


def _go_route_unit(
    path: str,
    source: bytes,
    node: Any,
    route_index: int,
) -> StructuralUnit | None:
    function_node = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function_node is None or arguments is None:
        return None
    callee = _normalize_space(_node_text(source, function_node))
    leaf = callee.rsplit(".", 1)[-1]
    if leaf not in {"Handle", "HandleFunc", "Methods"}:
        return None

    rendered_args: list[str] = []
    route_path = ""
    for child in arguments.named_children:
        text = _normalize_space(_node_text(source, child))
        if child.type in {"interpreted_string_literal", "raw_string_literal"}:
            rendered_args.append(text)
            if not route_path:
                route_path = text.strip("`\"")
        elif child.type in {"identifier", "selector_expression"}:
            rendered_args.append(text)
        elif child.type in {"func_literal", "function_literal"}:
            rendered_args.append("<handler>")
        elif len(rendered_args) < 3:
            rendered_args.append(text[:120])
    representation = f"{callee}({', '.join(rendered_args)})"
    route_name = route_path or f"registration_{route_index}"
    return StructuralUnit(
        path=path,
        language="go",
        kind="go_route",
        identity=f"go_route:{callee}:{route_index}",
        name=route_name,
        representation=representation,
        context="Go HTTP route registration",
        search_terms=_clean_terms({callee, leaf, route_path, *rendered_args}),
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        modification_is_bounded=True,
    )


def extract_compose_units(path: str, source: str) -> list[StructuralUnit]:
    """Extract deterministic service contracts from a Compose YAML document."""

    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise StructuralParseError(f"invalid Compose YAML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StructuralParseError(f"Compose document must be a mapping: {path}")
    services = parsed.get("services", {})
    if services is None:
        services = {}
    if not isinstance(services, dict):
        raise StructuralParseError(f"Compose services must be a mapping: {path}")

    units: list[StructuralUnit] = []
    for service_name, raw_service in sorted(services.items(), key=lambda item: str(item[0])):
        if not isinstance(service_name, str) or not isinstance(raw_service, dict):
            raise StructuralParseError(f"invalid Compose service in {path}: {service_name!r}")
        contract = {
            key: raw_service[key]
            for key in (
                "build",
                "command",
                "depends_on",
                "healthcheck",
                "image",
                "ports",
                "profiles",
            )
            if key in raw_service
        }
        if "environment" in raw_service:
            contract["environment"] = _compose_environment_keys(
                raw_service["environment"],
                path=path,
                service_name=service_name,
            )
        for key in ("depends_on", "ports", "profiles"):
            if isinstance(contract.get(key), list):
                contract[key] = _sorted_compose_values(contract[key])
        normalized = _json_normalize(contract)
        terms = {service_name}
        terms.update(contract.get("environment", []))
        for value in raw_service.get("ports", []) or []:
            terms.add(str(value))
        units.append(
            StructuralUnit(
                path=path,
                language="compose",
                kind="compose_service",
                identity=f"compose_service:{service_name}",
                name=service_name,
                representation=f"service {service_name}: {normalized}",
                context="Docker Compose microservice contract",
                search_terms=_clean_terms(terms),
                start_line=1,
                end_line=max(1, source.count("\n") + 1),
                modification_is_bounded=True,
            )
        )
    return units


def _diff_units(
    delta: SourceFileDelta,
    old_units: list[StructuralUnit],
    new_units: list[StructuralUnit],
) -> list[StructuralChange]:
    old_by_id = {unit.identity: unit for unit in old_units}
    new_by_id = {unit.identity: unit for unit in new_units}
    changes: list[StructuralChange] = []
    for identity in sorted(set(old_by_id) | set(new_by_id)):
        before = old_by_id.get(identity)
        after = new_by_id.get(identity)
        if before is not None and after is not None:
            if before.representation == after.representation:
                continue
            change_type = "modified"
        elif before is not None:
            change_type = "removed"
        else:
            change_type = "added"
        current = after or before
        assert current is not None
        path = delta.new_path or delta.old_path or current.path
        terms = _clean_terms(
            {
                *(before.search_terms if before else ()),
                *(after.search_terms if after else ()),
            }
        )
        context = (after.context if after else before.context) or ""
        digest_input = "\0".join(
            [
                path,
                identity,
                change_type,
                before.representation if before else "",
                after.representation if after else "",
            ]
        )
        changes.append(
            StructuralChange(
                change_id=hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16],
                path=path,
                language=current.language,
                kind=current.kind,
                name=current.name,
                change_type=change_type,
                before=before.representation if before else None,
                after=after.representation if after else None,
                context=context,
                search_terms=terms,
                start_line=(after.start_line if after else before.start_line),
                end_line=(after.end_line if after else before.end_line),
                auto_fix_eligible=(
                    change_type == "modified"
                    and current.kind in _BOUNDED_KINDS
                    and bool(before and before.modification_is_bounded)
                    and bool(after and after.modification_is_bounded)
                ),
            )
        )
    return changes


def _safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        value = ast.unparse(node)
    except Exception as exc:
        raise StructuralParseError(f"could not normalize Python AST node: {exc}") from exc
    return _normalize_space(value)[:2_000]


def _python_decorator_is_route(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return _call_name(target).split(".")[-1].lower() in _ROUTE_METHODS


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _python_literal_terms(nodes: Iterable[ast.AST | None]) -> set[str]:
    terms: set[str] = set()
    for node in nodes:
        if node is None:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                value = child.value.strip()
                if value:
                    terms.add(value[:240])
    return terms


def _python_start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    decorator_lines = [item.lineno for item in node.decorator_list]
    return min([node.lineno, *decorator_lines])


def _is_public_python_name(name: str) -> bool:
    return bool(name) and not name.startswith("_")


def _first_doc_line(docstring: str | None) -> str:
    if not docstring:
        return ""
    return docstring.strip().splitlines()[0][:500]


def _walk_tree(node: Any) -> Iterator[Any]:
    yield node
    for child in node.named_children:
        yield from _walk_tree(child)


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _first_error_point(root: Any) -> tuple[int, int]:
    for node in _walk_tree(root):
        if node.type == "ERROR" or node.is_missing:
            return node.start_point.row + 1, node.start_point.column + 1
    return root.start_point.row + 1, root.start_point.column + 1


def _dedupe_units(units: Iterable[StructuralUnit]) -> list[StructuralUnit]:
    result: dict[str, StructuralUnit] = {}
    for unit in units:
        result.setdefault(unit.identity, unit)
    return list(result.values())


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_terms(values: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if not normalized:
            continue
        result.add(normalized[:500])
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", normalized):
            result.add(token[:200])
    return tuple(sorted(result, key=lambda item: (item.lower(), item)))


def _json_normalize(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise StructuralParseError(f"Compose value is not serializable: {exc}") from exc


def _compose_environment_keys(
    value: Any,
    *,
    path: str,
    service_name: str,
) -> list[str]:
    """Normalize only the public environment-key contract, never its values."""

    if value is None:
        return []
    if isinstance(value, dict):
        keys = [str(key).strip() for key in value]
    elif isinstance(value, list):
        keys = []
        for item in value:
            if not isinstance(item, str):
                raise StructuralParseError(
                    f"Compose environment list for {service_name!r} in {path} "
                    "must contain strings"
                )
            keys.append(item.split("=", 1)[0].strip())
    else:
        raise StructuralParseError(
            f"Compose environment for {service_name!r} in {path} "
            "must be a mapping or list"
        )
    if any(not key for key in keys):
        raise StructuralParseError(
            f"Compose environment for {service_name!r} in {path} "
            "contains an empty key"
        )
    return sorted(set(keys), key=lambda item: (item.casefold(), item))


def _sorted_compose_values(values: list[Any]) -> list[Any]:
    """Normalize order-insensitive Compose contract lists deterministically."""

    return sorted(
        values,
        key=lambda value: json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )
