"""hwhomebridge integration entry point."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (DOMAIN, BACKEND_LEGACY, BACKEND_OPENHARMONY, BACKEND_DISABLED,
                    CONF_OH_DIRECTORY, configured_backend)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict):
    """准备组件；配置流确认后才启动选定后端，保留显式 YAML 旧入口。"""
    _LOGGER.info("Preparing hwhomebridge integration")
    hass.data.setdefault(DOMAIN, {})
    # 配置流加载组件时不提前启动旧库；显式 YAML 入口保留旧行为。
    if DOMAIN in config and not hass.config_entries.async_entries(DOMAIN):
        await _start_legacy_once(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """按明确保存的后端启动；旧条目默认使用原 HiLink。"""
    data = hass.data.setdefault(DOMAIN, {})
    try:
        mode = configured_backend(entry)
    except ValueError as error:
        raise ConfigEntryNotReady("Unknown bridge backend; select a backend explicitly") from error
    if mode != BACKEND_LEGACY and data.get("legacy_attempted"):
        raise ConfigEntryNotReady("Legacy SDK is resident; restart Home Assistant to switch backend")
    runtime = None
    if mode == BACKEND_OPENHARMONY:
        from .openharmony.deployment import load_deployment
        from .openharmony.ha_backend import HAOpenHarmonyBackend
        directory = entry.options.get(CONF_OH_DIRECTORY, entry.data.get(CONF_OH_DIRECTORY, ""))
        try:
            spec = await hass.async_add_executor_job(load_deployment, hass.config.config_dir, directory)
            runtime = HAOpenHarmonyBackend(hass, spec)
            runtime.relative_directory = directory
            await runtime.start()
        except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as error:
            raise ConfigEntryNotReady("OpenHarmony not ready; check its isolated directory, entities and ports") from error
    elif mode == BACKEND_LEGACY:
        await _start_legacy_once(hass)
        await _setup_legacy_entry(hass, entry)
    data[entry.entry_id] = {"backend": mode, "runtime": runtime}
    data["last_backend"] = mode
    entry.async_on_unload(entry.add_update_listener(_backend_options_updated))
    if runtime:
        async def stop_on_shutdown(_event):
            await runtime.stop()
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_on_shutdown))
        runtime.watch(entry.entry_id)
    return True


async def _start_legacy_once(hass):
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("legacy_attempted"):
        data["legacy_attempted"] = True
        from .hwbridge import start_hw_hilink_bridge
        await start_hw_hilink_bridge(hass)


async def _backend_options_updated(hass, entry):
    data = hass.data[DOMAIN]
    current = data.get(entry.entry_id)
    desired = configured_backend(entry)
    if data.get("legacy_attempted") and desired != BACKEND_LEGACY:
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(hass,
            "后端选择已保存。旧后端无法在当前进程中完全卸载，请重启 Home Assistant 后应用新的选择。",
            title="HuaweiHome Bridge：需要重启", notification_id="hwhomebridge_backend_restart")
        return
    if current is None:
        if entry.disabled_by is None:
            await hass.config_entries.async_reload(entry.entry_id)
        return
    directory = entry.options.get(CONF_OH_DIRECTORY, entry.data.get(CONF_OH_DIRECTORY))
    if desired != current["backend"] or (desired == BACKEND_OPENHARMONY and directory != current["runtime"].relative_directory):
        await hass.config_entries.async_reload(entry.entry_id)


async def _setup_legacy_entry(hass, entry):
    from .hwbridge import service_router, _create_bridge_device_entry_async, register_hilink_callbacks

    # 重载集成时 async_unload_entry 已 nullify 回调，这里重新注册
    register_hilink_callbacks()

    dev_reg = dr.async_get(hass)

    # 为在 async_setup_entry 之前已注册的设备补建 HA 设备注册表条目
    if service_router is not None:
        for vd in service_router.sn_manager.get_all_devices():
            device_entry = dev_reg.devices.get(vd.ha_device_id)
            name = ""
            model = ""
            manufacturer = ""
            if device_entry:
                name = device_entry.name_by_user or device_entry.name or ""
                model = device_entry.model or ""
                manufacturer = device_entry.manufacturer or ""
            _create_bridge_device_entry_async(vd, name, model, manufacturer)

    _LOGGER.info("hwhomebridge config entry setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """停止自有 OpenHarmony 子进程，或按原方式置空旧库回调。"""
    data = hass.data.setdefault(DOMAIN, {})
    active = data.get(entry.entry_id)
    if active and active["backend"] == BACKEND_OPENHARMONY:
        await active["runtime"].stop()
    elif active and active["backend"] == BACKEND_LEGACY:
        from .hwbridge import nullify_hilink_callbacks
        nullify_hilink_callbacks()
    data.pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """集成被永久删除时调用：删除网关、清理持久化文件

    HA 删除流程：先 async_unload_entry（移除实体状态，C 库仍存活），
    再 async_remove_entry（此时 C 库可用，exit_brg 可正常调用）。
    """
    # OpenHarmony 删除配置只停止自有进程，绝不解绑或清理配对目录。
    if hass.data.get(DOMAIN, {}).get("last_backend") != BACKEND_LEGACY:
        return
    from .hwbridge import stop_hw_hilink_bridge

    # 根据语言选择通知文案
    lang = hass.config.language or "en"
    if lang.startswith("zh"):
        notif_title = "请重启 Home Assistant"
        notif_message = (
            "HuaweiHome Bridge 已删除，如需重新添加本集成，请先重启 Home Assistant。"
            "否则本集成将无法重新正常工作。"
        )
    else:
        notif_title = "Please restart Home Assistant"
        notif_message = (
            "HuaweiHome Bridge has been removed. If you want to re-add this integration, "
            "please restart Home Assistant first, otherwise the integration will not work properly."
        )

    # 先创建重启通知（在 exit_brg 之前，防止 SDK 退出导致进程崩溃后通知丢失）
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": notif_title,
                "message": notif_message,
                "notification_id": "hwhomebridge_restart_required",
            },
            blocking=True,
        )
        _LOGGER.info("Restart notification created successfully")
    except Exception as e:
        _LOGGER.error(f"Failed to create restart notification: {e}")
    finally:
        # 确保无论通知是否成功，都执行清理
        await hass.async_add_executor_job(stop_hw_hilink_bridge, hass)
        _LOGGER.info("hwhomebridge integration removed, gateway cleaned up from cloud")
