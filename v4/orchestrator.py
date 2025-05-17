# orchestrator.py
import os
import sys
import subprocess
import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import time

def run_benchmark_for_single_model(challengename, modelname, base_html_dir_str, master_output_base_str, analyzer_script_path_str, global_run_timestamp_str, workers_per_model_count):
    base_html_dir = Path(base_html_dir_str)
    master_output_base = Path(master_output_base_str)
    analyzer_script_path = Path(analyzer_script_path_str)

    model_html_dir = base_html_dir / challengename / modelname
    
    # Define where the ui_benchmark_analyzer.py script should place its outputs for this model.
    # It will create a subfolder like {modelname}_{timestamp} inside this.
    # Output path: {master_output_base}/{challengename}/run_{global_run_timestamp}/
    # The analyzer will then create:
    # {master_output_base}/{challengename}/run_{global_run_timestamp}/{modelname}_{analyzer_script_timestamp}/
    output_dir_for_analyzer_base = master_output_base / challengename / f"run_{global_run_timestamp_str}"
    output_dir_for_analyzer_base.mkdir(parents=True, exist_ok=True)

    # Determine path to the master prompts config file.
    # Option 1: A single master config at the root of base_html_dir.
    # Option 2: A master config per challenge (e.g., base_html_dir / challengename / "master_prompts_config.json")
    # Option 3: A master config per model (e.g., model_html_dir / "master_prompts_config.json") - less likely for this setup.
    # For this example, let's prioritize per-challenge, then per-base_html_dir.
    
    master_config_file_path = base_html_dir / challengename / "master_prompts_benchmark_config.json"
    if not master_config_file_path.exists():
        master_config_file_path = base_html_dir / "master_prompts_benchmark_config.json" # Fallback
        if not master_config_file_path.exists():
            msg = f"Master prompts config not found for {challengename}/{modelname} (checked in challenge and base HTML dirs)."
            print(msg)
            return {"model": modelname, "status": "CONFIG_NOT_FOUND", "error": msg}

    cmd = [
        sys.executable,  # Path to python interpreter
        str(analyzer_script_path),
        str(master_config_file_path),
        str(model_html_dir),
        str(output_dir_for_analyzer_base) 
    ]
    
    env = os.environ.copy()
    if workers_per_model_count is not None and workers_per_model_count > 0:
        env["PROMPT_WORKERS_COUNT"] = str(workers_per_model_count)

    print(f"[Orchestrator] Running for model: {modelname}. Workers per model: {env.get('PROMPT_WORKERS_COUNT', 'DefaultInAnalyzer')}")
    print(f"[Orchestrator] Command: {' '.join(cmd)}")
    
    try:
        # Use Popen for non-blocking if you want to stream output, or run for simpler blocking.
        # Timeout should be generous, e.g., 1-2 hours per model depending on prompt count.
        process = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=7200) # 2-hour timeout

        if process.returncode == 0:
            print(f"[Orchestrator] Model {modelname} completed successfully.")
            return {"model": modelname, "status": "SUCCESS", "output_base": str(output_dir_for_analyzer_base), "stdout": process.stdout}
        else:
            print(f"[Orchestrator] Model {modelname} failed. RC: {process.returncode}")
            print(f"[Orchestrator] Model {modelname} STDOUT:\n{process.stdout}")
            print(f"[Orchestrator] Model {modelname} STDERR:\n{process.stderr}")
            return {"model": modelname, "status": "ANALYZER_FAILURE", "rc": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
    except subprocess.TimeoutExpired as e:
        print(f"[Orchestrator] Model {modelname} timed out.")
        return {"model": modelname, "status": "TIMEOUT", "stdout": e.stdout, "stderr": e.stderr}
    except Exception as e:
        print(f"[Orchestrator] Critical error running benchmark for model {modelname}: {e}")
        return {"model": modelname, "status": "ORCHESTRATOR_SUBPROCESS_ERROR", "error": str(e)}

def main_orchestrator():
    if len(sys.argv) < 4:
        print("Usage: python orchestrator.py <base_html_dir> <challengename> <master_output_dir> [max_parallel_models] [workers_per_model]")
        print("  <base_html_dir>: Directory containing challenge subdirectories (e.g., './data').")
        print("  <challengename>: Name of the challenge subdirectory (e.g., 'my_web_challenge').")
        print("  <master_output_dir>: Base directory for all benchmark results (e.g., './benchmark_runs').")
        print("  [max_parallel_models]: Max models to run concurrently (default: 2).")
        print("  [workers_per_model]: Max prompts per model to run concurrently (default: uses analyzer's default or PROMPT_WORKERS_COUNT env var).")
        print("\nExample: python orchestrator.py ./all_code_outputs web-challenge-alpha ./benchmark_results 3 4")
        print("  This expects HTMLs in: ./all_code_outputs/web-challenge-alpha/{model1_name}/prompt1.html, etc.")
        print("  And a master config like: ./all_code_outputs/web-challenge-alpha/master_prompts_benchmark_config.json")
        sys.exit(1)

    base_html_dir_arg = Path(sys.argv[1]).resolve()
    challengename_arg = sys.argv[2]
    master_output_dir_arg = Path(sys.argv[3]).resolve()
    
    max_parallel_models_arg = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    workers_per_model_arg = int(sys.argv[5]) if len(sys.argv) > 5 else None # None lets analyzer script use its default/env

    # Assuming orchestrator.py is in the same directory as ui_benchmark_analyzer.py
    analyzer_script_path_obj = (Path(__file__).parent / "ui_benchmark_analyzer.py").resolve()
    if not analyzer_script_path_obj.exists():
        print(f"FATAL: Analyzer script not found at {analyzer_script_path_obj}")
        sys.exit(1)

    challenge_data_path = base_html_dir_arg / challengename_arg
    if not challenge_data_path.is_dir():
        print(f"Challenge data directory not found: {challenge_data_path}")
        sys.exit(1)

    model_dirs = [d for d in challenge_data_path.iterdir() if d.is_dir()]
    if not model_dirs:
        print(f"No model subdirectories found in {challenge_data_path}")
        sys.exit(1)

    master_output_dir_arg.mkdir(parents=True, exist_ok=True)
    global_run_timestamp_val = time.strftime('%Y%m%d-%H%M%S')

    print(f"Orchestrator starting. Max parallel models: {max_parallel_models_arg}.")
    print(f"Analyzer script: {analyzer_script_path_obj}")
    
    all_model_run_results = []
    
    # Convert Paths to strings for ProcessPoolExecutor arguments for max compatibility
    tasks_for_model_workers = []
    for model_dir_path_obj in model_dirs:
        tasks_for_model_workers.append(
            (challengename_arg, model_dir_path_obj.name, str(base_html_dir_arg), 
             str(master_output_dir_arg), str(analyzer_script_path_obj), 
             global_run_timestamp_val, workers_per_model_arg)
        )

    with ProcessPoolExecutor(max_workers=max_parallel_models_arg) as executor:
        future_to_modelname = {
            executor.submit(run_benchmark_for_single_model, *task_args): task_args[1] # modelname is task_args[1]
            for task_args in tasks_for_model_workers
        }
        
        for future in as_completed(future_to_modelname):
            model_name_completed = future_to_modelname[future]
            try:
                result = future.result()
                all_model_run_results.append(result)
            except Exception as e_exec:
                print(f"[Orchestrator] CRITICAL EXCEPTION from model worker for {model_name_completed}: {e_exec}")
                all_model_run_results.append({"model": model_name_completed, "status": "ORCHESTRATOR_FUTURE_ERROR", "error": str(e_exec)})
    
    print("\n--- Orchestrator Overall Summary ---")
    successful_models = 0
    for res in all_model_run_results:
        print(f"  Model: {res.get('model')}, Status: {res.get('status')}")
        if res.get('status') == "SUCCESS":
            successful_models += 1
        elif res.get('stderr'):
            print(f"    Error Snippet: {res['stderr'][:300]}...")
    
    print(f"\nTotal models processed: {len(all_model_run_results)}. Successful models: {successful_models}.")
    print(f"Main benchmark outputs in: {master_output_dir_arg}/{challengename_arg}/run_{global_run_timestamp_val}/")
    print("Each successful model run will have a MASTER_BENCHMARK_SUMMARY.json inside its respective modelname_timestamp subfolder.")

if __name__ == "__main__":
    main_orchestrator()