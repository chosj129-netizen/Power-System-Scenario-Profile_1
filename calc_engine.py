from __future__ import annotations

import math
from typing import Any

import pandas as pd


VALID_SYSTEM_TYPES = {
    "conventional",
    "diesel_electric",
    "hybrid",
    "pure_electric",
}

VALID_ESS_MODES = {
    "charge",
    "discharge",
    "idle",
}


def validate_input_data(input_data: dict[str, Any]) -> None:
    system_type = input_data.get("system_type")
    if system_type not in VALID_SYSTEM_TYPES:
        raise ValueError(f"Invalid system_type: {system_type}")

    scenarios = input_data.get("scenarios", [])
    if not scenarios:
        raise ValueError("At least one scenario is required.")

    gen = input_data.get("generator_spec", {})
    if gen.get("unit_rating_kw", 0) <= 0:
        raise ValueError("generator_spec.unit_rating_kw must be > 0")
    if gen.get("count_installed", 0) <= 0:
        raise ValueError("generator_spec.count_installed must be > 0")
    if gen.get("target_load_factor", 0) <= 0:
        raise ValueError("generator_spec.target_load_factor must be > 0")

    ess = input_data.get("ess_spec", {})
    if ess:
        if ess.get("capacity_kwh", 1) <= 0:
            raise ValueError("ess_spec.capacity_kwh must be > 0")
        if not (0 <= ess.get("soc_min", 0) <= 1):
            raise ValueError("ess_spec.soc_min must be between 0 and 1")
        if not (0 <= ess.get("soc_max", 1) <= 1):
            raise ValueError("ess_spec.soc_max must be between 0 and 1")
        if ess.get("soc_min", 0) > ess.get("soc_max", 1):
            raise ValueError("ess_spec.soc_min must be <= soc_max")

    for idx, scenario in enumerate(scenarios, start=1):
        name = scenario.get("name", f"Scenario {idx}")
        if scenario.get("duration_hr", 0) <= 0:
            raise ValueError(f"{name}: duration_hr must be > 0")
        ess_mode = scenario.get("ess_mode", "idle")
        if ess_mode not in VALID_ESS_MODES:
            raise ValueError(f"{name}: invalid ess_mode '{ess_mode}'")


def _normalize_by_system_type(input_data: dict[str, Any], scenario: dict[str, Any]) -> dict[str, float]:
    system_type = input_data["system_type"]

    continuous = float(scenario.get("continuous_load_kw", 0.0))
    intermittent = float(scenario.get("intermittent_load_kw", 0.0))
    aux = float(scenario.get("aux_load_kw", 0.0))
    propulsion = float(scenario.get("propulsion_load_kw", 0.0))

    if system_type == "conventional":
        propulsion = 0.0

    return {
        "continuous_load_kw": continuous,
        "intermittent_load_kw": intermittent,
        "aux_load_kw": aux,
        "propulsion_load_kw": propulsion,
    }

def _compute_ess_power(
    input_data: dict[str, Any],
    scenario: dict[str, Any],
) -> tuple[float, float]:
    ess = input_data["ess_spec"]
    system_type = input_data["system_type"]

    if system_type not in {"hybrid", "pure_electric"}:
        return 0.0, 0.0

    if not ess.get("enabled", False):
        return 0.0, 0.0

    ess_mode = scenario.get("ess_mode", "idle")
    ess_power_kw = float(scenario.get("ess_power_kw", 0.0))

    ess_charge_kw = 0.0
    ess_discharge_kw = 0.0

    if ess_mode == "charge":
        ess_charge_kw = min(ess_power_kw, float(ess["max_charge_kw"]))
    elif ess_mode == "discharge":
        ess_discharge_kw = min(ess_power_kw, float(ess["max_discharge_kw"]))

    return ess_charge_kw, ess_discharge_kw


def _compute_total_load_kw(
    continuous_load_kw: float,
    intermittent_load_kw: float,
    aux_load_kw: float,
    propulsion_load_kw: float,
    ess_charge_kw: float,
    ess_discharge_kw: float,
) -> float:
    return (
        continuous_load_kw
        + intermittent_load_kw
        + aux_load_kw
        + propulsion_load_kw
        + ess_charge_kw
        - ess_discharge_kw
    )


def _compute_required_gen_count(
    total_load_kw: float,
    unit_rating_kw: float,
    target_load_factor: float,
    count_installed: int,
) -> int:
    required = max(
        1,
        math.ceil(total_load_kw / (unit_rating_kw * target_load_factor))
    )
    return min(required, count_installed)


def _compute_gen_loading_pct(
    total_load_kw: float,
    required_gen_count: int,
    unit_rating_kw: float,
) -> float:
    if required_gen_count <= 0:
        return 0.0
    return (total_load_kw / required_gen_count / unit_rating_kw) * 100.0


def _compute_soc_end(
    soc_start: float,
    duration_hr: float,
    ess_charge_kw: float,
    ess_discharge_kw: float,
    ess_spec: dict[str, Any],
) -> float:
    soc = soc_start

    if ess_charge_kw > 0:
        charge_energy_kwh = ess_charge_kw * duration_hr * float(ess_spec["charge_efficiency"])
        soc += charge_energy_kwh / float(ess_spec["capacity_kwh"])

    if ess_discharge_kw > 0:
        discharge_energy_kwh = (ess_discharge_kw * duration_hr) / float(ess_spec["discharge_efficiency"])
        soc -= discharge_energy_kwh / float(ess_spec["capacity_kwh"])

    soc = max(float(ess_spec["soc_min"]), min(soc, float(ess_spec["soc_max"])))
    return soc


