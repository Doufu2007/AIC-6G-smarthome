"""Transparent, standard-library scenario calculator; no network or billing calls."""
import argparse
import json
import math
from pathlib import Path
import sys


PARAMETERS = (
    "monthly_requests", "cloud_fraction", "input_tokens_per_cloud_attempt",
    "output_tokens_per_cloud_attempt", "cloud_attempts_per_routed_request",
    "cloud_input_per_million", "cloud_output_per_million", "gateway_purchase",
    "amortization_months", "monthly_uptime_hours", "idle_watts", "active_watts",
    "local_seconds_per_request", "electricity_per_kwh", "monthly_operations",
    "monthly_network",
)


def calculate(config):
    """Return known components and null totals whenever a required cost is unknown."""
    if not isinstance(config, dict) or config.get("schema") != "sensehome.cost.v1":
        raise ValueError("schema must be sensehome.cost.v1")
    if not isinstance(config.get("currency"), str) or not config["currency"].strip():
        raise ValueError("currency must name one shared currency for every monetary input")
    parameters = config.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != set(PARAMETERS):
        raise ValueError("parameters must contain exactly: " + ", ".join(PARAMETERS))
    values = {}
    for name in PARAMETERS:
        entry = parameters[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str) or not entry["source"].strip():
            raise ValueError(name + ": a nonempty source is required")
        status, value = entry.get("status"), entry.get("value")
        if status not in {"measured", "quote", "assumption", "unknown"}:
            raise ValueError(name + ": invalid evidence status")
        if (value is None) != (status == "unknown"):
            raise ValueError(name + ": null requires unknown, numeric values require an evidence status")
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(name + ": expected a finite nonnegative number")
            if name in {"monthly_requests", "amortization_months"} and value <= 0:
                raise ValueError(name + ": must be positive")
            if name == "monthly_requests" and int(value) != value:
                raise ValueError("monthly_requests must be an integer count")
            if name == "cloud_fraction" and value > 1:
                raise ValueError("cloud_fraction must be within [0, 1]")
            if name == "cloud_attempts_per_routed_request" and value < 1:
                raise ValueError("cloud_attempts_per_routed_request must be at least one")
        values[name] = value
    if values["monthly_requests"] is None or values["cloud_fraction"] is None:
        raise ValueError("monthly_requests and cloud_fraction are required for a scenario")
    if values["idle_watts"] is not None and values["active_watts"] is not None and values["active_watts"] < values["idle_watts"]:
        raise ValueError("active_watts must not be below idle_watts")

    n, fraction = values["monthly_requests"], values["cloud_fraction"]
    local_requests, cloud_requests = n * (1 - fraction), n * fraction
    seconds = values["local_seconds_per_request"]
    active_hours = None if seconds is None else local_requests * seconds / 3600
    uptime = values["monthly_uptime_hours"]
    if active_hours is not None and uptime is not None and active_hours > uptime:
        raise ValueError("serialized local active hours exceed configured uptime; use measured integrated energy or a valid workload")

    # At fraction zero, cloud parameters are unused rather than unknown costs.
    cloud_inputs = ("cloud_attempts_per_routed_request", "input_tokens_per_cloud_attempt",
                    "output_tokens_per_cloud_attempt", "cloud_input_per_million", "cloud_output_per_million")
    if cloud_requests == 0:
        cloud_cost, cloud_attempts = 0.0, 0.0
    elif any(values[name] is None for name in cloud_inputs):
        cloud_cost, cloud_attempts = None, None
    else:
        cloud_attempts = cloud_requests * values["cloud_attempts_per_routed_request"]
        cloud_cost = cloud_attempts * (
            values["input_tokens_per_cloud_attempt"] * values["cloud_input_per_million"]
            + values["output_tokens_per_cloud_attempt"] * values["cloud_output_per_million"]) / 1_000_000

    purchase, months = values["gateway_purchase"], values["amortization_months"]
    hardware = None if purchase is None or months is None else purchase / months
    energy_names = ("idle_watts", "active_watts", "monthly_uptime_hours", "local_seconds_per_request")
    if any(values[name] is None for name in energy_names):
        energy_kwh = None
    else:
        energy_kwh = (values["idle_watts"] * uptime
                      + (values["active_watts"] - values["idle_watts"]) * active_hours) / 1000
    tariff = values["electricity_per_kwh"]
    electricity = None if energy_kwh is None or tariff is None else energy_kwh * tariff
    components = {"hardware_amortization": hardware, "electricity": electricity,
                  "cloud_tokens": cloud_cost, "operations": values["monthly_operations"],
                  "network": values["monthly_network"]}
    known_subtotal = sum(value for value in components.values() if value is not None)
    incomplete = [name for name, value in components.items() if value is None]
    total = None if incomplete else known_subtotal
    statuses = sorted({entry["status"] for entry in parameters.values()})
    return {
        "schema": "sensehome.cost-result.v1",
        "scenario_name": config.get("scenario_name", "unnamed"),
        "currency": config["currency"],
        "result_kind": "parameterized scenario, not an invoice or measured total cost",
        "input_evidence_statuses": statuses,
        "monthly_requests": n, "monthly_cloud_routed_requests": cloud_requests,
        "monthly_cloud_attempts": cloud_attempts, "local_active_hours": active_hours,
        "energy_kwh": energy_kwh, "monthly_components": components,
        "monthly_known_subtotal": known_subtotal, "monthly_total": total,
        "per_1000_requests_components": {key: None if value is None else value * 1000 / n
                                         for key, value in components.items()},
        "per_1000_requests_known_subtotal": known_subtotal * 1000 / n,
        "per_1000_requests_total": None if total is None else total * 1000 / n,
        "unknown_parameters": [name for name in PARAMETERS if values[name] is None],
        "unknown_cost_components": incomplete,
        "parameters": parameters,
        "scope": "One always-on gateway, serialized average local work; token-only cloud API; no tax, finance, device purchase, audio, paid tools or failure compensation unless included in inputs.",
        "energy_boundary": "Whole-gateway idle plus incremental local-active power. CPU/RSS/TDP alone cannot supply watts. Probes, cloud-wait work and failed local work require integrated measurements or explicit allocation.",
    }


def main():
    parser = argparse.ArgumentParser(description="SenseHome transparent cost scenario calculator (standard library only)")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON including all evidence metadata")
    parser.add_argument("--out", type=Path, help="optionally save JSON result")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        result = calculate(config)
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, "Invalid cost configuration: " + str(exc) + "\n")
    if args.json:
        print(payload)
    else:
        print("情景：" + str(result["scenario_name"]))
        print("口径：参数化测算；不是实测总成本或账单。币种：" + result["currency"])
        for name, value in result["monthly_components"].items():
            print(name + " /月 = " + ("未知" if value is None else format(value, ".6f")))
        print("已知分项小计 /月 = %.6f" % result["monthly_known_subtotal"])
        print("总成本 /1000请求 = " + ("未知" if result["per_1000_requests_total"] is None
                                       else format(result["per_1000_requests_total"], ".6f")))
        print("未知参数：" + (", ".join(result["unknown_parameters"]) or "无；仍按输入证据属性解释"))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
