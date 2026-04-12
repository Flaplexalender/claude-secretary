import subprocess, sys, pathlib

project_dir = pathlib.Path(__file__).resolve().parent.parent

try:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=no", "-q"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(project_dir),
        timeout=120
    )
    out = result.stdout.strip().split("\n")
    report = "\n".join(out[-15:]) + f"\nEXIT: {result.returncode}"
except subprocess.TimeoutExpired as e:
    report = f"TIMED OUT after 120s\nPartial stdout:\n{e.stdout[-2000:] if e.stdout else 'none'}"
except Exception as e:
    report = f"ERROR: {e}"
(project_dir / "_test_result.txt").write_text(report, encoding="utf-8")
print("DONE")
