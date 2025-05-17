#!/usr/bin/env python3

import sys
import os
import json
import time
import random
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI, APIError, APITimeoutError
import tiktoken # Added for token counting

# --- Configuration ---

# Max retries for API calls if a retriable error occurs
MAX_API_RETRIES = 3
# Initial delay in seconds for retries, will be subject to exponential backoff
INITIAL_RETRY_DELAY = 5
# Factor for exponential backoff (e.g., 2 means delay doubles each time)
RETRY_BACKOFF_FACTOR = 2

# Define your models here
MODELS_CONFIG = [
    # {
    #     "name": "GPT4_O_OpenAI",
    #     "api_key": os.environ.get("OPENAI_API_KEY_GPT4O"),
    #     "api_base_url": "https://api.openai.com/v1",
    #     "model_identifier": "gpt-4o",
    #     "parallel_workers": 2,
    #     "tpm_limit": 100000, # Example TPM for GPT-4o
    #     "custom_params": {
    #         "temperature": 0.2,
    #         "max_tokens": 4090,
    #     }
    # },
]
BASE_OUTPUT_DIR = Path("generated_html_benchmarks")
SYSTEM_PROMPT_BASE = """You are an expert, accessibility-first front-end web developer. Your primary goal is to generate complete, correct, production-quality HTML, CSS (using Tailwind CSS primarily, but open to minimal custom CSS for complex needs), and JavaScript code based on the user's requirements. You will output a single code block.

Key Instructions for Code Generation:

1.  **Deep Understanding of Requirements:**
    *   Thoroughly analyze the user's prompt to understand the desired functionality, user experience, and accessibility outcomes.
    *   Prioritize creating a solution that is not only functional but also intuitive, robust, and inclusive.

2.  **Strict Adherence to Explicit Specifications:**
    *   If the prompt specifies particular HTML tag names, `id` attributes, `class` names, or `data-testid` attributes, you **MUST** use them exactly as specified. These are critical for automated testing.
    *   If specific ARIA roles or attributes are explicitly mentioned for an element in the prompt, implement them precisely.

3.  **Proactive Accessibility (A11y) Implementation:**
    *   **Semantic HTML First:** Always prioritize using appropriate semantic HTML5 elements for their intended purpose (e.g., `<header>`, `<nav>`, `<main>`, `<footer>`, `<article>`, `<section>`, `<button>`). Ensure a logical document outline with correct heading hierarchy (H1-H6).
    *   **ARIA Best Practices for Components:** For common interactive components (e.g., menus, dialogs/modals, tabs, accordions, carousels, sortable tables, custom form controls), you are expected to implement them according to established WAI-ARIA Authoring Practices, even if the prompt doesn't list every single `aria-*` attribute. For instance, if a "tabbed interface" is requested, implement the necessary `role="tablist"`, `role="tab"`, `role="tabpanel"`, `aria-selected`, `aria-controls`, and keyboard navigation.
    *   **Accessible Names:** All interactive elements (buttons, links, form inputs, etc.) and meaningful images MUST have clear, descriptive accessible names (e.g., through text content, `aria-label`, `aria-labelledby`, or `alt` text for images).
    *   **Keyboard Navigability & Focus Management:** All interactive components must be fully keyboard navigable (focusable, operable with standard keys like Enter/Space/Arrow keys/Escape as appropriate). Manage focus logically, especially in dynamic components like modals or menus. Ensure clear visual focus indicators.
    *   **Dynamic ARIA Updates:** Ensure ARIA attributes that reflect state (e.g., `aria-expanded`, `aria-pressed`, `aria-selected`, `aria-hidden`, `aria-sort`) are dynamically updated by JavaScript as the component's state changes.

4.  **Responsiveness and Modern Layout:**
    *   Ensure all UIs are fully responsive and adapt gracefully to different viewport sizes (mobile, tablet, desktop).
    *   Implement mobile-first principles where appropriate.
    *   A `<meta name="viewport" content="width=device-width, initial-scale=1.0">` tag MUST be present.
    *   Utilize modern CSS layout techniques (Flexbox, Grid) as needed, either via Tailwind utilities or minimal custom CSS if Tailwind is insufficient for a specific complex layout.

5.  **JavaScript for Functionality and Interactivity:**
    *   Provide clean, efficient, and modern JavaScript (ES6+) to implement all requested dynamic behaviors, interactions, and state management.
    *   Handle user events correctly and update the DOM and ARIA attributes appropriately.
    *   For simulations (like API calls or complex backend logic), clearly indicate this and focus on the client-side implementation.

6.  **Code Quality and Presentation:**
    *   Generate clean, well-formatted, and readable code.
    *   Use Tailwind CSS for primary styling as requested. The Tailwind CDN (<script src="https://cdn.tailwindcss.com"></script>) must be in the <head>. If highly specific styling or animations are needed that are cumbersome or impossible with Tailwind alone, you may include minimal, well-scoped custom CSS within `<style>` tags. Clearly comment on why custom CSS was necessary.
    *   Output the entire HTML, CSS (if any embedded custom styles), and JavaScript in a single, complete HTML code block. Only output the code block, no other text, explanation, or markdown backticks.

Your goal is to demonstrate a comprehensive understanding of modern front-end development, producing solutions that a senior developer would be proud of. When faced with ambiguity in how to implement a feature accessibly or functionally, defer to established best practices and the WAI-ARIA Authoring Practices.
The user will now provide the specific component or page they want you to build.
"""
USER_PROMPT_WRAPPER = "Please generate the following based on the system prompt instructions:\n\n{user_prompt_description}"

