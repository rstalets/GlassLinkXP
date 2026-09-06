import numpy as np
import pytest

from g1000_softkey import synth
from g1000_softkey.capture import (
    CaptureError,
    ImageCapture,
    WgcCapture,
    create_source,
    is_windows,
    list_windows,
    sources_for,
)
from g1000_softkey.config import DisplayConfig


@pytest.fixture
def frames_dir(tmp_path):
    synth.write_menus(tmp_path)
    return tmp_path


def test_image_capture_single_file(frames_dir):
    source = ImageCapture(frames_dir / "pfd_top.png")
    frame = source.grab()
    assert isinstance(frame, np.ndarray) and frame.ndim == 3
    assert np.array_equal(source.grab(), frame)  # single file repeats
    source.close()


def test_image_capture_cycles_a_directory(frames_dir):
    source = ImageCapture(frames_dir)
    frames = [source.grab() for _ in range(len(synth.MENUS) + 1)]
    assert np.array_equal(frames[0], frames[-1])  # wrapped around
    assert not np.array_equal(frames[0], frames[1])


def test_image_capture_can_stop_at_the_end(frames_dir):
    source = ImageCapture(frames_dir / "inset.png", loop=False)
    assert source.grab() is not None
    assert source.grab() is None


def test_missing_image_path_is_a_clean_error(tmp_path):
    with pytest.raises(CaptureError):
        ImageCapture(tmp_path / "nope.png")
    with pytest.raises(CaptureError):
        ImageCapture(tmp_path)  # empty directory


def test_create_source_spec(frames_dir):
    assert isinstance(create_source(f"image:{frames_dir / 'xpdr.png'}"), ImageCapture)
    with pytest.raises(CaptureError):
        create_source("magic:thing")


def test_sources_for_prefers_a_per_display_png(frames_dir):
    (frames_dir / "pfd.png").write_bytes((frames_dir / "pfd_top.png").read_bytes())
    displays = [DisplayConfig(key="pfd"), DisplayConfig(key="mfd")]
    sources = sources_for(displays, frames_dir)
    assert sources["pfd"].name.endswith("pfd.png")
    assert sources["mfd"].name.endswith(str(frames_dir))  # falls back to the directory


@pytest.mark.skipif(is_windows(), reason="checks the non-Windows error path")
def test_windows_only_entry_points_fail_cleanly():
    with pytest.raises(CaptureError) as excinfo:
        list_windows()
    assert "Windows" in str(excinfo.value)
    with pytest.raises(CaptureError) as excinfo:
        WgcCapture("G1000 PFD")
    assert "--image" in str(excinfo.value)


def test_resize_window_refuses_off_windows():
    """The Windows path is unexercised here; at least the guard is."""
    from g1000_softkey.capture import CaptureError, is_windows, resize_window

    if is_windows():  # pragma: no cover - not the CI platform
        pytest.skip("this asserts the non-Windows guard")
    with pytest.raises(CaptureError, match="requires Windows"):
        resize_window(0x1234, 1400, 1000)


# -- who is allowed to resize a window --------------------------------------
#
# Two settings that both fix one window's size is one more than can be true at
# once, and which of them won would come down to which ran last. So when window
# management has sized a display, the per-display setting is not consulted for
# it. WgcCapture is stood in for here because the real one needs Windows; what
# is being checked is which arguments it is handed, which is the whole of the
# coupling.


class _Recorder:
    def __init__(self, title, **kwargs):
        self.name = f"wgc:{title}"
        _Recorder.calls.append((title, kwargs))

    calls: list = []

    def grab(self):
        return None

    def close(self):
        return None


@pytest.fixture
def recorded(monkeypatch):
    from g1000_softkey import capture

    _Recorder.calls = []
    monkeypatch.setattr(capture, "WgcCapture", _Recorder)
    return _Recorder.calls


def _sized_display():
    return DisplayConfig(
        key="pfd", window_title="G1000 PFD",
        manage_window_size=True, window_size=(1400, 1000),
    )


def test_a_display_window_management_sized_is_not_sized_again(recorded):
    sources_for([_sized_display()], None, managed={"pfd"})

    assert recorded == [("G1000 PFD", {})], (
        "nothing about size should be passed for a window already placed -- not even "
        "a size with resizing turned off, which would trip the 'window_size is set but "
        "manage_window_size is off' note about a config that will not do what it says"
    )


def test_a_display_window_management_did_not_touch_keeps_its_own_setting(recorded):
    sources_for([_sized_display()], None, managed=frozenset())

    assert recorded == [
        ("G1000 PFD", {"target_size": (1400, 1000), "manage_size": True}),
    ]


def test_managing_one_display_leaves_the_other_alone(recorded):
    mfd = DisplayConfig(key="mfd", window_title="G1000 MFD",
                        manage_window_size=True, window_size=(1400, 1000))
    sources_for([_sized_display(), mfd], None, managed={"pfd"})

    assert recorded[0] == ("G1000 PFD", {})
    assert recorded[1] == ("G1000 MFD", {"target_size": (1400, 1000), "manage_size": True})


# -- the frame slot, and the window that goes away --------------------------
#
# WgcCapture itself needs Windows, but the slot it shares with the capture
# thread does not, and the slot is where the rule lives: a frame from a window
# that has closed is not a current frame. Without that rule the slot kept
# handing back the last frame of a closed pop-out for as long as the daemon
# ran -- the Stream Deck froze on whatever the labels were when the window
# went, nothing reported a problem because frames were still arriving, and the
# code that reopens a closed pop-out was never reached.


def _slot():
    from g1000_softkey.capture import _LatestFrame

    return _LatestFrame()


def test_an_empty_slot_has_nothing_to_give():
    assert _slot().take() is None


def test_the_newest_frame_is_what_comes_out():
    slot = _slot()
    slot.put(np.zeros((2, 2, 3), dtype=np.uint8))
    slot.put(np.ones((2, 2, 3), dtype=np.uint8))
    assert np.array_equal(slot.take(), np.ones((2, 2, 3), dtype=np.uint8))


def test_what_comes_out_is_a_copy():
    """The capture thread owns its buffer and will write to it again."""
    slot = _slot()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    slot.put(frame)
    taken = slot.take()
    taken[0, 0, 0] = 255
    assert slot.take()[0, 0, 0] == 0


def test_a_closed_window_has_no_frame_even_though_one_was_captured():
    slot = _slot()
    slot.put(np.zeros((2, 2, 3), dtype=np.uint8))
    assert slot.take() is not None

    slot.lose()
    assert slot.lost is True
    assert slot.take() is None, (
        "the last frame of a window that no longer exists is not a picture of "
        "anything current, and serving it hides the closure completely"
    )


def test_losing_the_window_is_permanent():
    """A WGC session does not outlive its window; the way back is a new source."""
    slot = _slot()
    slot.lose()
    slot.put(np.zeros((2, 2, 3), dtype=np.uint8))
    assert slot.take() is None


def test_stopping_is_not_losing_the_window():
    """Shutdown and a closed window are different states that both stop frames."""
    slot = _slot()
    slot.put(np.zeros((2, 2, 3), dtype=np.uint8))
    slot.stop()
    assert slot.stopping is True
    assert slot.lost is False
    assert slot.take() is not None, "a clean shutdown does not invalidate the last frame"


def test_a_source_with_no_window_to_lose_never_reports_one_lost(frames_dir):
    from g1000_softkey import capture

    assert capture.window_lost(ImageCapture(frames_dir)) is False
