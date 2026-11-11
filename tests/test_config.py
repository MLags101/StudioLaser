import contextlib
import io
import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mopa_luiz import cli
from mopa_luiz.cli import (
    JPT_M7_PULSE_WIDTHS_NS,
    load_markcfg,
    nearest_pulse_width,
)


SAMPLE_MARKCFG = textwrap.dedent("""\
    [LMC_CFG]
    LASERTYPE=4
    FIELDSIZE=200
    MINPWMFREQ=1000
    MAXPWMFREQ=4000000
    m_nIPGSerialNo=7
    m_bEnableIPGSetPulseWidth=1
    m_nEnableFiberLaserStateCheckInMarking=1
    REDLIGHTSPEED=3000
""")


class _CfgFixture:
    def __init__(self, text: str):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "markcfg7"
        self.path.write_text(text, encoding="latin-1")

    def cleanup(self):
        self._tmp.cleanup()


class ConfigParsingTests(unittest.TestCase):
    def test_parses_known_keys(self):
        fix = _CfgFixture(SAMPLE_MARKCFG)
        try:
            cfg = load_markcfg(fix.path)
            self.assertEqual(cfg.get_int("FIELDSIZE"), 200)
            self.assertEqual(cfg.get_int("MAXPWMFREQ"), 4_000_000)
            self.assertTrue(cfg.get_bool("m_bEnableIPGSetPulseWidth"))
            self.assertEqual(cfg.get_int("m_nIPGSerialNo"), 7)
        finally:
            fix.cleanup()

    def test_missing_section_raises(self):
        fix = _CfgFixture("[OTHER]\nFOO=1\n")
        try:
            with self.assertRaises(ValueError):
                load_markcfg(fix.path)
        finally:
            fix.cleanup()


class PulseWidthGuardTests(unittest.TestCase):
    def test_nearest_snaps_to_table(self):
        # Avoid exact-tie inputs; ties resolve to the first equal candidate.
        for raw, expected in [(0, 2), (5, 4), (7, 6), (199, 200), (430, 450), (999, 500)]:
            self.assertEqual(nearest_pulse_width(raw), expected)
            self.assertIn(nearest_pulse_width(raw), JPT_M7_PULSE_WIDTHS_NS)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def profile_command(self, command, text):
        fix = _CfgFixture(text)
        try:
            code, out = self.run_cli(command, "--profile", str(fix.path))
        finally:
            fix.cleanup()
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_pulse_widths_honours_profile_override(self):
        report = self.profile_command("pulse-widths", SAMPLE_MARKCFG + "PULSEWIDTHS=25, 10,500\n")
        self.assertEqual(report["pulse_widths_ns"], [10, 25, 500])

    def test_pulse_widths_defaults_to_m7_table(self):
        report = self.profile_command("pulse-widths", SAMPLE_MARKCFG)
        self.assertEqual(report["pulse_widths_ns"], list(JPT_M7_PULSE_WIDTHS_NS))

    def test_show_config_reads_the_profile(self):
        self.assertEqual(self.profile_command("show-config", SAMPLE_MARKCFG)["FIELDSIZE"], "200")

    def test_no_command_opens_the_desktop_app(self):
        parser = cli.build_parser()
        self.assertIs(parser.parse_args([]).func, cli.desktop_command)
        self.assertIs(parser.parse_args(["desktop"]).func, cli.desktop_command)

    def test_retired_commands_are_gone(self):
        for command in ("ui", "machines", "plan-test-box", "inspect-profile"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args([command])


if __name__ == "__main__":
    unittest.main()
