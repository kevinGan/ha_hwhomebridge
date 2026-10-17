"""独立虚拟实验的互斥 SDK 所有者；正常停止不会调用解绑接口。"""
import argparse
from collections import deque
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

from single_engine_fixture import private_root, save


class LegacyCallbacks:
    """SDK小栈线程仅投递命令或读取缓存；HTTP与SDK调用由主线程完成。"""
    def __init__(self, child_sn, services, gateway, state, snapshot):
        self.pending = deque()
        self.update(snapshot)
        self.buffers = {sid: C.create_string_buffer(1024) for sid in services}
        @C.CFUNCTYPE(None, C.c_char_p, C.c_char_p)
        def action(sn, payload):
            state['action_callbacks'] = state.get('action_callbacks', 0) + 1
            if sn != child_sn or not payload:
                return
            # SDK线程栈很小；回调禁止HTTP、JSON转换或再次调用SDK。
            if len(self.pending) < 32:
                self.pending.append(payload)
            else:
                state['light_write_rejected'] = state.get('light_write_rejected', 0) + 1
        @C.CFUNCTYPE(C.c_int, C.c_char_p)
        def kind(sn):
            return 1 if sn == child_sn else -1
        @C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_char_p)
        def status(sn, sid):
            if sn != child_sn or not sid:
                return 0
            try:
                values = self.cache.get(sid.decode(), {})
                return int(next(iter(values.values()), 0))
            except Exception:
                return 0
        @C.CFUNCTYPE(None, C.c_int)
        def notify(value):
            state['last_notification'] = value
        @C.CFUNCTYPE(C.c_void_p, C.c_char_p, C.c_char_p)
        def get(sn, sid):
            try:
                name = sid.decode() if sid else ''
                if sn != child_sn or name not in self.buffers:
                    state['unsupported_read_count'] = state.get('unsupported_read_count', 0) + 1
                    return 0
                self.buffers[name].value = self.encoded[name]
                return C.addressof(self.buffers[name])
            except Exception:
                return 0
        @C.CFUNCTYPE(C.c_int)
        def pin():
            state['pin_callbacks'] = state.get('pin_callbacks', 0) + 1
            return int(gateway['pin']) if time.time() < gateway['pin_expires_at'] else 0
        self.action, self.kind, self.status = action, kind, status
        self.notify, self.get, self.pin = notify, get, pin

    def update(self, snapshot):
        encoded = {sid: json.dumps(value, separators=(',', ':')).encode() for sid, value in snapshot.items()}
        if any(len(value) >= 1024 for value in encoded.values()):
            raise ValueError('legacy cached response too large')
        self.cache, self.encoded = snapshot, encoded


def verified_import(root, directory, binary, gateway):
    """显式实验接管：源快照、原生解密结果与旧子设备ID必须同时一致。"""
    if directory.parent != root / 'offline-import' or directory.is_symlink():
        raise ValueError('import must be inside the independent evaluation directory')
    receipt = json.loads((directory / 'import-receipt.json').read_text())
    if receipt.get('version') != 1 or set(receipt['source_sha256']) != {'config.cfg', 'bridge.cfg'}:
        raise ValueError('unsupported offline import receipt')
    for path in directory.rglob('*'):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private owned import artifacts required')
    for name, expected in receipt['source_sha256'].items():
        if name not in ('config.cfg', 'bridge.cfg') or hashlib.sha256((root / 'legacy' / name).read_bytes()).hexdigest() != expected:
            raise ValueError('legacy paired source changed after offline conversion')
    imported = json.loads((directory / 'gateway.local.json').read_text())
    if any(imported[key] != gateway[key] for key in ('sn', 'ac_key_hex', 'prodId', 'model', 'manuName')):
        raise ValueError('imported gateway identity mismatch')
    result = subprocess.run([str(binary.parent / 'identity_state_probe'), str(directory / 'sdk-state'),
                             str(directory / 'gateway.local.json')], capture_output=True, text=True, check=True, timeout=15)
    rows = [json.loads(line[15:]) for line in result.stdout.splitlines() if line.startswith('IDENTITY_PROBE ')]
    if len(rows) != 1:
        raise ValueError('exactly one native identity result required')
    identity = rows[0]
    if (not identity['bound_identity_loaded'] or identity['dev_id_sha256'] != receipt['root_id_sha256']
            or identity['fields'] != receipt['account_field_sha256']):
        raise ValueError('native reader did not confirm the imported legacy login identity')
    catalog = json.loads((directory / 'ha-children.local.json').read_text())
    child = json.loads((directory / 'ha-child-legacy-light.json').read_text())
    if len(catalog) != 1 or catalog[0].get('existing_cloud_only') is not True or catalog[0]['sn'] != child['sn']:
        raise ValueError('import must use only the existing cloud child')
    for field, expected in [('devId', 'child_id_sha256'), ('sn', 'child_sn_sha256')]:
        if hashlib.sha256(child[field].encode()).hexdigest() != receipt[expected]:
            raise ValueError('imported child identity mismatch')
    return directory, directory / 'gateway.local.json'


