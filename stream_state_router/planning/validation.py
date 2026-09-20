from __future__ import annotations

from dataclasses import dataclass

from ..obs.catalog import OBSResourceCatalog
from .models import DesiredState, ResourceKey


@dataclass(frozen=True, slots=True)
class ResourceValidation:
    resource: ResourceKey
    status: str
    code: str
    message: str
    provenance: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def as_mapping(self) -> dict[str, object]:
        return {
            "resource": self.resource.as_mapping(),
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "provenance": list(self.provenance),
        }


def validate_desired_state(
    desired: DesiredState,
    catalog: OBSResourceCatalog,
) -> tuple[ResourceValidation, ...]:
    return tuple(
        validate_resource(
            catalog,
            item.key,
            provenance=item.provenance,
        )
        for item in desired.properties
    )


def validate_resource(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
    *,
    provenance: tuple[str, ...] = (),
) -> ResourceValidation:
    context = _validate_collection_context(catalog, key)
    if context is not None:
        return _with_provenance(context, provenance)

    if key.kind == "scene_item" and key.property_name == "visible":
        result = _validate_scene_item(catalog, key)
    elif key.kind == "input" and key.property_name.startswith("settings."):
        result = _validate_input_setting(catalog, key)
    elif key.kind == "filter" and key.property_name == "enabled":
        result = _validate_filter_enabled(catalog, key)
    elif key.kind == "filter" and key.property_name.startswith("settings."):
        result = _validate_filter_setting(catalog, key)
    elif key.kind == "layout" and key.property_name == "profile":
        result = ResourceValidation(
            resource=key,
            status="not_applicable",
            code="layout_owned_by_layout_manager",
            message="La validation du LayoutProfile reste déléguée au moteur de layouts.",
        )
    else:
        result = ResourceValidation(
            resource=key,
            status="unknown",
            code="unsupported_resource_key",
            message=f"Type de propriété non pris en charge par le catalogue : {key.label()}",
        )
    return _with_provenance(result, provenance)


def _validate_collection_context(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
) -> ResourceValidation | None:
    expected = _collection_from_key(key)
    if not expected:
        return None
    current = str(catalog.scene_collection or "")
    if not current:
        return ResourceValidation(
            resource=key,
            status="unknown",
            code="scene_collection_unknown",
            message=(
                f"La propriété vise la Scene Collection '{expected}', "
                "mais le catalogue ne connaît pas la collection active."
            ),
        )
    if current != expected:
        return ResourceValidation(
            resource=key,
            status="missing",
            code="scene_collection_mismatch",
            message=(
                f"La propriété vise la Scene Collection '{expected}', "
                f"mais le catalogue courant est '{current}'."
            ),
        )
    return None


def _collection_from_key(key: ResourceKey) -> str:
    if key.kind == "scene_item" and len(key.scope) >= 1:
        return str(key.scope[0])
    if key.kind in {"input", "filter"} and len(key.scope) >= 1:
        return str(key.scope[0])
    return ""


def _validate_scene_item(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
) -> ResourceValidation:
    if len(key.scope) != 4:
        return _malformed(key)
    _collection, container, source, occurrence_raw = key.scope
    try:
        occurrence = max(1, int(occurrence_raw))
    except (TypeError, ValueError, OverflowError):
        return _malformed(key)

    found = any(
        item.container == container
        and item.source_name == source
        and item.occurrence == occurrence
        for item in catalog.scene_items
    )
    if found:
        return ResourceValidation(
            resource=key,
            status="verified",
            code="scene_item_found",
            message="Occurrence de Scene Item trouvée dans le catalogue courant.",
        )
    return _missing_or_unknown(
        catalog,
        key,
        code="scene_item_missing",
        message=(
            f"Scene Item introuvable : conteneur='{container}', "
            f"source='{source}', occurrence={occurrence}."
        ),
    )