# --- Global Concurrency & TPM Controls ---
TIKTOKEN_ENCODER = None # Will be initialized in main()
MODEL_TPM_DATA = {} # Stores TPM tracking info for each model
MODEL_SEMAPHORES = {} # Stores threading.Semaphore for each model

# --- Helper Functions ---

def initialize_concurrency_controls(active_models_config):
    """Initializes TPM data structures and semaphores for active models."""
    global TIKTOKEN_ENCODER
    try:
        TIKTOKEN_ENCODER = tiktoken.encoding_for_model("gpt-4o")
    except Exception as e:
        print(f"Error initializing tiktoken encoder: {e}. Token counting will be approximate.")
        TIKTOKEN_ENCODER = None # Fallback handled in count_tokens

    for mc in active_models_config:
        model_id = mc["model_identifier"]
        if mc.get("tpm_limit"):
            MODEL_TPM_DATA[model_id] = {
                "tokens_used_this_minute": 0,
                "minute_start_time": time.time(),
                "lock": threading.Lock(),
                "limit": mc["tpm_limit"]
            }
        MODEL_SEMAPHORES[model_id] = threading.Semaphore(mc["parallel_workers"])

def count_tokens(text: str) -> int:
    """Counts tokens in a string using the initialized TIKTOKEN_ENCODER."""
    if TIKTOKEN_ENCODER is None:
        # Fallback if tiktoken fails to initialize (e.g. offline, model name change)
        return len(text.split()) # Rough approximation: word count
    return len(TIKTOKEN_ENCODER.encode(text))

def wait_for_tpm_budget(model_config: dict, tokens_needed: int):
    """Waits if necessary to stay within the model's TPM limit."""
    model_id = model_config["model_identifier"]
    if model_id not in MODEL_TPM_DATA:
        return # No TPM limit for this model

    tpm_info = MODEL_TPM_DATA[model_id]
    while True:
        with tpm_info["lock"]:
            current_time = time.time()
            if current_time - tpm_info["minute_start_time"] >= 60:
                # print(f"    TPM: Minute reset for {model_config['name']}. Used {tpm_info['tokens_used_this_minute']} tokens.")
                tpm_info["tokens_used_this_minute"] = 0
                tpm_info["minute_start_time"] = current_time

            if tpm_info["tokens_used_this_minute"] + tokens_needed <= tpm_info["limit"]:
                tpm_info["tokens_used_this_minute"] += tokens_needed
                # print(f"    TPM: {model_config['name']} using {tokens_needed} tokens. Total this minute: {tpm_info['tokens_used_this_minute']}/{tpm_info['limit']}")
                break # Budget acquired
            else:
                wait_time = max(0.1, (tpm_info["minute_start_time"] + 60) - current_time)
        
        # print(f"    TPM: Limit for {model_config['name']} ({tpm_info['tokens_used_this_minute']}/{tpm_info['limit']}). Needs {tokens_needed}. Waiting {wait_time:.2f}s...")
        time.sleep(wait_time)


