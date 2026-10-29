"""Linux-only integration regression: real GDB, synthetic process, no WeChat data."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from capture_41 import _gdb_script, release_debugger

CHILD = r'''
import ctypes,json,mmap,os,signal,sys,threading,time
from pathlib import Path
root=Path(sys.argv[1])
code=mmap.mmap(-1,4096,prot=mmap.PROT_READ|mmap.PROT_WRITE|mmap.PROT_EXEC)
code.write(b"\xc3")
address=ctypes.addressof(ctypes.c_char.from_buffer(code))
call=ctypes.CFUNCTYPE(None,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_uint64)(address)
value=ctypes.create_string_buffer(b"S"*32)
signal.signal(signal.SIGUSR1,lambda signum,frame:None)
def beat():
 n=0
 while True:
  n+=1
  p=root/'heartbeat.tmp'; p.write_text(str(n)); p.replace(root/'heartbeat')
  time.sleep(.02)
threading.Thread(target=beat,daemon=True).start()
(root/'ready.json').write_text(json.dumps({'pid':os.getpid(),'address':address}))
while not (root/'trigger').exists(): time.sleep(.01)
call(0,ctypes.addressof(value),32)
(root/'continued').write_text('yes')
while True: time.sleep(.02)
'''


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Synthetic process did not make progress before deadline")


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("gdb") and os.environ.get("WECHAT_CAPTURE_INTEGRATION") == "1", "Run explicitly in the isolated Linux GDB container")
class CaptureIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.child = subprocess.Popen([sys.executable, "-c", CHILD, str(self.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.gdb = None
        self.log = None
        until(lambda: (self.root / "ready.json").exists())
        self.ready = json.loads((self.root / "ready.json").read_text())
        self.result = self.root / "candidates.json"

    def tearDown(self):
        if self.gdb is not None:
            try:
                release_debugger(self.gdb, self.child.pid)
            except (OSError, RuntimeError):
                if self.gdb.poll() is None:
                    self.gdb.kill()
                    self.gdb.wait(timeout=5)
        if self.log:
            self.log.close()
        self.child.terminate()
        self.child.wait(timeout=5)
        self.temporary.cleanup()

    def start_capture(self):
        script = self.root / "capture.gdb"
        script.write_text(_gdb_script(self.child.pid, self.ready["address"], self.result))
        self.log = (self.root / "gdb.log").open("w+")
        self.gdb = subprocess.Popen(["gdb", "-q", "--nx", "--batch", "-x", str(script)], stdout=self.log, stderr=subprocess.STDOUT)
        until(lambda: "CAPTURE_ARMED" in (self.root / "gdb.log").read_text())

    def assert_untraced(self):
        for task in Path(f"/proc/{self.child.pid}/task").iterdir():
            status = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in (task / "status").read_text().splitlines() if ":" in line}
            self.assertEqual(int(status["TracerPid"]), 0)

    def finish_capture(self):
        (self.root / "trigger").touch()
        self.gdb.wait(timeout=15)
        until(lambda: (self.root / "continued").exists())
        self.assertTrue(self.result.exists(), "No candidate captured; debugger may have stopped on an unrelated signal")
        self.assertEqual(json.loads(self.result.read_text()), [(b"S" * 32).hex()])
        self.assert_untraced()

    def test_capture_detaches_and_client_continues(self):
        self.start_capture()
        self.finish_capture()

    def test_runtime_signal_does_not_abort_capture(self):
        self.start_capture()
        before = int((self.root / "heartbeat").read_text())
        os.kill(self.child.pid, signal.SIGUSR1)
        until(lambda: int((self.root / "heartbeat").read_text()) > before + 2)
        self.finish_capture()

    def test_cancellation_releases_every_thread(self):
        self.start_capture()
        release_debugger(self.gdb, self.child.pid)
        self.assert_untraced()
        before = int((self.root / "heartbeat").read_text())
        until(lambda: int((self.root / "heartbeat").read_text()) > before + 2)
        self.assertFalse(self.result.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
