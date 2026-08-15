import unittest
from pathlib import Path

from gate_controller.runtime import require_python_version


class RuntimeTests(unittest.TestCase):
    def test_gpio_dependency_uses_the_pi_5_compatible_rpi_gpio_provider(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")

        self.assertIn("rpi-lgpio==0.6", requirements)
        self.assertNotIn("RPi.GPIO==", requirements)
        self.assertIn('platform_machine == "armv6l"', requirements)

    def test_rejects_unsupported_python_versions_before_startup(self):
        with self.assertRaisesRegex(RuntimeError, "Python 3.10"):
            require_python_version((3, 9, 18))

    def test_accepts_the_declared_minimum_python_version(self):
        require_python_version((3, 10, 0))