def call_llm_api(client: OpenAI, model_config: dict, system_prompt: str, user_prompt: str):
    """
    Calls the LLM API with retries and returns the content.
    Manages TPM budget before making the call.
    """
    model_identifier = model_config["model_identifier"]
    custom_params = model_config.get("custom_params", {})
    
    # Calculate input tokens for TPM
    input_text_for_token_count = system_prompt + user_prompt
    tokens_for_this_call = count_tokens(input_text_for_token_count)
    
    # Wait for TPM budget if applicable
    wait_for_tpm_budget(model_config, tokens_for_this_call)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    completion_params = {
        "model": model_identifier,
        "messages": messages,
        **custom_params
    }

    for attempt in range(MAX_API_RETRIES + 1):
        try:
            # print(f"    Attempt {attempt + 1}/{MAX_API_RETRIES + 1}: Calling {model_identifier} for prompt \"{user_prompt[:50].replace('\n', ' ')}...\" ({tokens_for_this_call} tokens)")
            
            start_api_call_time = time.time()
            response = client.chat.completions.create(**completion_params)
            end_api_call_time = time.time()
            # print(f"    API call to {model_identifier} (Prompt: \"{user_prompt[:50].replace('\n', ' ')}...\") took {end_api_call_time - start_api_call_time:.2f}s")

            if response.choices and response.choices[0].message and response.choices[0].message.content:
                content = response.choices[0].message.content
                if content.strip().startswith("```html"): content = content.strip()[7:]
                if content.strip().endswith("```"): content = content.strip()[:-3]

                if "<html" in content.lower() and "</html>" in content.lower():
                    return content.strip()
                else:
                    # print(f"    WARNING: Response for \"{user_prompt[:50].replace('\n', ' ')}...\" doesn't look like full HTML. Content head: {content[:100].replace('\n', ' ')}...")
                    return content.strip()
            else:
                # print(f"    ERROR: No valid choice/content for \"{user_prompt[:50].replace('\n', ' ')}...\" Response: {response}")
                if attempt < MAX_API_RETRIES:
                    # print(f"    Retrying due to no content...")
                    time.sleep(INITIAL_RETRY_DELAY * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0,1)) # Basic retry for no content
                    continue
                return "<!-- ERROR: No valid choice or message content in API response after retries -->"

        except APITimeoutError as e:
            # print(f"    ERROR: API call timed out for {model_identifier} (Prompt: \"{user_prompt[:50].replace('\n', ' ')}...\"): {e}")
            if attempt < MAX_API_RETRIES:
                delay = INITIAL_RETRY_DELAY * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, 1)
                # print(f"    Retrying in {delay:.2f}s...")
                time.sleep(delay)
                continue
            return f"<!-- ERROR: API call timed out after retries. Prompt: {user_prompt[:50]} -->"
        except APIError as e:
            # print(f"    ERROR: API error for {model_identifier} (Prompt: \"{user_prompt[:50].replace('\n', ' ')}...\"): {e}")
            # Retry on typical transient errors (e.g., rate limits, server errors often associated with Cloudflare)
            if e.status_code in [429, 500, 502, 503, 504] and attempt < MAX_API_RETRIES:
                delay = INITIAL_RETRY_DELAY * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, 1)
                # print(f"    Retriable APIError (status {e.status_code}). Retrying in {delay:.2f}s...")
                time.sleep(delay)
                continue
            return f"<!-- ERROR: API error - Status: {e.status_code}, Details: {str(e)} -->"
        except Exception as e:
            # print(f"    ERROR: Unexpected error for {model_identifier} (Prompt: \"{user_prompt[:50].replace('\n', ' ')}...\"): {e}")
            if attempt < MAX_API_RETRIES: # Optionally retry generic errors too
                delay = INITIAL_RETRY_DELAY * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0,1)
                # print(f"    Retrying unexpected error in {delay:.2f}s...")
                time.sleep(delay)
                continue
            return f"<!-- ERROR: Unexpected error - Details: {str(e)} -->"
    return "<!-- ERROR: API call failed after all retries -->"