def _validate_input_setting(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
) -> ResourceValidation:
    if len(key.scope) != 2:
        return _malformed(key)
    _collection, input_name = key.scope
    input_ref = next((item for item in catalog.inputs if item.name == input_name), None)
    if input_ref is None:
        return _missing_or_unknown(
            catalog,
            key,
            code="input_missing",
            message=f"Input OBS introuvable : '{input_name}'.",
        )

    setting = key.property_name.removeprefix("settings.")
    if input_ref.settings is None:
        return ResourceValidation(
            resource=key,
            status="partial",
            code="input_settings_not_loaded",
            message=(
                f"Input '{input_name}' trouvé, mais ses settings détaillés "
                "n'ont pas été chargés."
            ),
        )
    if setting in input_ref.settings:
        return ResourceValidation(
            resource=key,
            status="verified",
            code="input_setting_found",
            message=f"Setting '{setting}' trouvé sur l'input '{input_name}'.",
        )
    return ResourceValidation(
        resource=key,
        status="partial",
        code="input_setting_not_observed",
        message=(
            f"Le setting '{setting}' n'apparaît pas dans le snapshot de '{input_name}'. "
            "Son absence ne prouve pas que le plugin OBS ne le supporte pas."
        ),
    )


def _validate_filter_enabled(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
) -> ResourceValidation:
    filter_ref = _find_filter(catalog, key)
    if isinstance(filter_ref, ResourceValidation):
        return filter_ref
    if filter_ref.enabled is None:
        return ResourceValidation(
            resource=key,
            status="partial",
            code="filter_enabled_unknown",
            message=(
                f"Filtre '{filter_ref.source_name}/{filter_ref.name}' trouvé, "
                "mais son état enabled n'est pas observé."
            ),
        )
    return ResourceValidation(
        resource=key,
        status="verified",
        code="filter_enabled_observed",
        message=f"Filtre '{filter_ref.source_name}/{filter_ref.name}' trouvé.",
    )


def _validate_filter_setting(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
) -> ResourceValidation:
    filter_ref = _find_filter(catalog, key)
    if isinstance(filter_ref, ResourceValidation):
        return filter_ref
    setting = key.property_name.removeprefix("settings.")
    if filter_ref.settings is None:
        return ResourceValidation(
            resource=key,
            status="partial",
            code="filter_settings_not_loaded",
            message=(
                f"Filtre '{filter_ref.source_name}/{filter_ref.name}' trouvé, "
                "mais ses settings détaillés n'ont pas été chargés."
            ),
        )
    if setting in filter_ref.settings:
        return ResourceValidation(
            resource=key,
            status="verified",
            code="filter_setting_found",
            message=(
                f"Setting '{setting}' trouvé sur "
                f"'{filter_ref.source_name}/{filter_ref.name}'."
            ),
        )
    return ResourceValidation(
        resource=key,
        status="partial",
        code="filter_setting_not_observed",
        message=(
            f"Le setting '{setting}' n'apparaît pas dans le snapshot du filtre. "
            "Son absence ne prouve pas que le plugin OBS ne le supporte pas."
        ),
    )


def _find_filter(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
):
    if len(key.scope) != 3:
        return _malformed(key)
    _collection, source_name, filter_name = key.scope
    found = next(
        (
            item
            for item in catalog.filters
            if item.source_name == source_name and item.name == filter_name
        ),
        None,
    )
    if found is not None:
        return found
    return _missing_or_unknown(
        catalog,
        key,
        code="filter_missing",
        message=f"Filtre OBS introuvable : '{source_name}/{filter_name}'.",
    )


def _missing_or_unknown(
    catalog: OBSResourceCatalog,
    key: ResourceKey,
    *,
    code: str,
    message: str,
) -> ResourceValidation:
    if catalog.warnings:
        return ResourceValidation(
            resource=key,
            status="unknown",
            code=f"{code}_catalog_partial",
            message=(
                f"{message} Le catalogue est partiel ; l'absence ne peut pas "
                "être considérée comme confirmée."
            ),
        )
    return ResourceValidation(
        resource=key,
        status="missing",
        code=code,
        message=message,
    )


def _malformed(key: ResourceKey) -> ResourceValidation:
    return ResourceValidation(
        resource=key,
        status="unknown",
        code="malformed_resource_key",
        message=f"Clé de ressource mal formée : {key.label()}",
    )


def _with_provenance(
    result: ResourceValidation,
    provenance: tuple[str, ...],
) -> ResourceValidation:
    return ResourceValidation(
        resource=result.resource,
        status=result.status,
        code=result.code,
        message=result.message,
        provenance=tuple(provenance),
    )
