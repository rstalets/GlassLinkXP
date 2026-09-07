import numpy as np
import pytest

from glasslinkxp import synth
from glasslinkxp.capture import (
    CaptureError,
    ImageCapture,
    WgcCapture,
    create_source,
    is_windows,
    list_windows,
    sources_for,
)
from glasslinkxp.config import DisplayConfig


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


# -- who is allowed to size a window ----------------------------------------
#
# Exactly one thing: window management, through `place_window`. Opening a
# capture finds its window and changes nothing about it, for every display --
# which is what makes "only one thing sizes a window" true rather than a rule
# about which of two settings wins.


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
    from glasslinkxp import capture

    _Recorder.calls = []
    monkeypatch.setattr(capture, "WgcCapture", _Recorder)
    return _Recorder.calls


def test_opening_a_capture_says_nothing_about_size(recorded):
    """A capture takes the window as it finds it, for every display."""
    sources_for(
        [DisplayConfig(key="pfd", window_title="G1000 PFD"),
         DisplayConfig(key="mfd", window_title="G1000 MFD")],
        None,
    )

    assert recorded == [("G1000 PFD", {}), ("G1000 MFD", {})]


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
    from glasslinkxp.capture import _LatestFrame

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
    from glasslinkxp import capture

    assert capture.window_lost(ImageCapture(frames_dir)) is False
