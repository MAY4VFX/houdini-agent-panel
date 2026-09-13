# Reveal paths from the transcript

Cmd-click a local file path on macOS to reveal it in Finder. Folders open in
Finder; files are selected without launching their associated application.
Other platforms use Ctrl-click and the platform file manager.

The handler works on rendered prose, inline code and fenced code. It resolves
absolute paths, home-relative paths and relative paths under the conversation's
working directory. Spaces, non-ASCII text and trailing line numbers are handled.
Only existing paths resolve; abbreviated paths containing `...` are not guessed.
Normal clicks and selection retain Qt's behavior, and web URLs remain web links.

Resolution happens at the explicit modified click, not on each streamed chunk.
The file-manager subprocess runs off the UI thread through `childproc`.

Tests cover rendered inline-code clicks, Finder argv, relative paths and spaces.
`hython tests/smoke_file_paths.py [--source]` exercises native Qt prose/code event
handling including UTF-16 cursor offsets after an emoji. It captures reveal
requests without opening the file manager or launching an agent.
