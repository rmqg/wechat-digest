import ctypes
import ctypes.util
import importlib.util
from pathlib import Path
import select
import shutil
import subprocess
import unittest

MODULE = Path(__file__).with_name("send-report.py")
spec = importlib.util.spec_from_file_location("report_sender_x11", MODULE)
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("cursor", ctypes.c_ulong),
    ]


class X11WindowSafetyTests(unittest.TestCase):
    SCREEN_WIDTH = 1280
    SCREEN_HEIGHT = 900
    CALIBRATION = {
        "window": {"x": 181, "y": 117, "width": 917, "height": 667},
        "window_title_substring": "Weixin",
        "allowed_visible_window_titles": [],
    }
    OVERRIDE_REDIRECT = 1 << 9
    PROP_REPLACE = 0

    @classmethod
    def setUpClass(cls):
        xvfb = shutil.which("Xvfb")
        x11_path = ctypes.util.find_library("X11")
        if not xvfb or not x11_path or not ctypes.util.find_library("Xtst"):
            raise unittest.SkipTest("native X11 window tests require Xvfb, libX11 and libXtst")
        cls.xvfb = subprocess.Popen(
            [xvfb, "-displayfd", "1", "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        ready, _, _ = select.select([cls.xvfb.stdout], [], [], 10)
        if not ready:
            cls.xvfb.terminate()
            cls.xvfb.wait(timeout=5)
            raise unittest.SkipTest("Xvfb did not allocate a test display")
        number = cls.xvfb.stdout.readline().strip()
        if not number:
            cls.xvfb.terminate()
            cls.xvfb.wait(timeout=5)
            raise unittest.SkipTest("Xvfb did not report its test display")
        cls.display_name = f":{number}"
        cls.x = ctypes.CDLL(x11_path)
        cls._bind_test_xlib()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "xvfb", None) and cls.xvfb.poll() is None:
            cls.xvfb.terminate()
            cls.xvfb.wait(timeout=5)

    @classmethod
    def _bind_test_xlib(cls):
        x = cls.x
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        x.XCreateSimpleWindow.restype = ctypes.c_ulong
        x.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
        x.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XMoveResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_uint, ctypes.c_uint]
        x.XChangeWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                              ctypes.POINTER(XSetWindowAttributes)]
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XWarpPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
        x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                    ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]
        x.XQueryPointer.restype = ctypes.c_int
        x.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        x.XReparentWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        x.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
        x.XPending.argtypes = [ctypes.c_void_p]
        x.XPending.restype = ctypes.c_int

    def setUp(self):
        self.sender_x11 = sender.X11(self.display_name)
        self.display = self.x.XOpenDisplay(self.display_name.encode())
        self.root = self.x.XDefaultRootWindow(self.display)
        self.windows = []
        self.client_ids = []
        self._set_client_list()

    def tearDown(self):
        for window in reversed(self.windows):
            self.x.XDestroyWindow(self.display, window)
        self.client_ids.clear()
        self._set_client_list()
        self.x.XSync(self.display, 0)
        self.x.XCloseDisplay(self.display)
        self.sender_x11.close()

    def _atom(self, name):
        return int(self.x.XInternAtom(self.display, name.encode(), 0))

    def _sync(self):
        self.x.XSync(self.display, 0)

    def _set_client_list(self):
        values = (ctypes.c_ulong * len(self.client_ids))(*self.client_ids)
        self.x.XChangeProperty(
            self.display, self.root, self._atom("_NET_CLIENT_LIST"), self._atom("WINDOW"), 32,
            self.PROP_REPLACE, ctypes.cast(values, ctypes.POINTER(ctypes.c_ubyte)), len(self.client_ids),
        )
        self._sync()

    def _window(self, x, y, width=917, height=667, title="Weixin", *, managed=True,
                mapped=True, override_redirect=False):
        ident = int(self.x.XCreateSimpleWindow(self.display, self.root, x, y, width, height, 0, 0, 0))
        self.windows.append(ident)
        if title is not None:
            self.x.XStoreName(self.display, ident, title.encode())
        if override_redirect:
            attrs = XSetWindowAttributes()
            attrs.override_redirect = 1
            self.x.XChangeWindowAttributes(self.display, ident, self.OVERRIDE_REDIRECT, ctypes.byref(attrs))
        if managed:
            self.client_ids.append(ident)
            self._set_client_list()
        if mapped:
            self.x.XMapWindow(self.display, ident)
            self._sync()
        return ident

    def _reset_windows(self):
        for window in reversed(self.windows):
            self.x.XDestroyWindow(self.display, window)
        self.windows.clear()
        self.client_ids.clear()
        self._set_client_list()

    def _warp(self, x, y):
        self.x.XWarpPointer(self.display, 0, self.root, 0, 0, 0, 0, x, y)
        self._sync()

    def _pointer(self):
        root_return, child_return = ctypes.c_ulong(), ctypes.c_ulong()
        root_x, root_y, win_x, win_y = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        self.assertTrue(self.x.XQueryPointer(self.display, self.root, ctypes.byref(root_return),
                                             ctypes.byref(child_return), ctypes.byref(root_x),
                                             ctypes.byref(root_y), ctypes.byref(win_x),
                                             ctypes.byref(win_y), ctypes.byref(mask)))
        return root_x.value, root_y.value

    def test_live_origin_drives_full_screenshot_focus_and_click_at_screen_edges(self):
        for origin in ((179, 29), (0, 0), (363, 233)):
            with self.subTest(origin=origin):
                self._reset_windows()
                self._window(*origin)
                target = self.sender_x11.find_window(self.CALIBRATION)
                self.assertEqual((target["x"], target["y"]), origin)
                self.sender_x11.activate(target)
                self.assertTrue(self.sender_x11.focus_within(target))
                self.assertEqual(self.sender_x11.screenshot(target).size, (917, 667))
                point = (11, 17)
                self.sender_x11.click(target, list(point))
                self.assertEqual(self._pointer(), (origin[0] + point[0], origin[1] + point[1]))

    def test_missing_size_crop_hidden_and_ambiguous_candidates_are_distinguished(self):
        with self.subTest(case="missing"):
            self._window(179, 29, title="Other client")
            with self.assertRaisesRegex(RuntimeError, "Weixin client window not found"):
                self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        with self.subTest(case="wrong-size"):
            self._window(179, 29, width=916)
            with self.assertRaisesRegex(RuntimeError, "wrong size"):
                self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        with self.subTest(case="cropped"):
            self._window(364, 233)
            with self.assertRaisesRegex(RuntimeError, "cropped/offscreen"):
                self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        with self.subTest(case="hidden"):
            self._window(179, 29, mapped=False)
            with self.assertRaisesRegex(RuntimeError, "hidden or unmapped"):
                self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        with self.subTest(case="ambiguous-main-and-login"):
            self._window(179, 29)
            self._window(20, 20, width=292, height=396, title="Weixin login")
            with self.assertRaisesRegex(RuntimeError, "ambiguous Weixin"):
                self.sender_x11.find_window(self.CALIBRATION)

    def test_managed_client_frame_and_nonoverlapping_toolbar_do_not_block(self):
        frame = self._window(179, 29, title=None, managed=False, override_redirect=True)
        client = self._window(0, 0)
        self.x.XReparentWindow(self.display, client, frame, 0, 0)
        self.x.XMapWindow(self.display, client)
        self._window(1, 879, 1278, 20, title=None, managed=False, override_redirect=True)
        self._sync()
        target = self.sender_x11.find_window(self.CALIBRATION)
        self.assertEqual((target['window_id'], target['x'], target['y']), (client, 179, 29))
        self.sender_x11.activate(target)
        self.sender_x11.click(target, [20, 20])
        self.assertEqual(self._pointer(), (199, 49))
        self._window(400, 200, 100, 80, title=None, managed=False, override_redirect=True)
        self._warp(5, 6)
        with self.assertRaises(RuntimeError):
            self.sender_x11.click(target, [20, 20])
        self.assertEqual(self._pointer(), (5, 6))

    def test_approved_managed_popup_frame_inherits_its_client_policy(self):
        main = self._window(179, 29)
        frame = self._window(255, 104, 333, 67, title=None, managed=False, override_redirect=True)
        popup = self._window(0, 0, 333, 67, title="Approved search popup")
        self.x.XReparentWindow(self.display, popup, frame, 0, 0)
        self.x.XMapWindow(self.display, popup)
        self._sync()
        config = {**self.CALIBRATION, "allowed_visible_window_titles": ["Approved search popup"]}
        self.assertEqual(self.sender_x11.find_window(config)["window_id"], main)
        self.x.XStoreName(self.display, popup, b"Untrusted managed popup")
        self._sync()
        with self.assertRaises(RuntimeError):
            self.sender_x11.find_window(config)

    def test_unknown_titled_empty_title_and_override_redirect_windows_stop(self):
        self._window(179, 29)
        self._window(900, 10, 140, 80, title="Unexpected dialog")
        with self.assertRaisesRegex(RuntimeError, "unexpected visible window/dialog"):
            self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        self._window(179, 29)
        self._window(900, 10, 140, 80, title=None)
        with self.assertRaisesRegex(RuntimeError, "unexpected visible window/dialog"):
            self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        self._window(179, 29)
        self._window(400, 200, 200, 100, title=None, managed=False, override_redirect=True)
        with self.assertRaisesRegex(RuntimeError, "unexpected visible window/dialog"):
            self.sender_x11.find_window(self.CALIBRATION)
        self._reset_windows()
        self._window(179, 29)
        self._window(900, 10, 140, 80, title="Allowed dialog")
        config = {**self.CALIBRATION, "allowed_visible_window_titles": ["Allowed dialog"]}
        self.assertEqual(self.sender_x11.find_window(config)["title"], "Weixin")

    def test_motion_resize_hide_and_replaced_instance_refuse_before_click(self):
        for case in ("move", "resize", "hide", "replace"):
            with self.subTest(case=case):
                self._reset_windows()
                ident = self._window(179, 29)
                target = self.sender_x11.find_window(self.CALIBRATION)
                self._warp(5, 6)
                if case == "move":
                    self.x.XMoveResizeWindow(self.display, ident, 180, 29, 917, 667)
                elif case == "resize":
                    self.x.XMoveResizeWindow(self.display, ident, 179, 29, 918, 667)
                elif case == "hide":
                    self.x.XUnmapWindow(self.display, ident)
                else:
                    self.x.XDestroyWindow(self.display, ident)
                    self.windows.remove(ident)
                    self.client_ids.remove(ident)
                    self._window(179, 29)
                self._sync()
                attempts = []
                message = {"move": "moved during delivery", "resize": "size changed",
                           "hide": "hidden or unmapped", "replace": "target instance changed"}[case]
                with self.assertRaisesRegex(RuntimeError, message):
                    self.sender_x11.click(target, [20, 30], on_submit_state=lambda started: attempts.append(started))
                self.assertEqual(attempts, [])
                self.assertEqual(self._pointer(), (5, 6))

    def test_popup_after_lookup_and_callback_boundary_use_native_pointer_input(self):
        self._window(179, 29)
        target = self.sender_x11.find_window(self.CALIBRATION)
        self._warp(5, 6)
        self._window(400, 200, 200, 100, title=None, managed=False, override_redirect=True)
        attempts = []
        with self.assertRaisesRegex(RuntimeError, "unexpected visible window/dialog"):
            self.sender_x11.click(target, [20, 30], on_submit_state=lambda started: attempts.append(started))
        self.assertEqual(attempts, [])
        self.assertEqual(self._pointer(), (5, 6))

        self._reset_windows()
        origin = (179, 29)
        self._window(*origin)
        target = self.sender_x11.find_window(self.CALIBRATION)
        attempts = []
        self.sender_x11.click(target, [20, 30], on_submit_state=lambda started: attempts.append(started))
        self.assertEqual(attempts, [True])
        self.assertEqual(self._pointer(), (origin[0] + 20, origin[1] + 30))

    def test_button_press_c_result_controls_submit_state_and_release_cleanup(self):
        self._window(179, 29)
        target = self.sender_x11.find_window(self.CALIBRATION)
        native = self.sender_x11.xtst.XTestFakeButtonEvent
        native_ungrab = self.sender_x11.x.XUngrabServer
        submitted = {"value": False}

        def set_submitted(value):
            submitted["value"] = value

        try:
            calls = []

            def rejected(display, button, pressed, delay):
                calls.append(pressed)
                return 0 if pressed else 1

            self.sender_x11.xtst.XTestFakeButtonEvent = rejected
            with self.assertRaisesRegex(RuntimeError, "no button event was accepted"):
                self.sender_x11.click(target, [20, 30], on_submit_state=set_submitted)
            self.assertFalse(submitted["value"])
            self.assertEqual(calls, [1])

            submitted["value"] = False
            calls.clear()
            outcomes = iter((1, 0, 1))

            def release_rejected(display, button, pressed, delay):
                calls.append(pressed)
                return next(outcomes)

            self.sender_x11.xtst.XTestFakeButtonEvent = release_rejected
            with self.assertRaisesRegex(RuntimeError, "release failed after button-down"):
                self.sender_x11.click(target, [20, 30], on_submit_state=set_submitted)
            self.assertTrue(submitted["value"])
            self.assertEqual(calls, [1, 0, 0])

            submitted["value"] = False
            calls.clear()

            def press_raises(display, button, pressed, delay):
                calls.append(pressed)
                if pressed:
                    raise OSError("injected native press failure")
                return 1

            self.sender_x11.xtst.XTestFakeButtonEvent = press_raises
            with self.assertRaisesRegex(OSError, "injected native press failure"):
                self.sender_x11.click(target, [20, 30], on_submit_state=set_submitted)
            self.assertTrue(submitted["value"])
            self.assertEqual(calls, [1, 0])
            submitted["value"] = False
            calls.clear()
            ungrabs = []

            def tracked_ungrab(display):
                ungrabs.append(display)
                return native_ungrab(display)

            def cleanup_release_raises(display, button, pressed, delay):
                calls.append(pressed)
                if pressed:
                    return 1
                if calls.count(0) == 1:
                    return 0
                raise OSError("injected cleanup release failure")

            self.sender_x11.x.XUngrabServer = tracked_ungrab
            self.sender_x11.xtst.XTestFakeButtonEvent = cleanup_release_raises
            with self.assertRaisesRegex(OSError, "injected cleanup release failure"):
                self.sender_x11.click(target, [20, 30], on_submit_state=set_submitted)
            self.assertTrue(submitted["value"])
            self.assertEqual(calls, [1, 0, 0])
            self.assertEqual(len(ungrabs), 1)
        finally:
            self.sender_x11.xtst.XTestFakeButtonEvent = native
            self.sender_x11.x.XUngrabServer = native_ungrab

    def test_keyboard_input_is_refused_if_focus_moves_to_an_allowed_other_window(self):
        self._window(179, 29)
        other = self._window(950, 20, 240, 160, title="Allowed dialog")
        config = {**self.CALIBRATION, "allowed_visible_window_titles": ["Allowed dialog"]}
        self.sender_x11.find_window(config)
        self.x.XSelectInput(self.display, other, 1 << 0)
        self.x.XSetInputFocus(self.display, other, 2, 0)
        self._sync()
        with self.assertRaisesRegex(RuntimeError, "lost input focus"):
            self.sender_x11.key("a", True)
        with self.assertRaisesRegex(RuntimeError, "lost input focus"):
            self.sender_x11.type_ascii("a")
        self._sync()
        self.assertEqual(self.x.XPending(self.display), 0)


if __name__ == "__main__":
    unittest.main()
