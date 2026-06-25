from __future__ import annotations

from copy import deepcopy
import re

import pandas as pd
import streamlit as st
import altair as alt

from ela_read import parse_ela_excel, get_excel_diversity_factor
from adapters import build_input_data_from_dummy, build_input_data_from_ela_result
from calc_engine import build_scenario_dataframe
from voyage_scenario import build_voyage_profile_dataframe, build_calc_scenarios_from_voyage_profile
from config import DEFAULT_INPUT_DATA


st.set_page_config(page_title="Power System Scenario Profile Tool", layout="wide")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def build_generator_capacity_recommendation_df(scenario_df: pd.DataFrame) -> pd.DataFrame:
    if scenario_df is None or scenario_df.empty or "total_load_kw" not in scenario_df.columns:
        return pd.DataFrame()

    peak_row = scenario_df.loc[scenario_df["total_load_kw"].astype(float).idxmax()]
    peak_scenario = str(peak_row["scenario"])
    peak_total_load_kw = _safe_float(peak_row["total_load_kw"])

    targets = [75.0, 80.0, 85.0]
    rows = []
    for target_pct in targets:
        recommended_capacity_kw = peak_total_load_kw / (target_pct / 100.0) if target_pct else 0.0
        rows.append(
            {
                "peak_scenario": peak_scenario,
                "peak_total_load_kw": round(peak_total_load_kw, 2),
                "target_load_percent": target_pct,
                "recommended_generator_capacity_kw": round(recommended_capacity_kw, 2),
            }
        )

    return pd.DataFrame(rows)


def _classify_capacity_load_status(load_pct: float) -> str:
    """부하비율에 따른 권고 문구를 반환합니다.

    판정 기준:
    - pct < 30.0  -> '가능 (저부하 주의)'
    - pct < 75.0  -> '가능'
    - pct <= 85.0 -> '가능 (적정)'
    - pct <= 100.0 -> '가능'
    - 그 외 -> '불가'
    """
    try:
        pct = float(load_pct)
    except Exception:
        return "검토 필요"

    if pct < 30.0:
        return "가능 (저부하 주의)"
    if pct < 75.0:
        return "가능"
    if pct <= 85.0:
        return "가능 (적정)"
    if pct <= 100.0:
        return "가능"
    return "불가"


def build_generator_capacity_application_df(
    peak_total_load_kw: float,
    generator_capacity_kw: float,
    max_generators: int = 4,
) -> pd.DataFrame:
    rows = []
    for count in range(1, max_generators + 1):
        total_capacity_kw = generator_capacity_kw * count
        load_pct = (peak_total_load_kw / total_capacity_kw * 100.0) if total_capacity_kw > 0 else 0.0
        load_pct_int = int(round(load_pct))
        is_first_row = count == 1
        n_minus_one_capacity_kw = generator_capacity_kw * max(0, count - 1)
        n_minus_one_possible = peak_total_load_kw <= n_minus_one_capacity_kw

        if is_first_row:
            n_minus_one_status = _classify_capacity_load_status(load_pct_int)
        else:
            n_minus_one_status = _classify_capacity_load_status(load_pct_int) if n_minus_one_possible else "불가"

        operation_condition = "평상시 (1대)" if is_first_row else f"병렬운전 ({count}대)"
        label = "" if is_first_row else f"{count} X GENERATOR"
        rows.append(
            {
                "구분": label,
                "운전 조건": operation_condition,
                "사용 가능 용량 (LOAD %)": f"{total_capacity_kw:.1f} kW ({load_pct_int}%)",
                "N-1 가능 여부": n_minus_one_status,
            }
        )
    return pd.DataFrame(rows)
def build_generator_ess_application_df(
    peak_total_load_kw: float = 0.0,
    ess_generator_capacity_kw: float = 0.0,
    battery_capacity_kwh: float = 0.0,
    ess_df: pd.DataFrame | None = None,
    max_generators: int = 4,
) -> pd.DataFrame:
    """
    Build application table for Generator + ESS combined sizing.

    Columns:
    - 구분
    - 운전 조건
    - 발전기 총 용량
    - 발전기 목표 출력 (80%)
    - Peak Load
    - Peak 시 ESS 보조전력
    - 판정
    """
    rows = []
    for count in range(1, max_generators + 1):
        total_capacity_kw = ess_generator_capacity_kw * count
        generator_target_kw = total_capacity_kw * 0.8

        is_first_row = count == 1
        operation_condition = "평상시 (1대)" if is_first_row else f"병렬운전 ({count}대)"
        label = "" if is_first_row else f"{count} X GENERATOR"

        rows.append(
            {
                "구분": label,
                "운전 조건": operation_condition,
                "발전기 총 용량": f"{total_capacity_kw:.1f} kW",
                "발전기 목표 출력 (80%)": f"{generator_target_kw:.1f} kW",
            }
        )

    application_df = pd.DataFrame(rows)
    # align index style with generator-only table (empty label row + numbered rows)
    try:
        application_df.index = [""] + list(range(1, max_generators))
    except Exception:
        pass
    return application_df


