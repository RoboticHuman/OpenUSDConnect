"""Asset identifiers copied between USD layers."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import nullcontext

from pxr import Ar, Sdf

LOG = logging.getLogger(__name__)


def value_contains_asset_path(value) -> bool:
    """Return whether an Sdf field value contains an asset identifier."""
    if isinstance(value, Sdf.AssetPath):
        return True
    if isinstance(value, Sdf.Reference):
        return bool(value.assetPath) or value_contains_asset_path(value.customData)
    if isinstance(value, Sdf.Payload):
        return bool(value.assetPath)
    if isinstance(value, dict):
        return any(value_contains_asset_path(item) for item in value.values())

    if isinstance(value, Sdf.AssetPathArray):
        return True
    if isinstance(value, (Sdf.ReferenceListOp, Sdf.PayloadListOp)):
        return any(
            value_contains_asset_path(item)
            for bucket in (
                value.explicitItems,
                value.addedItems,
                value.prependedItems,
                value.appendedItems,
                value.deletedItems,
                value.orderedItems,
            )
            for item in bucket
        )
    if isinstance(value, (list, tuple)):
        return any(value_contains_asset_path(item) for item in value)
    return False


def _evaluate_asset_expression(
    identifier: str,
    expression_variables: dict | None,
) -> tuple[str, bool]:
    if not Sdf.VariableExpression.IsExpression(identifier):
        return identifier, True

    expression = Sdf.VariableExpression(identifier)
    errors = expression.GetErrors()
    if errors:
        LOG.warning("Invalid asset-path expression %r: %s", identifier, "; ".join(errors))
        return identifier, False

    result = expression.Evaluate(expression_variables or {})
    if result.errors or not isinstance(result.value, str):
        detail = "; ".join(result.errors) or "expression did not evaluate to a string"
        LOG.warning("Could not evaluate asset-path expression %r: %s", identifier, detail)
        return identifier, False
    return result.value, True


def transport_asset_identifier(
    source_layer: Sdf.Layer,
    identifier: str,
    *,
    expression_variables: dict | None = None,
    resolver_context: Ar.ResolverContext | None = None,
) -> str:
    """Return an identifier that keeps its meaning when authored elsewhere."""
    if not identifier:
        return ""
    evaluated, can_anchor = _evaluate_asset_expression(identifier, expression_variables)
    if source_layer.anonymous or not can_anchor:
        return evaluated
    binder = (
        Ar.ResolverContextBinder(resolver_context)
        if resolver_context is not None
        else nullcontext()
    )
    with binder:
        return source_layer.ComputeAbsolutePath(evaluated)


def _transform_asset_paths(
    value,
    transform: Callable[[str], str],
    *,
    use_evaluated_paths: bool,
):
    if isinstance(value, Sdf.AssetPath):
        identifier = (
            value.evaluatedPath or value.path
            if use_evaluated_paths
            else value.authoredPath or value.path
        )
        return Sdf.AssetPath(transform(identifier))
    if isinstance(value, Sdf.Reference):
        return Sdf.Reference(
            transform(value.assetPath) if value.assetPath else "",
            value.primPath,
            value.layerOffset,
            customData=_transform_asset_paths(
                value.customData,
                transform,
                use_evaluated_paths=use_evaluated_paths,
            ),
        )
    if isinstance(value, Sdf.Payload):
        return Sdf.Payload(
            transform(value.assetPath) if value.assetPath else "",
            value.primPath,
            value.layerOffset,
        )
    if isinstance(value, dict):
        return {
            key: _transform_asset_paths(
                item,
                transform,
                use_evaluated_paths=use_evaluated_paths,
            )
            for key, item in value.items()
        }

    if isinstance(value, Sdf.AssetPathArray):
        return type(value)(
            [
                _transform_asset_paths(
                    item,
                    transform,
                    use_evaluated_paths=use_evaluated_paths,
                )
                for item in value
            ]
        )
    if isinstance(value, (Sdf.ReferenceListOp, Sdf.PayloadListOp)):
        result = type(value)()
        if value.isExplicit:
            result.explicitItems = [
                _transform_asset_paths(
                    item,
                    transform,
                    use_evaluated_paths=use_evaluated_paths,
                )
                for item in value.explicitItems
            ]
            return result
        for bucket in (
            "addedItems",
            "prependedItems",
            "appendedItems",
            "deletedItems",
            "orderedItems",
        ):
            setattr(
                result,
                bucket,
                [
                    _transform_asset_paths(
                        item,
                        transform,
                        use_evaluated_paths=use_evaluated_paths,
                    )
                    for item in getattr(value, bucket)
                ],
            )
        return result
    if isinstance(value, list):
        return [
            _transform_asset_paths(
                item,
                transform,
                use_evaluated_paths=use_evaluated_paths,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _transform_asset_paths(
                item,
                transform,
                use_evaluated_paths=use_evaluated_paths,
            )
            for item in value
        )
    return value


def stabilize_layer_asset_paths(
    layer: Sdf.Layer,
    source_layer: Sdf.Layer,
    *,
    expression_variables: dict | None = None,
    resolver_context: Ar.ResolverContext | None = None,
    use_evaluated_paths: bool = False,
) -> None:
    """Re-anchor every asset value in *layer* to its owning source layer."""

    def _transform(identifier: str) -> str:
        return transport_asset_identifier(
            source_layer,
            identifier,
            expression_variables=expression_variables,
        )

    binder = (
        Ar.ResolverContextBinder(resolver_context)
        if resolver_context is not None
        else nullcontext()
    )
    # Bind once for the whole fragment; asset arrays may contain many paths.
    with binder:
        specs = []
        layer.Traverse(
            Sdf.Path.absoluteRootPath,
            lambda path: specs.append(layer.GetObjectAtPath(path)),
        )
        for spec in specs:
            if spec is None:
                continue
            for field in tuple(spec.ListInfoKeys()):
                value = spec.GetInfo(field)
                if value_contains_asset_path(value):
                    spec.SetInfo(
                        field,
                        _transform_asset_paths(
                            value,
                            _transform,
                            use_evaluated_paths=use_evaluated_paths,
                        ),
                    )


def repair_missing_duplicate_asset_paths(layer: Sdf.Layer) -> int:
    """Repair unresolved paths containing an accidental repeated directory.

    Some third-party USD assets contain paths such as
    ``textures/./textures/map.png``. Flattening anchors that malformed value
    into an absolute ``.../textures/textures/map.png`` path. Change it only
    when the original does not resolve and removing one adjacent duplicate
    component identifies an existing file.
    """

    resolver = Ar.GetResolver()

    def _resolves(identifier: str) -> bool:
        if os.path.exists(identifier):
            return True
        try:
            return bool(resolver.Resolve(identifier))
        except Exception:
            return False

    repaired_count = 0

    def _repair(identifier: str) -> str:
        nonlocal repaired_count
        if not identifier or "://" in identifier or "[" in identifier:
            return identifier
        normalized = os.path.normpath(identifier)
        if not os.path.isabs(normalized) or _resolves(normalized):
            return identifier
        drive, tail = os.path.splitdrive(normalized)
        components = [part for part in tail.split(os.sep) if part]
        for index in range(len(components) - 1):
            if components[index].casefold() != components[index + 1].casefold():
                continue
            candidate_components = components[: index + 1] + components[index + 2 :]
            candidate = os.path.join(drive + os.sep, *candidate_components)
            if not _resolves(candidate):
                continue
            repaired_count += 1
            repaired = candidate.replace("\\", "/")
            LOG.warning("Repaired unresolved duplicate asset path %s -> %s", identifier, repaired)
            return repaired
        return identifier

    specs = []
    layer.Traverse(
        Sdf.Path.absoluteRootPath,
        lambda path: specs.append(layer.GetObjectAtPath(path)),
    )
    for spec in specs:
        if spec is None:
            continue
        for field in tuple(spec.ListInfoKeys()):
            value = spec.GetInfo(field)
            if value_contains_asset_path(value):
                spec.SetInfo(
                    field,
                    _transform_asset_paths(
                        value,
                        _repair,
                        use_evaluated_paths=False,
                    ),
                )
    return repaired_count


__all__ = [
    "repair_missing_duplicate_asset_paths",
    "stabilize_layer_asset_paths",
    "transport_asset_identifier",
    "value_contains_asset_path",
]
