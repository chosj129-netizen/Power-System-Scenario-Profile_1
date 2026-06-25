from __future__ import annotations

from typing import Any
import re

import pandas as pd


def _to_mode_catalog_df(scenarios: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        rows.append(
            {
                "mode": str(scenario.get("name", "")).strip(),
                "continuous_load_kw": float(scenario.get("continuous_load_kw", 0.0)),
                "intermittent_load_raw_kw": float(scenario.get("intermittent_load_raw_kw", scenario.get("intermittent_load_kw", 0.0))),
                "diversity_factor": float(scenario.get("diversity_factor", 1.0)),
                "intermittent_load_kw": float(scenario.get("intermittent_load_kw", 0.0)),
                "hotel_load_kw": float(scenario.get("hotel_load_kw", 0.0)),
                "deck_machinery_load_kw": float(scenario.get("deck_machinery_load_kw", scenario.get("aux_load_kw", 0.0))),
                "aux_load_kw": float(scenario.get("aux_load_kw", 0.0)),
                "propulsion_load_kw": float(scenario.get("propulsion_load_kw", 0.0)),
                "ess_mode": str(scenario.get("ess_mode", "idle")),
                "ess_power_kw": float(scenario.get("ess_power_kw", 0.0)),
            }
        )
    return pd.DataFrame(rows)


def _normalize_mode_name(value) -> str:
    text = str(value).strip().upper()
    if not text:
        return ""

    text = text.replace("FITGTING", "FIGHTING")
    text = text.replace("FIRE FITGTING", "FIRE FIGHTING")
    text = text.replace("HARBOR", "HARBOUR")
    text = text.replace("ARR./PORT", "ARR_PORT")
    text = text.replace("ARR/PORT", "ARR_PORT")
    text = text.replace("DEPART & ARR./PORT", "DEPART_ARR_PORT")
    text = text.replace("DEPART & ARR/PORT", "DEPART_ARR_PORT")
    text = text.replace("&", " ")
    text = text.replace("/", " ")
    text = re.sub(r"\bSERVICE\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" ", "_")
    return text


def build_voyage_profile_dataframe(
    mode_catalog_scenarios: list[dict[str, Any]],
    voyage_rows: list[dict[str, Any]],
    voyage_count: int,
) -> pd.DataFrame:
    catalog_df = _to_mode_catalog_df(mode_catalog_scenarios)
    if catalog_df.empty:
        return pd.DataFrame()

    catalog_df["mode_key"] = catalog_df["mode"].apply(_normalize_mode_name)
    catalog_map = {row["mode_key"]: row for _, row in catalog_df.iterrows()}

    normalized_rows = [row for row in voyage_rows if float(row.get("duration_hr", 0.0)) > 0 and str(row.get("mode", "")).strip()]
    if not normalized_rows:
        return pd.DataFrame()

    timeline_rows: list[dict[str, Any]] = []
    elapsed_hr = 0.0

    for voyage_no in range(1, int(voyage_count) + 1):
        for segment_no, row in enumerate(normalized_rows, start=1):
            mode = str(row.get("mode", "")).strip()
            mode_key = _normalize_mode_name(mode)
            duration_hr = float(row.get("duration_hr", 0.0))
            if duration_hr <= 0:
                continue

            base = catalog_map.get(mode_key)
            if base is None:
                continue

            scenario_name = f"V{voyage_no:02d}-{segment_no:02d} {base['mode']}"
            start_hr = elapsed_hr
            end_hr = elapsed_hr + duration_hr

            timeline_rows.append(
                {
                    "scenario": scenario_name,
                    "voyage_no": voyage_no,
                    "segment_no": segment_no,
                    "mode": base["mode"],
                    "start_hr": start_hr,
                    "end_hr": end_hr,
                    "duration_hr": duration_hr,
                    "continuous_load_kw": float(base["continuous_load_kw"]),
                    "intermittent_load_raw_kw": float(base["intermittent_load_raw_kw"]),
                    "diversity_factor": float(base["diversity_factor"]),
                    "intermittent_load_kw": float(base["intermittent_load_kw"]),
                    "hotel_load_kw": float(base["hotel_load_kw"]),
                    "deck_machinery_load_kw": float(base["deck_machinery_load_kw"]),
                    "aux_load_kw": float(base["aux_load_kw"]),
                    "propulsion_load_kw": float(base["propulsion_load_kw"]),
                    "ess_mode": str(base["ess_mode"]),
                    "ess_power_kw": float(base["ess_power_kw"]),
                }
            )
            elapsed_hr = end_hr

    return pd.DataFrame(timeline_rows)


def build_calc_scenarios_from_voyage_profile(voyage_profile_df: pd.DataFrame) -> list[dict[str, Any]]:
    if voyage_profile_df.empty:
        return []

    scenario_columns = [
        "scenario",
        "duration_hr",
        "continuous_load_kw",
        "intermittent_load_raw_kw",
        "diversity_factor",
        "intermittent_load_kw",
        "hotel_load_kw",
        "deck_machinery_load_kw",
        "aux_load_kw",
        "propulsion_load_kw",
        "ess_mode",
        "ess_power_kw",
    ]

    output: list[dict[str, Any]] = []
    for _, row in voyage_profile_df.iterrows():
        item = {
            "name": str(row["scenario"]),
            "duration_hr": float(row["duration_hr"]),
            "continuous_load_kw": float(row["continuous_load_kw"]),
            "intermittent_load_raw_kw": float(row["intermittent_load_raw_kw"]),
            "diversity_factor": float(row["diversity_factor"]),
            "intermittent_load_kw": float(row["intermittent_load_kw"]),
            "hotel_load_kw": float(row["hotel_load_kw"]),
            "deck_machinery_load_kw": float(row["deck_machinery_load_kw"]),
            "aux_load_kw": float(row["aux_load_kw"]),
            "propulsion_load_kw": float(row["propulsion_load_kw"]),
            "ess_mode": str(row["ess_mode"]),
            "ess_power_kw": float(row["ess_power_kw"]),
        }
        output.append(item)

    return output