def main(root, backend, binary, import_directory=None):
    private_root(root)
    lock = (root / 'engine-owner.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gateway = json.loads((root / 'gateway.local.json').read_text())
    if not gateway['sn'].startswith('OHPOC-') or len(gateway['sn']) != 16:
        raise ValueError('independent short test identity required')
    runtime, gateway_file = root / 'runtime', root / 'gateway.local.json'
    # 禁止仅复制SN后启动新引擎；显式导入必须由原生离线读取器确认。
    if backend == 'openharmony' and any(
        path.is_file() and path.stat().st_size
        for path in (root / 'legacy/config.cfg', root / 'legacy/config_bak.cfg')
    ):
        if import_directory is None:
            raise ValueError('paired legacy identity import is not verified; new engine refused before startup')
        runtime, gateway_file = verified_import(root, import_directory, binary, gateway)
    state = {'backend': backend, 'pid': os.getpid(), 'root_sn_sha256': hashlib.sha256(gateway['sn'].encode()).hexdigest(),
             'SDK_unbind_called': False, 'virtual_only': True}
    save(root / 'engine-owner.json', state)
    stop = False
    process = None
    def terminate(*_):
        nonlocal stop
        stop = True
        if process is not None:
            process.terminate()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, terminate)
    if backend == 'openharmony':
        env = {**os.environ, 'POC_INTERFACE': 'eth0', 'POC_LAN_PORT': '15684', 'POC_CLOUD_STATUS': '1'}
        process = subprocess.Popen([str(binary), str(runtime), '--live', str(gateway_file)], env=env)
        try:
            result = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        return result

    os.environ['HILINK_CONFIG_DIR'] = str(root / 'legacy') + '/'
    os.chdir(root / 'legacy')
    library = C.CDLL(str(binary))
    library.GetGatewaySN.restype = C.c_char_p
    library.HILINK_SetPreferredIface.argtypes = [C.c_char_p]
    library.HILINK_SetAutoAc.argtypes = [C.POINTER(C.c_ubyte), C.c_uint]
    library.HILINK_SetAutoAc.restype = C.c_int
    library.HilinkSyncBrgDevStatus.argtypes = [C.c_char_p, C.c_int]
    library.UpdateHAStatus.argtypes = [C.c_char_p, C.c_char_p, C.c_char_p]
    library.HILINK_SetPreferredIface(b'eth0')
    ac = (root / 'legacy/device_ac').read_bytes()
    if len(ac) != 48 or ac.hex() != gateway['ac_key_hex']:
        raise ValueError('independent test AC mismatch')
    ac_buffer = (C.c_ubyte * 48).from_buffer_copy(ac)
    if library.HILINK_SetAutoAc(ac_buffer, 48):
        raise ValueError('test SDK AC setup failed')
    catalog = json.loads((root / 'runtime/ha-children.local.json').read_text())
    child = next(item for item in catalog if item['key'] == 'legacy-light')
    child_sn = child['sn'].encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def bridge(sid=None, data=None):
        payload = {'key': 'legacy-light'}
        if sid is not None:
            payload.update(sid=sid, data=data)
        request = urllib.request.Request('http://127.0.0.1:18089/bridge', data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        response = json.load(opener.open(request, timeout=10))
        if not response['ok']:
            raise ValueError('virtual backend rejected request')
        return response['state']
    fields = {'on': 'switch', 'brightness': 'brightness', 'colorTemperature': 'cct'}
    for attempt in range(120):
        try:
            snapshot = bridge()
            break
        except (OSError, ValueError):
            if stop or attempt == 119:
                raise RuntimeError('virtual backend did not become ready') from None
            time.sleep(.5)
    callbacks = LegacyCallbacks(child_sn, child['services'], gateway, state, snapshot)
    library.RegHomeAssistantPyCB(callbacks.action, callbacks.kind, callbacks.status, callbacks.notify, callbacks.get)
    library.RegPINQueryCallback(callbacks.pin)
    if library.main() != 0:
        raise RuntimeError('legacy test SDK startup failed')
    registered = False
    reported = {}
    next_poll = 0
    while not stop:
        # 主线程拥有正常Python栈；SDK回调只使用上述缓存和有界队列。
        while callbacks.pending:
            try:
                values = json.loads(callbacks.pending.popleft())
                if not isinstance(values, dict) or len(values) != 1 or next(iter(values)) not in fields:
                    raise ValueError('unsupported legacy light command')
                cache = bridge(fields[next(iter(values))], values)
                callbacks.update(cache)
                state['light_write_count'] = state.get('light_write_count', 0) + 1
            except Exception:
                state['light_write_rejected'] = state.get('light_write_rejected', 0) + 1
        if time.monotonic() < next_poll:
            time.sleep(.05)
            continue
        next_poll = time.monotonic() + 1
        try:
            cache = bridge()
            callbacks.update(cache)
        except (OSError, ValueError):
            state['backend_read_failures'] = state.get('backend_read_failures', 0) + 1
        sn = library.GetGatewaySN()
        if sn and sn.decode() != gateway['sn']:
            raise ValueError('legacy SDK did not preserve the supplied test SN')
        state['native_identity_confirmed'] = bool(sn)
        state['device_status'] = library.HILINK_GetDevStatus()
        if state['device_status'] == 1 and not registered:
            # 已注册身份仅上线；首次新增测试灯才使用DEV_ADD。
            saved = root / 'legacy-child-submitted.json'
            if not saved.exists():
                library.HilinkSyncBrgDevStatus(child_sn, 3)
                save(saved, {'sn_sha256': hashlib.sha256(child_sn).hexdigest()})
            library.HilinkSyncBrgDevStatus(child_sn, 1)
            registered = True
        if state['device_status'] == 1 and registered:
            for sid, value in callbacks.encoded.items():
                if reported.get(sid) != value:
                    library.UpdateHAStatus(child_sn, sid.encode(), value)
                    reported[sid] = value
        state['child_registration_submitted'] = registered
        save(root / 'engine-owner.json', state)
    state['stopped_without_unbind'] = True
    save(root / 'engine-owner.json', state)
    # SDK线程仍驻留，避免Python析构释放其持有的回调；由OS回收整个独立进程。
    os._exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', choices=('legacy', 'openharmony'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--import-directory', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    raise SystemExit(main(args.directory.resolve(), args.backend, args.binary.resolve(),
                         args.import_directory.absolute() if args.import_directory else None))