def process_prompt_for_model(prompt_data: dict, model_config: dict, model_output_dir: Path, system_prompt_base: str, user_prompt_wrapper: str):
    prompt_id = prompt_data["prompt_id"]
    prompt_description = prompt_data["prompt_description"]
    
    effective_system_prompt = system_prompt_base
    effective_user_prompt = user_prompt_wrapper.format(user_prompt_description=prompt_description)

    output_file_path = model_output_dir / f"{prompt_id}.html"

    if output_file_path.exists():
        # print(f"  Skipping {prompt_id} for {model_config['name']}, output exists: {output_file_path}")
        return model_config["name"], prompt_id, "skipped_exists", 0

    model_id = model_config["model_identifier"]
    semaphore = MODEL_SEMAPHORES[model_id]
    
    with semaphore: # Limits concurrent calls for THIS model
        start_prompt_time = time.time()
        # print(f"  Worker acquired for {model_config['name']}, prompt {prompt_id}. Active for model: {model_config['parallel_workers'] - semaphore._value}/{model_config['parallel_workers']}")
        try:
            client = OpenAI(api_key=model_config["api_key"], base_url=model_config["api_base_url"])
        except Exception as e:
            # print(f"  ERROR: Failed to initialize OpenAI client for {model_config['name']}: {e}")
            error_content = f"<!-- ERROR: Failed to initialize API client. Details: {str(e)} -->"
            try:
                with open(output_file_path, "w", encoding="utf-8") as f: f.write(error_content)
            except IOError: pass
            return model_config["name"], prompt_id, "client_init_failed", time.time() - start_prompt_time

        html_content = call_llm_api(client, model_config, effective_system_prompt, effective_user_prompt)

        try:
            with open(output_file_path, "w", encoding="utf-8") as f:
                f.write(html_content if html_content else "<!-- ERROR: LLM returned empty content -->")
            # print(f"  Saved HTML for {prompt_id} from {model_config['name']} to {output_file_path}")
            status = "success" if "<!-- ERROR:" not in html_content else "api_returned_error"
            return model_config["name"], prompt_id, status, time.time() - start_prompt_time
        except IOError as e:
            # print(f"  ERROR: Could not write HTML for {prompt_id} from {model_config['name']}: {e}")
            return model_config["name"], prompt_id, "write_failed", time.time() - start_prompt_time