def build_ess_capacity_sizing_df(
    scenario_df: pd.DataFrame,
    voyage_rows: list[dict],
    voyage_count: int,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    ESS sizing based on weighted average load and average running generator count.

    Returns (df, summary)
    """
    rows: list[dict] = []

    # prepare voyage map with normalized mode keys
    voyage_map: list[tuple[str, dict]] = []
    for vr in voyage_rows or []:
        mode = str(vr.get("mode", "")).strip()
        mode_norm = _normalize_token(mode)
        voyage_map.append((mode_norm, vr))

    # compute weighted averages across scenario_df using duration_hr
    total_weighted_load = 0.0
    total_duration = 0.0
    # for average running gen
    weighted_running_sum = 0.0

    # first pass: determine running_gen and duration for each scenario (matching voyage_rows)
    scenario_lookup: list[dict] = []
    for _, sc in scenario_df.iterrows():
        scenario_name = str(sc.get("scenario", ""))
        sc_norm = _normalize_token(scenario_name)
        total_load_kw = _safe_float(sc.get("total_load_kw", 0.0))
        sc_duration_hr = _safe_float(sc.get("duration_hr", 0.0))

        # match voyage row
        matched = None
        for mode_norm, vr in voyage_map:
            if not mode_norm:
                continue
            if mode_norm in sc_norm or sc_norm in mode_norm:
                matched = vr
                break

        if matched is not None:
            running_gen = int(matched.get("running_gen", 1)) if matched.get("running_gen", None) is not None else 1
            duration_hr = _safe_float(matched.get("duration_hr", sc_duration_hr))
        else:
            running_gen = 1
            duration_hr = sc_duration_hr

        scenario_lookup.append({
            "scenario": scenario_name,
            "total_load_kw": total_load_kw,
            "duration_hr": duration_hr,
            "running_gen": running_gen,
        })

        total_weighted_load += total_load_kw * duration_hr
        total_duration += duration_hr
        weighted_running_sum += running_gen * duration_hr

    if total_duration <= 0:
        weighted_avg_load_kw = 0.0
        average_running_gen = 1.0
    else:
        weighted_avg_load_kw = total_weighted_load / total_duration
        average_running_gen = weighted_running_sum / total_duration if total_duration else 1.0
        if average_running_gen == 0:
            average_running_gen = 1.0

    # ESS generator recommended per single generator
    ess_generator_recommended_kw = (
        weighted_avg_load_kw / (average_running_gen * 0.8) if average_running_gen and average_running_gen > 0 else 0.0
    )

    # Use energy-balance accumulation to compute required peak discharge (max 누적 방전량)
    energy_balance_kwh = 0.0
    max_required_energy_kwh = 0.0

    for item in scenario_lookup:
        scenario_name = item["scenario"]
        total_load_kw = item["total_load_kw"]
        duration_hr = item["duration_hr"]
        running_gen = int(item["running_gen"])

        generator_target_kw = ess_generator_recommended_kw * running_gen * 0.8
        ess_power_kw = total_load_kw - generator_target_kw
        if ess_power_kw > 0:
            ess_mode = "PTO"
        elif ess_power_kw < 0:
            ess_mode = "PTI"
        else:
            ess_mode = "IDLE"

        # scenario_df already includes per-voyage repetition (V01, V02...),
        # so do NOT multiply by voyage_count again here.
        daily_duration_hr = duration_hr
        ess_energy_kwh = ess_power_kw * duration_hr

        # energy balance accumulates ESS energy (positive = discharge demand, negative = charge)
        energy_balance_kwh += ess_energy_kwh
        if energy_balance_kwh < 0:
            energy_balance_kwh = 0.0

        # track the maximum required discharge (peak cumulative discharge)
        max_required_energy_kwh = max(max_required_energy_kwh, energy_balance_kwh)

        rows.append({
            "scenario": scenario_name,
            "total_load_kw": round(total_load_kw, 2),
            "duration_hr": round(duration_hr, 2),
            "voyage_count": int(voyage_count),
            "daily_duration_hr": round(daily_duration_hr, 2),
            "running_gen": running_gen,
            "weighted_avg_load_kw": round(weighted_avg_load_kw, 2),
            "average_running_gen": round(average_running_gen, 2),
            "ess_gen_recommended_kw": round(ess_generator_recommended_kw, 2),
            "gen_target_kw": round(generator_target_kw, 2),
            "ess_power_kw": round(ess_power_kw, 2),
            "ess_mode": ess_mode,
            "ess_energy_kwh": round(ess_energy_kwh, 2),
        })

    # ESS battery sizing using SOC min/max (DoD = usable SOC range)
    soc_min = 0.2
    soc_max = 0.8
    usable_soc_range = soc_max - soc_min
    # Use the maximum cumulative discharge (max_required_energy_kwh) to size battery.
    battery_capacity_kwh = round((max_required_energy_kwh / usable_soc_range) if max_required_energy_kwh > 0 else 0.0, 2)

    summary = {
        "weighted_avg_load_kw": round(weighted_avg_load_kw, 2),
        "average_running_gen": round(average_running_gen, 2),
        "ess_generator_recommended_kw": round(ess_generator_recommended_kw, 2),
        "required_energy_kwh": round(max_required_energy_kwh, 2),
        "battery_capacity_kwh": battery_capacity_kwh,
        "soc_min": soc_min,
        "soc_max": soc_max,
        "dod": round(usable_soc_range, 2),
    }

    ess_result_df = pd.DataFrame(rows)
    # Add SOC simulation columns to the result dataframe
    try:
        ess_result_df = add_soc_simulation_columns(
                ess_result_df,
                battery_capacity_kwh=battery_capacity_kwh,
                soc_init=0.8,
                soc_min=0.2,
                soc_max=0.8,
            )
    except Exception:
        # on any failure, return original df without SOC cols
        pass

    return ess_result_df, summary


def render_generator_capacity_application_table(application_df: pd.DataFrame) -> None:
    st.dataframe(application_df, use_container_width=True)


def render_generator_capacity_cards(recommendation_df: pd.DataFrame, selected_index: int, generator_count_text: str) -> int:
    st.markdown(
        """
        <style>
        .gen-cap-index {
            font-size: 16px;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .gen-cap-card {
            border: 2px solid #cbd5e1;
            padding: 18px 12px 10px 12px;
            text-align: center;
            min-height: 120px;
            background: #ffffff;
        }
        .gen-cap-card.selected {
            border-color: #2563eb;
            background: #eff6ff;
        }
        .gen-cap-kw {
            font-size: 24px;
            font-weight: 800;
            margin-bottom: 12px;
        }
        .gen-cap-sub {
            font-size: 14px;
            margin-top: 6px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(len(recommendation_df))
    new_selected_index = selected_index

    for idx, (_, row) in enumerate(recommendation_df.iterrows()):
        target_pct = int(round(_safe_float(row.get("target_load_percent"))))
        recommended_kw = _safe_float(row.get("recommended_generator_capacity_kw"))
        # only show visual selection when a generator option is actually applied
        selected_class = " selected" if (selected_index is not None and st.session_state.get("generator_capacity_option_applied", False) and idx == selected_index) else ""
        with cols[idx]:
            st.markdown(f'<div class="gen-cap-index">{idx + 1}.</div>', unsafe_allow_html=True)
            st.markdown(
                (
                    f'<div class="gen-cap-card{selected_class}">'
                    f'<div class="gen-cap-kw">{recommended_kw:.1f}kW</div>'
                    f'<div class="gen-cap-sub">LOAD PERCENT OF GEN. : {target_pct}%</div>'
                    f'</div>'
                ),
                unsafe_allow_html=True,
            )
            if st.button("적용", key=f"generator_capacity_option_{idx}", use_container_width=True):
                already_selected = st.session_state.get("selected_generator_capacity_option") == idx
                already_applied = st.session_state.get("generator_capacity_option_applied", False)
                new_selected_index = idx
                st.session_state["selected_generator_capacity_option"] = idx
                new_applied = not (already_selected and already_applied)
                st.session_state["generator_capacity_option_applied"] = new_applied
                if new_applied:
                    st.session_state["selected_sizing_mode"] = "generator_only"
                    st.session_state["generator_ess_option_applied"] = False
                else:
                    st.session_state["selected_sizing_mode"] = None
                st.rerun()

    return new_selected_index


def _normalize_token(value) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().upper()
    if not text:
        return ""
    # keep alphanumeric and underscore
    return re.sub(r"[^A-Z0-9_]", "", text)


def format_scenario_label(value) -> str:
    text = str(value)
    parts = text.split(" ", 1)
    if len(parts) == 2:
        return parts[0] + "\n" + parts[1]
    return text


def add_soc_simulation_columns(
    ess_df: pd.DataFrame,
    battery_capacity_kwh: float,
    soc_init: float = 0.8,
    soc_min: float = 0.2,
    soc_max: float = 0.8,
) -> pd.DataFrame:
    """
    Simulate SOC start/end for each scenario row in order.

    Adds columns:
    - battery_energy_start_kwh
    - battery_energy_end_kwh
    - soc_start_pct
    - soc_end_pct

    Rules:
    - If battery_capacity_kwh <= 0: fill zeros for SOC-related cols.
    - Use existing 'ess_energy_kwh' when present; otherwise compute as ess_power_kw * duration_hr.
    - Do NOT multiply by voyage_count (scenario rows already represent repetitions).
    - Numeric values rounded to 2 decimals.
    """
    if ess_df is None or ess_df.empty:
        return ess_df

    df = ess_df.copy()

    # ensure numeric columns exist
    df["ess_power_kw"] = df.get("ess_power_kw", 0.0).apply(_safe_float) if isinstance(df.get("ess_power_kw", 0.0), pd.Series) else df.get("ess_power_kw", 0.0)
    df["duration_hr"] = df.get("duration_hr", 0.0).apply(_safe_float) if isinstance(df.get("duration_hr", 0.0), pd.Series) else df.get("duration_hr", 0.0)

    # compute ess_energy_kwh if not present: ess_power_kw * duration_hr
    if "ess_energy_kwh" not in df.columns:
        df["ess_energy_kwh"] = (
            pd.to_numeric(df.get("ess_power_kw", 0.0), errors="coerce").fillna(0.0)
            * pd.to_numeric(df.get("duration_hr", 0.0), errors="coerce").fillna(0.0)
        )
    df["ess_energy_kwh"] = df["ess_energy_kwh"].apply(_safe_float)

    try:
        battery_capacity_kwh = float(battery_capacity_kwh)
    except Exception:
        battery_capacity_kwh = 0.0

    cols_start = []
    cols_end = []
    cols_soc_start = []
    cols_soc_end = []

    if battery_capacity_kwh <= 0:
        # fill zeros for all rows
        for _ in range(len(df)):
            cols_start.append(0.0)
            cols_end.append(0.0)
            cols_soc_start.append(0.0)
            cols_soc_end.append(0.0)

        df["battery_energy_start_kwh"] = cols_start
        df["battery_energy_end_kwh"] = cols_end
        df["soc_start_pct"] = cols_soc_start
        df["soc_end_pct"] = cols_soc_end

        for c in ["ess_energy_kwh", "ess_power_kw", "duration_hr"]:
            if c in df.columns:
                df[c] = df[c].apply(lambda v: round(_safe_float(v), 2))

        return df

    battery_energy_max_kwh = battery_capacity_kwh * float(soc_max)
    battery_energy_min_kwh = battery_capacity_kwh * float(soc_min)

    # start from initial SOC
    battery_energy_start = battery_capacity_kwh * float(soc_init)

    for _, row in df.iterrows():
        ess_energy_kwh = _safe_float(row.get("ess_energy_kwh", 0.0))

        # Positive ess_energy_kwh means discharge (PTO): battery supplies energy
        if ess_energy_kwh > 0:
            battery_energy_end = battery_energy_start - ess_energy_kwh
        elif ess_energy_kwh < 0:
            # negative means net charge (PTI)
            battery_energy_end = battery_energy_start + abs(ess_energy_kwh)
        else:
            battery_energy_end = battery_energy_start

        # enforce limits
        battery_energy_end = max(battery_energy_min_kwh, min(battery_energy_end, battery_energy_max_kwh))

        soc_start = (battery_energy_start / battery_capacity_kwh) * 100.0 if battery_capacity_kwh else 0.0
        soc_end = (battery_energy_end / battery_capacity_kwh) * 100.0 if battery_capacity_kwh else 0.0

        cols_start.append(round(battery_energy_start, 2))
        cols_end.append(round(battery_energy_end, 2))
        cols_soc_start.append(round(soc_start, 2))
        cols_soc_end.append(round(soc_end, 2))

        battery_energy_start = battery_energy_end

    df["battery_energy_start_kwh"] = cols_start
    df["battery_energy_end_kwh"] = cols_end
    df["soc_start_pct"] = cols_soc_start
    df["soc_end_pct"] = cols_soc_end

    for c in ["ess_energy_kwh", "ess_power_kw", "duration_hr"]:
        if c in df.columns:
            df[c] = df[c].apply(lambda v: round(_safe_float(v), 2))

    return df


STARTING_PROFILES = [
    {
        "name": "해당 없음",
        "initial_pct": -1.0,
        "stop_pct": None,
        "motor_factor": None,
        "keywords": ["HEATER", "PRE-HEATER", "PRE HEATER", "LIGHT", "LIGHTING", "CHARGER", "온수기", "조명", "충전기", "히터"],
    },
    {
        "name": "프로펠러 직결, 고관성 부하",
        "initial_pct": 75.0,
        "stop_pct": 25.0,
        "motor_factor": 1.80,
        "keywords": ["PROPULSION", "PROPELLER", "THRUSTER", "추진", "프로펠러", "쓰러스터", "THRUST"],
    },
    {
        "name": "윈치, 크레인, 컨베이어",
        "initial_pct": 75.0,
        "stop_pct": 25.0,
        "motor_factor": 1.80,
        "keywords": ["WINCH", "WINDLASS", "CRANE", "CONVEYOR", "윈치", "크레인", "컨베이어"],
    },
    {
        "name": "압축기",
        "initial_pct": 50.0,
        "stop_pct": 40.0,
        "motor_factor": 1.70,
        "keywords": ["COMPRESSOR", "압축기", "AIR COMP"],
    },
    {
        "name": "스크류펌프, 기어펌프",
        "initial_pct": 25.0,
        "stop_pct": 50.0,
        "motor_factor": 1.65,
        "keywords": ["SCREW PUMP", "SCREW", "스크류펌프", "스크류", "GEAR PUMP", "GEAR", "기어펌프", "기어"],
    },
    {
        "name": "원심펌프, 팬, 블로워",
        "initial_pct": 0.0,
        "stop_pct": 60.0,
        "motor_factor": 1.60,
        "keywords": ["PUMP", "P/P", "펌프", "FAN", "BLOWER", "VENT", "팬", "송풍기"],
    },
]


def classify_starting_profile(electric_consumer: str) -> dict | None:
    normalized_name = _normalize_token(electric_consumer)
    if not normalized_name:
        return None

    # Iterate STARTING_PROFILES in order and return the first matching profile.
    for profile in STARTING_PROFILES:
        for keyword in profile.get("keywords", []):
            normalized_keyword = _normalize_token(keyword)
            if normalized_keyword and normalized_keyword in normalized_name:
                return profile

    return None


def build_scenario_load_percent_map(scenario_df: pd.DataFrame, generator_capacity_kw: float) -> dict[str, float]:
    if generator_capacity_kw <= 0:
        return {}

    scenario_load_pct = {}
    for _, row in scenario_df.iterrows():
        scenario_name = str(row.get("scenario", ""))
        total_load_kw = _safe_float(row.get("total_load_kw"))
        load_pct = (total_load_kw / generator_capacity_kw) * 100.0 if generator_capacity_kw else 0.0

        # store full scenario key
        full_key = _normalize_token(scenario_name)
        scenario_load_pct[full_key] = load_pct

        # also store mode-only key (e.g., 'PORT_IN_OUT' from 'V01-02 PORT_IN_OUT')
        parts = scenario_name.split(" ", 1)
        if len(parts) == 2:
            mode_key = _normalize_token(parts[1])
            if mode_key:
                # keep the maximum load_pct for duplicate mode keys
                if mode_key not in scenario_load_pct:
                    scenario_load_pct[mode_key] = load_pct
                else:
                    scenario_load_pct[mode_key] = max(scenario_load_pct[mode_key], load_pct)
    return scenario_load_pct


def find_peak_mode_for_row(row: pd.Series, modes: list[str], il_df: float) -> str:
    best_mode = ""
    best_load = 0.0

    for mode in modes:
        cl_value = _safe_float(row.get(f"{mode}_CL"))
        il_applied_value = row.get(f"{mode}_IL_APPLIED")
        if pd.isna(il_applied_value):
            il_applied_value = _safe_float(row.get(f"{mode}_IL")) / float(il_df) if il_df else _safe_float(row.get(f"{mode}_IL"))
        else:
            il_applied_value = _safe_float(il_applied_value)

        mode_load = cl_value + il_applied_value
        if mode_load > best_load:
            best_load = mode_load
            best_mode = mode

    return best_mode


def recommend_starting_method(
    electric_consumer: str,
    motor_output_kw: float,
    scenario_name: str,
    scenario_load_pct_map: dict[str, float],
    generator_capacity_kw: float,
) -> str:
    profile = classify_starting_profile(electric_consumer)
    if profile is None:
        return "검토 필요"

    if profile["name"] == "해당 없음":
        return "해당 없음"

    load_percent = scenario_load_pct_map.get(_normalize_token(scenario_name))
    if load_percent is None:
        return "검토 필요"

    initial_pct = float(profile["initial_pct"])

    if motor_output_kw * float(profile["motor_factor"]) > generator_capacity_kw:
        return "Auto Transformer"

    if initial_pct in {0.0, 25.0}:
        if motor_output_kw <= generator_capacity_kw * 0.10:
            return "직입기동 (DOL)"
        return "Open Y-Δ"

    delta = load_percent - float(profile["stop_pct"])
    if delta <= 0:
        return "Open Y-Δ (DOL 비권장)"
    if delta <= 10:
        return "Open Y-Δ"
    if delta <= 20:
        return "Auto Transformer"
    return "Auto Transformer (검토 필요)"

def build_editable_input_data() -> dict:
    input_data = deepcopy(build_input_data_from_dummy(DEFAULT_INPUT_DATA))

    # ELA 업로드 전에는 load 항목 기본값을 0으로 초기화
    for scenario in input_data["scenarios"]:
        scenario["continuous_load_kw"] = 0.0
        scenario["intermittent_load_raw_kw"] = 0.0
        scenario["diversity_factor"] = 1.0
        scenario["intermittent_load_kw"] = 0.0
        scenario["hotel_load_kw"] = 0.0
        scenario["deck_machinery_load_kw"] = 0.0
        scenario["aux_load_kw"] = 0.0
        scenario["propulsion_load_kw"] = 0.0

    # ELA 결과가 있으면 default 값을 ELA 값으로 덮어쓰기
    ela_payload = st.session_state.get("adapter_handoff_payload", None)
    ela_df = st.session_state.get("adapter_handoff_df", None)
    if isinstance(ela_payload, dict):
        payload_summary = ela_payload.get("summary")
        if isinstance(payload_summary, pd.DataFrame):
            ela_df = payload_summary
        elif payload_summary is not None:
            ela_df = pd.DataFrame(payload_summary)

    if isinstance(ela_df, dict):
        ela_df = pd.DataFrame(ela_df)
    elif ela_df is None:
        ela_df = pd.DataFrame()
    elif not isinstance(ela_df, pd.DataFrame):
        ela_df = pd.DataFrame(ela_df)

    ela_loaded = not ela_df.empty
    if ela_loaded:
        try:
            input_data = build_input_data_from_ela_result(ela_df, input_data)
        except Exception as e:
            st.warning(f"ELA 연동 실패: {e}")

    return input_data


def render_voyage_scenario_planner(
    available_scenarios: list[str],
    scenario_df: pd.DataFrame,
    input_data: dict,
) -> None:
    st.markdown(
        """
        <style>
        div[data-testid="stButton"] button[kind="secondary"] {
            font-size: 1.05rem;
            padding: 0.5rem 1rem;
            font-weight: 600;
        }
        .full-width-button {
            margin: 0 0.25rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="full-width-button">', unsafe_allow_html=True)
    if st.button("선종별 운항 시나리오 (참고)", key="vessel_type_duration_hr", use_container_width=True):
        st.session_state["show_vessel_duration_buttons"] = not st.session_state.get("show_vessel_duration_buttons", False)

    if st.session_state.get("show_vessel_duration_buttons", False):
        # 선종 목록과 기본 duration 매핑 (클릭 시 운항 시나리오에 적용)
        vessel_names = [
            "방제정",
            "예인선",
            "유류바지",
            "행정선",
            "실습선",
            "여객선",
            "조사선",
        ]
        vessel_duration_map = {
            "방제정": [1.0, 1.0, 3.0, 1.0],
            "예인선": [1.0, 1.0, 2.0, 1.0],
            "유류바지": [1.0, 1.0, 2.0, 1.0],
            "행정선": [2.0, 1.0, 1.0, 1.0],
            "실습선": [3.0, 1.0, 0.5, 0.5],
            "여객선": [1.5, 0.5, 0.0, 0.5],
            "조사선": [2.0, 1.0, 4.0, 1.0],
        }
        default_mode_sequence = ["NORMAL", "PORT_IN_OUT", "WORKING", "HARBOUR"]

        vessel_cols = st.columns(len(vessel_names))
        for name, col in zip(vessel_names, vessel_cols):
            if col.button(name, key=f"vessel_duration_btn_{name}", use_container_width=True):
                # 버튼 클릭 시 voyage_rows를 해당 선종의 기본 시나리오로 설정
                rows: list[dict] = []
                for idx, mode in enumerate(default_mode_sequence):
                    duration = float(vessel_duration_map.get(name, [0, 0, 0, 0])[idx])
                    rows.append({"id": idx, "mode": mode, "duration_hr": duration, "running_gen": 1})

                # 세션에 저장 (widget 기본값을 즉시 반영하려면 개별 widget 키도 설정)
                st.session_state["voyage_rows"] = rows
                st.session_state["next_voyage_row_id"] = len(rows)
                for r in rows:
                    rid = int(r["id"])
                    st.session_state[f"voyage_mode_{rid}"] = r["mode"]
                    st.session_state[f"voyage_duration_{rid}"] = float(r["duration_hr"])
                    st.session_state[f"voyage_running_gen_{rid}"] = int(r.get("running_gen", 1))
                st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)
    st.subheader("Voyage Planning")
    voyage_count = st.number_input(
        "일일 항차 횟수",
        min_value=1,
        value=int(st.session_state.get("voyage_count", 1)),
        step=1,
        key="voyage_count",
    )
    st.caption(f"선택된 항차 횟수: {voyage_count}")

    st.markdown("**운항 시나리오 정의**")
    header_mode, header_duration, header_running_gen, _ = st.columns([2, 1, 1, 0.5])
    header_mode.markdown("**Mode**")
    header_duration.markdown("**duration_hr**")
    header_running_gen.markdown("**running_gen**")

    default_mode_options = ["NORMAL", "PORT_IN_OUT", "WORKING", "HARBOUR"]
    mode_options = [str(mode) for mode in available_scenarios if str(mode).strip()]
    if not mode_options:
        mode_options = default_mode_options
    next_voyage_row_id = int(st.session_state.get("next_voyage_row_id", 0))
    if "voyage_rows" not in st.session_state:
        # 초기에는 Mode가 위->아래로 NORMAL, PORT_IN_OUT, WORKING, HARBOUR 순서로 보이도록 설정
        st.session_state["voyage_rows"] = [
            {"id": idx, "mode": default_mode_options[idx], "duration_hr": 0.0, "running_gen": 1}
            for idx in range(4)
            ]
        next_voyage_row_id = 4
    for idx, row in enumerate(st.session_state["voyage_rows"]):
        if "id" not in row:
            row["id"] = next_voyage_row_id
            next_voyage_row_id += 1
        if str(row.get("mode", "")).strip() not in mode_options:
            # mode_options에 없는 경우 기본 순서대로 대응
            if idx < len(default_mode_options):
                row["mode"] = default_mode_options[idx]
            else:
                row["mode"] = mode_options[0]
    st.session_state["next_voyage_row_id"] = next_voyage_row_id
    
    if st.button("＋ 행 추가", key="add_voyage_row"):
        row_id = int(st.session_state.get("next_voyage_row_id", 0))
        st.session_state["voyage_rows"].append({"id": row_id, "mode": mode_options[0], "duration_hr": 0.0, "running_gen": 1})
        st.session_state["next_voyage_row_id"] = row_id + 1

    remove_row_id = None
    for idx, row in enumerate(st.session_state["voyage_rows"]):
        row_id = int(row.get("id", idx))
        col_mode, col_duration, col_running_gen, col_action = st.columns([2, 1, 1, 0.5])
        current_mode = str(row.get("mode", mode_options[0]))
        select_options = mode_options if current_mode in mode_options else [*mode_options, current_mode]
        current_mode_index = select_options.index(current_mode) if current_mode in select_options else 0

        # selectbox: if session state already has the key, let Streamlit use it (avoid passing index)
        mode_key = f"voyage_mode_{row_id}"
        if mode_key in st.session_state:
            row["mode"] = col_mode.selectbox(
                "Mode",
                options=select_options,
                key=mode_key,
                label_visibility="collapsed",
            )
        else:
            row["mode"] = col_mode.selectbox(
                "Mode",
                options=select_options,
                index=current_mode_index,
                key=mode_key,
                label_visibility="collapsed",
            )

        # duration: avoid passing a default value when session state already set for the widget
        duration_key = f"voyage_duration_{row_id}"
        if duration_key in st.session_state:
            row["duration_hr"] = col_duration.number_input(
                "duration_hr",
                min_value=0.0,
                step=1.0,
                key=duration_key,
                label_visibility="collapsed",
            )
        else:
            row["duration_hr"] = col_duration.number_input(
                "duration_hr",
                min_value=0.0,
                value=float(row.get("duration_hr", 0.0)),
                step=1.0,
                key=duration_key,
                label_visibility="collapsed",
            )

        # running_gen: same approach to avoid widget/session-state conflict warnings
        running_key = f"voyage_running_gen_{row_id}"
        if running_key in st.session_state:
            row["running_gen"] = int(col_running_gen.number_input(
                "running_gen",
                min_value=0,
                step=1,
                key=running_key,
                label_visibility="collapsed",
            ))
        else:
            row["running_gen"] = int(col_running_gen.number_input(
                "running_gen",
                min_value=0,
                value=int(row.get("running_gen", 1)),
                step=1,
                key=running_key,
                label_visibility="collapsed",
            ))
        if col_action.button("－", key=f"remove_voyage_row_{row_id}"):
            remove_row_id = row_id

    if remove_row_id is not None:
        st.session_state["voyage_rows"] = [row for row in st.session_state["voyage_rows"] if int(row.get("id", -1)) != remove_row_id]
        st.rerun()
        
    st.markdown("<div style='height: 1.25rem;'></div>", unsafe_allow_html=True)
    if st.button("발전기 및 ESS 배터리 적정 용량 산정", key="size_generator_ess", use_container_width=True):
        st.session_state["show_generator_sizing"] = True

    if st.session_state.get("show_generator_sizing", False):
        main_df = st.session_state.get("main_df", pd.DataFrame())
        ela_meta = st.session_state.get("ela_meta", {})

        if main_df.empty:
            st.warning("ELA 파일을 먼저 업로드해주세요.")
            return

        consumer_col = ela_meta.get("consumer_col", "ELEC. CONSUMER")
        output_col = ela_meta.get("output_col", "OUTPUT(KW)")
        qty_col = ela_meta.get("qty_col", "Q'TY")
        working_col = ela_meta.get("working_col", "WORKING")
        modes = ela_meta.get("modes", [])
        il_df = float(ela_meta.get("il_df", 1.0))
        generator_count_text = ""

        source_input_col = "INPUT_USED" if "INPUT_USED" in main_df.columns else ela_meta.get("input_col", "INPUT(KW)")
        if source_input_col not in main_df.columns:
            st.warning("input load 컬럼을 찾을 수 없습니다.")
            return

        recommendation_df = build_generator_capacity_recommendation_df(scenario_df)
        if recommendation_df.empty:
            st.warning("발전기 용량 추천을 계산할 수 없습니다.")
            return

        # Keep internal selected index separate from visual 'applied' state.
        selected_option = st.session_state.get("selected_generator_capacity_option", None)
        if selected_option is None or not (0 <= int(selected_option) < len(recommendation_df)):
            # do not visually select any card by default; use first row for internal calculations
            selected_option = None
            st.session_state["selected_generator_capacity_option"] = None

        # For calculation purposes, use the first recommendation if none selected
        calc_index = int(selected_option) if selected_option is not None else 0
        selected_row = recommendation_df.iloc[calc_index]
        generator_capacity_kw = _safe_float(selected_row["recommended_generator_capacity_kw"])
        scenario_load_pct_map = build_scenario_load_percent_map(scenario_df, generator_capacity_kw)

        top3_df = main_df.copy()
        top3_df[source_input_col] = pd.to_numeric(top3_df[source_input_col], errors="coerce").fillna(0.0)
        top3_df = top3_df.nlargest(3, source_input_col)

        result_df = pd.DataFrame(
            {
                "electric consumer": top3_df[consumer_col].astype(str),
                "output": pd.to_numeric(top3_df[output_col], errors="coerce").fillna(0.0).round(2),
                "q'ty": pd.to_numeric(top3_df[qty_col], errors="coerce").fillna(0).astype(int),
                "working": pd.to_numeric(top3_df[working_col], errors="coerce").fillna(0).astype(int),
                "input load": top3_df[source_input_col].round(2),
            }
        )

        def format_mode_load(cl: float, il: float) -> str:
            cl_text = f"C.L : {cl:.2f}" if cl != 0 else ""
            il_text = f"I.L : {il:.2f}" if il != 0 else ""
            if cl_text and il_text:
                return f"{cl_text} / {il_text}"
            if cl_text:
                return cl_text
            if il_text:
                return il_text
            return "-"

        for mode in modes:
            cl_col = f"{mode}_CL"
            il_col = f"{mode}_IL"
            cl_values = pd.to_numeric(top3_df.get(cl_col, 0), errors="coerce").fillna(0.0)
            il_values = pd.to_numeric(top3_df.get(il_col, 0), errors="coerce").fillna(0.0)
            result_df[f"{mode} load"] = [format_mode_load(cl, il) for cl, il in zip(cl_values, il_values)]

        recommendations = []
        for _, row in top3_df.iterrows():
            peak_mode = find_peak_mode_for_row(row, modes, il_df)
            recommendations.append(
                recommend_starting_method(
                    electric_consumer=str(row.get(consumer_col, "")),
                    motor_output_kw=_safe_float(row.get(output_col)),
                    scenario_name=peak_mode,
                    scenario_load_pct_map=scenario_load_pct_map,
                    generator_capacity_kw=generator_capacity_kw,
                )
            )

        result_df["권장 기동 방식"] = recommendations

        st.subheader("발전기 용량 추천")
        render_generator_capacity_cards(recommendation_df, selected_option, generator_count_text)

        # --- ESS Recommendation Card (single) ---
        voyage_rows = st.session_state.get("voyage_rows", [])
        voyage_count = int(st.session_state.get("voyage_count", 1))
        try:
            ess_df, ess_summary = build_ess_capacity_sizing_df(
                scenario_df=scenario_df,
                voyage_rows=voyage_rows,
                voyage_count=voyage_count,
            )
        except Exception:
            ess_df, ess_summary = pd.DataFrame(), {}

        st.subheader("발전기 + ESS 용량 추천")
        # Render a single card using existing generator card styles (gen-cap-card, gen-cap-kw, gen-cap-sub)
        ess_gen_kw = _safe_float(ess_summary.get("ess_generator_recommended_kw", 0.0))
        ess_batt_kwh = _safe_float(ess_summary.get("battery_capacity_kwh", 0.0))
        main_text = f"{ess_gen_kw:.1f}kW + {ess_batt_kwh:.1f}kWh"
        sub_text = "LOAD PERCENT OF GEN. : 80%"

        # determine selected class visually based on selected_sizing_mode
        selected_class = " selected" if st.session_state.get("selected_sizing_mode") == "generator_ess" else ""
        # place card full width with selection styling
        st.markdown(
            (
                f'<div class="gen-cap-card{selected_class}">'
                f'<div class="gen-cap-kw">{main_text}</div>'
                f'<div class="gen-cap-sub">{sub_text}</div>'
                f'</div>'
            ),
            unsafe_allow_html=True,
        )

        # ESS apply button (text changed to '적용')
        if st.button("적용", key="generator_ess_apply", use_container_width=True):
            st.session_state["selected_sizing_mode"] = "generator_ess"
            st.session_state["generator_ess_option_applied"] = True
            # unset generator_only applied to avoid conflict and clear selected index
            st.session_state["generator_capacity_option_applied"] = False
            st.session_state["selected_generator_capacity_option"] = None
            st.session_state["selected_generator_capacity_option"] = None
            st.rerun()

        # Determine which mode is applied
        generator_only_applied = st.session_state.get("generator_capacity_option_applied", False)
        generator_ess_applied = st.session_state.get("generator_ess_option_applied", False)

        # Pick peak_total_load_kw for application table (from selected_row)
        peak_total_load_kw = _safe_float(selected_row.get("peak_total_load_kw", 0.0))

        # Branch display based on selected mode
        if generator_only_applied:
            # 1) 발전기 대수별 비교
            st.subheader("발전기 대수별 비교")
            application_df = build_generator_capacity_application_df(
                peak_total_load_kw=peak_total_load_kw,
                generator_capacity_kw=generator_capacity_kw,
                max_generators=4,
            )
            render_generator_capacity_application_table(application_df)

            # 2) Top 3 개별 부하 (input load 기준)
            st.subheader("Top 3 개별 부하 (input load 기준)")
            st.dataframe(result_df, use_container_width=True)

            # 3) Load Profile
            st.subheader("Load Profile")

            load_profile_df = scenario_df[["scenario", "total_load_kw"]].copy()
            load_profile_df["scenario_label"] = load_profile_df["scenario"].astype(str).apply(format_scenario_label)
            load_profile_df["total_load_kw"] = (
                pd.to_numeric(load_profile_df["total_load_kw"], errors="coerce").fillna(0.0).round(1)
            )

            # add selected generator capacity baseline (single value repeated)
            load_profile_df["generator_capacity_kw"] = round(float(generator_capacity_kw), 1)

            chart_df = load_profile_df.melt(
                id_vars=["scenario", "scenario_label"],
                value_vars=["total_load_kw", "generator_capacity_kw"],
                var_name="series",
                value_name="value",
            )

            chart = alt.Chart(chart_df).mark_line(point=True).encode(
                x=alt.X(
                    "scenario_label:N",
                    title="scenario",
                    axis=alt.Axis(
                        labelAngle=0,
                        labelFontSize=11,
                        labelAlign="center",
                        labelBaseline="middle",
                        labelLimit=300,
                    ),
                    sort=None,
                ),
                y=alt.Y(
                    "value:Q",
                    title="kW",
                    axis=alt.Axis(orient="left"),
                ),
                color=alt.Color(
                    "series:N",
                    title="",
                    scale=alt.Scale(domain=["total_load_kw", "generator_capacity_kw"], range=["#1f77b4", "#ff7f0e"]),
                ),
                strokeDash=alt.StrokeDash(
                    "series:N",
                    title="",
                    scale=alt.Scale(domain=["total_load_kw", "generator_capacity_kw"], range=[[], [6, 3]]),
                ),
                tooltip=[
                    alt.Tooltip("scenario:N", title="scenario"),
                    alt.Tooltip("series:N", title="series"),
                    alt.Tooltip("value:Q", title="value", format=".1f"),
                ],
            ).properties(width="container", height=360)

            st.altair_chart(chart, use_container_width=True)

        elif generator_ess_applied:
            # 1) 발전기 대수별 비교 (Generator + ESS)
            st.subheader("발전기 대수별 비교")
            ess_gen_kw = _safe_float(ess_summary.get("ess_generator_recommended_kw", 0.0))
            batt_kwh = _safe_float(ess_summary.get("battery_capacity_kwh", 0.0))
            application_df = build_generator_ess_application_df(
                peak_total_load_kw=peak_total_load_kw,
                ess_generator_capacity_kw=ess_gen_kw,
                battery_capacity_kwh=batt_kwh,
                ess_df=ess_df,
                max_generators=4,
            )
            render_generator_capacity_application_table(application_df)

            # 2) Top 3 개별 부하 (input load 기준)
            st.subheader("Top 3 개별 부하 (input load 기준)")
            # Do not show recomputed '권장 기동 방식' in Generator+ESS mode
            result_df_ess = result_df.copy()
            if "권장 기동 방식" in result_df_ess.columns:
                result_df_ess = result_df_ess.drop(columns=["권장 기동 방식"])
            st.dataframe(result_df_ess, use_container_width=True)

            # 3) Load Profile
            st.subheader("Load Profile")

            load_profile_df = scenario_df[["scenario", "total_load_kw"]].copy()

            ess_profile_df = (
                ess_df[["scenario", "ess_energy_kwh", "soc_start_pct", "soc_end_pct"]].copy()
                if ess_df is not None and not ess_df.empty and "ess_energy_kwh" in ess_df.columns
                else pd.DataFrame({"scenario": [], "ess_energy_kwh": [], "soc_start_pct": [], "soc_end_pct": []})
            )

            load_profile_df = load_profile_df.merge(ess_profile_df, on="scenario", how="left")

            load_profile_df["scenario_label"] = load_profile_df["scenario"].astype(str).apply(format_scenario_label)

            # Create a CHARGING column by inverting raw ESS energy values.
            # This avoids modifying raw ess_energy_kwh while reflecting the requested sign flip.
            load_profile_df["charging_pct"] = (-1.0 * pd.to_numeric(load_profile_df["ess_energy_kwh"], errors="coerce").fillna(0.0)).round(1)

            # Prefer the SOC end percentage already calculated by the ESS sizing formula.
            if "soc_end_pct" in load_profile_df.columns and load_profile_df["soc_end_pct"].notna().any():
                load_profile_df["soc_pct"] = pd.to_numeric(load_profile_df["soc_end_pct"], errors="coerce").fillna(0.0).round(1)
            else:
                # Prepare data and get soc_start values if available
                soc_start_data = {}
                if ess_df is not None and not ess_df.empty and "soc_start_pct" in ess_df.columns:
                    for _, row in ess_df.iterrows():
                        soc_start_data[str(row["scenario"])] = float(row["soc_start_pct"])

                # Calculate soc_pct cumulatively using previous CHARGING value:
                # - First SOC = soc_start (default 80%)
                # - For i>0: SOC[i] = SOC[i-1] + CHARGING[i-1]
                soc_values = []
                cumulative_soc = None
                prev_charging = 0.0
                for idx, row in load_profile_df.iterrows():
                    # current row's charging value (used as prev_charging for next row)
                    current_charging = float(row.get("charging_pct", 0.0)) if pd.notna(row.get("charging_pct", None)) else 0.0

                    if cumulative_soc is None:
                        # First row: use soc_start (typically 80%)
                        scenario = str(row["scenario"])
                        cumulative_soc = soc_start_data.get(scenario, 80.0)
                        soc_values.append(cumulative_soc)
                    else:
                        # Subsequent rows: add previous row's charging (prev_charging)
                        cumulative_soc = cumulative_soc + prev_charging
                        # clamp to 0-100%
                        cumulative_soc = max(0.0, min(cumulative_soc, 100.0))
                        soc_values.append(cumulative_soc)

                    # update prev_charging for next iteration
                    prev_charging = current_charging

                load_profile_df["soc_pct"] = soc_values

            # ensure numeric and round to 1 decimal
            for c in ["total_load_kw", "charging_pct", "soc_pct"]:
                if c in load_profile_df.columns:
                    load_profile_df[c] = pd.to_numeric(load_profile_df[c], errors="coerce").fillna(0.0).round(1)
                else:
                    load_profile_df[c] = 0.0

            # melt to long form using internal column names
            chart_df = load_profile_df.melt(
                id_vars=["scenario", "scenario_label"],
                value_vars=[
                    "total_load_kw",
                    "charging_pct",
                    "soc_pct",
                ],
                var_name="series",
                value_name="value",
            )

            # map series to legend labels requested
            legend_labels = {
                "total_load_kw": "total_load (kW)",
                "charging_pct": "charging (%)",
                "soc_pct": "soc (%)",
            }
            chart_df["series_label"] = chart_df["series"].map(legend_labels).fillna(chart_df["series"])

            # color and dash settings
            domain = list(legend_labels.values())
            color_range = ["#1f77b4", "#2CA02C", "#9467bd"]
            dash_map = [[], [], []]

            base_x = alt.X(
                "scenario_label:N",
                title="scenario",
                axis=alt.Axis(labelAngle=0, labelFontSize=11, labelAlign="center", labelBaseline="middle", labelLimit=300),
                sort=None,
            )

            # Separate data for left axis (kW) and right axis (%)
            chart_df["axis"] = chart_df["series"].apply(
                lambda s: "left" if s == "total_load_kw" else "right"
            )

            # area for soc_pct (y-axis: right for percentage)
            area = (
                alt.Chart(chart_df)
                .transform_filter(alt.datum.series == "soc_pct")
                .mark_area(opacity=0.25, color="#9467bd")
                .encode(
                    x=base_x,
                    y=alt.Y("value:Q", title="soc (%)", axis=alt.Axis(orient="right")),
                    tooltip=[alt.Tooltip("scenario:N", title="scenario"), alt.Tooltip("series_label:N", title="series"), alt.Tooltip("value:Q", title="value", format=".1f")],
                )
            )

            # lines for all series
            lines = (
                alt.Chart(chart_df)
                .mark_line(point=True)
                .encode(
                    x=base_x,
                    y=alt.Y("value:Q", title="kW / kWh", axis=alt.Axis(orient="left")),
                    color=alt.Color("series_label:N", title="", scale=alt.Scale(domain=domain, range=color_range)),
                    strokeDash=alt.StrokeDash("series_label:N", title="", scale=alt.Scale(domain=domain, range=dash_map)),
                    tooltip=[alt.Tooltip("scenario:N", title="scenario"), alt.Tooltip("series_label:N", title="series"), alt.Tooltip("value:Q", title="value", format=".1f")],
                )
            )

            # Combine layers
            chart = (
                alt.layer(area, lines)
                .properties(width="container", height=360)
            )

            st.altair_chart(chart, use_container_width=True)

            # 4) ESS 배터리 용량 산정 결과 (ESS 모드에서만 표시)
            st.subheader("ESS 배터리 용량 산정 결과")
            if not ess_df.empty:
                # Prepare a display-only dataframe: keep internal columns in ess_df unchanged
                ess_display_df = ess_df.copy()

                # Rename SOC columns and keep some legacy display renames (doesn't affect internal df)
                rename_map = {
                    "soc_start_pct": "SOC Start (%)",
                    "soc_end_pct": "SOC End (%)",
                    "average_running_gen": "avg_run_gen",
                    "ess_gen_recommended_kw": "ess_gen_kw",
                    "ess_generator_recommended_kw": "ess_gen_kw",
                    "gen_target_kw": "gen_target_kw",
                    "ess_mode": "SGM",
                }
                ess_display_df = ess_display_df.rename(columns=rename_map)

                # display charging_pct in the results table with inverted sign from raw ESS energy
                if "charging_pct" in load_profile_df.columns:
                    charging_map = load_profile_df.set_index("scenario")["charging_pct"].to_dict()
                    ess_display_df["charging (%)"] = ess_display_df["scenario"].map(charging_map).fillna(0.0).round(1)

                # Select only the columns to display (if they exist)
                display_columns = [
                    "scenario",
                    "total_load_kw",
                    "duration_hr",
                    "gen_target_kw",
                    "ess_power_kw",
                    "SGM",
                    "charging (%)",
                    "SOC Start (%)",
                    "SOC End (%)",
                ]

                ess_display_df = ess_display_df[[c for c in display_columns if c in ess_display_df.columns]]

                st.dataframe(ess_display_df, use_container_width=True)

        else:
            st.info("발전기 또는 발전기+ESS 추천에서 '적용'을 누르면 상세 결과가 표시됩니다.")
    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    st.title("Power System Scenario Profile Tool")
    
    st.subheader("📂 ELA Upload")

    if "main_il_diversity_factor" not in st.session_state:
        st.session_state["main_il_diversity_factor"] = 2.0

    uploaded_file = st.file_uploader("Upload ELA Excel", type=["xlsx", "xls"])

    # placeholder for success message (will be filled after parsing)
    success_placeholder = st.empty()

    if uploaded_file is not None:
        current_file_id = f"{uploaded_file.name}_{uploaded_file.size}"

        if st.session_state.get("last_uploaded_file_id") != current_file_id:
            st.session_state["last_uploaded_file_id"] = current_file_id

            try:
                excel_df = get_excel_diversity_factor(uploaded_file)
            except Exception:
                excel_df = None

            if excel_df is not None:
                st.session_state["excel_diversity_factor"] = float(excel_df)
                st.session_state["main_il_diversity_factor"] = float(excel_df)
            else:
                st.session_state["excel_diversity_factor"] = None
                st.session_state["main_il_diversity_factor"] = 2.0

    il_df = st.number_input(
        "I.L Diversity Factor",
        min_value=0.1,
        max_value=10.0,
        step=0.1,
        key="main_il_diversity_factor",
    )



    if uploaded_file is not None:
        try:
            _, main_df, _, meta, _, adapter_handoff_payload = parse_ela_excel(uploaded_file, il_df=il_df)
            adapter_handoff_df = adapter_handoff_payload.get("summary", pd.DataFrame())
            if not isinstance(adapter_handoff_df, pd.DataFrame):
                adapter_handoff_df = pd.DataFrame(adapter_handoff_df)

            # 🔥 핵심: 세션에 저장
            st.session_state["adapter_handoff_payload"] = adapter_handoff_payload
            st.session_state["adapter_handoff_df"] = adapter_handoff_df
            st.session_state["main_df"] = main_df
            st.session_state["ela_meta"] = meta
            # propagate excel diversity factor into session state and main IL widget
            try:
                excel_df_val = meta.get("excel_diversity_factor", None)
                if excel_df_val is not None:
                    st.session_state["excel_diversity_factor"] = float(excel_df_val)
                    st.session_state["main_il_diversity_factor"] = float(excel_df_val)
            except Exception:
                pass

            # render success into the placeholder above the number_input
            try:
                success_placeholder.success("ELA loaded successfully")
            except Exception:
                st.success("ELA loaded successfully")

            # 확인용 (선택)
            display_df = adapter_handoff_df.drop(columns=["duration_hr"], errors="ignore")
            numeric_cols = display_df.select_dtypes(include="number").columns
            display_df[numeric_cols] = display_df[numeric_cols].round(2)
            st.dataframe(display_df, use_container_width=True)

        except Exception as e:
            st.error(f"ELA parsing error: {e}")
    else:
        st.session_state["adapter_handoff_payload"] = {}
        st.session_state["adapter_handoff_df"] = pd.DataFrame()
        st.session_state["show_generator_sizing"] = False
        st.info("ELA 파일 업로드 전에는 Scenario Load 값이 0으로 표시됩니다.")

    input_data = build_editable_input_data()

    available_scenarios = [
        str(scenario.get("name", ""))
        for scenario in input_data.get("scenarios", [])
        if str(scenario.get("name", "")).strip()
    ]

    voyage_rows = st.session_state.get("voyage_rows", [])
    voyage_count = int(st.session_state.get("voyage_count", 1))
    voyage_profile_df = build_voyage_profile_dataframe(
        mode_catalog_scenarios=input_data.get("scenarios", []),
        voyage_rows=voyage_rows,
        voyage_count=voyage_count,
    )
    if not voyage_profile_df.empty:
        input_data["scenarios"] = build_calc_scenarios_from_voyage_profile(voyage_profile_df)
        st.session_state["voyage_profile_df"] = voyage_profile_df
    else:
        st.session_state["voyage_profile_df"] = pd.DataFrame()

    try:
        scenario_df = build_scenario_dataframe(input_data)

    except Exception as exc:
        st.error(f"Calculation error: {exc}")
        st.stop()

    render_voyage_scenario_planner(available_scenarios, scenario_df, input_data)


if __name__ == "__main__":
    main()
