import os
import json
import shutil
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
import datetime
import operator # For sorting dictionaries by value

# --- CONFIGURATION ---
BENCHMARK_RESULTS_ROOT_DIR = Path(r"C:\Users\Smirk\Documents\Programming\Studio\UIGEN-Benchmark\v4\benchmark_results")
OUTPUT_SITE_DIR = Path("./uigel_benchmark_site_output")
CLOUDFLARE_WORKER_URL_SUGGEST_PROMPT = "https://uigen-suggestion-handler.manavmaj2001.workers.dev/" # !!! REPLACE THIS !!!

# --- JINJA ENVIRONMENT SETUP ---
TEMPLATES_DIR = Path(__file__).parent / "templates_new" # Ensure this points to your dark theme templates
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(['html', 'xml', 'js']),
    trim_blocks=True, lstrip_blocks=True
)

# --- HELPER FUNCTIONS & JINJA FILTERS ---
def slugify(text):
    if not text: return ""
    text = str(text).lower()
    text = re.sub(r'\s+', '-', text)
    text = re.sub(r'[^a-z0-9\-._]', '', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')

def format_prompt_id_display(prompt_id_str):
    if not prompt_id_str: return "Unknown Prompt"
    parts = prompt_id_str.split('_')
    name_part = ' '.join(parts[1:])
    return f"{name_part} ({parts[0]})" if name_part else parts[0]

def format_date_tested_from_run_id(run_id_str):
    if not run_id_str or not run_id_str.startswith("run_"): return "N/A"
    date_part = run_id_str.split('_')[1].split('-')[0] # run_YYYYMMDD-HHMMSS -> YYYYMMDD
    if len(date_part) == 8:
        return f"{date_part[4:6]}/{date_part[6:8]}/{date_part[0:4]}" # MM/DD/YYYY
    return run_id_str

def format_timestamp_display(timestamp_str): # For model/prompt specific timestamps
    if not timestamp_str or len(timestamp_str) < 15: return 'N/A' # YYYYMMDD-HHMMSS
    try:
        return f"{timestamp_str[4:6]}/{timestamp_str[6:8]}/{timestamp_str[0:4]} {timestamp_str[9:11]}:{timestamp_str[11:13]}:{timestamp_str[13:15]}"
    except IndexError: return timestamp_str

def format_timestamp_display_timeonly(timestamp_str):
    if not timestamp_str or len(timestamp_str) < 15: return 'N/A'
    try:
        return f"{timestamp_str[9:11]}:{timestamp_str[11:13]}:{timestamp_str[13:15]}"
    except IndexError: return timestamp_str

def format_config_name_display(config_path_str):
    if not config_path_str: return "N/A"
    return Path(config_path_str).name

env.filters['slugify'] = slugify
env.filters['format_prompt_id_display'] = format_prompt_id_display
env.filters['format_date_tested'] = format_date_tested_from_run_id
env.filters['format_timestamp_display'] = format_timestamp_display
env.filters['format_timestamp_display_timeonly'] = format_timestamp_display_timeonly
env.filters['format_config_name'] = format_config_name_display

# --- DATA DISCOVERY AND PROCESSING ---
def discover_and_process_data():
    all_data = {"runs": {}, "latest_run_id": None}
    if not BENCHMARK_RESULTS_ROOT_DIR.exists():
        print(f"ERROR: BENCHMARK_RESULTS_ROOT_DIR '{BENCHMARK_RESULTS_ROOT_DIR}' does not exist.")
        return all_data

    run_ids = []
    for prompt_set_dir_scan in BENCHMARK_RESULTS_ROOT_DIR.iterdir():
        if not prompt_set_dir_scan.is_dir(): continue
        for run_dir_scan in prompt_set_dir_scan.iterdir():
            if run_dir_scan.is_dir() and run_dir_scan.name.startswith("run_"):
                run_ids.append(run_dir_scan.name)
    
    if not run_ids:
        print("No runs found in benchmark results directory.")
        return all_data
        
    all_data["latest_run_id"] = sorted(run_ids, reverse=True)[0] 

    for prompt_set_dir in BENCHMARK_RESULTS_ROOT_DIR.iterdir():
        if not prompt_set_dir.is_dir(): continue
        prompt_set_name = prompt_set_dir.name

        for run_dir in prompt_set_dir.iterdir():
            if not run_dir.is_dir() or not run_dir.name.startswith("run_"): continue
            run_id = run_dir.name
            
            current_run_data = {
                "run_id": run_id, 
                "prompt_set_name": prompt_set_name, 
                "models": {},
                "latest_model_timestamp_in_run": "00000000-000000" # Should not be used with timestamps removed
            }
            
            for model_folder in run_dir.iterdir(): 
                if not model_folder.is_dir(): continue
                
                # This print statement confirms the script is looking at model folders
                # print(f"    Looking in model folder: {model_folder.name} for run {run_id}")

                summary_file_path = model_folder / "MASTER_BENCHMARK_SUMMARY.json"
                if not summary_file_path.exists():
                    print(f"    Warning: MASTER_BENCHMARK_SUMMARY.json not found in {model_folder}")
                    continue

                try:
                    with open(summary_file_path, 'r', encoding='utf-8') as f:
                        model_summary = json.load(f)
                except Exception as e:
                    print(f"    Error reading or parsing {summary_file_path}: {e}")
                    continue
                
                # Ensure essential keys for sorting and display exist
                if not model_summary.get("benchmark_run_name") or \
                   not model_summary.get("aggregate_scores", {}).get("overall_weighted_score_from_totals") is not None:
                    print(f"    Warning: Essential data missing in {summary_file_path} (e.g., benchmark_run_name or overall_weighted_score_from_totals). Skipping model.")
                    continue


                model_name = model_summary["benchmark_run_name"] 
                # model_folder_name_part = model_folder.name.split('_')[0] # Not strictly needed if using model_name from summary
                
                # Timestamps are removed from display, but if used for keys, ensure they are consistent
                model_timestamp_from_folder = model_folder.name.split('_')[-1] if '_' in model_folder.name else "00000000-000000"
                model_timestamp = model_summary.get("overall_run_timestamp_for_this_model", model_timestamp_from_folder)

                # if model_timestamp > current_run_data["latest_model_timestamp_in_run"]:
                #     current_run_data["latest_model_timestamp_in_run"] = model_timestamp # No longer displayed

                model_entry_key = f"{slugify(model_name)}_{model_timestamp}" # Use slugified name for consistency
                model_entry = {
                    "summary_data": model_summary,
                    "slug": slugify(model_name), 
                    # "model_folder_name_part": model_folder_name_part, 
                    "prompts_detailed": {}
                }
                current_run_data["models"][model_entry_key] = model_entry

                for prompt_result in model_summary.get("individual_prompt_results", []):
                    prompt_id = prompt_result["prompt_id"]
                    report_dir_path_str = prompt_result.get("report_directory")
                    if not report_dir_path_str: continue
                    
                    report_dir = Path(report_dir_path_str) 
                    detailed_report_filename = f"{prompt_id}_detailed_report.json"
                    detailed_report_path = report_dir / detailed_report_filename

                    if detailed_report_path.exists():
                        try:
                            with open(detailed_report_path, 'r', encoding='utf-8') as f_detail:
                                model_entry["prompts_detailed"][prompt_id] = json.load(f_detail)
                        except Exception as e:
                            print(f"    Error reading detailed report {detailed_report_path}: {e}")
                    else:
                        print(f"    Warning: Detailed report not found: {detailed_report_path}")
            
            if current_run_data["models"]: # Only add run if it has successfully processed models
                 all_data["runs"][run_id] = current_run_data
            else:
                print(f"  Info: Run {run_id} had no valid models processed, not adding to site data.")

    # Update latest_run_id if the original one had no models but others do
    if all_data["latest_run_id"] not in all_data["runs"] and all_data["runs"]:
        all_data["latest_run_id"] = sorted(all_data["runs"].keys(), reverse=True)[0]
    elif not all_data["runs"]: # No runs have any models
        all_data["latest_run_id"] = None


    return all_data

# --- ASSET COPYING ---
def copy_and_get_relative_assets(
    model_summary_data, 
    prompt_id, 
    run_id_for_asset_storage, 
    model_slug_for_path, 
    base_output_dir_for_site,
    html_page_path_from_site_root 
):
    copied_assets_info = {} 
    prompt_slug_for_path = slugify(prompt_id)
    absolute_asset_storage_dir = base_output_dir_for_site / "runs" / run_id_for_asset_storage / "assets" / model_slug_for_path / prompt_slug_for_path
    absolute_asset_storage_dir.mkdir(parents=True, exist_ok=True)
    asset_dir_path_from_site_root = Path("runs") / run_id_for_asset_storage / "assets" / model_slug_for_path / prompt_slug_for_path

    prompt_result_entry = next((p for p in model_summary_data.get("individual_prompt_results", []) if p["prompt_id"] == prompt_id), None)
    if not prompt_result_entry or not prompt_result_entry.get("report_directory"):
        return copied_assets_info

    source_report_dir = Path(prompt_result_entry["report_directory"])
    screenshots_subdir = source_report_dir / "screenshots"

    asset_map = {
        "screenshot_desktop": (screenshots_subdir, f"{prompt_id}_desktop.png", "screenshot_desktop.png"),
        "screenshot_mobile": (screenshots_subdir, f"{prompt_id}_mobile.png", "screenshot_mobile.png"),
        "axe_desktop": (source_report_dir, f"axe_report_{prompt_id}_desktop.json", "axe_desktop.json"),
        "axe_mobile": (source_report_dir, f"axe_report_{prompt_id}_mobile.json", "axe_mobile.json"),
    }

    for key, (src_dir, src_name, dest_name) in asset_map.items():
        src_path = src_dir / src_name
        if src_path.exists():
            dest_path_on_disk = absolute_asset_storage_dir / dest_name
            shutil.copy2(src_path, dest_path_on_disk)
            path_to_asset_from_html = os.path.relpath(
                asset_dir_path_from_site_root / dest_name,
                Path(html_page_path_from_site_root).parent
            ).replace("\\", "/")
            copied_assets_info[key] = path_to_asset_from_html
        # else:
            # print(f"  Debug: Asset {key} ({src_name}) not found at {src_dir}") # Less verbose

    html_source_base_dir_str = model_summary_data.get("html_source_directory")
    if html_source_base_dir_str:
        src_gen_ui_path = Path(html_source_base_dir_str) / f"{prompt_id}.html"
        dest_ui_name = "generated_ui.html"
        if src_gen_ui_path.exists():
            shutil.copy2(src_gen_ui_path, absolute_asset_storage_dir / dest_ui_name)
        else:
            # Fallback: check for <prompt_id>/index.html if <prompt_id>.html doesn't exist
            src_gen_ui_path = Path(html_source_base_dir_str) / prompt_id / "index.html"
            if src_gen_ui_path.exists():
                 shutil.copy2(src_gen_ui_path, absolute_asset_storage_dir / dest_ui_name)
            # else:
                # print(f"  Warning: Generated UI not found for prompt {prompt_id}") # Less verbose

        # If UI was copied (either primary or fallback)
        if (absolute_asset_storage_dir / dest_ui_name).exists():
            path_to_ui_from_html = os.path.relpath(
                asset_dir_path_from_site_root / dest_ui_name,
                Path(html_page_path_from_site_root).parent
            ).replace("\\", "/")
            copied_assets_info["generated_ui"] = path_to_ui_from_html
            
    return copied_assets_info


# --- BREADCRUMB HELPER ---
def get_breadcrumbs_and_rel_path(page_type, run_id=None, model_data_obj=None, is_main_index_latest_run=False):
    crumbs = []
    relative_path_to_root = ""

    if page_type == "main_index_latest_run": 
        relative_path_to_root = "" 
    elif page_type == "all_runs_archive":
        relative_path_to_root = "" 
    elif page_type == "model_detail": 
        if is_main_index_latest_run: 
            relative_path_to_root = "../" 
            # No breadcrumb for "Latest Benchmark" itself on model detail page of latest, as it's implied
        else: 
            relative_path_to_root = "../../" 
            crumbs.append({"text": f"Benchmark Archive", "url": f"{relative_path_to_root}all_runs_archive.html"})
            crumbs.append({"text": f"Run: {format_date_tested_from_run_id(run_id)}", "url": f"{relative_path_to_root}runs/{run_id}/index.html"})

    elif page_type == "prompt_detail": 
        if is_main_index_latest_run: 
            relative_path_to_root = "../../" 
            # crumbs.append({"text": f"Latest Benchmark ({format_date_tested_from_run_id(run_id)})", "url": f"{relative_path_to_root}index.html"})
            if model_data_obj:
                crumbs.append({"text": model_data_obj['summary_data']['benchmark_run_name'], "url": f"{relative_path_to_root}runs/{model_data_obj['slug']}.html"})
        else: 
            relative_path_to_root = "../../../" 
            crumbs.append({"text": f"Benchmark Archive", "url": f"{relative_path_to_root}all_runs_archive.html"})
            crumbs.append({"text": f"Run: {format_date_tested_from_run_id(run_id)}", "url": f"{relative_path_to_root}runs/{run_id}/index.html"})
            if model_data_obj:
                 crumbs.append({"text": model_data_obj['summary_data']['benchmark_run_name'], "url": f"{relative_path_to_root}runs/{run_id}/{model_data_obj['slug']}.html"})
    
    elif page_type == "archived_run_index":
        relative_path_to_root = "../../" 
        crumbs.append({"text": "Benchmark Archive", "url": f"{relative_path_to_root}all_runs_archive.html"})
        
    return crumbs, relative_path_to_root

# --- MAIN SCRIPT LOGIC ---
def main():
    print(f"Starting benchmark website generation at {datetime.datetime.now()}...")
    
    all_benchmark_data = discover_and_process_data()
    
    if not all_benchmark_data["runs"] or not all_benchmark_data["latest_run_id"]:
        print("No valid benchmark data or latest run ID found after processing. Check logs for warnings.")
        # Create a minimal site or an error page if desired
        if OUTPUT_SITE_DIR.exists(): shutil.rmtree(OUTPUT_SITE_DIR)
        OUTPUT_SITE_DIR.mkdir(parents=True, exist_ok=True)
        # Copy static files even for minimal site
        style_template = env.get_template("style.css")
        with open(OUTPUT_SITE_DIR / "style.css", "w", encoding="utf-8") as f: f.write(style_template.render())
        suggest_js_template = env.get_template("suggest_prompt.js")
        with open(OUTPUT_SITE_DIR / "suggest_prompt.js", "w", encoding="utf-8") as f: f.write(suggest_js_template.render(cloudflare_worker_url_suggest_prompt=CLOUDFLARE_WORKER_URL_SUGGEST_PROMPT))
        
        # Render index.html showing "No data"
        main_index_template = env.get_template("main_index.html") # Use the actual main_index.html template
        _, rel_path_main_empty = get_breadcrumbs_and_rel_path("main_index_latest_run")
        with open(OUTPUT_SITE_DIR / "index.html", "w", encoding="utf-8") as f:
            f.write(main_index_template.render(
                run_id=None, run_data=None, models_sorted=None, models_chart_data=None, # Explicitly None
                generation_time_str=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
                is_main_index=True, breadcrumbs=[], relative_path_to_root=rel_path_main_empty,
                current_page_title="Welcome"
            ))
        # Render empty archive page
        all_runs_template_empty = env.get_template("all_runs_archive.html")
        _, rel_path_archive_empty = get_breadcrumbs_and_rel_path("all_runs_archive")
        with open(OUTPUT_SITE_DIR / "all_runs_archive.html", "w", encoding="utf-8") as f:
             f.write(all_runs_template_empty.render(
                all_benchmark_data={"runs": {}, "latest_run_id": None}, # Empty data
                generation_time_str=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
                breadcrumbs=[], relative_path_to_root=rel_path_archive_empty,
                current_page_title="Benchmark Archive"
            ))
        print(f"Minimal site generated due to no valid run data. Output is in: {OUTPUT_SITE_DIR.resolve()}")
        return

    latest_run_id = all_benchmark_data["latest_run_id"]
    latest_run_data = all_benchmark_data["runs"][latest_run_id]

    if OUTPUT_SITE_DIR.exists():
        shutil.rmtree(OUTPUT_SITE_DIR)
    OUTPUT_SITE_DIR.mkdir(parents=True, exist_ok=True)

    style_template = env.get_template("style.css")
    with open(OUTPUT_SITE_DIR / "style.css", "w", encoding="utf-8") as f:
        f.write(style_template.render())

    suggest_js_template = env.get_template("suggest_prompt.js")
    with open(OUTPUT_SITE_DIR / "suggest_prompt.js", "w", encoding="utf-8") as f:
        f.write(suggest_js_template.render(cloudflare_worker_url_suggest_prompt=CLOUDFLARE_WORKER_URL_SUGGEST_PROMPT))

    generation_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. Create main index.html (shows LATEST run leaderboard)
    #    Uses the main_index.html template file directly
    print(f"Generating main index.html for latest run: {latest_run_id}...")
    main_site_index_template = env.get_template("main_index.html") 
    
    models_sorted_latest = sorted(
        latest_run_data["models"].items(), 
        key=lambda item: item[1]["summary_data"]["aggregate_scores"]["overall_weighted_score_from_totals"],
        reverse=True
    )
    models_chart_data_latest = [{
        "name": item_data["summary_data"]["benchmark_run_name"],
        "overallScore": item_data["summary_data"]["aggregate_scores"]["overall_weighted_score_from_totals"],
        "avgPromptScore": item_data["summary_data"]["aggregate_scores"]["average_prompt_weighted_score"]
    } for model_key, item_data in models_sorted_latest]

    breadcrumbs_main_latest, rel_path_main_latest = get_breadcrumbs_and_rel_path("main_index_latest_run")
    
    with open(OUTPUT_SITE_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(main_site_index_template.render( # Using main_index.html template
            run_id=latest_run_id, 
            run_data=latest_run_data,
            models_sorted=models_sorted_latest,
            models_chart_data=models_chart_data_latest,
            generation_time_str=generation_time_str,
            # is_main_index=True, # main_index.html template implies this
            breadcrumbs=breadcrumbs_main_latest,
            relative_path_to_root=rel_path_main_latest,
            # current_page_title is handled by the template itself for the latest run
        ))

    # Create directories for latest run's model pages and prompt pages
    latest_run_content_dir = OUTPUT_SITE_DIR / "runs" 
    latest_run_content_dir.mkdir(exist_ok=True) # This is for model_slug.html files for latest run
    latest_run_prompts_output_dir = latest_run_content_dir / "prompts" # This is for prompt_slug.html files for latest run
    latest_run_prompts_output_dir.mkdir(exist_ok=True)

    # Generate model detail and prompt detail pages for the LATEST run
    for model_key, model_details_obj in latest_run_data["models"].items():
        model_name_actual = model_details_obj["summary_data"]["benchmark_run_name"]
        model_slug = model_details_obj["slug"]
        print(f"  Processing model for latest run: {model_name_actual}")

        model_detail_template = env.get_template("model_detail.html")
        breadcrumbs_model, rel_path_model = get_breadcrumbs_and_rel_path(
            "model_detail", 
            run_id=latest_run_id, 
            model_data_obj=model_details_obj, 
            is_main_index_latest_run=True
        )
        
        model_html_path_rel_to_root = Path("runs") / f"{model_slug}.html" # e.g. runs/gemini-pro.html
        model_html_abs_path = OUTPUT_SITE_DIR / model_html_path_rel_to_root

        with open(model_html_abs_path, "w", encoding="utf-8") as f:
            f.write(model_detail_template.render(
                run_id=latest_run_id, model_data=model_details_obj,
                generation_time_str=generation_time_str, breadcrumbs=breadcrumbs_model,
                relative_path_to_root=rel_path_model, current_page_title=model_name_actual,
                is_latest_run_page=True # Flag for template if needed
            ))

        for prompt_id, prompt_detail_data in model_details_obj["prompts_detailed"].items():
            # print(f"    Processing prompt: {prompt_id}") # Less verbose
            prompt_page_slug = f"{model_slug}_{slugify(prompt_id)}"
            prompt_html_path_rel_to_root = Path("runs") / "prompts" / f"{prompt_page_slug}.html" # e.g. runs/prompts/gemini-pro_my-prompt.html
            prompt_html_abs_path = OUTPUT_SITE_DIR / prompt_html_path_rel_to_root

            copied_assets = copy_and_get_relative_assets(
                model_details_obj["summary_data"], prompt_id, 
                latest_run_id, # Assets stored under the specific run_id (latest_run_id here)
                model_slug, 
                OUTPUT_SITE_DIR,
                prompt_html_path_rel_to_root 
            )
            prompt_detail_template = env.get_template("prompt_detail.html")
            breadcrumbs_prompt, rel_path_prompt = get_breadcrumbs_and_rel_path(
                "prompt_detail", 
                run_id=latest_run_id, 
                model_data_obj=model_details_obj, 
                is_main_index_latest_run=True
            )
            with open(prompt_html_abs_path, "w", encoding="utf-8") as f:
                f.write(prompt_detail_template.render(
                    run_id=latest_run_id, model_name=model_name_actual, model_slug=model_slug,
                    prompt_data=prompt_detail_data, assets=copied_assets,
                    generation_time_str=generation_time_str, breadcrumbs=breadcrumbs_prompt,
                    relative_path_to_root=rel_path_prompt, 
                    current_page_title=format_prompt_id_display(prompt_id),
                    is_latest_run_page=True # Flag for template if needed
                ))

    # 2. Create all_runs_archive.html
    print("Generating all_runs_archive.html...")
    all_runs_template = env.get_template("all_runs_archive.html") # CORRECTED TEMPLATE
    breadcrumbs_archive, rel_path_archive = get_breadcrumbs_and_rel_path("all_runs_archive")
    with open(OUTPUT_SITE_DIR / "all_runs_archive.html", "w", encoding="utf-8") as f:
        f.write(all_runs_template.render(
            all_benchmark_data=all_benchmark_data, 
            generation_time_str=generation_time_str,
            breadcrumbs=breadcrumbs_archive, 
            relative_path_to_root=rel_path_archive,
            current_page_title="Benchmark Archive" # Used for breadcrumb in base_layout
            # No is_archive_page needed if all_runs_archive.html template is self-contained
        ))

    # 3. Create pages for ARCHIVED runs
    #    These will be under /runs/<run_id_archived>/
    for run_id_archived, run_data_archived in all_benchmark_data["runs"].items():
        if run_id_archived == latest_run_id: # Already processed as "latest"
            continue

        print(f"Processing ARCHIVED run: {run_id_archived} (Prompt Set: {run_data_archived['prompt_set_name']})")
        
        # Directory for this specific archived run's pages
        archived_run_pages_dir_rel_to_root = Path("runs") / run_id_archived
        archived_run_pages_output_dir_abs = OUTPUT_SITE_DIR / archived_run_pages_dir_rel_to_root
        archived_run_pages_output_dir_abs.mkdir(parents=True, exist_ok=True)
        
        models_sorted_archived = sorted(
            run_data_archived["models"].items(), 
            key=lambda item: item[1]["summary_data"]["aggregate_scores"]["overall_weighted_score_from_totals"],
            reverse=True
        )
        models_chart_data_archived = [{
            "name": item_data["summary_data"]["benchmark_run_name"],
            "overallScore": item_data["summary_data"]["aggregate_scores"]["overall_weighted_score_from_totals"],
            "avgPromptScore": item_data["summary_data"]["aggregate_scores"]["average_prompt_weighted_score"]
        } for model_key, item_data in models_sorted_archived]

        archived_run_index_template = env.get_template("run_index.html") # Re-use run_index for leaderboards
        breadcrumbs_arc_run, rel_path_arc_run = get_breadcrumbs_and_rel_path("archived_run_index", run_id=run_id_archived)
        
        # index.html for the archived run (e.g., runs/run_id_archived/index.html)
        with open(archived_run_pages_output_dir_abs / "index.html", "w", encoding="utf-8") as f:
            f.write(archived_run_index_template.render(
                run_id=run_id_archived, run_data=run_data_archived,
                models_sorted=models_sorted_archived, models_chart_data=models_chart_data_archived,
                generation_time_str=generation_time_str, breadcrumbs=breadcrumbs_arc_run,
                relative_path_to_root=rel_path_arc_run,
                current_page_title=f"Run: {format_date_tested_from_run_id(run_id_archived)}",
                is_main_index=False # Important flag for run_index.html template
            ))

        # Directory for prompts of this specific archived run
        archived_run_prompts_output_dir_abs = archived_run_pages_output_dir_abs / "prompts"
        archived_run_prompts_output_dir_abs.mkdir(exist_ok=True)

        for model_key, model_details_obj_archived in run_data_archived["models"].items():
            model_name_actual_archived = model_details_obj_archived["summary_data"]["benchmark_run_name"]
            model_slug_archived = model_details_obj_archived["slug"]
            # print(f"  Processing model for archived run {run_id_archived}: {model_name_actual_archived}") # Less verbose

            # Model detail page for archived run (e.g., runs/run_id_archived/model_slug.html)
            model_html_path_rel_to_root_arc = archived_run_pages_dir_rel_to_root / f"{model_slug_archived}.html"
            model_html_abs_path_archived = OUTPUT_SITE_DIR / model_html_path_rel_to_root_arc
            
            breadcrumbs_arc_model, rel_path_arc_model = get_breadcrumbs_and_rel_path(
                "model_detail", run_id=run_id_archived, 
                model_data_obj=model_details_obj_archived, 
                is_main_index_latest_run=False
            )
            with open(model_html_abs_path_archived, "w", encoding="utf-8") as f:
                f.write(model_detail_template.render( # Re-use model_detail.html template
                    run_id=run_id_archived, model_data=model_details_obj_archived,
                    generation_time_str=generation_time_str, breadcrumbs=breadcrumbs_arc_model,
                    relative_path_to_root=rel_path_arc_model, 
                    current_page_title=model_name_actual_archived,
                    is_latest_run_page=False # Flag for template if needed
                ))

            for prompt_id, prompt_detail_data_archived in model_details_obj_archived["prompts_detailed"].items():
                prompt_page_slug_archived = f"{model_slug_archived}_{slugify(prompt_id)}"
                # Prompt detail page for archived run (e.g., runs/run_id_archived/prompts/model_prompt_slug.html)
                prompt_html_path_rel_to_root_arc = archived_run_pages_dir_rel_to_root / "prompts" / f"{prompt_page_slug_archived}.html"
                prompt_html_abs_path_archived = OUTPUT_SITE_DIR / prompt_html_path_rel_to_root_arc

                copied_assets_archived = copy_and_get_relative_assets(
                    model_details_obj_archived["summary_data"], prompt_id, 
                    run_id_archived, # Assets stored under this specific archived run_id
                    model_slug_archived, 
                    OUTPUT_SITE_DIR,
                    prompt_html_path_rel_to_root_arc 
                )
                breadcrumbs_arc_prompt, rel_path_arc_prompt = get_breadcrumbs_and_rel_path(
                    "prompt_detail", run_id=run_id_archived, 
                    model_data_obj=model_details_obj_archived, 
                    is_main_index_latest_run=False
                )
                with open(prompt_html_abs_path_archived, "w", encoding="utf-8") as f:
                    f.write(prompt_detail_template.render( # Re-use prompt_detail.html template
                        run_id=run_id_archived, model_name=model_name_actual_archived, model_slug=model_slug_archived,
                        prompt_data=prompt_detail_data_archived, assets=copied_assets_archived,
                        generation_time_str=generation_time_str, breadcrumbs=breadcrumbs_arc_prompt,
                        relative_path_to_root=rel_path_arc_prompt,
                        current_page_title=format_prompt_id_display(prompt_id),
                        is_latest_run_page=False # Flag for template if needed
                    ))

    print(f"Website generation complete. Output is in: {OUTPUT_SITE_DIR.resolve()}")
    print(f"Remember to replace 'YOUR_CLOUDFLARE_WORKER_URL_FOR_SUGGESTIONS' in {Path(__file__).name} if you haven't.")

if __name__ == "__main__":
    main()