# --- Main Execution ---
def main():
    if len(sys.argv) < 2:
        print("Usage: python script_name.py <path_to_json_benchmark_file>")
        sys.exit(1)

    json_file_path = sys.argv[1]
    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            benchmark_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: JSON file not found at {json_file_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {json_file_path}. {e}")
        sys.exit(1)

    benchmark_run_name = benchmark_data.get("benchmark_run_name", f"UnnamedRun_{int(time.time())}")
    prompts_to_process = benchmark_data.get("prompts", [])

    if not prompts_to_process:
        print("Error: No prompts found in the JSON input.")
        sys.exit(1)

    # Filter models that have API keys
    active_models = [mc for mc in MODELS_CONFIG if mc.get("api_key")]
    if not active_models:
        print("FATAL: No API keys found for any configured models. Stopping.")
        sys.exit(1)
    
    initialize_concurrency_controls(active_models)

    run_specific_output_dir = BASE_OUTPUT_DIR / benchmark_run_name
    run_specific_output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Base output directory for this run: {run_specific_output_dir.resolve()}")

    overall_start_time = time.time()
    
    tasks = []
    for model_config in active_models:
        model_name_sanitized = model_config["name"].replace('/', '_')
        model_output_dir = run_specific_output_dir / model_name_sanitized
        model_output_dir.mkdir(parents=True, exist_ok=True)
        # print(f"  Configured model '{model_config['name']}'. Outputting to: {model_output_dir.resolve()}")
        for prompt in prompts_to_process:
            tasks.append({
                "prompt_data": prompt,
                "model_config": model_config,
                "model_output_dir": model_output_dir
            })
    
    total_tasks = len(tasks)
    print(f"Total prompts to process across all models: {total_tasks}")
    
    # Calculate total max workers for the global pool
    # This is the theoretical max if all models run all their workers,
    # actual concurrency limited by semaphores and TPM.
    total_max_workers = sum(mc["parallel_workers"] for mc in active_models)
    
    model_stats = {
        mc["name"]: {"success": 0, "api_error": 0, "other_failure": 0, "skipped": 0, "total_time": 0.0, "processed_count": 0, "num_prompts_for_model": len(prompts_to_process)}
        for mc in active_models
    }
    
    completed_tasks = 0

    # Use a single ThreadPoolExecutor for all tasks
    with ThreadPoolExecutor(max_workers=total_max_workers) as executor:
        future_to_task_info = {
            executor.submit(process_prompt_for_model, 
                            task["prompt_data"], 
                            task["model_config"], 
                            task["model_output_dir"], 
                            SYSTEM_PROMPT_BASE, 
                            USER_PROMPT_WRAPPER): task
            for task in tasks
        }

        for future in as_completed(future_to_task_info):
            task_info = future_to_task_info[future]
            model_name = task_info["model_config"]["name"]
            prompt_id_completed = task_info["prompt_data"]["prompt_id"]
            completed_tasks += 1
            
            try:
                _, _, status, time_taken = future.result() # model_name also returned, but we have it
                
                model_stats[model_name]["total_time"] += time_taken
                model_stats[model_name]["processed_count"] += 1

                if status == "success":
                    model_stats[model_name]["success"] += 1
                elif status == "skipped_exists":
                    model_stats[model_name]["skipped"] += 1
                elif status == "api_returned_error":
                     model_stats[model_name]["api_error"] += 1
                else: # client_init_failed, write_failed, etc.
                    model_stats[model_name]["other_failure"] += 1
                
                # print(f"  Progress: {completed_tasks}/{total_tasks} tasks. Last: {model_name} - {prompt_id_completed} ('{status}', {time_taken:.2f}s)")
            except Exception as e:
                model_stats[model_name]["other_failure"] += 1
                model_stats[model_name]["processed_count"] += 1
                print(f"    ERROR processing result for {model_name} - {prompt_id_completed}: {e}")
            
            # More detailed live progress per model
            current_model_progress = model_stats[model_name]["processed_count"]
            total_for_model = model_stats[model_name]["num_prompts_for_model"]
            print(f"  Overall Progress: {completed_tasks}/{total_tasks} | {model_name}: {current_model_progress}/{total_for_model} (Last: {prompt_id_completed}, Status: {status}, Time: {time_taken:.2f}s)")


    overall_end_time = time.time()
    print("\n--- Benchmark Run Summary ---")
    for model_name, stats in model_stats.items():
        print(f"\nModel: {model_name}")
        print(f"  Successfully generated: {stats['success']}")
        print(f"  API returned error:   {stats['api_error']}")
        print(f"  Other failures:       {stats['other_failure']}")
        print(f"  Skipped (exists):     {stats['skipped']}")
        avg_time = (stats['total_time'] / stats['success']) if stats['success'] > 0 else 0
        print(f"  Avg. time per successful prompt: {avg_time:.2f}s")
        print(f"  Total processing time for this model's prompts: {stats['total_time']:.2f}s")

    print(f"\nTotal benchmark run time: {overall_end_time - overall_start_time:.2f} seconds.")

if __name__ == "__main__":
    print("-" * 30)
    print("Model Configuration Check:")
    active_models_count = 0
    for mc_idx, mc in enumerate(MODELS_CONFIG):
        if mc.get("api_key"):
            print(f"  - Model {mc_idx+1}: '{mc['name']}' - API Key OK. Workers: {mc['parallel_workers']}. TPM Limit: {mc.get('tpm_limit', 'None')}.")
            active_models_count += 1
        else:
            print(f"  - Model {mc_idx+1}: '{mc['name']}' - WARNING: API Key NOT FOUND or empty. This model will be skipped.")
    
    if active_models_count == 0 and MODELS_CONFIG:
        print("\nFATAL: No API keys found for any configured models. Please set them.")
        print("Stopping execution.")
        sys.exit(1)
    elif not MODELS_CONFIG:
        print("\nFATAL: MODELS_CONFIG is empty. No models to process.")
        sys.exit(1)
    print(f"Found {active_models_count} model(s) with API keys to process.")
    print("-" * 30)
    
    main()