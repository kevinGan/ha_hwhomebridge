"""配置驱动的 OpenHarmony 服务路由；同一协议可用于 REST 或 HA 内部传输。"""
from __future__ import annotations

import copy
import json
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..product_matcher import DeviceInfo, ProductMatcher
from ..service_action import ServiceActionDispatcher
from ..service_router import ServiceRouter
from ..virtual_device import ServiceEntry, VirtualDevice
from ..fan_mapping import fan_command, fan_state, is_mapped_fan
from .configuration import OpenHarmonyRegistry, child_sn, integer, read_json


class OpenHarmonyActionDispatcher(ServiceActionDispatcher):
    """同步完成并等待读回；不使用旧后端的异步投递和设备定时器。"""

    def __init__(self, router):
        super().__init__()
        self.router = router

    def dispatch(self, sn, entity_id, mapping, payload):
        return self.router.apply_scalar(entity_id, mapping, payload)


class OpenHarmonyRouter(ServiceRouter):
    def __init__(self, transport):
        super().__init__()
        self.registry = OpenHarmonyRegistry()
        self.matcher = ProductMatcher(self.registry)
        self.action_dispatcher = OpenHarmonyActionDispatcher(self)
        self.transport = transport
        self.devices = {}
        self.roles = {}
        self.forms = {}
        self.write_entities = None
        self.settle_seconds = .8
        self.fan_settle_seconds = 2
        self.confirm_timeout = 6

    @classmethod
    async def create(cls, transport, configuration, instances):
        self = cls(transport)
        await self.registry.load_configuration(configuration)
        if instances.get("version") != 1 or not instances.get("devices"):
            raise ValueError("invalid instance configuration")
        parent = instances["parent_sn"]
        for source in instances["devices"]:
            if not isinstance(source.get("id"), str) or not source["id"]:
                raise ValueError("HA device registry ID required")
            roles = source["roles"]
            if not isinstance(roles, dict) or not roles or len(set(roles.values())) != len(roles):
                raise ValueError("explicit unique role bindings required")
            info = DeviceInfo(device_id=source["id"], name=source.get("name", ""),
                              model=source.get("model", ""), manufacturer=source.get("manufacturer", ""),
                              entity_domains=sorted({entity.split(".")[0] for entity in roles.values()}))
            product, _ = self.matcher.match_with_detail(info)
            composition = getattr(product, "composition", None)
            if not product or composition not in self.registry.bundles:
                raise ValueError("device has no configured composition")
            for card in self.registry.bundles[composition]:
                key = source.get("key_prefix", "") + card["key"]
                sn = child_sn(parent, key)
                if key in self.devices:
                    raise ValueError("duplicate card key")
                definition = self.registry.get_product(card["pid"])
                if definition is None:
                    raise ValueError("unknown composition PID")
                # 根匹配产品带有厂商覆盖；其他组合产品使用各自默认映射再叠加卡片覆盖。
                merged = copy.deepcopy(product) if product.pid == card["pid"] else self.registry.merge(definition)
                for sid, override in card.get("services", {}).items():
                    if sid not in merged.services:
                        raise ValueError("composition cannot invent Huawei services")
                    merged.services[sid].ha_mapping = self.registry._merge_ha_mapping(merged.services[sid].ha_mapping, override)
                # 组合聚合单元使用 (HA device, card key)；原 SNManager 的 1:1 行为不变。
                logical_id = json.dumps([source["id"], key], separators=(",", ":"))
                device = VirtualDevice(sn, merged, logical_id)
                device.source_device_id = source["id"]
                device.card_key = key
                self.roles[sn] = dict(roles)
                for sid, service in merged.services.items():
                    mapping = service.ha_mapping
                    if mapping.action == "aggregate":
                        members = mapping.options.get("members", [])
                        if not members or any(x == sid or x not in merged.services or merged.services[x].ha_mapping.action == "aggregate" for x in members):
                            raise ValueError("invalid aggregate members")
                        continue
                    entity = self.entity(sn, mapping.role)
                    if entity.split(".")[0] != mapping.domain:
                        raise ValueError("role domain does not match adapter")
                    device.add_service_entry(sid, ServiceEntry(entity, mapping.domain, sid))
                    for timer in self.timers(mapping).values():
                        for field, domain in (("enable", "switch"), ("duration", "number"), ("remaining", "sensor")):
                            if self.entity(sn, timer[field]).split(".")[0] != domain:
                                raise ValueError("invalid native timer binding")
                if not self.sn_manager.register(device):
                    raise ValueError("SN conflict")
                self.devices[key] = device
        allowed = {entity for roles in self.roles.values() for entity in roles.values()}
        self.transport.bind(allowed)
        return self

    def set_write_policy(self, policy):
        """正式 HA 部署只开放风扇或全部只读；完整映射保留用于查询。"""
        if policy not in ("fan_only", "read_only"):
            raise ValueError("unsupported OpenHarmony write policy")
        self.write_entities = frozenset(
            roles[role] for roles in self.roles.values()
            for role in ("fan", "fan_timer_enable", "fan_timer_duration")
        ) if policy == "fan_only" else frozenset()

    def authorize_entities(self, entities):
        if self.write_entities is not None and not set(entities).issubset(self.write_entities):
            raise ValueError("OpenHarmony write policy rejects this entity")

    def authorize_timers(self, sn, timers):
        self.authorize_entities(self.entity(sn, timer[field])
                                for timer in timers for field in ("enable", "duration"))

    def entity(self, sn, role):
        try:
            return self.roles[sn][role]
        except (KeyError, TypeError):
            raise ValueError("unbound role") from None

    @staticmethod
    def timers(mapping):
        options = mapping.options
        if mapping.action == "countdown":
            return options["timers"]
        if mapping.action in ("native_timer", "timer_array"):
            return {"timer": options["timer"]}
        return {}

    @staticmethod
    def active(state):
        if state["state"] not in ("on", "off"):
            raise ValueError("HA entity unavailable or not boolean")
        return int(state["state"] == "on")

    def state(self, states, sn, role):
        result = states[self.entity(sn, role)]
        if result["state"] in ("unknown", "unavailable"):
            raise ValueError("HA entity unavailable")
        return result

    def timer_remaining(self, sn, timer, states):
        if not self.active(self.state(states, sn, timer["enable"])):
            return 0
        return integer(float(self.state(states, sn, timer["remaining"])["state"]), 0, timer["maximum"])

    def snapshot(self, key, states=None):
        if key not in self.devices:
            raise ValueError("unknown card")
        device = self.devices[key]
        states = self.transport.states() if states is None else states
        result = {}
        for sid, service in device.product.services.items():
            value = self.read_service(device, sid, service, states)
            if value is not None:
                result[sid] = value
        for sid, service in device.product.services.items():
            mapping = service.ha_mapping
            if mapping.action == "aggregate":
                field = service.char_name
                result[sid] = {field: int(any(result[x].get(mapping.options.get("field", field), 0) for x in mapping.options["members"]))}
        return result

    def read_service(self, device, sid, service, states):
        mapping, sn = service.ha_mapping, device.sn
        options, action, field = mapping.options, mapping.action, service.char_name
        if action == "aggregate":
            return None
        if action == "countdown":
            fields, timers = options["fields"], options["timers"]
            remaining = {target: self.timer_remaining(sn, timer, states) for target, timer in timers.items()}
            positive_targets = [x for x, value in remaining.items() if value]
            inferred = int(positive_targets[0]) if len(positive_targets) == 1 else int(options["combined_target"])
            selection, minutes = self.forms.get((sn, sid), (inferred, None))
            selected = options["targets"][str(selection)]
            values = [remaining[x] for x in selected if remaining[x] > 0]
            if minutes is None:
                configured = [int(float(self.state(states, sn, timers[x]["duration"])["state"])) for x in selected]
                minutes = next((x for x in configured if x > 0), 1)
            return {fields["target"]: selection, fields["minutes"]: minutes,
                    fields["enabled"]: int(bool(values)), fields["remaining"]: min(values) if values else 0}
        if action in ("native_timer", "timer_array"):
            minutes = self.timer_remaining(sn, options["timer"], states)
            if action == "native_timer":
                return {field: minutes}
            result = {"action": 0, "num": int(minutes > 0), "timer": []}
            if minutes:
                measured = self.state(states, sn, options["timer"]["remaining"])["last_updated"]
                end = datetime.fromisoformat(measured) + timedelta(minutes=minutes)
                result["timer"] = [{"id": 1, "enable": 1, "sid": options["target_sid"], "para": options["target_field"],
                                    "paraValue": 0, "end": end.strftime("%Y%m%dT%H%M%SZ")}]
            return result
        state = self.state(states, sn, mapping.role)
        if action == "turn_on_off":
            value = self.active(state)
        elif action == "set_effect":
            value = int(bool(self.active(state)) and state["attributes"].get(mapping.value_attr) == options["effect"])
        elif is_mapped_fan(mapping):
            value = fan_state(mapping, state["state"], state["attributes"])
            if value is None:
                return None
        else:
            value = state["state"] if mapping.value_attr == "state" else state["attributes"].get(mapping.value_attr)
            if value is None:
                return None
            if action == "set_brightness":
                value = round(float(value) * mapping.brightness_range / options["ha_maximum"])
            elif mapping.value_mapping:
                reverse = {v: int(k) for k, v in mapping.value_mapping.mapping_dict.items()}
                value = reverse.get(value, options.get("unmapped"))
                if value is None:
                    return None
        value = self.registry.validate_value(device.product.pid, sid, field, value)
        result = {field: value, **options.get("constants", {})}
        if "label" in options:
            result[options.get("label_field", "name")] = options["label"]
        for characteristic, item in result.items():
            self.registry.validate_value(device.product.pid, sid, characteristic, item)
        return result

    def wait(self, predicate, seconds):
        deadline, confirmed = time.monotonic() + self.confirm_timeout, None
        while True:
            if predicate():
                if confirmed is None:
                    confirmed = time.monotonic()
                if time.monotonic() - confirmed >= seconds:
                    return
            else:
                confirmed = None
            if time.monotonic() >= deadline:
                raise TimeoutError("HA did not confirm a stable state")
            time.sleep(.25)

    def set_timer(self, sn, timer, minutes):
        self.authorize_timers(sn, [timer])
        minutes = integer(minutes, 0, timer["maximum"])
        if not minutes and not self.active(self.state(self.transport.states(), sn, timer["enable"])):
            return
        if minutes:
            self.transport.call("number", "set_value", self.entity(sn, timer["duration"]), value=minutes)
        self.transport.call("switch", "turn_on" if minutes else "turn_off", self.entity(sn, timer["enable"]))
        def confirmed():
            states = self.transport.states()
            enabled = self.active(self.state(states, sn, timer["enable"]))
            configured = float(self.state(states, sn, timer["duration"])["state"])
            return enabled == int(minutes > 0) and (not minutes or configured == minutes and self.timer_remaining(sn, timer, states) > 0)
        self.wait(confirmed, self.settle_seconds)

    def apply_scalar(self, entity, mapping, payload):
        self.authorize_entities([entity])
        action, options, domain = mapping.action, mapping.options, mapping.domain
        value = next(iter(payload.values()))
        if action == "turn_on_off":
            values = {}
            power = options.get("power_on")
            if value and power:
                current = self.transport.states()[entity]["attributes"].get(power["attribute"])
                if current is None:
                    raise ValueError("power-on attribute unavailable")
                if current <= power["maximum"]:
                    values = power["values"]
            self.transport.call(domain, "turn_on" if value else "turn_off", entity, **values)
        elif action == "set_brightness":
            self.transport.call(domain, "turn_on", entity, brightness=math.ceil(value * options["ha_maximum"] / mapping.brightness_range))
        elif action == "set_color_temp":
            self.transport.call(domain, "turn_on", entity, color_temp_kelvin=value)
        elif action in ("set_speed", "set_fan_mode", "set_direction"):
            service, values = fan_command(mapping, value)
            self.transport.call(domain, service, entity, **values)
        elif action == "set_effect":
            if value:
                self.transport.call(domain, "turn_on", entity, effect=options["effect"])
            elif self.active(self.transport.states()[entity]) and self.transport.states()[entity]["attributes"].get(mapping.value_attr) == options["effect"]:
                self.transport.call(domain, "turn_off", entity)
        else:
            raise ValueError("unsupported action")

    def write(self, key, sid, data):
        if key not in self.devices or not isinstance(data, dict) or not data:
            raise ValueError("invalid command")
        device = self.devices[key]
        if sid not in device.product.services:
            raise ValueError("unsupported service")
        service = device.product.services[sid]
        mapping, sn, field = service.ha_mapping, device.sn, service.char_name
        action, options = mapping.action, mapping.options
        if action == "aggregate":
            raise ValueError("aggregate is read-only")
        for name, value in data.items():
            # 标准 timer 数组协议会回带只读 num；只能等于本次数组的长度。
            count_echo = action == "timer_array" and name == "num"
            self.registry.validate_value(device.product.pid, sid, name, value, write=not count_echo)
            if count_echo and (not isinstance(data.get("timer"), list) or value != len(data["timer"])):
                raise ValueError("timer count does not match array")
        if action == "countdown":
            fields = options["fields"]
            if set(data) - {fields[x] for x in ("target", "enabled", "minutes")}:
                raise ValueError("unsupported countdown fields")
            current = self.snapshot(key)[sid]
            target = data.get(fields["target"], current[fields["target"]])
            targets = options["targets"].get(str(target))
            if not targets:
                raise ValueError("unsupported timer target")
            timers = [options["timers"][x] for x in targets]
            self.authorize_timers(sn, timers)
            minutes = integer(data.get(fields["minutes"], current[fields["minutes"]]), 1, min(t["maximum"] for t in timers))
            if fields["enabled"] in data:
                enabled = integer(data[fields["enabled"]], 0, 1)
                for timer in timers:
                    self.set_timer(sn, timer, minutes if enabled else 0)
            self.forms[sn, sid] = (target, minutes)
            return self.snapshot(key)
        if action == "timer_array":
            if set(data) - {"action", "num", "timer"}:
                raise ValueError("invalid timer-array fields")
            command = integer(data.get("action"), 0, 2)
            if command == 2:
                timers = data.get("timer", [])
                if timers and (len(timers) != 1 or set(timers[0]) != {"id"} or integer(timers[0]["id"], 1, 1) != 1):
                    raise ValueError("unknown native countdown ID")
                self.set_timer(sn, options["timer"], 0)
            else:
                timers = data.get("timer")
                if not isinstance(timers, list) or len(timers) != 1 or not isinstance(timers[0], dict):
                    raise ValueError("one native delayed-off timer required")
                timer = timers[0]
                if set(timer) - {"id", "enable", "sid", "para", "paraValue", "end"} or timer.get("sid", options["target_sid"]) != options["target_sid"] or timer.get("para", options["target_field"]) != options["target_field"] or timer.get("paraValue", 0) != 0:
                    raise ValueError("unsupported timer action")
                enabled = integer(timer.get("enable", 1), 0, 1)
                minutes = math.ceil((datetime.strptime(timer["end"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 60) if enabled else 0
                self.set_timer(sn, options["timer"], integer(minutes, 0 if not enabled else 1, options["timer"]["maximum"]))
            return self.snapshot(key)
        if set(data) != {field}:
            raise ValueError("one supported characteristic required")
        if action == "native_timer":
            self.set_timer(sn, options["timer"], data[field])
        else:
            entity = device.route_action(sid, data)
            if entity != self.entity(sn, mapping.role):
                raise ValueError("invalid entity route")
            self.action_dispatcher.dispatch(sn, entity, mapping, data)
        def confirmed():
            result = self.snapshot(key)
            actual = result.get(sid, {})
            tolerance = options.get("tolerance", 0)
            matches = field in actual and abs(actual[field] - data[field]) <= tolerance
            if options.get("requires_on"):
                matches = matches and bool(self.active(self.state(self.transport.states(), sn, mapping.role)))
            return matches
        self.wait(confirmed, max(self.settle_seconds, self.fan_settle_seconds if options.get("requires_on") else 0))
        return self.snapshot(key)

    def route_action(self, sn, payload):
        """兼容项目路由入口，但强制携带 SID，避免多个 on 控件被错误推断。"""
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or set(payload) != {"sid", "data"}:
            raise ValueError("explicit SID required")
        key = next((key for key, device in self.devices.items() if device.sn == sn), None)
        return self.write(key, payload["sid"], payload["data"])

    def get_char_state(self, sn, svc_id):
        """与项目的按 SN/SID 查询接口对接，所有数据仍来自当前 HA 状态。"""
        key = next((key for key, device in self.devices.items() if device.sn == sn), None)
        if key is None:
            return None
        value = self.snapshot(key).get(svc_id)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None