def build_scenario_dataframe(input_data: dict[str, Any]) -> pd.DataFrame:
    validate_input_data(input_data)

    rows: list[dict[str, Any]] = []
    gen = input_data["generator_spec"]
    ess = input_data["ess_spec"]
    generator_enabled = bool(gen.get("enabled", True))

    soc = float(ess["soc_init"])

    for scenario in input_data["scenarios"]:
        scenario_name = str(scenario["name"])
        duration_hr = float(scenario["duration_hr"])

        normalized = _normalize_by_system_type(input_data, scenario)
        continuous = normalized["continuous_load_kw"]
        intermittent = normalized["intermittent_load_kw"]
        aux = normalized["aux_load_kw"]
        propulsion = normalized["propulsion_load_kw"]

        ess_charge_kw, ess_discharge_kw = _compute_ess_power(input_data, scenario)

        total_load_kw = _compute_total_load_kw(
            continuous_load_kw=continuous,
            intermittent_load_kw=intermittent,
            aux_load_kw=aux,
            propulsion_load_kw=propulsion,
            ess_charge_kw=ess_charge_kw,
            ess_discharge_kw=ess_discharge_kw,
        )

        if generator_enabled:
            required_gen_count = _compute_required_gen_count(
                total_load_kw=total_load_kw,
                unit_rating_kw=float(gen["unit_rating_kw"]),
                target_load_factor=float(gen["target_load_factor"]),
                count_installed=int(gen["count_installed"]),
            )

            gen_loading_pct = _compute_gen_loading_pct(
                total_load_kw=total_load_kw,
                required_gen_count=required_gen_count,
                unit_rating_kw=float(gen["unit_rating_kw"]),
            )
        else:
            required_gen_count = 0
            gen_loading_pct = 0.0

        soc_start = soc
        soc_end = soc

        if input_data["system_type"] in {"hybrid", "pure_electric"} and ess.get("enabled", False):
            soc_end = _compute_soc_end(
                soc_start=soc_start,
                duration_hr=duration_hr,
                ess_charge_kw=ess_charge_kw,
                ess_discharge_kw=ess_discharge_kw,
                ess_spec=ess,
            )
            soc = soc_end

        rows.append(
            {
                "scenario": scenario_name,
                "duration_hr": duration_hr,
                "continuous_load_kw": continuous,
                "intermittent_load_raw_kw": float(scenario.get("intermittent_load_raw_kw", intermittent)),
                "diversity_factor": float(scenario.get("diversity_factor", 1.0)),
                "intermittent_load_kw": intermittent,
                "hotel_load_kw": float(scenario.get("hotel_load_kw", continuous + intermittent)),
                "deck_machinery_load_kw": float(scenario.get("deck_machinery_load_kw", aux)),
                "hotel_total_kw": continuous + intermittent,
                "aux_load_kw": aux,
                "propulsion_load_kw": propulsion,
                "ess_charge_kw": ess_charge_kw,
                "ess_discharge_kw": ess_discharge_kw,
                "total_load_kw": total_load_kw,
                "required_gen_count": required_gen_count,
                "gen_loading_pct": gen_loading_pct,
                "soc_start_pct": soc_start * 100.0,
                "soc_end_pct": soc_end * 100.0,
            }
        )

    return pd.DataFrame(rows)


def build_generator_loading_dataframe(
    scenario_df: pd.DataFrame,
    unit_rating_kw: float,
) -> pd.DataFrame:
    expanded_rows: list[dict[str, Any]] = []

    for _, row in scenario_df.iterrows():
        scenario = str(row["scenario"])
        required_gen_count = int(row["required_gen_count"])
        total_load_kw = float(row["total_load_kw"])

        if required_gen_count <= 0:
            continue

        loading_pct = (total_load_kw / required_gen_count / unit_rating_kw) * 100.0

        for idx in range(1, required_gen_count + 1):
            expanded_rows.append(
                {
                    "scenario": scenario,
                    "generator": f"DG{idx}",
                    "loading_pct": loading_pct,
                }
            )

    return pd.DataFrame(expanded_rows)


def build_summary_metrics(input_data: dict[str, Any], scenario_df: pd.DataFrame) -> dict[str, Any]:
    total_energy_kwh = float((scenario_df["total_load_kw"] * scenario_df["duration_hr"]).sum())
    max_total_load_kw = float(scenario_df["total_load_kw"].max())
    avg_gen_loading_pct = float(scenario_df["gen_loading_pct"].mean())
    max_gen_loading_pct = float(scenario_df["gen_loading_pct"].max())
    max_required_gen_count = int(scenario_df["required_gen_count"].max())

    return {
        "project_name": input_data["project_info"]["project_name"],
        "system_type": input_data["system_type"],
        "scenario_count": int(len(scenario_df)),
        "total_energy_kwh": total_energy_kwh,
        "max_total_load_kw": max_total_load_kw,
        "avg_gen_loading_pct": avg_gen_loading_pct,
        "max_gen_loading_pct": max_gen_loading_pct,
        "max_required_gen_count": max_required_gen_count,
    }