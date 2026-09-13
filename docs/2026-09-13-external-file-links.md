# Binary file links must not navigate the transcript

Clicking `[Download preview](/absolute/preview.mp4)` made QTextBrowser load the
video bytes as text in place of the message. `openExternalLinks=True` excludes
file and relative URLs; it does not disable the browser's own navigation.
A regression test reproduced the exact binary replacement before the fix.

The message browser now disables automatic navigation and routes anchor clicks
through QDesktopServices. Local paths become file URLs; relative paths use the
conversation folder. Normal clicks open the associated application, while
Cmd-click resolves a named link's target and reveals it in Finder. Web links
still open externally. The transcript is unchanged by either gesture.

Tests cover actual clicks on binary links and Cmd-clicks on named file URLs,
relative paths with percent-encoded spaces and HTTP links. The native Houdini
smoke in tests/smoke_file_paths.py exercises both gestures with a binary fixture.
