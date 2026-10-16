"""Constants for hwhomebridge integration."""

DOMAIN = "hwhomebridge"

CONF_BACKEND = "backend"
CONF_OH_DIRECTORY = "openharmony_directory"
BACKEND_LEGACY = "legacy"
BACKEND_OPENHARMONY = "openharmony"
BACKEND_DISABLED = "disabled"
BACKENDS = (BACKEND_LEGACY, BACKEND_OPENHARMONY, BACKEND_DISABLED)


def configured_backend(entry):
    """旧配置没有字段时保持原后端；未知值不自动回退。"""
    backend = entry.options.get(CONF_BACKEND, entry.data.get(CONF_BACKEND, BACKEND_LEGACY))
    if backend not in BACKENDS:
        raise ValueError("unknown bridge backend")
    return backend

# 来自这些集成（platform）的实体不做映射，避免循环接入。
# 例如社区插件 ha-huawei-smarthome（domain: huawei_smarthome）将华为设备接入 HA，
# 若再次桥接回华为会导致设备循环。
SKIP_PLATFORMS = {"huawei_smarthome"}
