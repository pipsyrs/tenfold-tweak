"""GUI consent handoff: worker thread -> UI dialog -> answer back into core.run."""

import threading
import time
import unittest
from unittest import mock

from tests.helpers import make_image, scratch_dir
from tests.test_desktop import reset_state

from tenfold_tweak import core, detect, gui


class FakeDialog:
    """Stands in for Gtk.AlertDialog / Adw.AlertDialog; answers via choose_finish."""
    result = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def __getattr__(self, name):  # add_response, set_default_response, ...
        return lambda *a, **k: None

    def choose(self, parent, cancellable, callback):
        callback(self, "res")

    def choose_finish(self, res):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        err = gui._load_toolkit(use_adw=False)
        if err:
            raise unittest.SkipTest(err)

    def pump(self, until, timeout=5.0):
        ctx = gui.GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.005)

    def ask_from_worker(self, dash, message="msg"):
        out = []
        t = threading.Thread(target=lambda: out.append(gui.Dashboard.confirm_blocking(dash, message)))
        t.start()
        self.pump(lambda: out)
        t.join(1)
        self.assertFalse(t.is_alive(), "worker still blocked")
        return out


class ConfirmBlockingTest(Base):
    def dash(self, answer):
        d = mock.Mock()
        d._ask.side_effect = lambda message, finish: finish(answer)
        return d

    def test_continue_returns_true_and_message_reaches_dialog(self):
        d = self.dash(True)
        self.assertEqual(self.ask_from_worker(d, core.GDM_WARNING), [True])
        self.assertEqual(d._ask.call_args.args[0], core.GDM_WARNING)

    def test_cancel_returns_false(self):
        self.assertEqual(self.ask_from_worker(self.dash(False)), [False])

    def test_dialog_runs_on_main_thread(self):
        seen = []
        d = mock.Mock()
        d._ask.side_effect = lambda m, finish: (seen.append(threading.current_thread()), finish(True))
        self.ask_from_worker(d)
        self.assertEqual(seen, [threading.main_thread()])


class GtkDialogMappingTest(Base):
    """Plain GTK AlertDialog: button 1 (Continue) is consent; anything else is not."""

    def answer(self, result):
        FakeDialog.result = result
        got = []
        with mock.patch.object(gui, "Adw", None), mock.patch.object(gui.Gtk, "AlertDialog", FakeDialog):
            gui.Dashboard._ask(mock.Mock(), "msg", got.append)
        return got

    def test_continue_button(self):
        self.assertEqual(self.answer(1), [True])

    def test_cancel_button(self):
        self.assertEqual(self.answer(0), [False])

    def test_dismissed_dialog_error(self):
        self.assertEqual(self.answer(gui.GLib.Error("dismissed")), [False])


class AdwDialogMappingTest(Base):
    def setUp(self):
        if gui._load_toolkit(use_adw=True) or gui.Adw is None:
            self.skipTest("libadwaita >= 1.5 not available")
        self.addCleanup(gui._load_toolkit, False)

    def answer(self, result):
        FakeDialog.result = result
        got = []
        with mock.patch.object(gui.Adw, "AlertDialog", FakeDialog):
            gui.Dashboard._ask(mock.Mock(), core.GDM_WARNING, got.append)
        return got

    def test_continue_response(self):
        self.assertEqual(self.answer("continue"), [True])

    def test_cancel_response(self):
        self.assertEqual(self.answer("cancel"), [False])


class RunWithGuiConsentTest(Base):
    """core.run on a worker thread, consent answered through the dashboard."""

    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)
        self.env = detect.Environment("gnome", "gdm3", {"desktop": None, "lock": None, "login": None})
        self.img = make_image(scratch_dir() + "/a.png")

    def run_login(self, answer):
        d = mock.Mock()
        d._ask.side_effect = lambda m, finish: finish(answer)
        out = []
        with mock.patch.object(core.system, "run_helper", return_value=(core.APPLIED, "")) as helper:
            t = threading.Thread(target=lambda: out.append(
                core.run("apply", self.img, ["login"], lambda m: gui.Dashboard.confirm_blocking(d, m), self.env)))
            t.start()
            self.pump(lambda: out)
            t.join(5)
        return {r.target: r for r in out[0]}, helper

    def test_declined_gdm_warning_skips_login(self):
        res, helper = self.run_login(False)
        self.assertEqual((res["login"].status, res["login"].reason), (core.SKIPPED, "declined"))
        helper.assert_not_called()

    def test_accepted_gdm_warning_applies_login(self):
        res, helper = self.run_login(True)
        self.assertEqual(res["login"].status, core.APPLIED)
        self.assertEqual(helper.call_args.args[0], "gdm-apply")


if __name__ == "__main__":
    unittest.main()
