"""独立虚拟实验的互斥 SDK 所有者；正常停止不会调用解绑接口。"""
import argparse
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


def main(root, backend, binary):
    private_root(root)
    lock = (root / 'engine-owner.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gateway = json.loads((root / 'gateway.local.json').read_text())
    if not gateway['sn'].startswith('OHPOC-') or len(gateway['sn']) != 16:
        raise ValueError('independent short test identity required')
    # 旧SDK已配对的持久化资料尚无经过验证的解码/导入器。
    # 禁止仅复制SN后启动新引擎，否则会把重新绑定误当作身份接管。
    if backend == 'openharmony' and any(
        path.is_file() and path.stat().st_size
        for path in (root / 'legacy/config.cfg', root / 'legacy/config_bak.cfg')
    ):
        raise ValueError('paired legacy identity import is not verified; new engine refused before startup')
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
        process = subprocess.Popen([str(binary), str(root / 'runtime'), '--live', str(root / 'gateway.local.json')], env=env)
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
    buffers = {sid: C.create_string_buffer(1024) for sid in child['services']}
    @C.CFUNCTYPE(None, C.c_char_p, C.c_char_p)
    def action(sn, payload):
        state['action_callbacks'] = state.get('action_callbacks', 0) + 1
        if sn != child_sn or not payload:
            return
        try:
            values = json.loads(payload)
            if len(values) != 1 or next(iter(values)) not in fields:
                raise ValueError('unsupported legacy light command')
            bridge(fields[next(iter(values))], values)
            state['light_write_count'] = state.get('light_write_count', 0) + 1
        except Exception:
            state['light_write_rejected'] = state.get('light_write_rejected', 0) + 1
    @C.CFUNCTYPE(C.c_int, C.c_char_p)
    def kind(sn):
        return 1 if sn == child_sn else -1
    @C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_char_p)
    def status(sn, sid):
        if sn != child_sn or not sid:
            return 0
        try:
            values = bridge().get(sid.decode(), {})
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
            if sn != child_sn or name not in buffers:
                state['unsupported_read_count'] = state.get('unsupported_read_count', 0) + 1
                return 0
            buffers[name].value = json.dumps(bridge()[name], separators=(',', ':')).encode()
            return C.addressof(buffers[name])
        except Exception:
            return 0
    @C.CFUNCTYPE(C.c_int)
    def pin():
        state['pin_callbacks'] = state.get('pin_callbacks', 0) + 1
        return int(gateway['pin']) if time.time() < gateway['pin_expires_at'] else 0
    library.RegHomeAssistantPyCB(action, kind, status, notify, get)
    library.RegPINQueryCallback(pin)
    if library.main() != 0:
        raise RuntimeError('legacy test SDK startup failed')
    registered = False
    while not stop:
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
        state['child_registration_submitted'] = registered
        save(root / 'engine-owner.json', state)
        time.sleep(1)
    state['stopped_without_unbind'] = True
    save(root / 'engine-owner.json', state)
    # SDK线程仍驻留，避免Python析构释放其持有的回调；由OS回收整个独立进程。
    os._exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', choices=('legacy', 'openharmony'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('binary', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    raise SystemExit(main(args.directory.resolve(), args.backend, args.binary.resolve()))
