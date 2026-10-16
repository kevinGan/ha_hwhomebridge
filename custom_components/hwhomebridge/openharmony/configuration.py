"""沿用项目三层注册表，增加显式角色绑定和组合卡片配置。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..product_registry import HAMapping, ProductRegistry, VALID_ACTION_DOMAIN_MAP


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 262144:
        raise ValueError("configuration too large")
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite number")))


def integer(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
        raise ValueError("value outside supported range")
    return int(value)


def child_sn(parent, key):
    if not isinstance(parent, str) or not parent.startswith("OHPOC-") or len(parent) >= 40:
        raise ValueError("independent OHPOC parent required")
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9-]{1,31}", key):
        raise ValueError("invalid child key")
    return "OHPOC-HA-" + hashlib.sha256(f"{parent}#ha-{key}-v1".encode()).hexdigest()[:24]


@dataclass
class OpenHarmonyMapping(HAMapping):
    role: str = ""
    options: dict = field(default_factory=dict)


class OpenHarmonyRegistry(ProductRegistry):
    """只扩展本后端的映射；原注册表与原映射字段不变。"""

    EXTRA_ACTIONS = {"set_direction": {"fan"}, "set_effect": {"light"},
                     "native_timer": {"number"}, "countdown": {"number"},
                     "timer_array": {"number"}, "aggregate": {""},
                     "set_fan_mode": {"fan"}}

    def _parse_adapter(self, data, filepath=""):
        adapter = super()._parse_adapter(data, filepath)
        if adapter is not None:
            adapter.composition = data.get("composition")
        return adapter

    def merge(self, product, adapter=None):
        merged = super().merge(product, adapter)
        merged.composition = getattr(adapter, "composition", None)
        return merged

    def _parse_ha_mapping(self, data):
        safe = dict(data)
        action = safe.get("action")
        supported = {"turn_on_off", "set_brightness", "set_color_temp", "set_speed"} | set(self.EXTRA_ACTIONS)
        if action not in supported:
            raise ValueError("action not implemented by OpenHarmony backend")
        domains = self.EXTRA_ACTIONS.get(action, VALID_ACTION_DOMAIN_MAP.get(action))
        if not domains or safe.get("domain", "") not in domains:
            raise ValueError("unsupported action/domain")
        if not isinstance(data.get("role", ""), str) or not isinstance(data.get("options", {}), dict):
            raise ValueError("invalid role/options")
        if action in self.EXTRA_ACTIONS:
            if safe.get("domain", "") not in self.EXTRA_ACTIONS[action]:
                raise ValueError("incompatible action/domain")
            safe["action"] = None
        base = super()._parse_ha_mapping(safe)
        base.action = action
        if base.value_mapping and base.value_mapping.mapping_type != "enum_to_number":
            raise ValueError("unsupported OpenHarmony value mapping")
        return OpenHarmonyMapping(**vars(base), role=data.get("role", ""),
                                  options=copy.deepcopy(data.get("options", {})))

    def _merge_ha_mapping(self, base, override):
        # 与原三层合并一致：厂家按字段覆盖；options 作为一个完整字段覆盖。
        data = vars(base).copy()
        data["colorTemperature_range"] = data.pop("color_temperature_range")
        data["colorTemperature_min"] = data.pop("color_temperature_min")
        if base.value_mapping:
            data["value_mapping"] = {"type": base.value_mapping.mapping_type,
                                     "mapping": base.value_mapping.mapping_dict}
        else:
            data.pop("value_mapping", None)
        if data.get("value_range") is None:
            data.pop("value_range", None)
        data.update(override)
        return self._parse_ha_mapping(data)

    async def load_configuration(self, directory):
        directory = Path(directory)
        framework = read_json(directory / "product_registry.json")
        self.definitions = {}
        for item in framework["products"]:
            pid = item["pid"]
            if pid in self.definitions:
                raise ValueError("duplicate product")
            self.definitions[pid] = item
        default_pids = set()
        vendor_count = 0
        for path in sorted((directory / "adapters").rglob("*.json")):
            data = read_json(path)
            if data["pid"] not in self.definitions or set(data) - {"pid", "services", "match_rules", "composition"}:
                raise ValueError("invalid adapter")
            if set(data.get("services", {})) - set(self.definitions[data["pid"]]["services"]):
                raise ValueError("adapter cannot invent Huawei services")
            if path.parent.name == "default":
                if data["pid"] in default_pids or data.get("match_rules"):
                    raise ValueError("invalid default adapter")
                default_pids.add(data["pid"])
            else:
                vendor_count += 1
        if not await self.load(str(directory / "product_registry.json"), str(directory / "adapters")):
            raise ValueError("registry load failed")
        if self.get_product_count() != len(self.definitions) or self.get_default_count() != len(default_pids) or self.get_adapter_count() != vendor_count:
            raise ValueError("incomplete registry")
        self.bundles = read_json(directory / "compositions.json")["bundles"]

    def validate_value(self, pid, sid, field_name, value, write=False):
        schema = self.definitions[pid]["services"][sid]["characteristics"].get(field_name)
        if not schema or write and "W" not in schema["method"]:
            raise ValueError("unsupported characteristic")
        kind = schema["type"]
        if kind in ("bool", "enum", "int"):
            value = integer(value, schema.get("min", 0), schema.get("max", 2147483647))
            if "values" in schema and value not in schema["values"]:
                raise ValueError("unsupported enum")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > schema.get("max_length", 256):
                raise ValueError("invalid text")
        elif kind == "array[object]":
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("invalid object array")
        else:
            raise ValueError("unsupported profile type")
        return value

    def catalog(self, devices):
        result = []
        for key, device in devices.items():
            definition = self.definitions[device.product.pid]
            metadata = definition["cloud"]
            result.append({"key": key, "pid": device.product.pid,
                           **{k: metadata[k] for k in ("devType", "manu", "model", "protType")},
                           "services": {sid: definition["services"][sid].get("cloud_service_type", svc.service_type)
                                        for sid, svc in device.product.services.items()}})
        return result
