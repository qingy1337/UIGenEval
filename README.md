
# UIGenEval Benchmark

**Bridging the Evaluation Gap in AI-Driven UI Generation**  
Maintained by the Tesslate Team | [team@tesslate.com](mailto:team@tesslate.com)

----------

## Overview

UIGenEval is a comprehensive benchmark framework for evaluating AI-generated user interfaces (UIs). It closes the "Evaluation Gap" between what generative models produce and how we assess them. Unlike traditional methods that focus on narrow metrics like HTML validity or pixel similarity, UIGenEval provides a holistic, rigorous, and scalable approach to testing generated UIs across:

1.  **Technical Quality**
    
2.  **Prompt Adherence & Feature Completeness**
    
3.  **Interaction & Dynamic Behavior**
    
4.  **Responsive Design**
    

----------

## Why UIGenEval is Better

### ✨ Multi-Pillar Evaluation

-   **Not just valid code**: We test accessibility (WCAG), JS console health, SEO, CSS structure, and semantic HTML.
    
-   **Not just visuals**: We test dynamic behavior (clicks, inputs, DOM updates) and cross-device responsiveness.
    

### ⚙ Programmatic, Reproducible, Scalable

-   Designed to be config-driven and auto-run across multiple models.
    
-   Reproducible scores and reports for each model and prompt.
    

### 🤖 Tailored for LLMs & Generative Models

-   Prompt adherence scoring captures how well instructions are followed.
    
-   Easily define prompt-specific checks using JSON.
    

----------

## Evaluation Pillars

Pillar

What it Evaluates

Technical Quality

Axe-core accessibility, Lighthouse scores, CSS/HTML/JS health

Prompt Adherence

Does the UI do what the prompt says? Checks CSS, text, order, counts, attributes

Dynamic Interaction

Can the UI respond to clicks, inputs, dynamic flows using WebDriver?

Responsive Design

Mobile, desktop, and custom viewport integrity checks

----------

## Running UIGenEval (Step-by-Step)

### 1. ✈️ Install Prerequisites

#### Python (via [uv](https://github.com/astral-sh/uv))

```bash
uv venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
uv pip install selenium webdriver-manager axe-selenium-python wcag-contrast-ratio jinja2 matplotlib pandas
uv pip install openai tiktoken
```

#### Chrome & Lighthouse (optional but recommended)

```bash
npm install -g lighthouse

```

----------

### 2. 📑 Collect Model Outputs

Run your script to generate model outputs:

```bash
python data_collection/data_collection.py all_code_outputs/fullpage_challenge/master_prompts_benchmark_config.json

```

Produces:

```
generated_html_benchmarks/
└── fullpage_challenge/
    ├── model1/
    └── model2/

```

----------

### 3. 📂 Prepare Benchmark Input Directory

```bash
mv generated_html_benchmarks/fullpage_challenge ./all_code_outputs/
cp prompts.json ./all_code_outputs/fullpage_challenge/master_prompts_benchmark_config.json

```

Directory structure:

```
all_code_outputs/
└── fullpage_challenge/
    ├── model1/
    ├── model2/
    └── master_prompts_benchmark_config.json

```

----------

### 4. 🧰 Run the Benchmark

```bash
python orchestrator.py all_code_outputs fullpage_challenge benchmark_results 5 10

```

-   5 = Max models in parallel
    
-   10 = Prompts per model in parallel
    

Output will be saved to:

```
benchmark_results/fullpage_challenge/run_<timestamp>/

```

----------

### 5. 🔢 View Basic Charts

```bash
python view_scores.py benchmark_results fullpage_challenge

```

Creates PNG bar charts:

```
benchmark_results/fullpage_challenge/analysis_charts/

```

----------

### 6. 🌐 Generate Full Static Site

Edit path in `benchmark_site_generator/generate_site.py`:

```python
BENCHMARK_RESULTS_ROOT_DIR = Path("./benchmark_results")

```

Run:

```bash
python benchmark_site_generator/generate_site.py

```

Open:

```
uigel_benchmark_site_output/index.html

```

----------

## Configuration Example

```json
{
  "prompt_id": "login_form_simple",
  "prompt_description": "Create a login form with a username field, a password field, and a blue Login button.",
  "adherence_checks": [
    {
      "check_name": "username_field_present",
      "check_type": "element_presence",
      "selector": "input[type='text'][name='username']",
      "expected_to_be_visible": true,
      "points": 5
    }
  ]
}

```

----------

## 🌟 Scoring Methodology

Each prompt = 0-100 score:

-   **Technical Quality** (e.g., Axe, Lighthouse, HTML/CSS/JS): ~30%
    
-   **Prompt Adherence + Interaction**: ~70%
    

Prompts range from simple to complex:

-   Easy: check if a button exists with right text
    
-   Hard: responsive dashboards with keyboard navigation, aria attributes, or animations
    

----------

## ✅ Summary

-   UIGenEval is the first **comprehensive benchmark** purpose-built for evaluating AI-generated UIs
    
-   Built for real-world prompts, web standards, and LLM-based workflows
    
-   Scalable, reproducible, and packed with diagnostics
    

----------

For questions, issues, or contributions, reach out to:  
**[team@tesslate.com](mailto:team@tesslate.com)**  
[https://github.com/TesslateAI/UIGenEval](https://github.com/TesslateAI/UIGenEval)
