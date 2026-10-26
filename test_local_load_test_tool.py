"""
Platform-dependent behaviour of tools/local_load_test.py.

The tool used `import resource` (Unix only) at module level, so on Windows pytest
failed while COLLECTING it: the file name matches pytest's default `*_test.py`
pattern even though it is a command-line tool with no tests. These tests pin the
fix: the module imports without `resource`, peak memory is reported as
UNAVAILABLE (never invented), Linux/macOS measurements are unchanged (macOS units
corrected), and importing the module no longer changes process-wide settings.

Windows is SIMULATED on any OS by putting `None` into sys.modules['resource'],
which makes `import resource` raise ImportError exactly as on Windows. The one
test that exercises real Windows behaviour only runs on Windows (sys.platform ==
"win32"); on other systems it is reported as skipped, not as passed.
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parent
TOOL = ROOT / "tools" / "local_load_test.py"
HAS_RESOURCE = importlib.util.find_spec("resource") is not None

# Makes `import resource` fail the way it does on Windows.
BLOCK_RESOURCE = "import sys; sys.modules['resource'] = None; "


def run_python(code, *args, env_drop=()):
    env = dict(os.environ)
    for name in env_drop:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-c", code, *args], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=180,
    )


def load_tool():
    spec = importlib.util.spec_from_file_location("local_load_test_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestWithoutTheResourceModule:
    """What happens on Windows (simulated)."""

    def test_the_module_imports_and_reports_memory_as_unavailable(self):
        proc = run_python(BLOCK_RESOURCE + textwrap.dedent(f"""
            import importlib.util
            spec = importlib.util.spec_from_file_location('t', {str(TOOL)!r})
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            assert m.resource is None
            assert m.rss_mb() is None, m.rss_mb()
            assert m.format_rss(m.rss_mb()).strip() == 'n/a'
            print('IMPORT_OK')
        """))
        assert proc.returncode == 0, proc.stderr
        assert "IMPORT_OK" in proc.stdout

    def test_pytest_collection_of_the_tool_no_longer_errors(self):
        """The exact reported failure: collecting tools/local_load_test.py."""
        proc = run_python(BLOCK_RESOURCE + textwrap.dedent(f"""
            import pytest
            raise SystemExit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider', {str(TOOL)!r}]))
        """))
        output = proc.stdout + proc.stderr
        assert proc.returncode in (0, 5), output          # 5 = "no tests collected": it is a tool, not a test
        assert "ModuleNotFoundError" not in output and "error" not in output.lower().replace("0 errors", "")

    def test_the_whole_tool_runs_and_prints_na_instead_of_a_number(self):
        proc = run_python(BLOCK_RESOURCE + textwrap.dedent(f"""
            import runpy
            sys.argv = ['local_load_test.py', '--stages', '2', '--generation-seconds', '0.01']
            runpy.run_path({str(TOOL)!r}, run_name='__main__')
        """))
        assert proc.returncode == 0, proc.stderr
        assert "Traceback" not in proc.stderr
        assert "peak RSS: unavailable" in proc.stdout
        rows = [line for line in proc.stdout.splitlines() if line.strip().startswith("2 ")]
        assert rows, proc.stdout
        assert rows[-1].split()[-1] == "n/a", "no invented memory number"
        assert rows[-1].split()[2] == "0", "the load itself still ran without errors"

    @pytest.mark.skipif(sys.platform != "win32", reason="real Windows behaviour: runs only on Windows")
    def test_real_windows_has_no_resource_module_and_reports_unavailable(self):
        module = load_tool()
        assert module.resource is None and module.rss_mb() is None


class TestWhereResourceExists:
    """Linux/macOS behaviour is preserved."""

    @pytest.mark.skipif(not HAS_RESOURCE, reason="needs the Unix-only 'resource' module")
    def test_a_real_positive_measurement_is_returned(self):
        value = load_tool().rss_mb()
        assert isinstance(value, float) and 1.0 < value < 100_000.0

    def test_linux_reports_kib_so_the_value_is_divided_by_1024(self, monkeypatch):
        module = load_tool()

        class FakeResource:
            RUSAGE_SELF = 0

            @staticmethod
            def getrusage(_):
                return type("U", (), {"ru_maxrss": 204_800})()   # 204,800 KiB = 200 MiB
        monkeypatch.setattr(module, "resource", FakeResource)
        monkeypatch.setattr(module.sys, "platform", "linux")
        assert module.rss_mb() == pytest.approx(200.0)

    def test_macos_reports_bytes_so_the_unit_is_corrected(self, monkeypatch):
        """Before this change macOS bytes were divided by 1024 only, overstating
        memory 1024-fold. The Linux result above is unchanged."""
        module = load_tool()

        class FakeResource:
            RUSAGE_SELF = 0

            @staticmethod
            def getrusage(_):
                return type("U", (), {"ru_maxrss": 200 * 1024 * 1024})()   # 200 MiB in bytes
        monkeypatch.setattr(module, "resource", FakeResource)
        monkeypatch.setattr(module.sys, "platform", "darwin")
        assert module.rss_mb() == pytest.approx(200.0)

    @pytest.mark.skipif(not HAS_RESOURCE, reason="needs the Unix-only 'resource' module")
    def test_the_whole_tool_prints_a_numeric_peak_rss(self):
        proc = run_python(textwrap.dedent(f"""
            import runpy, sys
            sys.argv = ['local_load_test.py', '--stages', '2', '--generation-seconds', '0.01']
            runpy.run_path({str(TOOL)!r}, run_name='__main__')
        """))
        assert proc.returncode == 0, proc.stderr
        assert "unavailable" not in proc.stdout
        rows = [line for line in proc.stdout.splitlines() if line.strip().startswith("2 ")]
        assert float(rows[-1].split()[-1]) > 0 and rows[-1].split()[2] == "0"


class TestFormatting:
    def test_numbers_are_right_aligned_to_the_table_width(self):
        module = load_tool()
        assert module.format_rss(123.456) == f"{'123.5':>13}"
        assert module.format_rss(None) == f"{'n/a':>13}"
        assert len(module.format_rss(None)) == len(module.format_rss(1.0))


class TestImportHasNoGlobalSideEffects:
    def test_importing_does_not_switch_quotas_off_or_silence_logging(self):
        """pytest collects this file, so an import must not change the settings of
        every other test (it used to set QUOTA_MODE=off and disable INFO logs)."""
        proc = run_python(textwrap.dedent(f"""
            import importlib.util, logging, os
            spec = importlib.util.spec_from_file_location('t', {str(TOOL)!r})
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            print('QUOTA_MODE=' + repr(os.environ.get('QUOTA_MODE')))
            print('LOGGING_DISABLE=' + str(logging.root.manager.disable))
        """), env_drop=("QUOTA_MODE",))
        assert proc.returncode == 0, proc.stderr
        assert "QUOTA_MODE=None" in proc.stdout
        assert "LOGGING_DISABLE=0" in proc.stdout

    def test_running_as_a_script_still_applies_them(self):
        module = load_tool()
        saved_mode = os.environ.get("QUOTA_MODE")
        try:
            module.configure_for_cli()
            assert os.environ["QUOTA_MODE"] == "off"
        finally:
            import logging
            logging.disable(logging.NOTSET)
            if saved_mode is None:
                os.environ.pop("QUOTA_MODE", None)
            else:
                os.environ["QUOTA_MODE"] = saved_mode