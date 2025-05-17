#!/usr/bin/env python3

import sys
import os
import re
import json
import subprocess
import time
import shutil
from urllib.parse import urlparse, unquote, quote
from pathlib import Path
import threading
import socket
import http.server
import socketserver
from functools import partial
from difflib import SequenceMatcher
from concurrent.futures import ProcessPoolExecutor, as_completed # Add this

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException,
    StaleElementReferenceException, ElementNotInteractableException,
    ElementClickInterceptedException
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager # Ensure this is installed
import wcag_contrast_ratio as contrast_lib
from axe_selenium_python import Axe # Ensure this is installed

# --- Configuration ---
DEFAULT_VIEWPORTS = {
    "desktop": (1920, 1080),
    "mobile": (375, 667)
}
LIGHTHOUSE_CATEGORIES = ['performance', 'accessibility', 'best-practices', 'seo']

# Baseline Max Points for Technical Quality Categories (Applied per prompt)
TECHNICAL_QUALITY_MAX_POINTS_CONFIG = {
    "Accessibility (Axe-core)": 20,
    "Performance (Lighthouse)": 20,
    "Accessibility (Lighthouse)": 10,
    "Best Practices (Lighthouse)": 5,
    "SEO (Lighthouse)": 5,
    "Rendered Color & Contrast": 15,
    "HTML Structure & Semantics": 10,
    "CSS Quality": 5,
    "Responsiveness (Viewport & Scroll)": 10,
    "JavaScript Health": 5
}
# TOTAL_TECHNICAL_QUALITY_MAX_FOR_PROMPT will be dynamic

# Scoring Weights
WEIGHT_TECHNICAL_QUALITY = 0.3
WEIGHT_PROMPT_ADHERENCE = 0.7

# NEW: Configuration for parallel prompt processing within a single model run
# Adjust default based on typical machine capabilities or make it a script argument
DEFAULT_PROMPT_WORKERS = max(1, os.cpu_count() // 2)
PROMPT_WORKERS_COUNT = int(os.environ.get("PROMPT_WORKERS_COUNT", DEFAULT_PROMPT_WORKERS))


# --- Helper Functions ---
def parse_color_string_to_rgb_tuple(color_str):
    if not color_str: return None
    color_str = color_str.lower().strip()
    if color_str == 'transparent': return None
    if color_str.startswith('#'):
        hex_color = color_str[1:]
        if len(hex_color) == 3: return tuple(int(c * 2, 16) for c in hex_color) + (1.0,)
        if len(hex_color) == 6: return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4)) + (1.0,)
        if len(hex_color) == 8: # RGBA hex
            r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
            a = int(hex_color[6:8], 16) / 255.0
            if a == 0: return None
            return (r, g, b, a)
    match_rgba = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", color_str)
    if match_rgba:
        r, g, b, a_str = match_rgba.groups()
        a = float(a_str) if a_str is not None else 1.0
        if a == 0: return None
        return (int(r), int(g), int(b), a)
    match_rgb = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color_str)
    if match_rgb:
        return (int(match_rgb.group(1)), int(match_rgb.group(2)), int(match_rgb.group(3)), 1.0)
    named = {"white": (255,255,255,1.0), "black": (0,0,0,1.0), "red":(255,0,0,1.0),
             "green":(0,128,0,1.0), "blue":(0,0,255,1.0)}
    return named.get(color_str)

def normalize_rgb_for_contrast(rgb_0_255_tuple): # Expects (r,g,b)
    if not rgb_0_255_tuple or len(rgb_0_255_tuple) != 3: return None
    return tuple(c / 255.0 for c in rgb_0_255_tuple)

def blend_colors(fg_rgba, bg_rgb): # fg_rgba=(r,g,b,a), bg_rgb=(r,g,b)
    fg_r, fg_g, fg_b, alpha = fg_rgba
    bg_r, bg_g, bg_b = bg_rgb
    r = int(fg_r * alpha + bg_r * (1 - alpha))
    g = int(fg_g * alpha + bg_g * (1 - alpha))
    b = int(fg_b * alpha + bg_b * (1 - alpha))
    return (r, g, b)

def get_effective_background_rgb(element, driver):
    current_el = element
    path_colors_with_alpha = []
    doc_bg_color_str = driver.execute_script("return getComputedStyle(document.documentElement).backgroundColor;")
    doc_bg_rgba = parse_color_string_to_rgb_tuple(doc_bg_color_str)
    effective_bg_rgb = (doc_bg_rgba[0], doc_bg_rgba[1], doc_bg_rgba[2]) if doc_bg_rgba and doc_bg_rgba[3] == 1.0 else (255, 255, 255)

    while current_el:
        try:
            tag_name = current_el.tag_name.lower()
            is_root_or_body = tag_name in ['html', 'body']
            bg_color_str = driver.execute_script("return getComputedStyle(arguments[0]).backgroundColor;", current_el)
            parsed_rgba = parse_color_string_to_rgb_tuple(bg_color_str)
            if parsed_rgba:
                if parsed_rgba[3] == 1.0: # Fully opaque
                    effective_bg_rgb = (parsed_rgba[0], parsed_rgba[1], parsed_rgba[2])
                    for layer_rgba in reversed(path_colors_with_alpha): effective_bg_rgb = blend_colors(layer_rgba, effective_bg_rgb)
                    return effective_bg_rgb
                elif parsed_rgba[3] > 0: path_colors_with_alpha.append(parsed_rgba)
            if is_root_or_body: break
            parent = driver.execute_script("return arguments[0].parentElement;", current_el)
            if not parent or current_el == parent : break
            current_el = parent
        except (WebDriverException, StaleElementReferenceException): break
    for layer_rgba in reversed(path_colors_with_alpha): effective_bg_rgb = blend_colors(layer_rgba, effective_bg_rgb)
    return effective_bg_rgb

def get_element_desc(element):
    try:
        tag = element.tag_name; el_id = element.get_attribute('id'); el_class = element.get_attribute('class')
        el_testid = element.get_attribute('data-testid'); desc = f"<{tag}"
        if el_id: desc += f" id='{el_id}'"
        if el_testid: desc += f" data-testid='{el_testid}'"
        if el_class: desc += f" class='{el_class[:30]}{'...' if len(el_class)>30 else ''}'"
        desc += ">"
        return desc
    except: return "<stale_or_invalid_element>"

def string_similarity(a, b):
    return SequenceMatcher(None, str(a), str(b)).ratio()

