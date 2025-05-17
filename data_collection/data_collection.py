#!/usr/bin/env python3

import sys
import os
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI, APIError, APITimeoutError

# --- Configuration ---

# Define your models here
# Ensure you have the 'openai' package installed: pip install openai
MODELS_CONFIG = [
    {
        "name": "Qwen3-30B-A3B-FP8",  # Friendly name for the model
        "api_key": os.environ.get("OPENAI_API_KEY_GPT35"), # Use environment variables for keys
        "api_base_url": "https://api.openai.com/v1",
        "model_identifier": "Qwen/Qwen3-30B-A3B-FP8", # Specific model string for the API
        "parallel_workers": 20, # Number of parallel calls for this model
        "custom_params": {       # Additional parameters for the API call
            "temperature": 0.2,
            "max_tokens": 30000, # Max tokens for the generated completion
        }
    },
    # {
    #     "name": "GPT4_O_OpenAI",
    #     "api_key": os.environ.get("OPENAI_API_KEY_GPT4O"),
    #     "api_base_url": "https://api.openai.com/v1",
    #     "model_identifier": "gpt-4o",
    #     "parallel_workers": 2,
    #     "custom_params": {
    #         "temperature": 0.2,
    #         "max_tokens": 4000,
    #     }
    # },
    # Example for a hypothetical OpenAI-compatible custom endpoint
    # {
    #     "name": "MyCustomLLM_Fast",
    #     "api_key": "YOUR_CUSTOM_LLM_API_KEY",
    #     "api_base_url": "https://api.customllmprovider.com/v1",
    #     "model_identifier": "custom-model-v2-fast",
    #     "parallel_workers": 5,
    #     "custom_params": {
    #         "temperature": 0.5,
    #         "max_tokens": 3000,
    #         "top_p": 0.9,
    #     }
    # },
]
# Root directory for all generated benchmark outputs
BASE_OUTPUT_DIR = Path("generated_html_benchmarks")

# System prompt template as specified by the user
# The {prompt_description} will be filled in from the JSON
SYSTEM_PROMPT_TEMPLATE = "{} Make it a html css js tailwind(cdn) website. Output everything in one html block. Only output the code block, no other text or explanation. Ensure the Tailwind CDN (<script src=\"https://cdn.tailwindcss.com\"></script>) is in the <head>."

# --- Helper Functions ---

def call_llm_api(client: OpenAI, model_identifier: str, system_prompt: str, user_prompt: str, custom_params: dict):
    """
    Calls the LLM API and returns the content of the assistant's message.
    """
    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        completion_params = {
            "model": model_identifier,
            "messages": messages,
            **custom_params  # Spread the custom parameters
        }

        print(f"    Calling model: {model_identifier} with user prompt: \"{user_prompt[:50]}...\"")
        
        response = client.chat.completions.create(**completion_params)
        
        if response.choices and response.choices[0].message:
            content = response.choices[0].message.content
            # Basic check if it looks like HTML (very naive)
            if content and "<html" in content.lower() and "</html>" in content.lower():
                 return content.strip()
            else:
                print(f"    WARNING: Response for \"{user_prompt[:50]}...\" doesn't look like full HTML. Content: {content[:100]}...")
                return content.strip() # Return whatever we got
        else:
            print(f"    ERROR: No valid choice or message content in response for \"{user_prompt[:50]}...\"")
            return "<!-- ERROR: No valid choice or message content in API response -->"

    except APITimeoutError:
        print(f"    ERROR: API call timed out for model {model_identifier}, prompt \"{user_prompt[:50]}...\"")
        return f"<!-- ERROR: API call timed out for prompt: {user_prompt} -->"
    except APIError as e:
        print(f"    ERROR: API error for model {model_identifier}, prompt \"{user_prompt[:50]}...\": {e}")
        return f"<!-- ERROR: API error for prompt: {user_prompt} - Details: {str(e)} -->"
    except Exception as e:
        print(f"    ERROR: Unexpected error calling model {model_identifier}, prompt \"{user_prompt[:50]}...\": {e}")
        return f"<!-- ERROR: Unexpected error for prompt: {user_prompt} - Details: {str(e)} -->"

