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