class UIBenchmarkAnalyzer:
    # Categories whose max points scale if run on multiple standard viewports (e.g., desktop & mobile)
    PER_VIEWPORT_SCALABLE_CATEGORIES = [
        "Accessibility (Axe-core)",
        "Rendered Color & Contrast",
        "Responsiveness (Viewport & Scroll)",
        "Performance (Lighthouse)",
        "Accessibility (Lighthouse)",
        "Best Practices (Lighthouse)",
        "SEO (Lighthouse)"
    ]

    def __init__(self, html_file_path, prompt_config_object, output_base_dir, run_timestamp_str, viewports=None):
        self.file_path = Path(html_file_path).resolve()
        self.prompt_config = prompt_config_object

        if not self.file_path.is_file():
            raise FileNotFoundError(f"HTML file not found: {self.file_path}")

        self.selenium_uri = self.file_path.as_uri()
        self.run_timestamp = run_timestamp_str
        self.prompt_id = self.prompt_config.get("prompt_id", self.file_path.stem)

        self.prompt_output_dir = Path(output_base_dir) / f"{self.prompt_id}_{self.run_timestamp}"
        self.prompt_output_dir.mkdir(parents=True, exist_ok=True)

        self.screenshots_dir = self.prompt_output_dir / "screenshots"
        self.screenshots_dir.mkdir(exist_ok=True)

        self.viewports_to_test = viewports or self.prompt_config.get("viewports_to_test", DEFAULT_VIEWPORTS)

        self.current_prompt_scores = {
            "technical_quality": {"earned": 0.0, "max": 0.0, "categories": {}},
            "prompt_adherence": {
                "earned": 0.0,
                "max": sum(check.get("points", 0) for check in self.prompt_config.get("adherence_checks", [])),
                "details": []
            },
            "page_load_errors": []
        }
        self.technical_checks_completed_flags = {}

        self.page_title = "N/A"
        self.lighthouse_path = shutil.which("lighthouse")
        if not self.lighthouse_path:
            print(f"WARNING (Prompt: {self.prompt_id}): Lighthouse CLI not found. Performance/some quality checks will be skipped.")

        self.http_server = None
        self.http_thread = None
        self.server_port = None
        self.local_server_url_for_lighthouse = None

        options = ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu'); options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage'); options.add_experimental_option('excludeSwitches', ['enable-logging'])
        options.add_argument('--log-level=3'); options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

        try:
            try: driver_path = ChromeDriverManager().install(); service = ChromeService(executable_path=driver_path)
            except Exception: print(f"WebDriverManager failed for {self.prompt_id}, trying system ChromeDriver."); service = ChromeService()
            self.driver = webdriver.Chrome(service=service, options=options)
        except WebDriverException as e:
            raise WebDriverException(f"Fatal WebDriver Error for prompt {self.prompt_id}: {e}")

        self.current_viewport_name = "initial"
        self.global_run_timestamp = run_timestamp_str

    def _start_local_server(self):
        if self.http_server: return self.local_server_url_for_lighthouse
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM); sock.bind(('localhost', 0))
        self.server_port = sock.getsockname()[1]; sock.close()
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(self.file_path.parent))
        socketserver.TCPServer.allow_reuse_address = True
        self.http_server = socketserver.TCPServer(("localhost", self.server_port), handler)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True); self.http_thread.start()
        self.local_server_url_for_lighthouse = f"http://localhost:{self.server_port}/{quote(self.file_path.name)}"
        print(f"  (Prompt {self.prompt_id}) Local HTTP server started: {self.local_server_url_for_lighthouse}")
        time.sleep(0.5); return self.local_server_url_for_lighthouse

    def _stop_local_server(self):
        if self.http_server:
            print(f"  (Prompt {self.prompt_id}) Stopping local HTTP server...")
            self.http_server.shutdown(); self.http_server.server_close()
            if self.http_thread and self.http_thread.is_alive(): self.http_thread.join(timeout=2)
            self.http_server = self.http_thread = self.local_server_url_for_lighthouse = None
            print(f"  (Prompt {self.prompt_id}) Local HTTP server stopped.")

    def _add_finding(self, category_key, check_name, points_earned, max_points_for_check, message, status="INFO", data=None, is_adherence_check=False):
        points_earned = float(points_earned)
        max_points_for_check = float(max_points_for_check)

        finding_detail = {
            "check": check_name, "status": status, "message": message,
            "points_earned": points_earned, "max_points_for_this_check": max_points_for_check,
            "viewport": self.current_viewport_name, "data": data
        }

        if is_adherence_check:
            self.current_prompt_scores["prompt_adherence"]["details"].append(finding_detail)
        else: # Technical Quality Check
            if category_key not in self.current_prompt_scores["technical_quality"]["categories"]:
                self.current_prompt_scores["technical_quality"]["categories"][category_key] = {
                    "earned": 0.0,
                    "max": 0.0,
                    "details": []
                }

            if self.current_prompt_scores["technical_quality"]["categories"][category_key]["max"] == 0.0:
                 base_max_for_category = TECHNICAL_QUALITY_MAX_POINTS_CONFIG.get(category_key, 0.0)
                 effective_max_for_category = base_max_for_category
                 if category_key in self.PER_VIEWPORT_SCALABLE_CATEGORIES:
                     has_desktop_viewport = "desktop" in self.viewports_to_test
                     has_mobile_viewport = "mobile" in self.viewports_to_test
                     if has_desktop_viewport and has_mobile_viewport:
                         effective_max_for_category = base_max_for_category * 2

                 self.current_prompt_scores["technical_quality"]["categories"][category_key]["max"] = effective_max_for_category
                 self.current_prompt_scores["technical_quality"]["max"] += effective_max_for_category

            self.current_prompt_scores["technical_quality"]["categories"][category_key]["earned"] += points_earned
            self.current_prompt_scores["technical_quality"]["categories"][category_key]["details"].append(finding_detail)

        if status == "FAIL": print(f"    -> [FAIL] ({self.prompt_id}@{self.current_viewport_name}) {category_key} - {check_name}: {message}")
        elif status == "WARN" and max_points_for_check > 0: print(f"    -> [WARN] ({self.prompt_id}@{self.current_viewport_name}) {category_key} - {check_name}: {message}")

    def _load_page_at_viewport(self, viewport_name, width, height):
        print(f"\n--- Viewport: {viewport_name} ({width}x{height}) for Prompt: {self.prompt_id} ---")
        self.current_viewport_name = viewport_name
        self.driver.set_window_size(width, height)
        try:
            self.driver.get(self.selenium_uri)
            WebDriverWait(self.driver, 15).until(lambda d: d.execute_script('return document.readyState') == 'complete')
            self.page_title = self.driver.title or "N/A"
            time.sleep(1.0)
            screenshot_path = self.screenshots_dir / f"{self.prompt_id}_{viewport_name}.png"
            self.driver.save_screenshot(str(screenshot_path))
            print(f"  Page loaded. Screenshot: {screenshot_path.name}")
        except TimeoutException:
            self._add_finding("Page Load Errors", "Page Completeness", 0, 0, f"Page timed out loading at {viewport_name}.", "FAIL")
            self.current_prompt_scores["page_load_errors"].append(f"Timeout at {viewport_name}")
            raise
        except WebDriverException as e:
            self._add_finding("Page Load Errors", "Driver Operation", 0, 0, f"WebDriver error at {viewport_name}: {e}", "FAIL")
            self.current_prompt_scores["page_load_errors"].append(f"WebDriver error at {viewport_name}: {e}")
            raise

    def _find_element_by_config(self, check_config, base_element=None):
        context = base_element if base_element else self.driver
        selector = check_config.get("selector")
        selector_type_str = check_config.get("selector_type", "css").lower()
        by_type = By.CSS_SELECTOR
        if selector_type_str == "xpath": by_type = By.XPATH
        elif selector_type_str == "id": by_type = By.ID
        elif selector_type_str == "name": by_type = By.NAME
        elif selector_type_str == "class_name": by_type = By.CLASS_NAME
        elif selector_type_str == "tag_name": by_type = By.TAG_NAME
        elif selector_type_str == "link_text": by_type = By.LINK_TEXT
        elif selector_type_str == "partial_link_text": by_type = By.PARTIAL_LINK_TEXT
        elif selector_type_str == "data-testid":
            by_type = By.XPATH
            if base_element:
                 selector = f".//*[@data-testid='{selector}']" # Context-aware XPath
            else:
                 selector = f"//*[@data-testid='{selector}']"

        try:
            if check_config.get("find_multiple", False): return context.find_elements(by_type, selector)
            else: return context.find_element(by_type, selector)
        except NoSuchElementException: return None
        except WebDriverException as e:
            print(f"    WARN: Error finding element '{selector}' ({selector_type_str}): {e}")
            return None

    # --- Technical Quality Check Methods ---
    def check_html_structure_semantics(self):
        category_key = "HTML Structure & Semantics"
        if self.technical_checks_completed_flags.get(category_key): return
        print(f"\nRunning Checks: {category_key} (Prompt: {self.prompt_id})")

        category_max_points = TECHNICAL_QUALITY_MAX_POINTS_CONFIG[category_key]

        sub_check_defs = {
            "HTML Lang": {"points": 1, "pass": False},
            "Page Title": {"points": 1, "pass": False},
            "Main Tag": {"points": 1, "pass": False},
            "Nav Tag": {"points": 1, "pass": False},
            "Footer Tag": {"points": 1, "pass": False},
            "H1 Count": {"points": 1, "pass": False},
            "Heading Order Logic": {"points": 1, "pass": False}, # Changed from 2 to 1
            "Image Alts": {"points": 1, "pass": False},
            "Form Labels": {"points": 2, "pass": False}
        }
        # Sum of points: 1+1+1+1+1+1+1+1+2 = 10
        if sum(d["points"] for d in sub_check_defs.values()) != category_max_points:
            print(f"  WARN: HTML Structure sub-check points sum to {sum(d['points'] for d in sub_check_defs.values())}, but category max is {category_max_points}. Review config.")

        # HTML Lang
        try:
            lang = self.driver.find_element(By.TAG_NAME, "html").get_attribute("lang")
            if lang and lang.strip(): sub_check_defs["HTML Lang"]["pass"] = True
        except: pass
        self._add_finding(category_key, "HTML Lang", sub_check_defs["HTML Lang"]["points"] if sub_check_defs["HTML Lang"]["pass"] else 0, sub_check_defs["HTML Lang"]["points"], "PASS" if sub_check_defs["HTML Lang"]["pass"] else "Missing/empty.", "PASS" if sub_check_defs["HTML Lang"]["pass"] else "FAIL")

        # Page Title
        if self.page_title and self.page_title != "N/A" and len(self.page_title.strip()) > 0: sub_check_defs["Page Title"]["pass"] = True
        self._add_finding(category_key, "Page Title", sub_check_defs["Page Title"]["points"] if sub_check_defs["Page Title"]["pass"] else 0, sub_check_defs["Page Title"]["points"], "PASS" if sub_check_defs["Page Title"]["pass"] else "Missing/empty.", "PASS" if sub_check_defs["Page Title"]["pass"] else "WARN")

        # Main, Nav, Footer Tags
        for tag_key, tag_name_str in {"Main Tag": "main", "Nav Tag": "nav", "Footer Tag": "footer"}.items():
            try:
                elements = self.driver.find_elements(By.TAG_NAME, tag_name_str)
                if any(el.is_displayed() for el in elements): sub_check_defs[tag_key]["pass"] = True
                status_msg = "PASS" if sub_check_defs[tag_key]["pass"] else f"No visible <{tag_name_str}>."
                status_level = "PASS" if sub_check_defs[tag_key]["pass"] else ("FAIL" if tag_name_str == "main" else "WARN")
                self._add_finding(category_key, tag_key, sub_check_defs[tag_key]["points"] if sub_check_defs[tag_key]["pass"] else 0, sub_check_defs[tag_key]["points"], status_msg, status_level)
            except WebDriverException: self._add_finding(category_key, tag_key, 0, sub_check_defs[tag_key]["points"], f"Error checking <{tag_name_str}>.", "FAIL")

        # Headings (H1 Count and Order)
        h1_pass_flag = False; heading_order_pass_flag = False
        try:
            headings = self.driver.find_elements(By.CSS_SELECTOR, "h1, h2, h3, h4, h5, h6")
            sorted_visible_headings = sorted([h for h in headings if h.is_displayed()], key=lambda x: (x.location['y'], x.location['x']))
            if not sorted_visible_headings:
                h1_msg = "No visible headings."; h_order_msg = "No visible headings."
            else:
                h1s = [h for h in sorted_visible_headings if h.tag_name == 'h1']
                if len(h1s) == 1: h1_pass_flag = True; h1_msg = "PASS"
                elif not h1s: h1_msg = "No visible <h1>."
                else: h1_msg = f"Multiple visible <h1>s ({len(h1s)})."

                last_level = 0; order_ok_temp = True
                for i, h_el in enumerate(sorted_visible_headings):
                    current_level = int(h_el.tag_name[1])
                    if i == 0 and current_level != 1 and any(vh.tag_name == 'h1' for vh in sorted_visible_headings) : order_ok_temp = False
                    if i > 0 and current_level > last_level + 1: order_ok_temp = False; break
                    last_level = current_level
                if order_ok_temp: heading_order_pass_flag = True; h_order_msg = "PASS"
                else: h_order_msg = "Heading order issue."
            sub_check_defs["H1 Count"]["pass"] = h1_pass_flag
            sub_check_defs["Heading Order Logic"]["pass"] = heading_order_pass_flag
        except WebDriverException: h1_msg = "Error processing."; h_order_msg = "Error processing."
        self._add_finding(category_key, "H1 Count", sub_check_defs["H1 Count"]["points"] if sub_check_defs["H1 Count"]["pass"] else 0, sub_check_defs["H1 Count"]["points"], h1_msg, "PASS" if sub_check_defs["H1 Count"]["pass"] else "FAIL")
        self._add_finding(category_key, "Heading Order Logic", sub_check_defs["Heading Order Logic"]["points"] if sub_check_defs["Heading Order Logic"]["pass"] else 0, sub_check_defs["Heading Order Logic"]["points"], h_order_msg, "PASS" if sub_check_defs["Heading Order Logic"]["pass"] else "FAIL")

        # Image Alts
        try:
            images = [img for img in self.driver.find_elements(By.TAG_NAME, "img") if img.is_displayed()]
            if not images: sub_check_defs["Image Alts"]["pass"] = True; alt_msg = "No visible images (INFO)."
            elif all(img.get_attribute("alt") is not None for img in images): sub_check_defs["Image Alts"]["pass"] = True; alt_msg = "PASS"
            else: alt_msg = f"{sum(1 for img in images if img.get_attribute('alt') is None)} visible images missing 'alt'."
            alt_status = "PASS" if sub_check_defs["Image Alts"]["pass"] else "FAIL"
            if alt_msg.endswith("(INFO)."): alt_status = "INFO"
            self._add_finding(category_key, "Image Alts", sub_check_defs["Image Alts"]["points"] if sub_check_defs["Image Alts"]["pass"] else 0, sub_check_defs["Image Alts"]["points"], alt_msg, alt_status)
        except WebDriverException: self._add_finding(category_key, "Image Alts", 0, sub_check_defs["Image Alts"]["points"], "Error checking image alts.", "FAIL")

        # Form Labels
        try:
            inputs_selector = 'input:not([type="hidden"]):not([type="submit"]):not([type="reset"]):not([type="button"]):not([type="image"]), select, textarea'
            form_inputs = [inp for inp in self.driver.find_elements(By.CSS_SELECTOR, inputs_selector) if inp.is_displayed()]
            unlabeled_inputs = 0 # Define before conditional assignment
            if not form_inputs: sub_check_defs["Form Labels"]["pass"] = True; label_msg = "No relevant form inputs (INFO)."
            else:
                for inp in form_inputs:
                    inp_id = inp.get_attribute("id"); has_label = False
                    if inp_id:
                        try: self.driver.find_element(By.CSS_SELECTOR, f"label[for='{inp_id}']"); has_label = True
                        except NoSuchElementException: pass
                    if not has_label:
                        try: inp.find_element(By.XPATH, "ancestor::label"); has_label = True
                        except NoSuchElementException: pass
                    if not has_label and (inp.get_attribute("aria-label") or inp.get_attribute("aria-labelledby")): has_label = True
                    if not has_label: unlabeled_inputs += 1
                if unlabeled_inputs == 0: sub_check_defs["Form Labels"]["pass"] = True; label_msg = "PASS"
                else: label_msg = f"{unlabeled_inputs} inputs unlabeled."
            label_status = "PASS" if sub_check_defs["Form Labels"]["pass"] else "FAIL"
            if label_msg.endswith("(INFO)."): label_status = "INFO"
            self._add_finding(category_key, "Form Labels", sub_check_defs["Form Labels"]["points"] if sub_check_defs["Form Labels"]["pass"] else 0, sub_check_defs["Form Labels"]["points"], label_msg, label_status, data={"unlabeled_count":unlabeled_inputs})
        except WebDriverException: self._add_finding(category_key, "Form Labels", 0, sub_check_defs["Form Labels"]["points"],"Error checking form labels.", "FAIL")

        self.technical_checks_completed_flags[category_key] = True

    def check_accessibility_axe(self):
        category_key = "Accessibility (Axe-core)"
        print(f"\nRunning Checks: {category_key} (Prompt: {self.prompt_id}, Viewport: {self.current_viewport_name})")
        max_points_per_run = TECHNICAL_QUALITY_MAX_POINTS_CONFIG[category_key] # This is the base for one viewport
        try:
            axe = Axe(self.driver); axe.inject(); results = axe.run()
            report_path_obj = self.prompt_output_dir / f"axe_report_{self.prompt_id}_{self.current_viewport_name}.json"
            axe.write_results(results, str(report_path_obj))

            violations = results.get("violations", [])
            if not violations:
                self._add_finding(category_key, "Axe Violations", max_points_per_run, max_points_per_run, "No violations.", "PASS")
            else:
                critical_issues = sum(1 for v in violations if v['impact'] == 'critical')
                serious_issues = sum(1 for v in violations if v['impact'] == 'serious')
                moderate_issues = sum(1 for v in violations if v['impact'] == 'moderate')
                minor_issues = sum(1 for v in violations if v['impact'] == 'minor') # Added minor

                # Penalty: Crit:10, Ser:5, Mod:2, Minor:1 (per violation, up to max_points_per_run)
                penalty = (critical_issues * 10) + (serious_issues * 5) + (moderate_issues * 2) + (minor_issues *1)
                earned_points = max(0, max_points_per_run - penalty)
                status = "PASS" if earned_points >= max_points_per_run * 0.9 else "FAIL" if critical_issues > 0 or serious_issues > 0 else "WARN"
                msg = f"{len(violations)} Axe violations (Crit:{critical_issues},Ser:{serious_issues},Mod:{moderate_issues},Min:{minor_issues}). Report: {report_path_obj.name}"
                self._add_finding(category_key, "Axe Violations", earned_points, max_points_per_run, msg, status, data=[{"id":v['id'], "impact":v['impact'], "help":v['help']} for v in violations])
        except Exception as e_axe:
            self._add_finding(category_key, "Axe Execution", 0, max_points_per_run, f"Error running Axe-core: {str(e_axe)[:200]}", "FAIL")

    def check_css_quality(self):
        category_key = "CSS Quality"
        if self.technical_checks_completed_flags.get(category_key): return
        print(f"\nRunning Checks: {category_key} (Prompt: {self.prompt_id})")

        category_max_points = TECHNICAL_QUALITY_MAX_POINTS_CONFIG[category_key]
        sub_check_defs = {
            "CSS Variables": {"points": 2, "pass": False},
            "Modern Layout Body/Main": {"points": 1, "pass": False},
            "Inline Styles": {"points": 1, "pass": False},
            "!important Usage": {"points": 1, "pass": False}
        }
        if sum(d["points"] for d in sub_check_defs.values()) != category_max_points:
             print(f"  WARN: CSS Quality sub-check points sum to {sum(d['points'] for d in sub_check_defs.values())}, but category max is {category_max_points}.")

        # CSS Variables
        try:
            vars_count = len(self.driver.execute_script("return Object.keys(getComputedStyle(document.documentElement)).filter(k => k.startsWith('--'));") or [])
            if vars_count > 3 : sub_check_defs["CSS Variables"]["pass"] = True; css_vars_msg = f"Good use ({vars_count} vars)."
            elif vars_count > 0: sub_check_defs["CSS Variables"]["pass"] = True; css_vars_msg = f"Some use ({vars_count} vars)."
            else: css_vars_msg = "No :root CSS variables."
            self._add_finding(category_key, "CSS Variables", sub_check_defs["CSS Variables"]["points"] if sub_check_defs["CSS Variables"]["pass"] else 0, sub_check_defs["CSS Variables"]["points"], css_vars_msg, "PASS" if sub_check_defs["CSS Variables"]["pass"] else "WARN")
        except: self._add_finding(category_key, "CSS Variables", 0, sub_check_defs["CSS Variables"]["points"], "Error checking CSS vars.", "FAIL")

        # Modern Layout (Flex/Grid on body or main)
        try:
            body_display = self.driver.execute_script("return getComputedStyle(document.body).display;")
            main_els = self.driver.find_elements(By.TAG_NAME, "main")
            main_display = self.driver.execute_script("return getComputedStyle(arguments[0]).display;", main_els[0]) if main_els and main_els[0].is_displayed() else ""
            if body_display in ['flex', 'grid'] or main_display in ['flex', 'grid']: sub_check_defs["Modern Layout Body/Main"]["pass"] = True
            modern_layout_msg = "Uses flex/grid on body/main." if sub_check_defs["Modern Layout Body/Main"]["pass"] else "Flex/Grid not on body/main."
            self._add_finding(category_key, "Modern Layout Body/Main", sub_check_defs["Modern Layout Body/Main"]["points"] if sub_check_defs["Modern Layout Body/Main"]["pass"] else 0, sub_check_defs["Modern Layout Body/Main"]["points"], modern_layout_msg, "PASS" if sub_check_defs["Modern Layout Body/Main"]["pass"] else "INFO")
        except: self._add_finding(category_key, "Modern Layout Body/Main", 0, sub_check_defs["Modern Layout Body/Main"]["points"], "Error checking layout.", "FAIL")

        # Inline Styles
        try:
            inline_styles_count = len([el for el in self.driver.find_elements(By.CSS_SELECTOR, "[style]") if el.is_displayed() and el.get_attribute("style").strip()])
            if inline_styles_count <= 3: sub_check_defs["Inline Styles"]["pass"] = True; inline_style_msg = "Minimal inline styles."
            elif inline_styles_count <= 10: inline_style_msg = f"Moderate inline styles ({inline_styles_count})."
            else: inline_style_msg = f"Excessive inline styles ({inline_styles_count})."
            self._add_finding(category_key, "Inline Styles", sub_check_defs["Inline Styles"]["points"] if sub_check_defs["Inline Styles"]["pass"] else (sub_check_defs["Inline Styles"]["points"]*0.5 if inline_styles_count <=10 else 0) , sub_check_defs["Inline Styles"]["points"], inline_style_msg, "PASS" if sub_check_defs["Inline Styles"]["pass"] else ("WARN" if inline_styles_count <=10 else "FAIL"))
        except: self._add_finding(category_key, "Inline Styles", 0, sub_check_defs["Inline Styles"]["points"], "Error checking inline styles.", "FAIL")

        # !important Usage
        try:
            styles_content = self.driver.execute_script("let c=''; document.querySelectorAll('style').forEach(s=>c+=s.textContent); return c;")
            important_count = styles_content.lower().count("!important")
            if important_count == 0: sub_check_defs["!important Usage"]["pass"] = True; important_msg = "No !important in <style> tags."
            elif important_count <= 2: important_msg = f"Low !important usage ({important_count})."
            else: important_msg = f"High !important usage ({important_count})."
            self._add_finding(category_key, "!important Usage", sub_check_defs["!important Usage"]["points"] if sub_check_defs["!important Usage"]["pass"] else (sub_check_defs["!important Usage"]["points"]*0.5 if important_count <=2 else 0), sub_check_defs["!important Usage"]["points"], important_msg, "PASS" if sub_check_defs["!important Usage"]["pass"] else ("WARN" if important_count <=2 else "FAIL"))
        except: self._add_finding(category_key, "!important Usage", 0, sub_check_defs["!important Usage"]["points"], "Error checking !important.", "FAIL")

        self.technical_checks_completed_flags[category_key] = True

    def check_responsiveness_viewport_scroll(self):
        category_key = "Responsiveness (Viewport & Scroll)"
        print(f"\nRunning Checks: {category_key} (Prompt: {self.prompt_id}, Viewport: {self.current_viewport_name})")

        # Base points for these sub-checks (per viewport where applicable)
        vp_meta_base_points = 4
        h_scroll_base_points = 6

        # Viewport Meta (only score it once per prompt, max points for this sub-check is vp_meta_base_points)
        if not self.technical_checks_completed_flags.get("viewport_meta_checked"):
            try:
                vp_tag = self.driver.find_element(By.CSS_SELECTOR, "meta[name='viewport']")
                content = vp_tag.get_attribute("content").lower()
                if "width=device-width" in content and ("initial-scale=1" in content or "initial-scale=1.0" in content) and \
                   "user-scalable=no" not in content and "maximum-scale=1" not in content :
                    self._add_finding(category_key, "Viewport Meta Tag", vp_meta_base_points, vp_meta_base_points, "Configured correctly for responsiveness.", "PASS")
                else: self._add_finding(category_key, "Viewport Meta Tag", 0, vp_meta_base_points, f"Suboptimal or restrictive viewport: '{content}'.", "FAIL")
            except NoSuchElementException: self._add_finding(category_key, "Viewport Meta Tag", 0, vp_meta_base_points, "Viewport meta tag missing.", "FAIL")
            except WebDriverException: self._add_finding(category_key, "Viewport Meta Tag", 0, vp_meta_base_points, "Error checking viewport meta.", "FAIL")
            self.technical_checks_completed_flags["viewport_meta_checked"] = True
        else:
             self._add_finding(category_key, "Viewport Meta Tag", 0, 0, "Already scored for this prompt.", "INFO") # Max points for this specific check is 0 if already scored

        # Horizontal Scroll (specific to this viewport, max points for this sub-check is h_scroll_base_points)
        try:
            WebDriverWait(self.driver, 3).until(lambda d: d.execute_script('return document.body && document.body.scrollHeight > 0'))
            has_h_scroll = self.driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth || document.body.scrollWidth > document.body.clientWidth;")
            if not has_h_scroll:
                self._add_finding(category_key, "Horizontal Scrollbar", h_scroll_base_points, h_scroll_base_points, "No horizontal scrollbar detected.", "PASS")
            else: self._add_finding(category_key, "Horizontal Scrollbar", 0, h_scroll_base_points, "Horizontal scrollbar detected.", "FAIL")
        except TimeoutException: self._add_finding(category_key, "Horizontal Scrollbar", 0, h_scroll_base_points, "Page content not fully loaded for scroll check.", "WARN")
        except WebDriverException: self._add_finding(category_key, "Horizontal Scrollbar", 0, h_scroll_base_points, "Error checking scroll.", "FAIL")

    def check_rendered_color_contrast(self):
        category_key = "Rendered Color & Contrast"
        max_points_per_run = TECHNICAL_QUALITY_MAX_POINTS_CONFIG[category_key] # Base for one viewport
        print(f"\nRunning Checks: {category_key} (Prompt: {self.prompt_id}, Viewport: {self.current_viewport_name})")
        text_elements_script = """
            const elementsData = [];
            const treeWalker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, {
                acceptNode: function (node) {
                    if (node.nodeName === 'SCRIPT' || node.nodeName === 'STYLE' || node.nodeName === 'NOSCRIPT' ||
                        node.nodeName === 'IFRAME' || node.nodeName === 'HEAD' || node.nodeName === 'META' ||
                        node.nodeName === 'LINK' || node.nodeName === 'TITLE' || node.closest('svg')) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    let hasDirectText = false;
                    for (let child = node.firstChild; child; child = child.nextSibling) {
                        if (child.nodeType === Node.TEXT_NODE && child.nodeValue.trim().length > 0) {
                            hasDirectText = true; break;
                        }
                    }
                    let hasPlaceholderText = ( (node.nodeName === 'INPUT' || node.nodeName === 'TEXTAREA') &&
                                               node.placeholder && node.placeholder.trim().length > 0 );
                    if (!hasDirectText && !hasPlaceholderText) return NodeFilter.FILTER_SKIP;

                    const style = window.getComputedStyle(node);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' ||
                        parseFloat(style.opacity) === 0 || parseFloat(style.fontSize) < 8 ||
                        node.offsetWidth === 0 || node.offsetHeight === 0) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    return NodeFilter.FILTER_ACCEPT;
                }
            });
            let count = 0; const MAX_ELEMENTS_TO_CHECK = 150;
            while (treeWalker.nextNode() && count < MAX_ELEMENTS_TO_CHECK) { elementsData.push(treeWalker.currentNode); count++;}
            return elementsData;
        """
        candidate_elements_from_script = []
        try:
            candidate_elements_from_script = self.driver.execute_script(text_elements_script)
        except WebDriverException as e_fetch:
            self._add_finding(category_key, "Contrast Element Fetch", 0, max_points_per_run, f"Error fetching text elements: {e_fetch}", "FAIL"); return

        if not candidate_elements_from_script :
             self._add_finding(category_key, "Contrast Check", max_points_per_run, max_points_per_run, "No visible text elements by script to check.", "INFO")
             return

        elements_to_check = [el for el in candidate_elements_from_script if hasattr(el, 'tag_name') and el.is_displayed()]
        if not elements_to_check:
            self._add_finding(category_key, "Contrast Check", max_points_per_run, max_points_per_run, "No valid text elements after filtering.", "INFO"); return

        print(f"  Checking contrast for ~{len(elements_to_check)} text candidate(s)...")
        failure_count_aa = 0; checked_count = 0; warning_count_aaa = 0
        script_get_styles = "const el = arguments[0]; if (!el || !el.checkVisibility || !el.checkVisibility()) return null; const style = window.getComputedStyle(el); if (!style) return null; return {color: style.color, fontSize: style.fontSize, fontWeight: style.fontWeight, opacity: style.opacity};"

        for el_web_element in elements_to_check:
            try:
                if not el_web_element.is_displayed(): continue
                text_content_py = el_web_element.text.strip()
                placeholder_text_py = (el_web_element.get_attribute("placeholder") or "").strip() if el_web_element.tag_name.lower() in ['input', 'textarea'] else ""
                effective_text_content = placeholder_text_py if not text_content_py and placeholder_text_py else text_content_py
                if not effective_text_content: continue

                style_dict = self.driver.execute_script(script_get_styles, el_web_element)
                if not style_dict: continue

                fg_color_str, font_size_str, font_weight_str, opacity_str = style_dict.get('color'), style_dict.get('fontSize'), style_dict.get('fontWeight'), style_dict.get('opacity')
                if not fg_color_str or not font_size_str: continue

                fg_rgba_tuple = parse_color_string_to_rgb_tuple(fg_color_str)
                if not fg_rgba_tuple: continue

                effective_text_alpha = fg_rgba_tuple[3] * (float(opacity_str) if opacity_str else 1.0)
                if effective_text_alpha < 0.1: continue

                fg_rgb_for_contrast = (fg_rgba_tuple[0], fg_rgba_tuple[1], fg_rgba_tuple[2])
                bg_rgb = get_effective_background_rgb(el_web_element, self.driver)
                if not bg_rgb: continue

                checked_count += 1
                font_size_px = float(re.sub(r'[^\d.]', '', font_size_str))
                is_bold = (str(font_weight_str).lower() in ['bold', 'bolder'] or (str(font_weight_str).isdigit() and int(font_weight_str) >= 700))
                is_large_wcag = (font_size_px >= 24) or (font_size_px >= 18.66 and is_bold)

                norm_fg, norm_bg = normalize_rgb_for_contrast(fg_rgb_for_contrast), normalize_rgb_for_contrast(bg_rgb)
                if norm_fg and norm_bg:
                    ratio = contrast_lib.rgb(norm_fg, norm_bg)
                    passes_aa, passes_aaa = contrast_lib.passes_AA(ratio, large=is_large_wcag), contrast_lib.passes_AAA(ratio, large=is_large_wcag)
                    snippet = effective_text_content[:40].replace('\n', ' ') + ('...' if len(effective_text_content) > 40 else '')
                    el_desc = get_element_desc(el_web_element)
                    if not passes_aa:
                        failure_count_aa += 1
                        # This finding has 0 points and 0 max_points because it's a detail of the main "Contrast Check Result"
                        self._add_finding(category_key, "Contrast Failure (AA)", 0, 0, f"AA FAIL {ratio:.2f} for '{snippet}' in {el_desc}", "FAIL", data={"ratio": ratio, "text": snippet, "fg": fg_color_str, "bg_eff": f"rgb{bg_rgb}"})
                    elif not passes_aaa:
                        warning_count_aaa +=1
                        self._add_finding(category_key, "Contrast Suboptimal (AAA)", 0, 0, f"AAA WARN {ratio:.2f} for '{snippet}' in {el_desc}", "INFO", data={"ratio": ratio})
            except (StaleElementReferenceException, WebDriverException): continue
            except Exception as e_inner: print(f"    Error in contrast loop: {e_inner}")

        if checked_count == 0 and elements_to_check: self._add_finding(category_key, "Contrast Check Result", 0, max_points_per_run, f"Checked 0 of {len(elements_to_check)} candidates.", "FAIL")
        elif failure_count_aa > 0:
            penalty_per_failure = 2.5
            earned_points = max(0, max_points_per_run - (failure_count_aa * penalty_per_failure))
            self._add_finding(category_key, "Contrast Check Result", earned_points, max_points_per_run, f"{failure_count_aa} WCAG AA failures on {checked_count} instances.", "FAIL")
        elif checked_count > 0: self._add_finding(category_key, "Contrast Check Result", max_points_per_run, max_points_per_run, f"All {checked_count} instances meet WCAG AA. ({warning_count_aaa} only AA, not AAA).", "PASS")

    def check_performance_lighthouse(self):
        category_prefix = "Lighthouse"
        print(f"\nRunning Checks: Lighthouse Suite (Prompt: {self.prompt_id}, Viewport: {self.current_viewport_name})")

        if not self.lighthouse_path:
            for cat_key_conf in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key_conf, f"{category_prefix} Execution", 0, TECHNICAL_QUALITY_MAX_POINTS_CONFIG[cat_key_conf], "Lighthouse CLI not found.", "WARN")
            return

        url_to_check_lh = self._start_local_server()
        if not url_to_check_lh:
             for cat_key_conf in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key_conf, f"{category_prefix} Server", 0, TECHNICAL_QUALITY_MAX_POINTS_CONFIG[cat_key_conf], "Failed to start local server.", "FAIL")
             return

        lh_reports_dir = self.prompt_output_dir / f"lighthouse_reports_{self.current_viewport_name}"
        lh_reports_dir.mkdir(parents=True, exist_ok=True)
        lh_base_report_name = lh_reports_dir / f"lh_{self.prompt_id}"

        cmd = [self.lighthouse_path, url_to_check_lh, "--output=json", "--output=html",
               f"--output-path={lh_base_report_name}", "--quiet", "--throttling-method=simulate",
               f"--chrome-flags=--headless=new --disable-gpu --no-sandbox --no-zygote",
               "--only-categories=" + ",".join(LIGHTHOUSE_CATEGORIES)]

        lh_viewport_name_lower = self.current_viewport_name.lower()
        if lh_viewport_name_lower == 'desktop':
            cmd.append("--preset=desktop")
        elif lh_viewport_name_lower != 'mobile':
             print(f"  Lighthouse: Viewport '{self.current_viewport_name}' is custom. Using LH default settings based on browser emulation.")


        print(f"  Running Lighthouse on {url_to_check_lh} ({self.current_viewport_name})...")
        try:
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
            report_path_json = lh_base_report_name.with_suffix(".report.json")

            if process.returncode != 0 or not report_path_json.exists():
                err_msg = f"CLI failed. Code: {process.returncode}. Stderr: {process.stderr[:500]}"
                for cat_key_conf in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                    self._add_finding(cat_key_conf, f"{category_prefix} Execution", 0, TECHNICAL_QUALITY_MAX_POINTS_CONFIG[cat_key_conf], err_msg, "FAIL", data={"cmd": " ".join(cmd)})
                return

            with open(report_path_json, 'r', encoding='utf-8') as f: lh_results = json.load(f)
            if lh_results.get("runtimeError"):
                err_msg = f"Runtime error: {lh_results['runtimeError'].get('code')} - {lh_results['runtimeError'].get('message')}"
                for cat_key_conf in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                     self._add_finding(cat_key_conf, f"{category_prefix} Runtime Error", 0, TECHNICAL_QUALITY_MAX_POINTS_CONFIG[cat_key_conf], err_msg, "FAIL")
                return

            lh_category_mapping = {
                'performance': "Performance (Lighthouse)", 'accessibility': "Accessibility (Lighthouse)",
                'best-practices': "Best Practices (Lighthouse)", 'seo': "SEO (Lighthouse)"
            }
            for lh_id, TQ_key in lh_category_mapping.items():
                max_cat_pts_per_run = TECHNICAL_QUALITY_MAX_POINTS_CONFIG[TQ_key] # Base for one viewport
                cat_data = lh_results.get('categories', {}).get(lh_id)
                if cat_data and cat_data.get('score') is not None:
                    score_0_1 = cat_data['score']
                    earned_pts = round(score_0_1 * max_cat_pts_per_run, 2)
                    status = "PASS" if score_0_1 >= 0.9 else "WARN" if score_0_1 >= 0.5 else "FAIL"
                    self._add_finding(TQ_key, f"{category_prefix} Score", earned_pts, max_cat_pts_per_run, f"{int(score_0_1*100)}/100", status)
                else: self._add_finding(TQ_key, f"{category_prefix} Score", 0, max_cat_pts_per_run, "Score not found.", "FAIL")
            print(f"  Lighthouse reports generated in: {lh_reports_dir.name}")
        except subprocess.TimeoutExpired:
            for cat_key_conf in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key_conf, f"{category_prefix} Execution", 0, TECHNICAL_QUALITY_MAX_POINTS_CONFIG[cat_key_conf], "Timed out (5 min).", "FAIL")
        except Exception as e_lh:
            for cat_key_conf in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key_conf, f"{category_prefix} Main Error", 0, TECHNICAL_QUALITY_MAX_POINTS_CONFIG[cat_key_conf], f"Error: {str(e_lh)[:200]}", "FAIL")
        finally: self._stop_local_server()


    def check_javascript_health(self):
        category_key = "JavaScript Health"
        if self.technical_checks_completed_flags.get(category_key): return
        print(f"\nRunning Checks: {category_key} (Prompt: {self.prompt_id})")
        max_points = TECHNICAL_QUALITY_MAX_POINTS_CONFIG[category_key]
        try:
            logs = self.driver.get_log('browser')
            js_errors = []
            ignore_patterns = ["favicon.ico", "extension", "custom-element", "deprecated", "doubleclick.net", "googlesyndication.com", "net::ERR_FAILED", "ResizeObserver loop limit exceeded"]
            for entry in logs:
                message = entry.get('message', '')
                if entry['level'] == 'SEVERE' and not any(p.lower() in message.lower() for p in ignore_patterns):
                    if "Unchecked runtime.lastError" in message and "extension" in message: continue
                    js_errors.append(f"JS ERROR: {message.splitlines()[0][:200]}")

            if not js_errors:
                self._add_finding(category_key, "JS Console Errors", max_points, max_points, "No significant JS errors.", "PASS")
            else:
                self._add_finding(category_key, "JS Console Errors", 0, max_points, f"{len(js_errors)} JS error(s).", "FAIL", data=js_errors[:5])
        except WebDriverException:
            self._add_finding(category_key, "JS Console Errors", 0, max_points, "Could not retrieve browser logs.", "WARN")
        self.technical_checks_completed_flags[category_key] = True

    def check_prompt_adherence(self):
        print(f"\n--- Running Checks: Prompt Adherence (Prompt: {self.prompt_id}, Viewport: {self.current_viewport_name}) ---")
        adherence_config_list = self.prompt_config.get("adherence_checks", [])
        if not adherence_config_list: return

        if not hasattr(self, '_adherence_checks_passed_this_prompt'):
            self._adherence_checks_passed_this_prompt = {}

        for check_item_config in adherence_config_list:
            check_type = check_item_config.get("type")
            check_name = check_item_config.get("name", f"Unnamed {check_type} check")
            points_for_this_check = float(check_item_config.get("points", 0))

            check_applies_to_current_viewport = False
            specified_viewports = check_item_config.get("viewports")
            if not specified_viewports or self.current_viewport_name in specified_viewports:
                check_applies_to_current_viewport = True
            if not check_applies_to_current_viewport: continue

            earned_for_this_instance = 0.0
            status_for_this_instance = "FAIL"
            message_for_this_instance = "Check not successfully executed or verified."
            data_for_this_instance = None

            try:
                passed_check, msg, data = (False, "Unknown check type", None)
                if check_type == "element_presence": passed_check, msg, data = self._verify_element_presence(check_item_config)
                elif check_type == "element_order": passed_check, msg, data = self._verify_element_order(check_item_config)
                elif check_type == "element_count": passed_check, msg, data = self._verify_element_count(check_item_config)
                elif check_type == "text_content": passed_check, msg, data = self._verify_text_content(check_item_config)
                elif check_type == "attribute_value": passed_check, msg, data = self._verify_attribute_value(check_item_config)
                elif check_type == "css_property": passed_check, msg, data = self._verify_css_property(check_item_config)
                elif check_type == "interaction": passed_check, msg, data = self._execute_and_verify_interaction(check_item_config)
                elif check_type == "custom_script_evaluates_true":
                    script_to_run = check_item_config.get("script")
                    temp_passed_check, temp_msg, temp_data = True, "", None # Initialize for this block
                    if not script_to_run:
                        temp_passed_check, temp_msg = False, "No script provided for custom_script_evaluates_true check."
                    else:
                        target_element_for_script = None
                        el_selector = check_item_config.get("selector")
                        if el_selector: # Optional target element
                            target_element_for_script = self._find_element_by_config(check_item_config)
                            if not target_element_for_script and check_item_config.get("element_required_for_script", False):
                                 temp_passed_check, temp_msg = False, f"Required element '{el_selector}' for script not found."

                        if temp_passed_check: # Only proceed if not already failed (e.g., missing required element)
                            try:
                                script_result = self.driver.execute_script(script_to_run, target_element_for_script)
                                temp_passed_check = bool(script_result)
                                temp_msg = f"Custom script evaluation result: {script_result} (expected true)."
                                temp_data = {"script_result": script_result}
                            except WebDriverException as e_script:
                                temp_passed_check, temp_msg = False, f"Custom script execution error: {e_script}"
                    passed_check, msg, data = temp_passed_check, temp_msg, temp_data
                else: msg = f"Unknown adherence check type: {check_type}"


                if passed_check: earned_for_this_instance = points_for_this_check; status_for_this_instance = "PASS"
                message_for_this_instance = msg; data_for_this_instance = data

                is_first_pass_for_named_check = (check_name not in self._adherence_checks_passed_this_prompt)

                if earned_for_this_instance > 0 and is_first_pass_for_named_check:
                    self.current_prompt_scores["prompt_adherence"]["earned"] += earned_for_this_instance
                    self._adherence_checks_passed_this_prompt[check_name] = True
                    self._add_finding("Prompt Adherence Details", check_name, points_for_this_check, points_for_this_check, message_for_this_instance, status_for_this_instance, data_for_this_instance, is_adherence_check=True)
                elif earned_for_this_instance > 0 and not is_first_pass_for_named_check:
                     self._add_finding("Prompt Adherence Details", check_name, 0, points_for_this_check, f"(Points already awarded) {message_for_this_instance}", "PASS", data_for_this_instance, is_adherence_check=True)
                else:
                     self._add_finding("Prompt Adherence Details", check_name, 0, points_for_this_check, message_for_this_instance, status_for_this_instance, data_for_this_instance, is_adherence_check=True)
            except Exception as e_adh_check:
                 self._add_finding("Prompt Adherence Details", check_name, 0, points_for_this_check, f"CRITICAL ERROR during check execution: {e_adh_check}", "FAIL", data={"traceback": str(e_adh_check)}, is_adherence_check=True)

    def _verify_element_presence(self, config):
        element = self._find_element_by_config(config)
        is_present = element is not None
        is_visible = is_present and element.is_displayed()
        should_not_exist = config.get("should_not_exist", False)
        passed, msg = (False, "")

        if should_not_exist:
            if not is_visible: passed = True; msg = f"Element '{config.get('selector')}' correctly not visible/present."
            else: msg = f"Element '{config.get('selector')}' unexpectedly visible."
        else:
            if is_visible: passed = True; msg = f"Element '{config.get('selector')}' present and visible."
            elif is_present: msg = f"Element '{config.get('selector')}' present but NOT visible."
            else: msg = f"Element '{config.get('selector')}' NOT found."
        return passed, msg, {"selector": config.get("selector"), "is_present": is_present, "is_visible": is_visible if is_present else False}

    def _verify_element_order(self, config):
        selectors_in_order = config.get("selectors_in_order", [])
        if len(selectors_in_order) < 2: return False, "Order check needs >= 2 selectors.", None
        elements_found = []
        for sel_config_item in selectors_in_order:
            sel_conf = sel_config_item if isinstance(sel_config_item, dict) else {"selector": sel_config_item}
            el = self._find_element_by_config(sel_conf)
            if not el or not el.is_displayed(): return False, f"Order element '{sel_conf.get('selector')}' not found/visible.", None
            elements_found.append(el)
        try:
            source_indices = self.driver.execute_script("return Array.from(arguments).map(el => Array.from(document.querySelectorAll('*')).indexOf(el));", *elements_found)
            for i in range(1, len(source_indices)):
                if source_indices[i] < source_indices[i-1] and source_indices[i] != -1 and source_indices[i-1] != -1 :
                    try:
                        elements_found[i].find_element(By.XPATH, f".//*[self::node()[name()='{elements_found[i-1].tag_name}' and @id='{elements_found[i-1].get_attribute('id')}']]")
                        if elements_found[i-1].id == elements_found[i].id: continue
                    except NoSuchElementException:
                        pass
                    except StaleElementReferenceException: return False, "Stale element during order parent check.", None

                    return False, "Elements not in expected DOM source order.", {"indices": source_indices, "selectors": [get_element_desc(e) for e in elements_found]}
            return True, "Elements in expected DOM source order.", {"indices": source_indices}
        except WebDriverException as e: return False, f"Error getting element order: {e}", None

    def _verify_element_count(self, config):
        elements = self._find_element_by_config({**config, "find_multiple": True})
        actual_count = len(elements) if elements else 0
        passed, msg_part, _ = self._compare_counts(actual_count, config.get("expected_count"), config.get("comparison", "equals"), config.get("min_count"), config.get("max_count"))
        msg = f"Count is {actual_count} ({msg_part})."
        return passed, msg, {"actual": actual_count, "expected_str": msg_part}

    def _verify_text_content(self, config):
        element = self._find_element_by_config(config)
        if not element or not element.is_displayed(): return False, f"Element '{config.get('selector')}' not found/visible for text.", None
        actual_text = element.text.strip()
        expected_text_raw = config.get("expected_text", "")
        expected_text_stripped = str(expected_text_raw).strip()
        match_type = config.get("match_type", "exact").lower()
        min_similarity = config.get("min_similarity", 0.85)
        case_sensitive = config.get("case_sensitive", False)
        passed = False
        op_actual, op_expected = (actual_text, expected_text_stripped) if case_sensitive else (actual_text.lower(), expected_text_stripped.lower())

        if match_type == "exact": passed = op_actual == op_expected
        elif match_type == "contains": passed = op_expected in op_actual
        elif match_type == "similar": passed = string_similarity(op_actual, op_expected) >= min_similarity
        elif match_type == "regex":
            try: passed = bool(re.search(str(expected_text_raw), actual_text, 0 if case_sensitive else re.IGNORECASE))
            except re.error: return False, "Invalid regex pattern.", {"actual": actual_text, "expected_regex": expected_text_raw}
        else: return False, f"Unknown match_type '{match_type}'.", None
        msg = f"Text (type: {match_type}{'' if case_sensitive else ',i'}). Expected '{expected_text_stripped[:50]}...', Actual '{actual_text[:50]}...'"
        return passed, msg, {"actual": actual_text, "expected": expected_text_stripped, "match_type":match_type}

    def _verify_attribute_value(self, config):
        element = self._find_element_by_config(config)
        if not element: return False, f"Element '{config.get('selector')}' not found for attribute.", None
        attr_name = config.get("attribute_name")
        expected_value = config.get("expected_value")
        actual_value = element.get_attribute(attr_name)
        passed = False

        if isinstance(expected_value, bool):
            if actual_value is not None and actual_value != "false": passed = expected_value is True
            else: passed = expected_value is False
        elif expected_value is None: passed = actual_value is None
        elif attr_name.lower() == "class" and config.get("class_contains_all"):
            actual_classes = set((actual_value or "").split())
            expected_classes = set(expected_value if isinstance(expected_value, list) else [expected_value])
            passed = expected_classes.issubset(actual_classes)
        elif attr_name.lower() == "class" and config.get("class_contains_any"):
            actual_classes = set((actual_value or "").split())
            expected_classes = set(expected_value if isinstance(expected_value, list) else [expected_value])
            passed = not expected_classes.isdisjoint(actual_classes)
        else: passed = str(actual_value) == str(expected_value)
        msg = f"Attr '{attr_name}'. Expected: '{expected_value}', Actual: '{actual_value}'."
        return passed, msg, {"actual": actual_value, "expected": expected_value, "attr_name": attr_name}

    def _verify_css_property(self, config):
        element = self._find_element_by_config(config)
        if not element or not element.is_displayed(): return False, f"Element '{config.get('selector')}' not found/visible for CSS.", None
        prop_name = config.get("property_name")
        expected_val_str = str(config.get("expected_value","")).strip()
        try: actual_val_str = element.value_of_css_property(prop_name).strip()
        except: return False, f"Could not get CSS property '{prop_name}'.", None
        passed = False

        if "color" in prop_name.lower():
            actual_rgb = parse_color_string_to_rgb_tuple(actual_val_str)
            expected_rgb = parse_color_string_to_rgb_tuple(expected_val_str)
            if actual_rgb and expected_rgb: passed = actual_rgb[:3] == expected_rgb[:3]
            elif actual_val_str == expected_val_str : passed = True
        elif prop_name == "font-weight":
            norm_actual = actual_val_str.replace("normal", "400").replace("bold", "700")
            norm_expected = expected_val_str.replace("normal", "400").replace("bold", "700")
            passed = norm_actual == norm_expected
        else: passed = actual_val_str == expected_val_str
        msg = f"CSS '{prop_name}'. Expected: '{expected_val_str}', Actual: '{actual_val_str}'."
        return passed, msg, {"actual": actual_val_str, "expected": expected_val_str, "prop_name": prop_name}

    def _perform_setup_cleanup_action(self, action_config):
        action_type = action_config.get("action_type")
        print(f"    Performing setup/cleanup: {action_type} - {action_config.get('name', 'N/A')}")
        if action_type == "navigate_to_url_fragment": self.driver.get(f"{self.driver.current_url.split('#')[0]}#{action_config.get('fragment')}")
        elif action_type == "set_cookie": self.driver.add_cookie({"name": action_config.get("name"), "value": str(action_config.get("value"))})
        elif action_type == "delete_cookie": self.driver.delete_cookie(action_config.get("name"))
        elif action_type == "execute_script": self.driver.execute_script(action_config.get("script"))
        elif action_type == "clear_local_storage_key": self.driver.execute_script(f"localStorage.removeItem('{action_config.get('key')}');")
        elif action_type == "set_local_storage_key": self.driver.execute_script(f"localStorage.setItem('{action_config.get('key')}', '{action_config.get('value')}');")
        else: raise ValueError(f"Unknown setup/cleanup action type: {action_type}")
        time.sleep(0.2)

    def _execute_action_on_element(self, action_config, element_to_act_on):
        action_type = action_config.get("type", "click").lower()
        chains = ActionChains(self.driver)
        if action_type == "click":
            try: element_to_act_on.click()
            except ElementClickInterceptedException: self.driver.execute_script("arguments[0].click();", element_to_act_on)
        elif action_type == "hover": chains.move_to_element(element_to_act_on).perform()
        elif action_type == "focus": self.driver.execute_script("arguments[0].focus();", element_to_act_on)
        elif action_type == "blur": self.driver.execute_script("arguments[0].blur();", element_to_act_on)
        elif action_type == "type_text":
            if action_config.get("clear_before_type", False): element_to_act_on.clear()
            text, delay = str(action_config.get("text_to_type", "")), action_config.get("key_delay_ms", 0)
            if delay > 0:
                for char in text: element_to_act_on.send_keys(char); time.sleep(delay/1000.0)
            else: element_to_act_on.send_keys(text)
        elif action_type == "select_option":
            sel_el = Select(element_to_act_on)
            if "option_value" in action_config: sel_el.select_by_value(str(action_config["option_value"]))
            elif "option_text" in action_config: sel_el.select_by_visible_text(action_config["option_text"])
            elif "option_index" in action_config: sel_el.select_by_index(int(action_config["option_index"]))
            else: raise ValueError("select_option needs option_value/text/index.")
        elif action_type == "drag_and_drop":
            target_el = self._find_element_by_config({"selector": action_config.get("target_element_selector"), "selector_type": action_config.get("target_element_selector_type")})
            if not target_el: raise ValueError("Drag target not found.")
            chains.drag_and_drop(element_to_act_on, target_el).perform()
        elif action_type == "scroll_to_element": self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element_to_act_on)
        elif action_type == "scroll_window":
            x, y, direction = action_config.get("x_pixels", 0), action_config.get("y_pixels", 0), action_config.get("direction", "").lower()
            if direction == "bottom": self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            elif direction == "top": self.driver.execute_script("window.scrollTo(0, 0);")
            else: self.driver.execute_script(f"window.scrollBy({x}, {y});")
        elif action_type == "submit_form":
            if element_to_act_on.tag_name.lower() == 'form': element_to_act_on.submit()
            else: element_to_act_on.click()
        elif action_type == "key_press":
            keys_str, target_sel_config = action_config.get("key", ""), action_config.get("target_element_selector_config") # Renamed for clarity
            target_for_keys = self._find_element_by_config(target_sel_config) if target_sel_config else element_to_act_on

            parts = keys_str.upper().split('+')
            keys_to_send = [getattr(Keys, p.strip(), p.strip().lower()) for p in parts]
            if len(keys_to_send) > 1 and any(k in [Keys.CONTROL, Keys.ALT, Keys.SHIFT, Keys.COMMAND] for k in keys_to_send[:-1]):
                for k in keys_to_send[:-1]: chains.key_down(k)
                chains.send_keys_to_element(target_for_keys, keys_to_send[-1])
                for k in reversed(keys_to_send[:-1]): chains.key_up(k)
                chains.perform()
            elif keys_to_send: ActionChains(self.driver).send_keys_to_element(target_for_keys, *keys_to_send).perform()
        elif action_type == "execute_script_on_element": self.driver.execute_script(action_config.get("script"), element_to_act_on)
        elif action_type == "wait": time.sleep(action_config.get("duration_ms", 100) / 1000.0)
        else: raise ValueError(f"Unsupported action type: {action_type}")

    def _verify_single_outcome(self, outcome_config, step_trigger_element):
        outcome_type = outcome_config.get("outcome_type")
        outcome_el_config = {"selector": outcome_config.get("element_selector"), "selector_type": outcome_config.get("element_selector_type"), "find_multiple": outcome_config.get("find_multiple", False)}
        target_el_outcome = self._find_element_by_config(outcome_el_config) if outcome_el_config.get("selector") else None

        if outcome_type == "attribute_change": return self._verify_attribute_value({**outcome_el_config, **outcome_config})
        elif outcome_type == "class_change":
            if not target_el_outcome: return False, f"Target for class_change ('{outcome_el_config.get('selector')}') not found.", None
            current_classes = set((target_el_outcome.get_attribute("class") or "").split())
            passed, msg_parts = True, []
            if "expected_class_present" in outcome_config:
                cls_p = outcome_config["expected_class_present"]
                if cls_p not in current_classes: passed = False
                msg_parts.append(f"Present '{cls_p}': {cls_p in current_classes}")
            if "expected_class_absent" in outcome_config:
                cls_a = outcome_config["expected_class_absent"]
                if cls_a in current_classes: passed = False
                msg_parts.append(f"Absent '{cls_a}': {cls_a not in current_classes}")
            if not msg_parts: return False, "No class condition for class_change.", None
            return passed, f"Classes: {', '.join(msg_parts)}. Current: '{' '.join(sorted(list(current_classes)))}'", {"current_classes": list(current_classes)}
        elif outcome_type == "visibility_change":
            expected_vis = outcome_config.get("expected_visibility", "visible").lower()
            actual_displayed = target_el_outcome is not None and target_el_outcome.is_displayed()
            passed = (expected_vis == "visible" and actual_displayed) or (expected_vis == "hidden" and not actual_displayed)
            return passed, f"Visibility: Expected '{expected_vis}', Actual is_displayed: {actual_displayed}", {"is_displayed": actual_displayed}
        elif outcome_type == "text_content_change": return self._verify_text_content({**outcome_el_config, **outcome_config})
        elif outcome_type == "css_property_change": return self._verify_css_property({**outcome_el_config, **outcome_config})
        elif outcome_type == "element_exists":
            passed = target_el_outcome is not None
            msg = f"Element '{outcome_el_config.get('selector')}' exists in DOM: {passed}."
            if passed and "expected_visibility" in outcome_config :
                vis_passed, vis_msg, _ = self._verify_single_outcome({"outcome_type": "visibility_change", **outcome_el_config, **outcome_config}, step_trigger_element)
                passed = passed and vis_passed; msg += f" {vis_msg}"
            return passed, msg, {"is_in_dom": passed}
        elif outcome_type == "element_does_not_exist":
            passed = target_el_outcome is None
            return passed, f"Element '{outcome_el_config.get('selector')}' does not exist in DOM: {passed}", {"is_in_dom": not passed}
        elif outcome_type == "custom_script_evaluates_true":
            script = outcome_config.get("script")
            if not script: return False, "No script for custom_script_evaluates_true.", None
            try:
                script_result = self.driver.execute_script(script, step_trigger_element, target_el_outcome)
                return bool(script_result), f"Custom script result: {script_result} (expected true)", {"script_result": script_result}
            except WebDriverException as e: return False, f"Custom script error: {e}", None
        elif outcome_type == "new_element_count":
            parent_el_for_count = target_el_outcome
            if not parent_el_for_count: return False, f"Parent ('{outcome_el_config.get('selector')}') not found for count.", None
            child_sel_conf = {"selector": outcome_config.get("child_element_selector"), "selector_type": outcome_config.get("child_element_selector_type", "css"), "find_multiple": True}
            actual_children = self._find_element_by_config(child_sel_conf, base_element=parent_el_for_count)
            actual_count = len(actual_children) if actual_children else 0
            passed, msg_part, _ = self._compare_counts(actual_count, outcome_config.get("expected_count"), outcome_config.get("comparison", "equals"), outcome_config.get("min_count"), outcome_config.get("max_count"))
            return passed, f"Child count: {msg_part}", {"actual_count": actual_count}
        elif outcome_type == "url_change":
            current_url, expected_part, match_type = self.driver.current_url, str(outcome_config.get("expected_url_part", "")), outcome_config.get("match_type", "contains").lower()
            passed = False
            if match_type == "contains": passed = expected_part in current_url
            elif match_type == "exact": passed = expected_part == current_url
            elif match_type == "starts_with": passed = current_url.startswith(expected_part)
            elif match_type == "ends_with": passed = current_url.endswith(expected_part)
            elif match_type == "regex_match_fragment": passed = bool(re.search(expected_part, urlparse(current_url).fragment))
            elif match_type == "regex_match_path": passed = bool(re.search(expected_part, urlparse(current_url).path))
            elif match_type == "regex_match_full": passed = bool(re.search(expected_part, current_url))
            else: return False, f"Unknown URL match_type '{match_type}'.", None
            return passed, f"URL: Actual='{current_url}', Expected {match_type} '{expected_part}'", {"current_url": current_url}
        elif outcome_type == "animation_or_transition_ends":
            if not target_el_outcome: return False, f"Target for animation ('{outcome_el_config.get('selector')}') not found.", None
            max_wait_s = outcome_config.get("max_wait_ms", 2000) / 1000.0
            event_flag_attr = f"data-event-ended-{int(time.time()*1000)}"
            js_listener = f"const el=arguments[0]; if(!el)return false; el.setAttribute('{event_flag_attr}','false'); let ef=false; const h=ev=>{{if(ev.target===el){{el.setAttribute('{event_flag_attr}','true');el.removeEventListener('transitionend',h);el.removeEventListener('animationend',h);ef=true;}}}}; el.addEventListener('transitionend',h); el.addEventListener('animationend',h); setTimeout(()=>{{if(!ef){{/*el.setAttribute('{event_flag_attr}','timeout');*/}}}},{max_wait_s*1000+200}); return true;"
            try:
                self.driver.execute_script(js_listener, target_el_outcome)
                WebDriverWait(self.driver, max_wait_s, 0.05).until(lambda d: target_el_outcome.get_attribute(event_flag_attr) == "true")
                self.driver.execute_script(f"arguments[0].removeAttribute('{event_flag_attr}');", target_el_outcome)
                return True, "Animation/Transition event detected.", {"event_attr": event_flag_attr}
            except TimeoutException:
                if target_el_outcome and target_el_outcome.is_displayed(): # Avoid error if element disappears
                    self.driver.execute_script(f"arguments[0].removeAttribute('{event_flag_attr}');", target_el_outcome)
                return False, "Animation/Transition did not end within max_wait.", None
            except WebDriverException as e: return False, f"Error during animation check: {e}", None
        return False, f"Unknown outcome type '{outcome_type}'.", None

    def _compare_counts(self, actual_count, expected_count, comparison_type, min_val=None, max_val=None):
        passed, expected_msg_part = False, ""
        comparison_type = comparison_type.lower()
        if expected_count is not None:
            if comparison_type == "equals": passed = actual_count == expected_count
            elif comparison_type == "not_equals": passed = actual_count != expected_count
            elif comparison_type == "greater_than": passed = actual_count > expected_count
            elif comparison_type == "less_than": passed = actual_count < expected_count
            elif comparison_type == "greater_than_or_equals": passed = actual_count >= expected_count
            elif comparison_type == "less_than_or_equals": passed = actual_count <= expected_count
            else: return False, "Invalid comparison type for expected_count", None
            expected_msg_part = f"{comparison_type.replace('_',' ')} {expected_count}"
        elif min_val is not None and max_val is not None: passed = min_val <= actual_count <= max_val; expected_msg_part = f"between {min_val}-{max_val}"
        elif min_val is not None: passed = actual_count >= min_val; expected_msg_part = f">= {min_val}"
        elif max_val is not None: passed = actual_count <= max_val; expected_msg_part = f"<= {max_val}"
        else: return False, "Invalid count parameters.", None
        msg = f"Actual: {actual_count}, Expected: {expected_msg_part}"
        return passed, msg, {"actual": actual_count, "expected_str": expected_msg_part}

    def _execute_and_verify_interaction(self, interaction_config):
        overall_passed, log_msgs = True, []
        for setup_action_config in interaction_config.get("initial_setup", []):
            try: self._perform_setup_cleanup_action(setup_action_config)
            except Exception as e: return False, f"Interaction setup failed: {e}", {"setup_error": str(e)}

        for i, step_config in enumerate(interaction_config.get("sequence", [])):
            step_name = step_config.get("step_name", f"Step {i+1}"); log_msgs.append(f"--- Executing Step: {step_name} ---")
            trigger_el_config = step_config.get("trigger_element")
            if not trigger_el_config or not trigger_el_config.get("selector"): log_msgs.append(f"  [FAIL] Trigger config missing."); overall_passed = False; break
            trigger_el = self._find_element_by_config(trigger_el_config)
            if not trigger_el: log_msgs.append(f"  [FAIL] Trigger '{trigger_el_config.get('selector')}' not found."); overall_passed = False; break
            if not trigger_el.is_displayed() or not trigger_el.is_enabled(): log_msgs.append(f"  [FAIL] Trigger not displayed/enabled."); overall_passed = False; break
            action_config = step_config.get("action")
            if not action_config or not action_config.get("type"): log_msgs.append(f"  [FAIL] Action config missing."); overall_passed = False; break

            try:
                self._execute_action_on_element(action_config, trigger_el)
                log_msgs.append(f"  Action '{action_config.get('type')}' on {get_element_desc(trigger_el)}.")
            except Exception as e_act: log_msgs.append(f"  [FAIL] Action '{action_config.get('type')}' failed: {e_act}"); overall_passed = False; break

            wait_s = step_config.get("wait_for_outcome_ms", 500) / 1000.0
            all_step_outcomes_passed = True
            for o_idx, o_config in enumerate(step_config.get("expected_outcomes", [])):
                o_name = o_config.get("name", f"Outcome {o_idx+1}"); o_passed, o_msg, o_data = (False, "", None)
                try:
                    WebDriverWait(self.driver, wait_s, 0.1).until(lambda d: self._verify_single_outcome(o_config, trigger_el)[0])
                    o_passed, o_msg, o_data = self._verify_single_outcome(o_config, trigger_el)
                except TimeoutException: _, o_msg, o_data = self._verify_single_outcome(o_config, trigger_el); o_msg = f"Timeout. Last state: {o_msg}"
                except Exception as e_o_v: o_msg = f"Error verifying: {e_o_v}"

                log_msgs.append(f"    [{'PASS' if o_passed else 'FAIL'}] {o_name}: {o_msg}")
                if not o_passed: all_step_outcomes_passed = False
            if not all_step_outcomes_passed: overall_passed = False; break
            log_msgs.append(f"--- Step {step_name} Passed ---")

        for cleanup_action_config in interaction_config.get("final_cleanup", []):
            try: self._perform_setup_cleanup_action(cleanup_action_config)
            except Exception as e: log_msgs.append(f"  WARN: Cleanup failed: {e}")
        final_msg = "All interaction steps/outcomes verified." if overall_passed else "One or more interaction steps/outcomes failed."
        return overall_passed, final_msg, {"interaction_log": log_msgs}

    def run_single_prompt_analysis(self):
        print(f"--- Starting Analysis for Prompt: {self.prompt_id} (File: {self.file_path.name}) ---")
        print(f"--- Description: {self.prompt_config.get('prompt_description', 'N/A')} ---")
        self._adherence_checks_passed_this_prompt = {}
        page_load_ok_once = False

        for vp_name, (vp_width, vp_height) in self.viewports_to_test.items():
            try:
                self._load_page_at_viewport(vp_name, vp_width, vp_height)
                page_load_ok_once = True

                run_page_level_checks = not self.technical_checks_completed_flags.get("any_page_checks_done", False)
                if vp_name.lower() == "desktop" and not self.technical_checks_completed_flags.get("desktop_page_checks_done"):
                    run_page_level_checks = True
                    self.technical_checks_completed_flags["desktop_page_checks_done"] = True


                if run_page_level_checks:
                    self.check_html_structure_semantics()
                    self.check_css_quality()
                    self.check_javascript_health()
                    self.technical_checks_completed_flags["any_page_checks_done"] = True

                self.check_accessibility_axe()
                self.check_rendered_color_contrast()
                self.check_responsiveness_viewport_scroll()

                lh_preset_key = vp_name.lower()
                # Only run Lighthouse once per standard viewport type (desktop/mobile) to avoid redundant checks
                # if multiple custom viewports of the same "type" are defined.
                if lh_preset_key in ["desktop", "mobile"] and self.lighthouse_path and \
                   not self.technical_checks_completed_flags.get(f"lighthouse_{lh_preset_key}_done"):
                    self.check_performance_lighthouse()
                    self.technical_checks_completed_flags[f"lighthouse_{lh_preset_key}_done"] = True
                elif self.lighthouse_path and lh_preset_key not in ["desktop", "mobile"] and \
                     not self.technical_checks_completed_flags.get(f"lighthouse_custom_{lh_preset_key}_done"): # For custom viewports
                    print(f"  INFO: Running Lighthouse for custom viewport '{lh_preset_key}'.")
                    self.check_performance_lighthouse()
                    self.technical_checks_completed_flags[f"lighthouse_custom_{lh_preset_key}_done"] = True


                self.check_prompt_adherence()

            except (TimeoutException, WebDriverException) as e_vp_critical:
                print(f"  CRITICAL ERROR for prompt {self.prompt_id} at viewport {vp_name}: {type(e_vp_critical).__name__}. Skipping further checks for this viewport.")
                continue

        if not page_load_ok_once:
            print(f"  FATAL: Page for prompt {self.prompt_id} failed to load on all viewports. Most checks skipped.")
        self._stop_local_server()
        print(f"\n--- Analysis Finished for Prompt: {self.prompt_id} ---")

    def get_prompt_report_data(self):
        # Cap earned points for TQ categories at their max
        for cat_key, cat_data in self.current_prompt_scores["technical_quality"]["categories"].items():
            if cat_data["earned"] > cat_data["max"]:
                print(f"    INFO ({self.prompt_id}): Capping TQ category '{cat_key}'. Earned: {cat_data['earned']}, Max: {cat_data['max']}.")
                cat_data["earned"] = cat_data["max"]

        total_tq_earned = sum(cat_data['earned'] for cat_data in self.current_prompt_scores["technical_quality"]["categories"].values())
        total_tq_max = self.current_prompt_scores["technical_quality"]["max"]

        adh_earned = self.current_prompt_scores["prompt_adherence"]["earned"]
        adh_max = self.current_prompt_scores["prompt_adherence"]["max"]

        overall_raw_earned = total_tq_earned + adh_earned
        overall_raw_max = total_tq_max + adh_max

        tq_percentage_score = (total_tq_earned / total_tq_max * 100) if total_tq_max > 0 else 0.0
        adh_percentage_score = (adh_earned / adh_max * 100) if adh_max > 0 else 0.0

        weighted_overall_percentage = round(
            (tq_percentage_score * WEIGHT_TECHNICAL_QUALITY) +
            (adh_percentage_score * WEIGHT_PROMPT_ADHERENCE), 2
        )

        prompt_report = {
            "prompt_id": self.prompt_id, "html_file": str(self.file_path.name),
            "prompt_description": self.prompt_config.get("prompt_description", "N/A"),
            "page_title": self.page_title, "analysis_timestamp_for_this_prompt": self.run_timestamp,
            "scores": {
                "technical_quality": {
                    "earned": round(total_tq_earned, 2),
                    "max": round(total_tq_max, 2),
                    "percentage": round(tq_percentage_score, 2),
                    "categories": self.current_prompt_scores["technical_quality"]["categories"]
                },
                "prompt_adherence": {
                    "earned": round(adh_earned, 2),
                    "max": round(adh_max, 2),
                    "percentage": round(adh_percentage_score, 2),
                    "details": self.current_prompt_scores["prompt_adherence"]["details"]
                },
                "overall": { # Raw sum for potential other uses
                    "earned_raw_sum": round(overall_raw_earned, 2),
                    "max_raw_sum": round(overall_raw_max, 2),
                    "percentage_weighted": weighted_overall_percentage
                }
            },
            "page_load_errors": self.current_prompt_scores["page_load_errors"],
            "output_directory_for_this_prompt": str(self.prompt_output_dir)
        }
        with open(self.prompt_output_dir / f"{self.prompt_id}_detailed_report.json", "w", encoding='utf-8') as f:
            json.dump(prompt_report, f, indent=2)
        return prompt_report

    def close(self):
        self._stop_local_server()
        if hasattr(self, 'driver') and self.driver:
            self.driver.quit()
            print(f"\nWebDriver closed for prompt {self.prompt_id}.")