def process_prompt_for_model(prompt_data: dict, model_config: dict, benchmark_output_dir: Path):
    """
    Processes a single prompt for a given model: calls the API and saves the result.
    """
    prompt_id = prompt_data["prompt_id"]
    prompt_description = prompt_data["prompt_description"]
    
    # Construct the system prompt using the template and current prompt's description
    effective_system_prompt = SYSTEM_PROMPT_TEMPLATE.format(prompt_description)
    # The user prompt for the API will be the description itself
    effective_user_prompt = prompt_description

    output_file_path = benchmark_output_dir / f"{prompt_id}.html"

    # Skip if output file already exists (e.g. from a previous partial run)
    if output_file_path.exists():
        print(f"  Skipping {prompt_id} for model {model_config['name']}, output file already exists: {output_file_path}")
        return prompt_id, "skipped_exists"

    # Initialize OpenAI client for this specific model's configuration
    try:
        client = OpenAI(
            api_key=model_config["api_key"],
            base_url=model_config["api_base_url"]
        )
    except Exception as e:
        print(f"  ERROR: Failed to initialize OpenAI client for model {model_config['name']}: {e}")
        # Create an error HTML file
        error_content = f"<!-- ERROR: Failed to initialize API client for model {model_config['name']}. Details: {str(e)} -->"
        with open(output_file_path, "w", encoding="utf-8") as f:
            f.write(error_content)
        return prompt_id, "client_init_failed"

    # Call the LLM API
    html_content = call_llm_api(
        client,
        model_config["model_identifier"],
        effective_system_prompt,
        effective_user_prompt,
        model_config.get("custom_params", {})
    )

    # Save the HTML content
    try:
        with open(output_file_path, "w", encoding="utf-8") as f:
            f.write(html_content if html_content else "<!-- ERROR: LLM returned empty content -->")
        print(f"  Saved HTML for {prompt_id} from model {model_config['name']} to {output_file_path}")
        return prompt_id, "success"
    except IOError as e:
        print(f"  ERROR: Could not write HTML file for {prompt_id} from model {model_config['name']}: {e}")
        return prompt_id, "write_failed"

# --- Main Execution ---
def main():
    if len(sys.argv) < 2:
        print("Usage: python main_harness.py \"<json_benchmark_string>\"")
        print("Example: python main_harness.py \"{'benchmark_run_name': 'MyTestRun', 'prompts': [{'prompt_id': 'P001', 'prompt_description': 'Create a simple red button.'}]}\"")
        sys.exit(1)

    json_input_string = sys.argv[1]

    try:
        benchmark_data = json.loads(json_input_string)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON input provided. {e}")
        sys.exit(1)

    benchmark_run_name = benchmark_data.get("benchmark_run_name", f"UnnamedRun_{int(time.time())}")
    prompts_to_process = benchmark_data.get("prompts", [])

    if not prompts_to_process:
        print("Error: No prompts found in the JSON input.")
        sys.exit(1)

    BASE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Base output directory: {BASE_OUTPUT_DIR.resolve()}")

    for model_config in MODELS_CONFIG:
        model_name = model_config["name"]
        num_workers = model_config["parallel_workers"]

        if not model_config.get("api_key"):
            print(f"\n--- Skipping Model: {model_name} (API key not configured) ---")
            continue
            
        print(f"\n--- Processing Model: {model_name} (Parallel Workers: {num_workers}) ---")

        # Create model-specific output directory
        model_benchmark_output_dir = BASE_OUTPUT_DIR / f"{model_name}_{benchmark_run_name}"
        model_benchmark_output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Outputting to: {model_benchmark_output_dir.resolve()}")

        start_time_model = time.time()
        processed_count = 0
        failed_count = 0

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(process_prompt_for_model, prompt, model_config, model_benchmark_output_dir): prompt["prompt_id"]
                for prompt in prompts_to_process
            }

            for future in as_completed(futures):
                prompt_id_completed = futures[future]
                try:
                    _, status = future.result()
                    if status == "success":
                        processed_count += 1
                    elif status != "skipped_exists": # Don't count skips as failures unless they are actual processing failures
                        failed_count +=1
                    print(f"  Completed processing for prompt_id: {prompt_id_completed} with status: {status}")
                except Exception as e:
                    failed_count +=1
                    print(f"  ERROR: An exception occurred while processing prompt_id {prompt_id_completed}: {e}")
        
        end_time_model = time.time()
        print(f"--- Finished Model: {model_name} ---")
        print(f"  Successfully generated HTMLs: {processed_count}")
        print(f"  Failed generations (or client init): {failed_count}")
        print(f"  Skipped (already exist): {len(prompts_to_process) - processed_count - failed_count}")
        print(f"  Time taken for {model_name}: {end_time_model - start_time_model:.2f} seconds")

    print("\nAll models processed.")

if __name__ == "__main__":
    # Check for API keys and provide guidance
    missing_keys_models = []
    for mc in MODELS_CONFIG:
        if not mc.get("api_key"):
            missing_keys_models.append(mc["name"])
    if missing_keys_models:
        print("WARNING: API key(s) not found for the following configured models:")
        for mn in missing_keys_models:
            print(f"  - {mn}. Please set the corresponding environment variable (e.g., OPENAI_API_KEY_GPT35 for GPT3_5_Turbo_OpenAI).")
        print("These models will be skipped.")
        print("-" * 30)
    
    main()