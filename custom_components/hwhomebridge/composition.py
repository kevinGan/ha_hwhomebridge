"""在同一父网关下规划子卡；不创建、启动或配对网关。"""
import copy
import json
import re

from .native_timer import timers_for_mapping, validate_timer_mapping
from .role_binding import RoleBindings
from .virtual_device import ServiceEntry, SNManager, VirtualDevice


def validate_compositions(registry):
    if not isinstance(registry.bundles, dict) or not registry.bundles:
        raise ValueError("composition bundles required")
    for name, cards in registry.bundles.items():
        if not isinstance(name, str) or not name or not isinstance(cards, list) or not cards:
            raise ValueError("invalid composition")
        keys = set()
        primary_count = 0
        for card in cards:
            if not isinstance(card, dict) or set(card) - {"key", "pid", "services", "primary"}:
                raise ValueError("invalid composition card")
            key = card.get("key")
            if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9-]{1,31}", key) or key in keys:
                raise ValueError("duplicate or invalid composition card key")
            keys.add(key)
            if type(card.get("primary", False)) is not bool:
                raise ValueError("primary must be boolean")
            primary_count += int(card.get("primary", False))
            definition = registry.get_product(card.get("pid"))
            if definition is None:
                raise ValueError("unknown composition PID")
            overrides = card.get("services", {})
            if not isinstance(overrides, dict) or set(overrides) - set(definition.services):
                raise ValueError("composition cannot invent Huawei services")
            for override in overrides.values():
                if not isinstance(override, dict) or {"service_type", "char_name", "c_service_id", "value_type"} & set(override):
                    raise ValueError("composition cannot override Huawei definitions")
        if primary_count > 1:
            raise ValueError("only one primary card per physical device")


def _bind_product(registry, device, bindings):
    for sid, service in device.product.services.items():
        mapping = service.ha_mapping
        registry._check_mapping(mapping)
        characteristics = registry.definitions[device.product.pid]["services"][sid].get("characteristics")
        compound = mapping.action in ("countdown", "timer_array")
        if not isinstance(characteristics, dict) or not characteristics or (not compound and service.char_name not in characteristics):
            raise ValueError("active composition requires characteristic schemas")
        for schema in characteristics.values():
            if not isinstance(schema, dict) or schema.get("type") not in ("bool", "enum", "int", "string", "array[object]") or not isinstance(schema.get("method"), str) or not schema["method"]:
                raise ValueError("unsupported characteristic schema")
        constants = mapping.options.get("constants", {})
        if not isinstance(constants, dict) or service.char_name in constants:
            raise ValueError("constants cannot replace actual characteristic state")
        if mapping.action == "aggregate":
            members = mapping.options.get("members", [])
            if not isinstance(members, list) or not members or any(
                key == sid or key not in device.product.services or device.product.services[key].ha_mapping.action == "aggregate"
                for key in members
            ):
                raise ValueError("invalid aggregate members")
            continue
        entity = bindings.entity(mapping.role, mapping.domain)
        device.add_service_entry(sid, ServiceEntry(entity, mapping.domain, sid))
        validate_timer_mapping(mapping)
        for timer in timers_for_mapping(mapping).values():
            timer.validate_roles(bindings)
            if mapping.action != "countdown" and bindings.entity(timer.duration) != entity:
                raise ValueError("timer mapping must bind its duration role")


def _controls(device):
    result = set()
    for sid, entry in device.service_entries.items():
        mapping = device.product.services[sid].ha_mapping
        if mapping.action and mapping.action != "read_only":
            effect = mapping.options.get("effect") if mapping.action == "set_effect" else None
            result.add((entry.entity_id, mapping.action, effect))
    return result


def service_dependencies(device, bindings):
    entities = {entry.entity_id for entry in device.service_entries.values()}
    for service in device.product.services.values():
        for timer in timers_for_mapping(service.ha_mapping).values():
            entities.update(bindings.entity(role) for role in (timer.enable, timer.duration, timer.remaining))
    return frozenset(entities)


def plan_composition(registry, sn_manager, product, source, parent_identity,
                     owned_entities, supported_pids, sn_factory=None):
    """纯规划：任何卡片不合法时，都不会改变原路由或注册表。"""
    if not isinstance(parent_identity, str) or not 0 < len(parent_identity) <= 128:
        raise ValueError("one existing parent gateway identity required")
    owned_entities = frozenset(owned_entities)
    bindings = RoleBindings(source.get("id"), source.get("roles"), owned_entities)
    if product.composition not in registry.bundles:
        raise ValueError("device has no configured composition")
    prefix = source.get("key_prefix", "")
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9-]{0,31}", prefix):
        raise ValueError("invalid composition key prefix")
    supported = frozenset(supported_pids)
    existing = sn_manager.get_by_device_id(source["id"])
    if existing and any(entry.entity_id not in owned_entities for entry in existing.service_entries.values()):
        raise ValueError("existing primary does not belong to supplied device inventory")
    preserved, devices = {}, {}
    for card in registry.bundles[product.composition]:
        key = prefix + card["key"]
        if card.get("primary") and existing is not None:
            preserved[key] = existing
            continue
        pid = card["pid"]
        if pid not in supported:
            raise ValueError("native backend has not verified this PID")
        definition = registry.get_product(pid)
        merged = copy.deepcopy(product) if product.pid == pid else registry.merge(definition)
        for sid, override in card.get("services", {}).items():
            merged.services[sid].ha_mapping = registry._merge_ha_mapping(merged.services[sid].ha_mapping, override)
        if card.get("primary"):
            logical_id, sn = source["id"], SNManager.generate_sn(source["id"])
        else:
            logical_id = json.dumps(["hwhomebridge:logical:v1", parent_identity, source["id"], key], separators=(",", ":"))
            sn = sn_factory(parent_identity, source["id"], key) if sn_factory else SNManager.generate_sn(logical_id)
        if not isinstance(sn, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", sn):
            raise ValueError("invalid logical child identity")
        device = VirtualDevice(sn, merged, logical_id)
        device.source_device_id = source["id"]
        device.card_key = key
        device.parent_identity = parent_identity
        _bind_product(registry, device, bindings)
        if existing and _controls(existing) & _controls(device):
            raise ValueError("composition duplicates an existing primary control; preserve the primary or choose a separate capability card")
        old = sn_manager.get_by_sn(sn)
        if old and (old.ha_device_id != logical_id or old.product.pid != pid):
            raise ValueError("composition SN or product conflict")
        devices[key] = device
    return devices, preserved, bindings


def publish_compositions(sn_manager, devices):
    """先统一查重，再发布已验证的内存路由；不调用任何原生注册接口。"""
    seen = set()
    for device in devices.values():
        if device.sn in seen:
            raise ValueError("composition identity conflict")
        seen.add(device.sn)
        existing = sn_manager.get_by_sn(device.sn)
        if existing and (existing.ha_device_id != device.ha_device_id or existing.product.pid != device.product.pid):
            raise ValueError("composition identity already owned")
    for device in devices.values():
        if not sn_manager.register(device):
            raise ValueError("SN registration conflict")
