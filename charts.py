from __future__ import annotations

import pandas as pd
import matplotlib.pyplot as plt


def plot_load_profile(scenario_df: pd.DataFrame) -> None:
    x = scenario_df["scenario"]

    plt.figure(figsize=(10, 5))
    plt.stackplot(
        x,
        scenario_df["hotel_load_kw"],
        scenario_df["aux_load_kw"],
        scenario_df["propulsion_load_kw"],
        scenario_df["intermittent_load_kw"],
        labels=["Hotel", "Aux", "Propulsion", "Intermittent"],
    )
    plt.plot(x, scenario_df["total_load_kw"], marker="o", label="Total Load")
    plt.title("Scenario Load Profile")
    plt.ylabel("kW")
    plt.legend()
    plt.tight_layout()
    plt.show()


def plot_generator_loading(
    generator_loading_df: pd.DataFrame,
    target_lf_pct: float = 80.0,
    absolute_max_pct: float = 100.0,
) -> None:
    if generator_loading_df.empty:
        print("Generator loading dataframe is empty.")
        return

    pivot_df = (
        generator_loading_df
        .pivot(index="scenario", columns="generator", values="loading_pct")
        .fillna(0.0)
    )

    pivot_df.plot(kind="bar", figsize=(10, 5))
    plt.axhline(target_lf_pct, linestyle="--", label=f"Target LF {target_lf_pct:.0f}%")
    plt.axhline(absolute_max_pct, linestyle="--", label=f"Absolute Max {absolute_max_pct:.0f}%")
    plt.title("Generator Loading Profile")
    plt.ylabel("Load Factor (%)")
    plt.legend()
    plt.tight_layout()
    plt.show()


def plot_soc_profile(scenario_df: pd.DataFrame) -> None:
    if "soc_start_pct" not in scenario_df.columns or "soc_end_pct" not in scenario_df.columns:
        print("SOC columns not found.")
        return

    plt.figure(figsize=(10, 5))
    plt.plot(scenario_df["scenario"], scenario_df["soc_start_pct"], marker="o", label="SOC Start")
    plt.plot(scenario_df["scenario"], scenario_df["soc_end_pct"], marker="o", label="SOC End")
    plt.title("ESS SOC Profile")
    plt.ylabel("SOC (%)")
    plt.ylim(0, 100)
    plt.legend()
    plt.tight_layout()
    plt.show()


def plot_system_comparison(comparison_df: pd.DataFrame) -> None:
    """
    comparison_df 예시 컬럼:
    system_type, max_total_load_kw, avg_gen_loading_pct, max_gen_loading_pct, max_required_gen_count
    """
    if comparison_df.empty:
        print("Comparison dataframe is empty.")
        return

    metrics = [
        "max_total_load_kw",
        "avg_gen_loading_pct",
        "max_gen_loading_pct",
        "max_required_gen_count",
    ]

    for metric in metrics:
        if metric not in comparison_df.columns:
            continue

        plt.figure(figsize=(8, 4))
        plt.bar(comparison_df["system_type"], comparison_df[metric])
        plt.title(f"System Comparison - {metric}")
        plt.ylabel(metric)
        plt.tight_layout()
        plt.show()