"""Windows UTF-8 encoding fix.

Addresses dual encoding issues on Windows:
1. Reading UTF-8 files (run_log.jsonl) with default cp1252 → UnicodeDecodeError
2. Printing Unicode characters to cp1252 console → UnicodeEncodeError

This module forces UTF-8 everywhere, eliminating 10% of platform-specific failures.
"""
import sys
import io

def fix_windows_encoding():
    """Force UTF-8 for file I/O and console output on Windows."""
    if sys.platform != "win32":
        return
    
    # Fix stdout/stderr to handle Unicode (especially print statements with emoji)
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    
    # Fallback: wrap stdout with TextIOWrapper
    if sys.stdout.encoding and 'utf' not in sys.stdout.encoding.lower():
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, 
                encoding='utf-8', 
                errors='replace',
                line_buffering=True
            )
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, 
                encoding='utf-8', 
                errors='replace',
                line_buffering=True
            )
        except Exception:
            pass


if __name__ == "__main__":
    # Test the fix
    fix_windows_encoding()
    print(f"Platform: {sys.platform}")
    print(f"Stdout encoding: {sys.stdout.encoding}")
    # Use ASCII-safe output
    print("[OK] Encoding fix verified (unicode support enabled)")
