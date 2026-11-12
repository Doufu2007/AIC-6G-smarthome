"""Independent arithmetic and unknown-data contracts for the cost CLI."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


def load_calculator():
    path = HERE / "cost_calculator.py"
    if not path.exists():
        raise AssertionError("cost calculator has not been implemented")
    spec = importlib.util.spec_from_file_location("sensehome_cost_calculator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    # Hand calculation: 2,000 requests; 500 cloud requests -> 0.50 input
    # + 1.00 output; hardware 10; energy (10*720+20*1.5)/1000=7.23 kWh
    # -> 3.615; maintenance 4; network 2; total 21.115, per 1,000=10.5575.
    values = dict(monthly_requests=2000, cloud_fraction=0.25,
                  input_tokens_per_cloud_attempt=1000,
                  output_tokens_per_cloud_attempt=500,
                  cloud_attempts_per_routed_request=1,
                  cloud_input_per_million=1, cloud_output_per_million=4,
                  gateway_purchase=240, amortization_months=24,
                  monthly_uptime_hours=720, idle_watts=10, active_watts=30,
                  local_seconds_per_request=3.6, electricity_per_kwh=0.5,
                  monthly_operations=4, monthly_network=2)
    return {"schema": "sensehome.cost.v1", "scenario_name": "arithmetic fixture",
            "currency": "CNY", "parameters": {
                key: {"value": value, "status": "assumption", "source": "test fixture"}
                for key, value in values.items()}}


class CostCalculatorTests(unittest.TestCase):
    def test_all_cost_components_and_per_thousand_use_independent_arithmetic(self):
        result = load_calculator().calculate(fixture())
        self.assertAlmostEqual(result["monthly_total"], 21.115)
        self.assertAlmostEqual(result["per_1000_requests_total"], 10.5575)
        self.assertAlmostEqual(result["energy_kwh"], 7.23)
        self.assertAlmostEqual(result["monthly_components"]["cloud_tokens"], 1.5)

    def test_unknown_power_is_never_silently_zero_and_total_remains_unknown(self):
        config = fixture()
        config["parameters"]["idle_watts"] = {"value": None, "status": "unknown", "source": "not measured"}
        result = load_calculator().calculate(config)
        self.assertIsNone(result["monthly_total"])
        self.assertIsNone(result["per_1000_requests_total"])
        self.assertIsNone(result["monthly_components"]["electricity"])
        self.assertIn("idle_watts", result["unknown_parameters"])
        self.assertAlmostEqual(result["monthly_known_subtotal"], 17.5)

    def test_cloud_retry_attempts_are_billed_in_the_scenario(self):
        config = fixture()
        config["parameters"]["cloud_attempts_per_routed_request"]["value"] = 2
        self.assertAlmostEqual(load_calculator().calculate(config)["monthly_components"]["cloud_tokens"], 3.0)

    def test_zero_cloud_share_needs_no_cloud_quote(self):
        config = fixture()
        config["parameters"]["cloud_fraction"]["value"] = 0
        for name in ("cloud_input_per_million", "cloud_output_per_million"):
            config["parameters"][name] = {"value": None, "status": "unknown", "source": "unused"}
        result = load_calculator().calculate(config)
        self.assertEqual(result["monthly_components"]["cloud_tokens"], 0)
        self.assertIsNotNone(result["monthly_total"])

    def test_invalid_or_unphysical_inputs_are_rejected(self):
        for name, bad in (("monthly_requests", 0), ("monthly_requests", -1),
                          ("monthly_requests", float("nan")), ("monthly_requests", True),
                          ("cloud_fraction", 1.1), ("cloud_attempts_per_routed_request", 0.5),
                          ("amortization_months", 0), ("active_watts", 5),
                          ("local_seconds_per_request", 3000)):
            with self.subTest(name=name, bad=bad):
                config = fixture()
                config["parameters"][name]["value"] = bad
                with self.assertRaises(ValueError):
                    load_calculator().calculate(config)

    def test_metadata_cannot_label_a_number_as_unknown_or_drop_a_typo(self):
        for mutate in (lambda c: c["parameters"]["idle_watts"].update(status="unknown"),
                       lambda c: c["parameters"].update(idle_wats={"value": 1, "status": "assumption", "source": "typo"}),
                       lambda c: c["parameters"]["idle_watts"].update(source="")):
            config = fixture()
            mutate(config)
            with self.assertRaises(ValueError):
                load_calculator().calculate(config)

    def test_cli_reads_a_config_and_outputs_json_without_dependencies(self):
        self.assertTrue((HERE / "cost_calculator.py").exists(), "cost calculator has not been implemented")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            path.write_text(json.dumps(fixture()), encoding="utf-8")
            result = subprocess.run([sys.executable, str(HERE / "cost_calculator.py"),
                                     "--config", str(path), "--json"], capture_output=True,
                                    text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertAlmostEqual(json.loads(result.stdout)["monthly_total"], 21.115)


if __name__ == "__main__":
    unittest.main()
