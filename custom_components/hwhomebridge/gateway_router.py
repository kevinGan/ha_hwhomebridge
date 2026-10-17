"""接管后继续使用公共产品、角色、定时及控制路由；实体归属由HA注册表确认。"""
import hashlib
import time
from pathlib import Path


from .ha_transport import HomeAssistantTransport, async_owned_entity_inventory
from .profile_bridge import ProfileBridge
from .service_router import ServiceRouter
from .virtual_device import VirtualDevice, ServiceEntry, SNManager
from .capability_router import CapabilityRouter
from .openharmony.configuration import OpenHarmonyRegistry, child_sn


class GatewayRouter:
    @classmethod
    async def create(cls, hass, deployment, entry_id):
        from homeassistant.helpers import device_registry
        self = cls()
        self.legacy = ServiceRouter()
        self.legacy.set_hass(hass)
        config = Path(__file__).parent / 'config'
        if not await self.legacy.load_product_registry(str(config / 'product_registry.json')):
            raise ValueError('original product registry unavailable')
        self.bridges = {}
        self.entities = set()
        rows = deployment.bindings['devices']
        devices = device_registry.async_get(hass)
        excluded = deployment.directory / 'legacy/excluded_devices.txt'
        if excluded.exists() and excluded.read_text().strip():
            raise ValueError('excluded legacy devices require an explicit offline adoption plan')
        for row in rows:
            source = devices.async_get(row['source_id'])
            if source is None or source.disabled_by is not None:
                raise ValueError('existing source device is missing or disabled')
        # 沿用既有桥接ID，也沿用其禁用决定；不因换引擎重新启用。
        for device in device_registry.async_entries_for_config_entry(devices, entry_id):
            if device.disabled_by is not None:
                raise ValueError('disabled bridge devices require an explicit offline adoption plan')
        inventory = async_owned_entity_inventory(hass, {row['source_id'] for row in rows})
        for row in rows:
            if set(row['entities'].values()) - inventory[row['source_id']]:
                raise ValueError('entity belongs to a different HA device')
            base = self.legacy.registry.get_product(row['pid'])
            if base is None or row['sn'] != SNManager.generate_sn(row['source_id']):
                raise ValueError('existing product or deterministic SN mismatch')
            adapter = None
            if row.get('adapter'):
                choices = [a for a in self.legacy.registry.get_all_adapters()
                           if a.pid == row['pid'] and a.services == row['adapter']]
                if len(choices) != 1:
                    raise ValueError('existing vendor adapter changed')
                adapter = choices[0]
            product = self.legacy.registry.merge(base, adapter)
            device = VirtualDevice(row['sn'], product, row['source_id'])
            for sid, entity in row['entities'].items():
                mapping = product.services[sid].ha_mapping
                if mapping is None or entity.split('.')[0] != mapping.domain:
                    raise ValueError('existing service/entity mapping changed')
                device.add_service_entry(sid, ServiceEntry(entity, mapping.domain, sid))
            if not self.legacy.sn_manager.register(device):
                raise ValueError('duplicate existing SN')
            profile = deployment.read('profiles/' + row['cloud_pid'] + '.json')
            if deployment.settings['profiles'][row['cloud_pid']] != hashlib.sha256(
                    (deployment.directory / 'profiles' / (row['cloud_pid'] + '.json')).read_bytes()).hexdigest():
                raise ValueError('legacy profile changed')
            bridge = ProfileBridge(self.legacy, device, profile)
            catalog = next(item for item in deployment.catalog if item['key'] == row['key'])
            if catalog['sn'] != device.sn or catalog['pid'] != row['cloud_pid'] or catalog['services'] != bridge.services:
                raise ValueError('old cloud catalog differs from public router')
            self.bridges[row['key']] = bridge
            self.entities.update(row['entities'].values())
        instance = deployment.read('instances.json')
        composition_inventory = async_owned_entity_inventory(hass, {row['id'] for row in instance['devices']})
        class BoundRouter(CapabilityRouter):
            registry_class = OpenHarmonyRegistry
        self.capabilities = await BoundRouter.create(HomeAssistantTransport(hass), deployment.directory / 'mapping', instance,
            owned_entities=composition_inventory, supported_pids={row['pid'] for row in deployment.catalog},
            sn_manager=self.legacy.sn_manager, sn_factory=lambda parent, source, key: child_sn(parent, key))
        self.capabilities.set_hass(hass)
        expected = self.capabilities.registry.catalog(self.capabilities.devices)
        actual = {row['key']: {k: v for k, v in row.items() if k != 'sn'} for row in deployment.catalog
                  if row['key'] not in self.bridges}
        if {row['key']: row for row in expected} != actual:
            raise ValueError('composition catalog differs from configured capabilities')
        self.entities.update(entity for source in instance['devices'] for entity in source['roles'].values())
        self.manager = self.legacy.sn_manager
        return self

    def snapshot(self, key):
        return self.bridges[key].snapshot() if key in self.bridges else self.capabilities.snapshot(key)

    def write(self, key, sid, data):
        if key not in self.bridges:
            return self.capabilities.write(key, sid, data)
        self.bridges[key].write(sid, data)
        deadline = time.monotonic() + 6
        while True:
            state = self.snapshot(key)
            value = state[sid]
            if sid == 'delay':
                confirmed = value['action'] == (2 if data['action'] == 2 else 0)
            else:
                confirmed = all(name in value and abs(value[name] - requested) <= (1 if sid == 'brightness' else 0)
                                for name, requested in data.items())
            if confirmed:
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError('HA did not confirm the requested state')
            time.sleep(.05)