# NEW: Wrapper function for processing a single prompt in a separate process
def process_single_prompt_wrapper(prompt_config_obj_tuple):
    # Unpack tuple: (prompt_config_obj, html_dir_path_str, current_run_output_dir_str, run_timestamp_str)
    prompt_config_obj, html_dir_path_str, current_run_output_dir_str, run_timestamp_str = prompt_config_obj_tuple

    html_dir_path = Path(html_dir_path_str)
    current_run_output_dir = Path(current_run_output_dir_str)

    prompt_id = prompt_config_obj.get("prompt_id")
    # Basic info for logging from worker
    worker_pid = os.getpid()
    print(f"[Worker PID: {worker_pid}] Processing prompt: {prompt_id}")

    if not prompt_id:
        return {"prompt_id": "UNKNOWN", "status": "NO_PROMPT_ID", "error": "Prompt config has no prompt_id",
                "scores": {"overall": {"percentage_weighted":0}, "technical_quality": {"earned":0, "max":0}, "prompt_adherence": {"earned":0, "max":0}}}

    html_file = html_dir_path / f"{prompt_id}.html"
    if not html_file.exists():
        return {"prompt_id": prompt_id, "status": "FILE_NOT_FOUND", "error": f"HTML file {html_file.name} not found in {html_dir_path}.",
                "scores": {"overall": {"percentage_weighted":0}, "technical_quality": {"earned":0, "max":0}, "prompt_adherence": {"earned":0, "max":0}}}

    analyzer = None
    try:
        analyzer = UIBenchmarkAnalyzer(html_file, prompt_config_obj, current_run_output_dir, run_timestamp_str)
        analyzer.run_single_prompt_analysis()
        report_data = analyzer.get_prompt_report_data()
        # Ensure status for easier aggregation later
        if "status" not in report_data:
             report_data["status"] = "SUCCESS"
        return report_data
    except WebDriverException as e_wd:
        error_msg = f"WebDriver error for prompt {prompt_id} in worker {worker_pid}: {e_wd}"
        print(error_msg)
        return {"prompt_id": prompt_id, "status": "WEBDRIVER_ERROR", "error": str(e_wd),
                "scores": {"overall": {"percentage_weighted":0}, "technical_quality": {"earned":0, "max":0}, "prompt_adherence": {"earned":0, "max":0}}}
    except Exception as e_prompt:
        error_msg = f"Error analyzing prompt {prompt_id} in worker {worker_pid}: {e_prompt}"
        print(error_msg)
        # import traceback # For more detailed debugging if needed
        # traceback.print_exc()
        return {"prompt_id": prompt_id, "status": "ANALYSIS_ERROR", "error": str(e_prompt),
                "scores": {"overall": {"percentage_weighted":0}, "technical_quality": {"earned":0, "max":0}, "prompt_adherence": {"earned":0, "max":0}}}
    finally:
        if analyzer:
            analyzer.close()
        print(f"[Worker PID: {worker_pid}] Finished prompt: {prompt_id}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python ui_benchmark_analyzer.py <path_to_master_prompts_config.json> <path_to_html_files_directory> [output_base_dir]")
        dummy_master_config_path = Path("dummy_prompts_benchmark_config.json")
        if not dummy_master_config_path.exists():
            dummy_master_prompts = { "benchmark_run_name": "Dummy Run", "prompts": [
                { "prompt_id": "example_prompt_001", "prompt_description": "A simple page.",
                  "viewports_to_test": {"desktop": [1920, 1080]},
                  "adherence_checks": [{"type": "text_content", "name": "Page Title Check", "selector": "title", "selector_type": "tag_name", "expected_text": "Test Page", "points": 5}]}]}
            with open(dummy_master_config_path, "w", encoding="utf-8") as f: json.dump(dummy_master_prompts, f, indent=2)
            print(f"Created dummy config: {dummy_master_config_path}. Ensure 'example_prompt_001.html' exists in 'html_tests'.")
            print(f"Run: python {sys.argv[0]} {dummy_master_config_path} ./html_tests")
        sys.exit(1)

    master_config_path = Path(sys.argv[1])
    html_dir_path = Path(sys.argv[2]) # This is effectively {challengename}/{modelname}/
    output_base_dir_arg = Path(sys.argv[3] if len(sys.argv) > 3 else "ui_benchmark_master_runs")

    if not master_config_path.is_file(): print(f"Master config not found: {master_config_path}"); sys.exit(1)
    if not html_dir_path.is_dir(): print(f"HTML directory not found: {html_dir_path}"); sys.exit(1)
    try:
        with open(master_config_path, 'r', encoding='utf-8') as f: master_config = json.load(f)
    except Exception as e: print(f"Error loading master config: {e}"); sys.exit(1)


    all_prompts_results = []

    run_folder_name_prefix = html_dir_path.name # Should be modelname
    if not run_folder_name_prefix or run_folder_name_prefix in ['.', '..']:
        run_folder_name_prefix = "unknown_model_run"

    run_timestamp = time.strftime('%Y%m%d-%H%M%S')
    # This creates a directory for the current model's run, e.g., ui_benchmark_master_runs/modelname_timestamp/
    current_run_output_dir = output_base_dir_arg / f"{run_folder_name_prefix}_{run_timestamp}"
    current_run_output_dir.mkdir(parents=True, exist_ok=True)

    prompts_to_process_configs = master_config.get("prompts", [])

    # Prepare arguments for the worker function
    # Paths are converted to strings as a safeguard for pickling with ProcessPoolExecutor,
    # though Path objects are generally picklable in modern Python.
    tasks_for_workers = []
    for prompt_config_obj in prompts_to_process_configs:
        tasks_for_workers.append(
            (prompt_config_obj, str(html_dir_path), str(current_run_output_dir), run_timestamp)
        )

    print(f"Starting analysis for model: {run_folder_name_prefix}")
    print(f"Processing {len(tasks_for_workers)} prompts using up to {PROMPT_WORKERS_COUNT} parallel workers.")

    with ProcessPoolExecutor(max_workers=PROMPT_WORKERS_COUNT) as executor:
        # Use a dictionary to map futures to prompt_ids for better error reporting if needed
        future_to_prompt_id = {}
        for task_args_tuple in tasks_for_workers:
            prompt_config_obj = task_args_tuple[0]
            prompt_id = prompt_config_obj.get("prompt_id", "UNKNOWN_PROMPT_IN_CONFIG")
            future = executor.submit(process_single_prompt_wrapper, task_args_tuple)
            future_to_prompt_id[future] = prompt_id

        for future in as_completed(future_to_prompt_id):
            prompt_id_for_future = future_to_prompt_id[future]
            try:
                result = future.result()
                all_prompts_results.append(result)
            except Exception as e_exec: # Should ideally be caught by worker, but this is a fallback
                print(f"CRITICAL EXCEPTION from worker for prompt {prompt_id_for_future}: {e_exec}")
                all_prompts_results.append({
                    "prompt_id": prompt_id_for_future, "status": "EXECUTOR_ERROR", "error": str(e_exec),
                    "scores": {"overall": {"percentage_weighted":0}, "technical_quality": {"earned":0, "max":0}, "prompt_adherence": {"earned":0, "max":0}}
                })

    # --- Aggregation and Master Report ---
    master_summary = {
        "benchmark_run_name": run_folder_name_prefix, # This is the model name
        "benchmark_config_file": str(master_config_path.name),
        "html_source_directory": str(html_dir_path),
        "overall_run_timestamp_for_this_model": run_timestamp, # Clarify this is for the model
        "total_prompts_configured": len(master_config.get("prompts", [])),
        "aggregate_scores": {
            "total_tq_earned": 0.0, "total_tq_max": 0.0,
            "total_adh_earned": 0.0, "total_adh_max": 0.0,
            "average_prompt_weighted_score": 0.0,
            "overall_weighted_score_from_totals": 0.0
        },
        "individual_prompt_results": []
    }

    total_weighted_percentage_sum, num_scored_prompts = 0, 0
    successful_analysis_count = 0

    for res in all_prompts_results:
        # Update logic for counting successful prompts based on status
        if res.get("status") == "SUCCESS":
            successful_analysis_count += 1
            is_success_for_scoring = True # For scoring, count if analysis completed, even if scores are low
        elif res.get("status") in ["FILE_NOT_FOUND", "NO_PROMPT_ID"]:
             is_success_for_scoring = False # These weren't analyzed
        else: # WEBDRIVER_ERROR, ANALYSIS_ERROR, EXECUTOR_ERROR etc.
            is_success_for_scoring = False # Failed analysis

        tq_earned = res.get("scores",{}).get("technical_quality",{}).get("earned",0)
        tq_max = res.get("scores",{}).get("technical_quality",{}).get("max",0)
        adh_earned = res.get("scores",{}).get("prompt_adherence",{}).get("earned",0)
        adh_max = res.get("scores",{}).get("prompt_adherence",{}).get("max",0)
        overall_weighted_perc = res.get("scores",{}).get("overall",{}).get("percentage_weighted",0)

        master_summary["individual_prompt_results"].append({
            "prompt_id": res.get("prompt_id"), "status": res.get("status", "UNKNOWN_STATUS"),
            "error_message": res.get("error"),
            "technical_quality_earned": tq_earned, "technical_quality_max": tq_max,
            "prompt_adherence_earned": adh_earned, "prompt_adherence_max": adh_max,
            "overall_weighted_percentage": overall_weighted_perc,
            "report_directory": res.get("output_directory_for_this_prompt") # This comes from analyzer.get_prompt_report_data()
        })

        # Only include fully successful analyses in aggregate scores that depend on max points being reliable
        # If a prompt had a fatal error, its max points might be 0 or unreliable.
        if is_success_for_scoring and res.get("status") == "SUCCESS": # Stricter check for score aggregation
            master_summary["aggregate_scores"]["total_tq_earned"] += tq_earned
            if tq_max > 0 : master_summary["aggregate_scores"]["total_tq_max"] += tq_max # Only add to max if it was actually computed

            master_summary["aggregate_scores"]["total_adh_earned"] += adh_earned
            if adh_max > 0: master_summary["aggregate_scores"]["total_adh_max"] += adh_max

            total_weighted_percentage_sum += overall_weighted_perc
            num_scored_prompts +=1

    master_summary["prompts_analyzed_successfully"] = successful_analysis_count # Use the new counter

    if num_scored_prompts > 0 :
        master_summary["aggregate_scores"]["average_prompt_weighted_score"] = round(total_weighted_percentage_sum / num_scored_prompts, 2)

    agg_tq_earned = master_summary["aggregate_scores"]["total_tq_earned"]
    agg_tq_max = master_summary["aggregate_scores"]["total_tq_max"]
    agg_adh_earned = master_summary["aggregate_scores"]["total_adh_earned"]
    agg_adh_max = master_summary["aggregate_scores"]["total_adh_max"]

    agg_tq_perc_score = (agg_tq_earned / agg_tq_max * 100) if agg_tq_max > 0 else 0.0
    agg_adh_perc_score = (agg_adh_earned / agg_adh_max * 100) if agg_adh_max > 0 else 0.0

    master_summary["aggregate_scores"]["overall_weighted_score_from_totals"] = round(
        (agg_tq_perc_score * WEIGHT_TECHNICAL_QUALITY) +
        (agg_adh_perc_score * WEIGHT_PROMPT_ADHERENCE), 2
    )

    master_report_path = current_run_output_dir / "MASTER_BENCHMARK_SUMMARY.json"
    with open(master_report_path, "w", encoding='utf-8') as f: json.dump(master_summary, f, indent=2)

    # Correct final printout for clarity
    print(f"\n\n{'='*20} MODEL BENCHMARK SUMMARY ({run_folder_name_prefix}) {'='*20}")
    print(f"Model: {master_summary['benchmark_run_name']}")
    print(f"Total Configured Prompts for this model: {master_summary['total_prompts_configured']}, Analyzed Successfully: {master_summary['prompts_analyzed_successfully']}")
    print(f"Agg. TQ Earned: {master_summary['aggregate_scores']['total_tq_earned']:.2f}, Agg. TQ Max: {master_summary['aggregate_scores']['total_tq_max']:.2f}")
    print(f"Agg. Adherence Earned: {master_summary['aggregate_scores']['total_adh_earned']:.2f}, Agg. Adherence Max: {master_summary['aggregate_scores']['total_adh_max']:.2f}")
    print(f"Avg. Prompt Weighted Score: {master_summary['aggregate_scores']['average_prompt_weighted_score']:.2f}%")
    print(f"Overall Weighted Score (from Totals): {master_summary['aggregate_scores']['overall_weighted_score_from_totals']:.2f}%")
    print(f"Master summary: {master_report_path}")
    print(f"Individual reports in subdirectories within: {current_run_output_dir}")

if __name__ == "__main__":
    main()