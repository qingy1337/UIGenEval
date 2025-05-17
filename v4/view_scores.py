#!/usr/bin/env python3

import os
import json
import argparse
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
# import numpy as np # Not strictly needed for this version of stacked bar

# Define weights (MUST match the ones in your ui_benchmark_analyzer.py)
WEIGHT_TECHNICAL_QUALITY = 0.3
WEIGHT_PROMPT_ADHERENCE = 0.7

def find_summary_files(master_output_dir: Path, challenge_name: str):
    """
    Finds all MASTER_BENCHMARK_SUMMARY.json files for a given challenge
    across all its orchestrator runs.
    """
    challenge_path = master_output_dir / challenge_name
    if not challenge_path.is_dir():
        print(f"Error: Challenge directory not found: {challenge_path}")
        return []

    summary_files_info = []
    # Iterate through run_* directories (e.g., run_20250517-111716)
    for run_dir in challenge_path.iterdir():
        if run_dir.is_dir() and run_dir.name.startswith("run_"):
            # Iterate through model_name_timestamp directories (e.g., Groq-Llama4-Scout_20250517-111719)
            for model_instance_dir in run_dir.iterdir():
                if model_instance_dir.is_dir():
                    summary_file = model_instance_dir / "MASTER_BENCHMARK_SUMMARY.json"
                    if summary_file.is_file():
                        summary_files_info.append({
                            "path": summary_file,
                            "run_id": run_dir.name, # e.g., "run_20250517-111716"
                            "model_instance_id": model_instance_dir.name # e.g., "Groq-Llama4-Scout_20250517-111719"
                        })
    if not summary_files_info:
        print(f"No summary files found under {challenge_path}.")
    return summary_files_info

def parse_summary_data(summary_file_info):
    """
    Parses a single summary file and extracts relevant data.
    Returns a dictionary with the parsed data or None on error.
    """
    try:
        with open(summary_file_info["path"], 'r', encoding='utf-8') as f:
            data = json.load(f)

        model_name_from_json = data.get("benchmark_run_name", "UnknownModel")
        # Create a display name that includes the run_id for uniqueness in charts
        # if multiple runs are processed for the same model.
        display_model_name = f"{model_name_from_json}"

        agg_scores = data.get("aggregate_scores", {})
        overall_score = agg_scores.get("overall_weighted_score_from_totals", 0.0)

        tq_earned = agg_scores.get("total_tq_earned", 0.0)
        tq_max = agg_scores.get("total_tq_max", 0.0)
        adh_earned = agg_scores.get("total_adh_earned", 0.0)
        adh_max = agg_scores.get("total_adh_max", 0.0)

        # Calculate weighted components that sum up to the overall_score (as percentages)
        tq_contribution_pct = 0.0
        if tq_max > 0:
            tq_contribution_pct = (tq_earned / tq_max) * WEIGHT_TECHNICAL_QUALITY * 100

        adh_contribution_pct = 0.0
        if adh_max > 0:
            adh_contribution_pct = (adh_earned / adh_max) * WEIGHT_PROMPT_ADHERENCE * 100
            
        # Verification (optional):
        # calculated_overall = tq_contribution_pct + adh_contribution_pct
        # if abs(calculated_overall - overall_score) > 0.1: # Allow for small float discrepancies
        #     print(f"Warning: Score mismatch for {display_model_name}. JSON: {overall_score:.2f}, Calc: {calculated_overall:.2f}")

        return {
            "model_name_for_plot": display_model_name,
            "original_model_name": model_name_from_json,
            "run_id": summary_file_info["run_id"],
            "overall_score_pct": overall_score,
            "tq_contribution_pct": tq_contribution_pct,
            "adh_contribution_pct": adh_contribution_pct
        }
    except Exception as e:
        print(f"Error parsing summary file {summary_file_info['path']}: {e}")
        return None

def plot_total_scores(data_df: pd.DataFrame, output_dir: Path, challenge_name: str, plot_title_prefix: str):
    """
    Plots a bar chart of overall scores.
    """
    if data_df.empty:
        print("No data to plot for total scores.")
        return

    plt.figure(figsize=(max(10, len(data_df) * 0.8), 7)) # Adjust width based on number of models
    
    # Sort by overall score for better visualization
    data_df_sorted = data_df.sort_values(by="overall_score_pct", ascending=False)
    
    bars = plt.bar(data_df_sorted["model_name_for_plot"], data_df_sorted["overall_score_pct"], color='cornflowerblue')
    plt.xlabel("Model (Run ID)")
    plt.ylabel("Overall Weighted Score (%)")
    plt.title(f"{plot_title_prefix}Overall Benchmark Scores for Challenge: {challenge_name}")
    plt.xticks(rotation=60, ha="right", fontsize=9)
    plt.ylim(0, 100) # Scores are percentages
    plt.grid(axis='y', linestyle='--', alpha=0.7)

    # Add text labels on bars
    for bar in bars:
        yval = bar.get_height()
        plt.text(bar.get_x() + bar.get_width()/2.0, yval + 0.5, f'{yval:.2f}', ha='center', va='bottom', fontsize=8)

    plt.tight_layout()
    plot_filename = f"{challenge_name.replace(' ', '_')}_overall_scores.png"
    plot_path = output_dir / plot_filename
    plt.savefig(plot_path)
    print(f"Saved total scores plot to: {plot_path}")
    plt.close()

