from __future__ import annotations

from copy import deepcopy
from typing import Any

import pandas as pd


def build_input_data_from_dummy(default_input_data: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(default_input_data)


def build_input_data_from_ela_result(
    ela_summary_df: pd.DataFrame | dict[str, Any],
    base_input_data: dict[str, Any],
) -> dict[str, Any]:
    input_data = deepcopy(base_input_data)
    
    # ----------------------------------
    # 🔹 1. 컬럼명 변환 (deck_mach → aux)
    # ----------------------------------
    if isinstance(ela_summary_df, dict):
        summary_df = ela_summary_df.get("summary", pd.DataFrame())
        df = summary_df.copy() if isinstance(summary_df, pd.DataFrame) else pd.DataFrame(summary_df)
    else:
        df = ela_summary_df.copy()

    if "deck_machinery_load_kw" in df.columns:
        df["aux_load_kw"] = df["deck_machinery_load_kw"]

    # ----------------------------------
    # 🔹 2. 필수 컬럼 체크
    # ----------------------------------
    required_columns = {
        "scenario",
        "duration_hr",
        "continuous_load_kw",
        "intermittent_load_raw_kw",
        "intermittent_load_kw",
        "hotel_load_kw",
        "deck_machinery_load_kw",
        "propulsion_load_kw",
    }

    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"ELA summary dataframe is missing columns: {sorted(missing)}")

    # ----------------------------------
    # 🔹 3. scenario 구성
    # ----------------------------------
    scenarios: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        scenarios.append(
            {
                "name": str(row["scenario"]),
                "duration_hr": float(row["duration_hr"]),
                "continuous_load_kw": float(row["continuous_load_kw"]),
                "intermittent_load_raw_kw": float(row["intermittent_load_raw_kw"]),
                "diversity_factor": float(row.get("diversity_factor", 1.0)),
                "intermittent_load_kw": float(row["intermittent_load_kw"]),
                "hotel_load_kw": float(row["hotel_load_kw"]),
                "deck_machinery_load_kw": float(row["deck_machinery_load_kw"]),
                "aux_load_kw": float(row["deck_machinery_load_kw"]),  # 내부 계산 호환용
                "propulsion_load_kw": float(row["propulsion_load_kw"]),
                "ess_mode": "idle",
                "ess_power_kw": 0.0,
            }
        )

    input_data["scenarios"] = scenarios
    return input_data