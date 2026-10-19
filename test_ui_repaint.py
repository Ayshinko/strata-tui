"""Regression test for TUI repaint cleanup (same-row suffix artifacts)."""
import io
from unittest.mock import patch

from test_merged_load import tui


def test_paint_clears_each_line_before_redrawing():
    stream = io.StringIO()
    with patch.object(tui.sys, "stdout", stream), patch.object(tui, "CLEAR_NEXT", False):
        tui.paint("long stale row from previous frame\nCPU Offload\n")
    output = stream.getvalue()
    expected = "long stale row from previous frame\x1b[K\nCPU Offload\x1b[K\n"
    assert expected in output, repr(output)


if __name__ == "__main__":
    test_paint_clears_each_line_before_redrawing()
    print("paint row-cleanup regression: PASS")