def plot_stacked_scores(data_df: pd.DataFrame, output_dir: Path, challenge_name: str, plot_title_prefix: str):
    """
    Plots a stacked bar chart of TQ and Adherence contributions to the overall score.
    """
    if data_df.empty:
        print("No data to plot for stacked scores.")
        return
        
    # Sort by overall score for consistent ordering with the other chart
    data_df_sorted = data_df.sort_values(by="overall_score_pct", ascending=False)
    
    model_names = data_df_sorted["model_name_for_plot"]
    tq_contributions = data_df_sorted["tq_contribution_pct"]
    adh_contributions = data_df_sorted["adh_contribution_pct"]

    plt.figure(figsize=(max(12, len(data_df) * 0.9), 8)) # Adjust width
    
    bar_width = 0.7
    
    # Bottom bars (Technical Quality)
    bars_tq = plt.bar(model_names, tq_contributions, color='lightcoral', edgecolor='grey', width=bar_width, label=f'Technical Quality (Weighted {WEIGHT_TECHNICAL_QUALITY*100:.0f}%)')
    # Top bars (Prompt Adherence), stacked on TQ
    bars_adh = plt.bar(model_names, adh_contributions, bottom=tq_contributions, color='lightskyblue', edgecolor='grey', width=bar_width, label=f'Prompt Adherence (Weighted {WEIGHT_PROMPT_ADHERENCE*100:.0f}%)')

    plt.xlabel("Model (Run ID)")
    plt.ylabel("Weighted Score Contribution (%)")
    plt.title(f"{plot_title_prefix}Score Breakdown for Challenge: {challenge_name}")
    plt.xticks(rotation=60, ha="right", fontsize=9)
    plt.ylim(0, 100) # Max possible score is 100%
    plt.legend(loc='upper right') # Auto-place legend, or use bbox_to_anchor for fine-tuning
    plt.grid(axis='y', linestyle='--', alpha=0.7)

    # Add text labels for total on top of stacked bars
    for i, model_name in enumerate(model_names):
        total_height = tq_contributions.iloc[i] + adh_contributions.iloc[i]
        plt.text(i, total_height + 0.5, f'{total_height:.2f}', ha='center', va='bottom', fontsize=8, fontweight='bold')
        # Optional: labels for individual components if space permits and values are significant
        # if tq_contributions.iloc[i] > 5: # only if component is somewhat large
        #    plt.text(i, tq_contributions.iloc[i]/2, f'{tq_contributions.iloc[i]:.1f}', ha='center', va='center', fontsize=7, color='white')
        # if adh_contributions.iloc[i] > 5:
        #    plt.text(i, tq_contributions.iloc[i] + adh_contributions.iloc[i]/2, f'{adh_contributions.iloc[i]:.1f}', ha='center', va='center', fontsize=7, color='white')


    plt.tight_layout()
    plot_filename = f"{challenge_name.replace(' ', '_')}_stacked_scores.png"
    plot_path = output_dir / plot_filename
    plt.savefig(plot_path)
    print(f"Saved stacked scores plot to: {plot_path}")
    plt.close()

def main():
    parser = argparse.ArgumentParser(description="Generate benchmark summary charts from orchestrator outputs.")
    parser.add_argument("master_output_dir", type=str, help="Base directory where orchestrator stores results (e.g., './benchmark_results').")
    parser.add_argument("challenge_name", type=str, help="Name of the specific challenge subdirectory to analyze (e.g., 'fullpage_challenge').")
    parser.add_argument("--chart_output_dir", type=str, default=None, help="Directory to save generated charts (defaults to master_output_dir/challenge_name/analysis_charts).")
    parser.add_argument("--title_prefix", type=str, default="", help="Optional prefix for chart titles (e.g., 'Run X - ').")
    
    args = parser.parse_args()

    master_output_path = Path(args.master_output_dir).resolve()
    
    if args.chart_output_dir:
        chart_output_path = Path(args.chart_output_dir).resolve()
    else:
        # Create a dedicated subdir for charts within the challenge's output dir
        chart_output_path = master_output_path / args.challenge_name / "analysis_charts"
    
    chart_output_path.mkdir(parents=True, exist_ok=True)

    print(f"Looking for summary files in: {master_output_path / args.challenge_name}")
    summary_files_info_list = find_summary_files(master_output_path, args.challenge_name)
    
    if not summary_files_info_list:
        print("No summary files found. Please check paths and challenge name. Exiting.")
        return

    all_parsed_model_data = []
    for file_info in summary_files_info_list:
        parsed_data = parse_summary_data(file_info)
        if parsed_data:
            all_parsed_model_data.append(parsed_data)

    if not all_parsed_model_data:
        print("No valid data could be parsed from the found summary files. Exiting.")
        return
        
    data_df = pd.DataFrame(all_parsed_model_data)
    
    # Check for duplicate model_name_for_plot entries, which could indicate an issue.
    if data_df['model_name_for_plot'].duplicated().any():
        print("\nWarning: Duplicate 'model_name_for_plot' entries found. This might mean multiple summary files resolve to the same display name.")
        print("Consider if model_instance_id should be part of display_model_name if run_id is not enough.")
        print(data_df[data_df['model_name_for_plot'].duplicated(keep=False)])
    
    # Add a prefix to the plot titles if provided
    plot_title_prefix = args.title_prefix if args.title_prefix else ""


    plot_total_scores(data_df, chart_output_path, args.challenge_name, plot_title_prefix)
    plot_stacked_scores(data_df, chart_output_path, args.challenge_name, plot_title_prefix)
    
    print(f"\nAll charts successfully generated in: {chart_output_path}")
    
    # Optional: Print a summary table to console
    print("\n--- Parsed Benchmark Data ---")
    print(data_df[['model_name_for_plot', 'overall_score_pct', 'tq_contribution_pct', 'adh_contribution_pct']].to_string(index=False))

if __name__ == "__main__":
    main()