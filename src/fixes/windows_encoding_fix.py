"""Windows UTF-8 encoding fixes.

Ensures all output and file I/O uses UTF-8 on Windows, preventing
cp1252 encoding errors with non-ASCII characters.
"""
import sys
import os
from pathlib import Path


def setup_windows_utf8():
    """Configure Windows to use UTF-8 for stdout, stderr, and file I/O.
    
    Should be called at application startup.
    """
    if sys.platform != 'win32':
        return  # Only needed on Windows
    
    # Set environment variable for subprocess calls
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    # For Python 3.7+, enable UTF-8 mode
    if sys.version_info >= (3, 7):
        # This is set via env var PYTHONUTF8=1, but we can also force it here
        # by reconfiguring stdout/stderr
        try:
            # Force UTF-8 on stdout/stderr
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
            print("✓ Windows UTF-8 mode enabled")
        except Exception as e:
            print(f"Warning: Could not enable UTF-8 mode: {e}")


def safe_print(text: str, **kwargs):
    """Print with fallback for encoding errors."""
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        # Fallback: encode as ASCII with replacements
        safe_text = text.encode('ascii', errors='replace').decode('ascii')
        print(safe_text, **kwargs)


def read_file_utf8(path: Path, errors: str = 'replace') -> str:
    """Read file with explicit UTF-8 encoding."""
    return path.read_text(encoding='utf-8', errors=errors)


def write_file_utf8(path: Path, content: str):
    """Write file with explicit UTF-8 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


# Recommended startup sequence in __main__.py:
#
# if __name__ == "__main__":
#     from .fixes.windows_encoding_fix import setup_windows_utf8
#     setup_windows_utf8()
#     # ... rest of application code ...
#
# Then use safe_print() instead of print() for output with special characters.
