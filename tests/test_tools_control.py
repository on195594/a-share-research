"""Local IPC and admission tests; no production endpoint or HTTP credential is used."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import stat
import subprocess
import sys
import threading

import pytest

from a_share_research.tools.control import ControlError, ControlServer, Gate, client, pause_offline


def test_admission_limit_and_exception_release():
    gate=Gate()
    assert gate.status()=={'paused':False,'in_flight':0}
    with gate.acquire(), gate.acquire():
        assert gate.status()['in_flight']==2
        with pytest.raises(ControlError) as error:
            with gate.acquire():
                pytest.fail('full gate admitted request')
        assert (error.value.code,error.value.status)==('busy',503)
    with pytest.raises(RuntimeError,match='request failed'):
        with gate.acquire():
            raise RuntimeError('request failed')
    assert gate.status()['in_flight']==0


def test_pause_waits_for_admitted_request_and_rejects_new_work(tmp_path):
    gate=Gate(tmp_path/'paused')
    admitted, release, paused = threading.Event(),threading.Event(),threading.Event()
    def work():
        with gate.acquire():
            admitted.set()
            assert release.wait(3)
    original=gate.status
    def pause():
        paused.set()
        return gate.pause(3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker=pool.submit(work)
        assert admitted.wait(1)
        waiter=pool.submit(pause)
        assert paused.wait(1)
        # Acquiring the condition proves pause reached its wait without arbitrary sleeps.
        with gate._condition:
            assert gate._condition.wait_for(lambda: gate._paused, timeout=1)
        assert original()=={'paused':True,'in_flight':1}
        assert not waiter.done()
        with pytest.raises(ControlError) as error:
            with gate.acquire():
                pytest.fail('paused gate admitted request')
        assert error.value.code=='paused' and error.value.status==503
        release.set()
        worker.result(timeout=2)
        assert waiter.result(timeout=2)=={'paused':True,'in_flight':0}
    assert gate.resume()=={'paused':False,'in_flight':0}


def test_timeout_keeps_marker_and_pause_after_request_finishes(tmp_path):
    marker=tmp_path/'paused'
    gate=Gate(marker)
    with gate.acquire():
        with pytest.raises(ControlError) as error:
            gate.pause(0)
        assert error.value.code=='timeout'
    assert gate.status()=={'paused':True,'in_flight':0}
    assert Gate(marker).status()['paused']
    assert stat.S_IMODE(marker.stat().st_mode)==0o600
    assert gate.resume()=={'paused':False,'in_flight':0}
    assert not marker.exists()
    assert not Gate(marker).status()['paused']
    assert gate.resume()=={'paused':False,'in_flight':0}


def test_offline_pause_is_idempotent_and_survives_first_start(tmp_path):
    marker=tmp_path/'new'/'paused'
    assert not Gate(marker).status()['paused']
    pause_offline(marker)
    before=(marker.stat().st_ino,marker.read_bytes())
    pause_offline(marker)
    assert (marker.stat().st_ino,marker.read_bytes())==before
    gate=Gate(marker)
    assert gate.status()['paused']
    gate.resume()
    assert not marker.exists()


@pytest.mark.parametrize('kind',['unknown','symlink','mode','hardlink'])
def test_invalid_marker_is_never_overwritten_or_removed(tmp_path,kind):
    marker=tmp_path/'paused'
    gate=Gate(marker)
    target=tmp_path/'target'
    if kind=='symlink':
        target.write_text('do not delete')
        marker.symlink_to(target)
    elif kind=='unknown':
        marker.write_text('not our marker')
        marker.chmod(0o600)
    else:
        pause_offline(marker)
        if kind=='mode':
            marker.chmod(0o644)
        else:
            os.link(marker,target)
    before=marker.read_bytes()
    for action in (lambda:Gate(marker),lambda:pause_offline(marker),lambda:gate.pause(0),gate.resume):
        with pytest.raises(ControlError,match='marker'):
            action()
        assert marker.read_bytes()==before
        assert marker.is_symlink()==(kind=='symlink')
    assert gate.status()['paused']


def test_parent_symlink_is_not_followed(tmp_path):
    actual=tmp_path/'actual'
    actual.mkdir()
    alias=tmp_path/'alias'
    alias.symlink_to(actual,target_is_directory=True)
    with pytest.raises(OSError):
        pause_offline(alias/'paused')
    assert not (actual/'paused').exists()


def test_real_unix_client_and_cli(tmp_path):
    path=tmp_path/'control.sock'
    gate=Gate(tmp_path/'paused')
    with ControlServer(gate,path):
        assert stat.S_IMODE(path.stat().st_mode)==0o600
        assert client(path,'status')=={'paused':False,'in_flight':0}
        assert client(path,'pause')['paused']
        completed=subprocess.run([sys.executable,'-m','a_share_research.tools.control','status','--socket',str(path)],
                                 capture_output=True,text=True,timeout=5)
        assert completed.returncode==0
        assert json.loads(completed.stdout)=={'paused':True,'in_flight':0}
        assert not client(path,'resume')['paused']
        with pytest.raises(ControlError) as error:
            client(path,'unknown')
        assert error.value.code=='invalid_command'
    assert not path.exists()
    failed=subprocess.run([sys.executable,'-m','a_share_research.tools.control','status','--socket',str(path)],
                          capture_output=True,text=True,timeout=5)
    assert failed.returncode!=0 and 'error' in json.loads(failed.stdout)


def test_unix_pause_timeout_remains_paused(tmp_path):
    gate=Gate(tmp_path/'paused')
    with ControlServer(gate,tmp_path/'control.sock'), gate.acquire():
        with pytest.raises(ControlError) as error:
            client(tmp_path/'control.sock','pause',timeout=0)
        assert error.value.code=='timeout'
        assert client(tmp_path/'control.sock','status')=={'paused':True,'in_flight':1}
    assert gate.status()['paused']


@pytest.mark.parametrize('payload',[b'not-json\n',b'[]\n',b'{"action":"status","authorization":"bearer"}\n',
                                    b'{"action":"pause","timeout":-1}\n',b'{"action":"pause","timeout":NaN}\n',
                                    b'x'*4097+b'\n'])
def test_invalid_protocol_is_rejected_without_stopping_server(tmp_path,payload):
    path=tmp_path/'control.sock'
    with ControlServer(Gate(),path):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(payload)
            with connection.makefile('rb') as stream:
                assert json.loads(stream.readline())['error']['code']=='invalid_command'
        assert client(path,'status')=={'paused':False,'in_flight':0}


def test_socket_directory_must_be_private_and_existing_files_are_preserved(tmp_path):
    public=tmp_path/'public'
    public.mkdir(mode=0o755)
    with pytest.raises(ControlError,match='0700'):
        with ControlServer(Gate(),public/'control.sock'):
            pass
    path=tmp_path/'control.sock'
    path.write_text('unrelated file')
    with pytest.raises(OSError):
        with ControlServer(Gate(),path):
            pass
    assert path.read_text()=='unrelated file'


def test_shutdown_does_not_unlink_a_replacement_socket_path(tmp_path):
    path=tmp_path/'control.sock'
    with ControlServer(Gate(),path):
        path.unlink()
        path.write_text('replacement')
    assert path.read_text()=='replacement'


@pytest.mark.parametrize('timeout',[-1,float('inf'),float('nan'),True,'1',10**400])
def test_invalid_timeout_cannot_change_admission(timeout):
    gate=Gate()
    with pytest.raises(ControlError):
        gate.pause(timeout)
    assert not gate.status()['paused']
