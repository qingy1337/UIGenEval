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
from difflib import SequenceMatcher # For text similarity

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException,
    StaleElementReferenceException, ElementNotInteractableException
)
# from selenium.webdriver.common.desired_capabilities import DesiredCapabilities # Deprecated
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager
import wcag_contrast_ratio as contrast_lib
from axe_selenium_python import Axe

# --- Configuration ---
DEFAULT_VIEWPORTS = {
    "desktop": (1920, 1080), # Desktop first for comprehensive checks
    "mobile": (375, 667)
}
LIGHTHOUSE_CATEGORIES = ['performance', 'accessibility', 'best-practices', 'seo']

# Baseline Max Points for Technical Quality Categories
TECHNICAL_QUALITY_MAX_POINTS = {
    "Accessibility (Axe-core)": 20,
    "Performance (Lighthouse)": 20,
    "Accessibility (Lighthouse)": 10,
    "Best Practices (Lighthouse)": 5,
    "SEO (Lighthouse)": 5,
    "Rendered Color & Contrast": 15,
    "HTML Structure & Semantics": 10, # Reduced as some specifics might be in adherence
    "CSS Quality & Responsiveness": 10,
    "JavaScript Health": 5,
    "Responsiveness (No Horizontal Scroll)": 5 # New specific check
}
TOTAL_TECHNICAL_QUALITY_MAX_POINTS = sum(TECHNICAL_QUALITY_MAX_POINTS.values())


# --- Helper Functions (Mostly Unchanged, added a few) ---
def parse_color_string_to_rgb_tuple(color_str):
    if not color_str: return None
    color_str = color_str.lower().strip()
    if color_str == 'transparent': return None # Treat transparent as no background for blending
    # Check for hex first as it's common
    if color_str.startswith('#'):
        hex_color = color_str[1:]
        if len(hex_color) == 3: return tuple(int(c * 2, 16) for c in hex_color)
        if len(hex_color) == 6: return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        if len(hex_color) == 8: return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4)) # RGBA hex

    match_rgba = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", color_str)
    if match_rgba:
        r, g, b, a_str = match_rgba.groups()
        a = float(a_str) if a_str is not None else 1.0 # Handle rgba if a is omitted (it shouldn't be but robust)
        if a == 0: return None # Fully transparent, won't contribute to effective background
        return (int(r), int(g), int(b), a) # Return with alpha for blending
    
    match_rgb = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color_str)
    if match_rgb:
        return (int(match_rgb.group(1)), int(match_rgb.group(2)), int(match_rgb.group(3)), 1.0) # Add alpha

    # Limited named colors, add more if needed or use a library
    named = {"white": (255,255,255,1.0), "black": (0,0,0,1.0), "red":(255,0,0,1.0), 
             "green":(0,128,0,1.0), "blue":(0,0,255,1.0)}
    return named.get(color_str)


def normalize_rgb_for_contrast(rgb_0_255_tuple): # Expects (r,g,b)
    if not rgb_0_255_tuple or len(rgb_0_255_tuple) != 3: return None
    return tuple(c / 255.0 for c in rgb_0_255_tuple)

def blend_colors(fg_rgba, bg_rgb): # fg_rgba=(r,g,b,a), bg_rgb=(r,g,b)
    fg_r, fg_g, fg_b, alpha = fg_rgba
    bg_r, bg_g, bg_b = bg_rgb
    # Standard alpha compositing formula
    r = int(fg_r * alpha + bg_r * (1 - alpha))
    g = int(fg_g * alpha + bg_g * (1 - alpha))
    b = int(fg_b * alpha + bg_b * (1 - alpha))
    return (r, g, b)

def get_effective_background_rgb(element, driver):
    current_el = element
    path_colors_with_alpha = [] # Stores (r,g,b,a) tuples

    # Start with document background as a fallback if all parents are transparent
    doc_bg_color_str = driver.execute_script("return getComputedStyle(document.documentElement).backgroundColor;")
    doc_bg_rgba = parse_color_string_to_rgb_tuple(doc_bg_color_str)
    # Default to white if document background is transparent or unparsable
    effective_bg_rgb = (doc_bg_rgba[0], doc_bg_rgba[1], doc_bg_rgba[2]) if doc_bg_rgba and doc_bg_rgba[3] == 1.0 else (255, 255, 255)


    while current_el:
        try:
            # Optimization: if current_el itself is the html or body and fully opaque, use it directly
            tag_name = current_el.tag_name.lower()
            if tag_name in ['html', 'body']:
                bg_color_str = driver.execute_script("return getComputedStyle(arguments[0]).backgroundColor;", current_el)
                parsed_rgba = parse_color_string_to_rgb_tuple(bg_color_str)
                if parsed_rgba and parsed_rgba[3] == 1.0: # Fully opaque
                    effective_bg_rgb = (parsed_rgba[0], parsed_rgba[1], parsed_rgba[2])
                    # Blend any collected transparent layers on top of this opaque background
                    for layer_rgba in reversed(path_colors_with_alpha):
                        effective_bg_rgb = blend_colors(layer_rgba, effective_bg_rgb)
                    return effective_bg_rgb
                elif parsed_rgba and parsed_rgba[3] > 0: # Transparent but has color
                    path_colors_with_alpha.append(parsed_rgba)
                # If html/body is transparent, continue to default white or doc_bg
                break # Stop at html/body if not fully opaque


            bg_color_str = driver.execute_script("return getComputedStyle(arguments[0]).backgroundColor;", current_el)
            parsed_rgba = parse_color_string_to_rgb_tuple(bg_color_str)

            if parsed_rgba:
                if parsed_rgba[3] == 1.0: # Fully opaque background found
                    effective_bg_rgb = (parsed_rgba[0], parsed_rgba[1], parsed_rgba[2])
                    # Blend any collected transparent layers on top of this opaque background
                    for layer_rgba in reversed(path_colors_with_alpha):
                        effective_bg_rgb = blend_colors(layer_rgba, effective_bg_rgb)
                    return effective_bg_rgb
                elif parsed_rgba[3] > 0: # Transparent color, add to stack
                    path_colors_with_alpha.append(parsed_rgba)

            # Move to parent
            parent = driver.execute_script("return arguments[0].parentElement;", current_el)
            if not parent or current_el == parent: # Stop if no parent or self-parent
                break
            current_el = parent
        except (WebDriverException, StaleElementReferenceException):
            break # Element became stale or other WebDriver issue

    # If loop finishes, all parents were transparent or unstyled, blend collected layers over the initial effective_bg_rgb (default white or doc bg)
    for layer_rgba in reversed(path_colors_with_alpha):
        effective_bg_rgb = blend_colors(layer_rgba, effective_bg_rgb)
    return effective_bg_rgb


def get_element_desc(element):
    try:
        tag = element.tag_name
        el_id = element.get_attribute('id')
        el_class = element.get_attribute('class')
        el_testid = element.get_attribute('data-testid')
        desc = f"<{tag}"
        if el_id: desc += f" id='{el_id}'"
        if el_testid: desc += f" data-testid='{el_testid}'"
        if el_class: desc += f" class='{el_class[:30]}{'...' if len(el_class)>30 else ''}'"
        desc += ">"
        return desc
    except StaleElementReferenceException: return "<stale_element>"
    except WebDriverException: return "<invalid_element_for_desc>"

def string_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

class UIBenchmarkAnalyzer:
    def __init__(self, html_file_path, prompt_config_path, output_base_dir="ui_benchmark_reports", viewports=None):
        self.file_path = Path(html_file_path).resolve()
        self.prompt_config_path = Path(prompt_config_path).resolve()

        if not self.file_path.is_file():
            raise FileNotFoundError(f"HTML file not found: {self.file_path}")
        if not self.prompt_config_path.is_file():
            raise FileNotFoundError(f"Prompt config JSON file not found: {self.prompt_config_path}")

        try:
            with open(self.prompt_config_path, 'r', encoding='utf-8') as f:
                self.prompt_config = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in prompt config file {self.prompt_config_path}: {e}")
        except Exception as e:
            raise ValueError(f"Error loading prompt config file {self.prompt_config_path}: {e}")

        self.selenium_uri = self.file_path.as_uri()
        self.run_timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.output_dir_name_prefix = self.prompt_config.get("prompt_id", self.file_path.stem)
        self.output_dir = Path(output_base_dir) / f"{self.output_dir_name_prefix}_{self.run_timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.screenshots_dir = self.output_dir / "screenshots"
        self.screenshots_dir.mkdir(exist_ok=True)

        self.viewports = viewports or self.prompt_config.get("viewports", DEFAULT_VIEWPORTS)
        self.scores = {} 
        self.page_title = "N/A"
        self.lighthouse_path = shutil.which("lighthouse")
        if not self.lighthouse_path:
            print("WARNING: Lighthouse CLI not found in PATH. Some performance/quality checks will be skipped.")

        self.http_server = None
        self.http_thread = None
        self.server_port = None
        self.local_server_url_for_lighthouse = None

        options = ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        options.add_argument('--log-level=3')
        # options.set_capability('goog:loggingPrefs', {'browser': 'ALL', 'performance': 'ALL'}) # New way
        options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})


        try:
            # Try WebDriverManager first
            try:
                driver_path = ChromeDriverManager().install()
                service = ChromeService(executable_path=driver_path)
                print(f"Using ChromeDriver from WebDriverManager: {driver_path}")
            except Exception as e_wdm:
                print(f"WebDriverManager failed ({e_wdm}), trying system ChromeDriver.")
                service = ChromeService() # Assumes chromedriver is in PATH
            self.driver = webdriver.Chrome(service=service, options=options)
        except WebDriverException as e:
            print(f"Fatal WebDriver Error during setup: {e}")
            print("Ensure Chrome browser is installed and a compatible ChromeDriver is in your PATH or installable by webdriver-manager.")
            sys.exit(1)
        
        self.current_viewport_name = "initial"
        self.current_prompt_total_adherence_max_points = sum(check.get("points", 0) for check in self.prompt_config.get("adherence_checks", []))


    def _start_local_server(self):
        # (Unchanged from your original, good as is)
        if self.http_server: return self.local_server_url_for_lighthouse
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('localhost', 0))
        self.server_port = sock.getsockname()[1]
        sock.close()
        serve_directory = str(self.file_path.parent)
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=serve_directory)
        socketserver.TCPServer.allow_reuse_address = True
        self.http_server = socketserver.TCPServer(("localhost", self.server_port), handler)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        file_name_encoded = quote(self.file_path.name)
        self.local_server_url_for_lighthouse = f"http://localhost:{self.server_port}/{file_name_encoded}"
        print(f"  Local HTTP server started for Lighthouse at: {self.local_server_url_for_lighthouse}")
        time.sleep(0.5) 
        return self.local_server_url_for_lighthouse

    def _stop_local_server(self):
        # (Unchanged from your original, good as is)
        if self.http_server:
            print("  Stopping local HTTP server for Lighthouse...")
            self.http_server.shutdown()
            self.http_server.server_close()
            if self.http_thread and self.http_thread.is_alive():
                 self.http_thread.join(timeout=2) 
            self.http_server = None
            self.http_thread = None
            self.local_server_url_for_lighthouse = None
            print("  Local HTTP server stopped.")

    def _add_finding(self, category, check_name, points_earned, max_points, message, status="INFO", data=None):
        if category not in self.scores: self.scores[category] = {'earned': 0, 'max': 0, 'details': []}
        # Ensure points are numbers
        points_earned = float(points_earned) if isinstance(points_earned, (int, float)) else 0.0
        max_points = float(max_points) if isinstance(max_points, (int, float)) else 0.0

        self.scores[category]['earned'] += points_earned
        self.scores[category]['max'] += max_points
        
        finding_detail = {"check": check_name, "status": status, "message": message,
                          "points_earned": points_earned, "max_points": max_points,
                          "viewport": self.current_viewport_name}
        if data: finding_detail["data"] = data
        self.scores[category]['details'].append(finding_detail)
        
        # Print FAIL/WARN immediately for visibility during run
        if status == "FAIL": print(f"    -> [FAIL] {check_name}: {message}")
        elif status == "WARN" and max_points > 0 : print(f"    -> [WARN] {check_name}: {message}") # Only print warning if it affects score or is significant


    def _load_page_at_viewport(self, viewport_name, width, height):
        # (Minor change to print category)
        print(f"\n--- Viewport: {viewport_name} ({width}x{height}) ---")
        self.current_viewport_name = viewport_name
        self.driver.set_window_size(width, height)
        try:
            self.driver.get(self.selenium_uri)
            WebDriverWait(self.driver, 15).until(lambda d: d.execute_script('return document.readyState') == 'complete')
            self.page_title = self.driver.title or "No Title"
            time.sleep(1.5) # Reduced sleep, readyState should be enough
            screenshot_path = self.screenshots_dir / f"{self.output_dir_name_prefix}_{viewport_name}.png"
            self.driver.save_screenshot(str(screenshot_path))
            print(f"  Page loaded. Screenshot: {screenshot_path.name}")
        except TimeoutException:
            self._add_finding("Page Load", "Page Completeness", 0, 0, f"Page timed out loading at {viewport_name} viewport.", "FAIL")
            raise 
        except WebDriverException as e:
            self._add_finding("Page Load", "Driver Operation", 0, 0, f"WebDriver error at {viewport_name}: {e}", "FAIL")
            raise

    def _find_element_by_config(self, check_config):
        selector = check_config.get("selector")
        selector_type_str = check_config.get("selector_type", "css").lower()
        
        by_type = By.CSS_SELECTOR # default
        if selector_type_str == "xpath": by_type = By.XPATH
        elif selector_type_str == "id": by_type = By.ID
        elif selector_type_str == "name": by_type = By.NAME
        elif selector_type_str == "class_name": by_type = By.CLASS_NAME
        elif selector_type_str == "tag_name": by_type = By.TAG_NAME
        elif selector_type_str == "link_text": by_type = By.LINK_TEXT
        elif selector_type_str == "partial_link_text": by_type = By.PARTIAL_LINK_TEXT
        elif selector_type_str == "data-testid": # Custom convenience
            by_type = By.XPATH 
            selector = f"//*[@data-testid='{selector}']"


        try:
            if check_config.get("find_multiple", False):
                return self.driver.find_elements(by_type, selector)
            else:
                return self.driver.find_element(by_type, selector)
        except NoSuchElementException:
            return None
        except WebDriverException as e:
            self._add_finding("Prompt Adherence", check_config.get("name", "Element Find"), 0, check_config.get("points",0), 
                              f"Error finding element with selector '{selector}' ({selector_type_str}): {e}", "FAIL")
            return None

    # --- Technical Quality Checks (Revised points, some logic refined) ---

    def check_html_structure_semantics(self):
        category = "HTML Structure & Semantics"
        max_total_points = TECHNICAL_QUALITY_MAX_POINTS[category]
        current_earned = 0
        # Individual check points should sum up to max_total_points
        check_points = {"HTML Lang": 1, "Page Title": 1, "Main Tag": 1, "Nav Tag": 1, "Footer Tag":1,
                        "H1 Count": 1, "Heading Order Logic": 2, 
                        "Image Alts": 1, "Form Labels": 2}
        # Ensure check_points sum to max_total_points or adjust logic
        if sum(check_points.values()) != max_total_points:
            print(f"WARN: HTML Structure check points ({sum(check_points.values())}) don't sum to category max ({max_total_points}). Review configuration.")

        print(f"\nRunning Checks: {category}")
        try:
            html_tag = self.driver.find_element(By.TAG_NAME, "html")
            lang = html_tag.get_attribute("lang")
            if lang and lang.strip(): current_earned += check_points["HTML Lang"]; self._add_finding(category, "HTML Lang", check_points["HTML Lang"], check_points["HTML Lang"], "PASS")
            else: self._add_finding(category, "HTML Lang", 0, check_points["HTML Lang"], "HTML 'lang' attribute is missing or empty.", "FAIL")
        except NoSuchElementException: self._add_finding(category, "HTML Lang", 0, check_points["HTML Lang"], "<html> tag not found.", "FAIL")
        
        if self.page_title and self.page_title != "No Title" and len(self.page_title.strip()) > 0: current_earned += check_points["Page Title"]; self._add_finding(category, "Page Title", check_points["Page Title"], check_points["Page Title"],"PASS")
        else: self._add_finding(category, "Page Title", 0, check_points["Page Title"], "Page title is missing or empty.", "WARN")

        for tag_name_key, points_val in {"Main Tag": ("main", check_points["Main Tag"]), "Nav Tag": ("nav", check_points["Nav Tag"]), "Footer Tag": ("footer", check_points["Footer Tag"])}.items():
            tag_name, points = points_val
            try:
                elements = self.driver.find_elements(By.TAG_NAME, tag_name)
                if any(el.is_displayed() for el in elements): current_earned += points; self._add_finding(category, tag_name_key, points, points, "PASS")
                elif elements: self._add_finding(category, tag_name_key, 0, points, f"<{tag_name}> found but not visible.", "WARN")
                else: self._add_finding(category, tag_name_key, 0, points, f"No <{tag_name}> element found.", "FAIL" if tag_name == "main" else "WARN")
            except WebDriverException: self._add_finding(category, tag_name_key, 0, points, f"Error checking <{tag_name}>.", "FAIL")

        try: # Headings
            headings = self.driver.find_elements(By.CSS_SELECTOR, "h1, h2, h3, h4, h5, h6")
            sorted_visible_headings = sorted([h for h in headings if h.is_displayed()], key=lambda x: (x.location['y'], x.location['x']))
            if not sorted_visible_headings: self._add_finding(category, "H1 Count", 0, check_points["H1 Count"], "No visible headings.", "FAIL"); self._add_finding(category, "Heading Order Logic", 0, check_points["Heading Order Logic"], "No visible headings.", "FAIL")
            else:
                h1s = [h for h in sorted_visible_headings if h.tag_name == 'h1']
                if len(h1s) == 1: current_earned += check_points["H1 Count"]; self._add_finding(category, "H1 Count", check_points["H1 Count"], check_points["H1 Count"], "PASS")
                elif not h1s : self._add_finding(category, "H1 Count", 0, check_points["H1 Count"], "No visible <h1>.", "FAIL")
                else: self._add_finding(category, "H1 Count", 0, check_points["H1 Count"], f"Multiple visible <h1>s ({len(h1s)}).", "FAIL")
                
                last_level = 0; order_ok = True
                for i, h_el in enumerate(sorted_visible_headings):
                    current_level = int(h_el.tag_name[1])
                    if i == 0 and current_level != 1 and any(vh.tag_name == 'h1' for vh in sorted_visible_headings) : order_ok = False # Should start with H1 if H1 exists
                    if i > 0 and current_level > last_level + 1: order_ok = False; break
                    last_level = current_level
                if order_ok: current_earned += check_points["Heading Order Logic"]; self._add_finding(category, "Heading Order Logic", check_points["Heading Order Logic"], check_points["Heading Order Logic"], "PASS")
                else: self._add_finding(category, "Heading Order Logic", 0, check_points["Heading Order Logic"], "Heading order issue.", "FAIL")
        except WebDriverException: self._add_finding(category, "Headings Processing",0, check_points["H1 Count"] + check_points["Heading Order Logic"], "Error processing headings.", "FAIL")

        try: # Image Alts
            images = [img for img in self.driver.find_elements(By.TAG_NAME, "img") if img.is_displayed()]
            if not images: self._add_finding(category, "Image Alts", check_points["Image Alts"], check_points["Image Alts"], "No visible images.", "INFO") # No penalty if no images
            elif all(img.get_attribute("alt") is not None for img in images): # Stricter: alt must exist. Empty alt "" is for presentational.
                current_earned += check_points["Image Alts"]; self._add_finding(category, "Image Alts", check_points["Image Alts"], check_points["Image Alts"], "PASS")
            else:
                missing_alts = sum(1 for img in images if img.get_attribute("alt") is None)
                self._add_finding(category, "Image Alts", 0, check_points["Image Alts"], f"{missing_alts} visible images missing 'alt' attribute.", "FAIL")
        except WebDriverException: self._add_finding(category, "Image Alts", 0, check_points["Image Alts"], "Error checking image alts.", "FAIL")

        try: # Form Labels
            inputs_selector = 'input:not([type="hidden"]):not([type="submit"]):not([type="reset"]):not([type="button"]):not([type="image"]), select, textarea'
            form_inputs = [inp for inp in self.driver.find_elements(By.CSS_SELECTOR, inputs_selector) if inp.is_displayed()]
            if not form_inputs: self._add_finding(category, "Form Labels", check_points["Form Labels"], check_points["Form Labels"], "No relevant form inputs.", "INFO")
            else:
                unlabeled_inputs = 0
                for inp in form_inputs:
                    inp_id = inp.get_attribute("id"); has_label = False
                    if inp_id:
                        try: self.driver.find_element(By.CSS_SELECTOR, f"label[for='{inp_id}']"); has_label = True
                        except NoSuchElementException: pass
                    if not has_label:
                        try: inp.find_element(By.XPATH, "ancestor::label"); has_label = True # Implicit
                        except NoSuchElementException: pass
                    if not has_label and (inp.get_attribute("aria-label") or inp.get_attribute("aria-labelledby")): has_label = True
                    if not has_label: unlabeled_inputs += 1
                if unlabeled_inputs == 0: current_earned += check_points["Form Labels"]; self._add_finding(category, "Form Labels", check_points["Form Labels"], check_points["Form Labels"], "PASS")
                else: self._add_finding(category, "Form Labels", 0, check_points["Form Labels"], f"{unlabeled_inputs} inputs unlabeled.", "FAIL", data={"count":unlabeled_inputs})
        except WebDriverException: self._add_finding(category, "Form Labels", 0, check_points["Form Labels"],"Error checking form labels.", "FAIL")


    def check_accessibility_axe(self):
        category = "Accessibility (Axe-core)"
        max_points = TECHNICAL_QUALITY_MAX_POINTS[category]
        print(f"\nRunning Checks: {category}")
        try:
            axe = Axe(self.driver); axe.inject(); results = axe.run() 
            report_path_obj = self.output_dir / f"axe_report_{self.output_dir_name_prefix}_{self.current_viewport_name}.json"
            axe.write_results(results, str(report_path_obj))
            # self._add_finding(category, "Axe Report Generated", 0, 0, f"Axe report: {report_path_obj.name}", "INFO") # Too noisy
            
            violations = results.get("violations", [])
            if not violations: 
                self._add_finding(category, "Axe Violations", max_points, max_points, "No violations.", "PASS")
            else:
                critical_issues = sum(1 for v in violations if v['impact'] == 'critical')
                serious_issues = sum(1 for v in violations if v['impact'] == 'serious')
                moderate_issues = sum(1 for v in violations if v['impact'] == 'moderate')
                minor_issues = sum(1 for v in violations if v['impact'] == 'minor')

                penalty = (critical_issues * 5) + (serious_issues * 3) + (moderate_issues * 1) + (minor_issues * 0.5)
                earned_points = max(0, max_points - penalty)
                status = "PASS" if earned_points == max_points else "FAIL" if critical_issues > 0 or serious_issues > 1 else "WARN"
                msg = f"{len(violations)} Axe violations (Crit:{critical_issues},Ser:{serious_issues},Mod:{moderate_issues},Min:{minor_issues}). Report: {report_path_obj.name}"
                self._add_finding(category, "Axe Violations", earned_points, max_points, msg, status, data=[v['id'] for v in violations])
        except Exception as e_axe: 
            self._add_finding(category, "Axe Execution", 0, max_points, f"Error running Axe-core: {e_axe}", "FAIL")

    def check_css_quality_and_responsiveness(self): # Combined and renamed
        category = "CSS Quality & Responsiveness"
        max_total_points = TECHNICAL_QUALITY_MAX_POINTS[category]
        current_earned = 0
        check_points = {"Viewport Meta": 2, "CSS Variables": 2, "Modern Layout": 2, 
                        "Inline Styles": 2, "!important Usage": 2} # Sums to 10
        if sum(check_points.values()) != max_total_points:
            print(f"WARN: CSS Quality check points ({sum(check_points.values())}) don't sum to category max ({max_total_points}).")

        print(f"\nRunning Checks: {category}")
        # Viewport Meta
        try:
            vp_tag = self.driver.find_element(By.CSS_SELECTOR, "meta[name='viewport']")
            content = vp_tag.get_attribute("content").lower()
            if "width=device-width" in content and ("initial-scale=1" in content or "initial-scale=1.0" in content) \
               and "user-scalable=no" not in content and "maximum-scale=1" not in content : # Basic good viewport
                current_earned += check_points["Viewport Meta"]; self._add_finding(category, "Viewport Meta", check_points["Viewport Meta"], check_points["Viewport Meta"], "PASS")
            elif "user-scalable=no" in content or "maximum-scale=1" in content:
                 self._add_finding(category, "Viewport Meta", 0, check_points["Viewport Meta"], "Restricts user scaling.", "FAIL")
            else: self._add_finding(category, "Viewport Meta", 0, check_points["Viewport Meta"], "Suboptimal viewport.", "WARN")
        except NoSuchElementException: self._add_finding(category, "Viewport Meta", 0, check_points["Viewport Meta"], "Viewport meta tag missing.", "FAIL")

        # CSS Variables
        try:
            vars_count = len(self.driver.execute_script("return Object.keys(getComputedStyle(document.documentElement)).filter(k => k.startsWith('--'));") or [])
            if vars_count > 5 : current_earned += check_points["CSS Variables"]; self._add_finding(category, "CSS Variables", check_points["CSS Variables"], check_points["CSS Variables"], f"{vars_count} vars on :root. PASS")
            elif vars_count > 0: current_earned += check_points["CSS Variables"] * 0.5; self._add_finding(category, "CSS Variables", check_points["CSS Variables"] * 0.5, check_points["CSS Variables"], f"{vars_count} vars. WARN")
            else: self._add_finding(category, "CSS Variables", 0, check_points["CSS Variables"], "No :root CSS variables.", "WARN")
        except WebDriverException: self._add_finding(category, "CSS Variables", 0, check_points["CSS Variables"], "Error checking CSS vars.", "FAIL")

        # Modern Layout (Flex/Grid on body or main)
        try:
            body_display = self.driver.execute_script("return getComputedStyle(document.body).display;")
            main_els = self.driver.find_elements(By.TAG_NAME, "main")
            main_display = self.driver.execute_script("return getComputedStyle(arguments[0]).display;", main_els[0]) if main_els and main_els[0].is_displayed() else ""
            if body_display in ['flex', 'grid'] or main_display in ['flex', 'grid']:
                current_earned += check_points["Modern Layout"]; self._add_finding(category, "Modern Layout", check_points["Modern Layout"], check_points["Modern Layout"], "PASS")
            else: self._add_finding(category, "Modern Layout", 0, check_points["Modern Layout"], "Flex/Grid not on body/main.", "INFO")
        except WebDriverException: self._add_finding(category, "Modern Layout", 0, check_points["Modern Layout"], "Error checking layout.", "FAIL")

        # Inline Styles
        try:
            inline_styles_count = len([el for el in self.driver.find_elements(By.CSS_SELECTOR, "[style]") if el.is_displayed() and el.get_attribute("style").strip()])
            if inline_styles_count <= 3: current_earned += check_points["Inline Styles"]; self._add_finding(category, "Inline Styles", check_points["Inline Styles"], check_points["Inline Styles"], "PASS")
            elif inline_styles_count <= 10: current_earned += check_points["Inline Styles"] * 0.5; self._add_finding(category, "Inline Styles", check_points["Inline Styles"] * 0.5, check_points["Inline Styles"], f"{inline_styles_count} inline styles. WARN")
            else: self._add_finding(category, "Inline Styles", 0, check_points["Inline Styles"], f"{inline_styles_count} inline styles. FAIL")
        except WebDriverException: self._add_finding(category, "Inline Styles", 0, check_points["Inline Styles"], "Error checking inline styles.", "FAIL")
        
        # !important Usage
        try:
            styles_content = self.driver.execute_script("let c=''; document.querySelectorAll('style').forEach(s=>c+=s.textContent); return c;")
            # Also check linked stylesheets if feasible - complex, skip for now. Focus on inline <style>
            important_count = styles_content.lower().count("!important")
            if important_count == 0: current_earned += check_points["!important Usage"]; self._add_finding(category, "!important Usage", check_points["!important Usage"], check_points["!important Usage"], "PASS")
            elif important_count <= 2: current_earned += check_points["!important Usage"] * 0.5; self._add_finding(category, "!important Usage", check_points["!important Usage"] * 0.5, check_points["!important Usage"], f"{important_count} !important. WARN")
            else: self._add_finding(category, "!important Usage", 0, check_points["!important Usage"], f"{important_count} !important. FAIL")
        except WebDriverException: self._add_finding(category, "!important Usage", 0, check_points["!important Usage"], "Error checking !important.", "FAIL")
        
        # Responsiveness (No Horizontal Scroll) - Moved to its own check for clarity in scoring
    
    def check_responsiveness_no_horizontal_scroll(self):
        category = "Responsiveness (No Horizontal Scroll)"
        max_points = TECHNICAL_QUALITY_MAX_POINTS[category]
        print(f"\nRunning Checks: {category} ({self.current_viewport_name})")
        try:
            # Ensure body has loaded and rendered
            WebDriverWait(self.driver, 5).until(
                lambda d: d.execute_script('return document.body && document.body.scrollHeight > 0')
            )
            has_horizontal_scroll = self.driver.execute_script(
                "return document.documentElement.scrollWidth > document.documentElement.clientWidth || document.body.scrollWidth > document.body.clientWidth;"
            )
            if not has_horizontal_scroll:
                self._add_finding(category, "Horizontal Scrollbar", max_points, max_points, "No horizontal scrollbar detected.", "PASS")
            else:
                self._add_finding(category, "Horizontal Scrollbar", 0, max_points, "Horizontal scrollbar detected.", "FAIL")
        except TimeoutException:
             self._add_finding(category, "Horizontal Scrollbar", 0, max_points, "Page content not fully loaded to check scroll.", "WARN")
        except WebDriverException as e:
            self._add_finding(category, "Horizontal Scrollbar", 0, max_points, f"Error checking for horizontal scrollbar: {e}", "FAIL")


    def check_rendered_color_contrast(self):
        category = "Rendered Color & Contrast"
        max_points = TECHNICAL_QUALITY_MAX_POINTS[category]
        print(f"\nRunning Checks: {category} ({self.current_viewport_name})")
        # Script to get elements with text, including placeholders
        text_elements_script = """
            const elementsData = [];
            const treeWalker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, {
                acceptNode: function (node) {
                    if (node.nodeName === 'SCRIPT' || node.nodeName === 'STYLE' || 
                        node.nodeName === 'NOSCRIPT' || node.nodeName === 'IFRAME' ||
                        node.nodeName === 'HEAD' || node.nodeName === 'META' || 
                        node.nodeName === 'LINK' || node.nodeName === 'TITLE' ||
                        node.closest('svg')) { // Check if node or any ancestor is SVG
                        return NodeFilter.FILTER_REJECT;
                    }
                    // Check direct text content
                    let hasDirectText = false;
                    for (let child = node.firstChild; child; child = child.nextSibling) {
                        if (child.nodeType === Node.TEXT_NODE && child.nodeValue.trim().length > 0) {
                            hasDirectText = true; break;
                        }
                    }
                    // Check for placeholder text in input/textarea
                    let hasPlaceholderText = ( (node.nodeName === 'INPUT' || node.nodeName === 'TEXTAREA') && 
                                               node.placeholder && node.placeholder.trim().length > 0 );

                    if (!hasDirectText && !hasPlaceholderText) {
                        return NodeFilter.FILTER_SKIP; // Skip if no direct text and no placeholder
                    }
                    
                    const style = window.getComputedStyle(node);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' || 
                        parseFloat(style.opacity) === 0 || parseFloat(style.fontSize) < 6 /*px min font */ || // Increased min font size
                        node.offsetWidth === 0 || node.offsetHeight === 0) {
                        return NodeFilter.FILTER_REJECT; // Reject if not visible or too small
                    }
                    return NodeFilter.FILTER_ACCEPT;
                }
            });
            let count = 0;
            const MAX_ELEMENTS_TO_CHECK = 200; // Limit number of elements
            while (treeWalker.nextNode() && count < MAX_ELEMENTS_TO_CHECK) { 
                elementsData.push(treeWalker.currentNode); 
                count++;
            }
            return elementsData;
        """
        try: 
            candidate_elements_from_script = self.driver.execute_script(text_elements_script)
            if not candidate_elements_from_script :
                 self._add_finding(category, "Contrast Check", max_points, max_points, "No visible text elements by script to check.", "INFO")
                 return
            # Filter again in Python for safety and to use Selenium WebElements
            elements_to_check = []
            for el_proxy in candidate_elements_from_script:
                try:
                    # Re-fetch element by its attributes to ensure it's a valid WebElement
                    # This is a bit heavy but ensures we are dealing with live elements.
                    # A more efficient way would be to pass back enough info to re-select if needed, or trust the proxy if stable.
                    # For now, let's try to use the proxy if it's usable.
                    if hasattr(el_proxy, 'tag_name') and el_proxy.is_displayed(): # Basic check
                         elements_to_check.append(el_proxy)
                except (StaleElementReferenceException, WebDriverException):
                    continue # Skip stale elements
        
        except WebDriverException as e_fetch: 
            self._add_finding(category, "Contrast Element Fetch", 0, max_points, f"Error fetching text elements: {e_fetch}", "FAIL"); return
        
        if not elements_to_check: 
            self._add_finding(category, "Contrast Check", max_points, max_points, "No valid text elements after filtering.", "INFO"); return

        print(f"  Checking contrast for ~{len(elements_to_check)} text candidate(s)...")
        failure_count_aa = 0; checked_count = 0; warning_count_aaa = 0

        script_get_styles = """
            const el = arguments[0];
            if (!el || typeof el.getBoundingClientRect !== 'function' || !el.checkVisibility || !el.checkVisibility()) return {'error':'not_visible_at_style_fetch'};
            const style = window.getComputedStyle(el); if (!style) return null;
            return {color: style.color, fontSize: style.fontSize, fontWeight: style.fontWeight, opacity: style.opacity};
        """
        for el_web_element in elements_to_check:
            try:
                # Double check visibility right before style fetching
                if not el_web_element.is_displayed(): continue

                text_content_py = el_web_element.text.strip()
                placeholder_text_py = ""
                tag_name = el_web_element.tag_name.lower()
                if tag_name in ['input', 'textarea']:
                    placeholder_text_py = el_web_element.get_attribute("placeholder")
                    if placeholder_text_py: placeholder_text_py = placeholder_text_py.strip()

                # Use placeholder if primary text is empty and placeholder exists
                effective_text_content = placeholder_text_py if not text_content_py and placeholder_text_py else text_content_py
                if not effective_text_content: continue # Skip if no text at all

                style_dict = self.driver.execute_script(script_get_styles, el_web_element)

                if not style_dict or style_dict.get('error'): continue
                
                fg_color_str = style_dict.get('color'); font_size_str = style_dict.get('fontSize')
                font_weight_str = style_dict.get('fontWeight'); opacity_str = style_dict.get('opacity')

                if not fg_color_str or not font_size_str: continue

                fg_rgba_tuple = parse_color_string_to_rgb_tuple(fg_color_str) # (r,g,b,a)
                if not fg_rgba_tuple: continue

                # If text itself is transparent, it has no contrast
                text_alpha = fg_rgba_tuple[3]
                element_opacity = float(opacity_str) if opacity_str else 1.0
                effective_text_alpha = text_alpha * element_opacity
                if effective_text_alpha < 0.1: # Consider effectively transparent text as having no contrast (or unreadable)
                    continue 

                # If text has alpha, we need to blend it against its own parent's effective BG first
                # For simplicity here, we assume text color is solid, or its alpha is handled by browser against immediate BG.
                # A more precise model would blend the text_rgba with a white intermediate bg if its direct parent is transparent.
                # However, contrast usually assumes solid text color. If fg_rgba_tuple[3] < 1.0, it's tricky.
                # Let's use the RGB part of fg_rgba_tuple for contrast calculation.
                fg_rgb_for_contrast = (fg_rgba_tuple[0], fg_rgba_tuple[1], fg_rgba_tuple[2])

                bg_rgb = get_effective_background_rgb(el_web_element, self.driver) 
                if not bg_rgb: continue # Should not happen if get_effective_background_rgb has a default

                checked_count += 1
                font_size_px = float(re.sub(r'[^\d.]', '', font_size_str))
                is_bold_from_weight = (str(font_weight_str).lower() == 'bold' or 
                                       (str(font_weight_str).isdigit() and int(font_weight_str) >= 700) or
                                       str(font_weight_str).lower() == 'bolder') # Added bolder
                is_large_text_wcag = (font_size_px >= 24) or (font_size_px >= 18.66 and is_bold_from_weight)
                
                norm_fg_rgb = normalize_rgb_for_contrast(fg_rgb_for_contrast)
                norm_bg_rgb = normalize_rgb_for_contrast(bg_rgb)

                if norm_fg_rgb and norm_bg_rgb:
                    actual_contrast_ratio = contrast_lib.rgb(norm_fg_rgb, norm_bg_rgb)
                    passes_aa = contrast_lib.passes_AA(actual_contrast_ratio, large=is_large_text_wcag)
                    passes_aaa = contrast_lib.passes_AAA(actual_contrast_ratio, large=is_large_text_wcag)
                    
                    snippet = effective_text_content[:40].replace('\n', ' ') + ('...' if len(effective_text_content) > 40 else '')
                    el_desc = get_element_desc(el_web_element)

                    if not passes_aa:
                        failure_count_aa += 1
                        self._add_finding(category, "Contrast Failure (AA)", 0, 0, # Points handled at summary
                                          f"AA FAIL {actual_contrast_ratio:.2f} for '{snippet}' in {el_desc}", "FAIL", 
                                          data={"ratio": actual_contrast_ratio, "text": snippet, "fg": fg_color_str, "bg_eff": f"rgb{bg_rgb}", "font": f"{font_size_str} {font_weight_str}"})
                    elif not passes_aaa: # Passes AA, but not AAA
                        warning_count_aaa +=1
                        # Don't make this a FAIL, just a note for now, as AA is the primary target for points.
                        # This finding won't deduct points but provides info.
                        self._add_finding(category, "Contrast Suboptimal (AAA)", 0, 0,
                                          f"AAA WARN {actual_contrast_ratio:.2f} for '{snippet}' in {el_desc}", "INFO",
                                          data={"ratio": actual_contrast_ratio, "text": snippet})
            except (StaleElementReferenceException, WebDriverException): 
                continue # Element became stale during processing
            except Exception as e_inner_contrast:
                print(f"    Error in contrast loop for an element: {e_inner_contrast}")
                continue

        if checked_count == 0 and elements_to_check: 
            self._add_finding(category, "Contrast Check Result", 0, max_points, f"Found {len(elements_to_check)} text candidates but checked 0.", "FAIL")
        elif failure_count_aa > 0: 
            # Deduct more points per failure
            penalty_per_failure = 3 
            earned_points = max(0, max_points - (failure_count_aa * penalty_per_failure))
            self._add_finding(category, "Contrast Check Result", earned_points, max_points, f"{failure_count_aa} WCAG AA failures on {checked_count} instances.", "FAIL")
        elif checked_count > 0: 
            msg = f"All {checked_count} instances meet WCAG AA."
            if warning_count_aaa > 0: msg += f" ({warning_count_aaa} only meet AA, not AAA)."
            self._add_finding(category, "Contrast Check Result", max_points, max_points, msg, "PASS")
        # else no text elements found, already handled.

    def check_performance_lighthouse(self):
        # Combined Lighthouse checks into one method that distributes points
        print(f"\nRunning Checks: Lighthouse Suite")
        if not self.lighthouse_path:
            for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key, "Lighthouse Execution", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], "Lighthouse CLI not found.", "WARN")
            return
        
        url_to_check_lh = self._start_local_server()
        if not url_to_check_lh:
             for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key, "Lighthouse Server", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], "Failed to start local server for Lighthouse.", "FAIL")
             return
        
        # Create a sub-directory for lighthouse reports for this specific run
        lh_reports_dir = self.output_dir / f"lighthouse_reports_{self.current_viewport_name}"
        lh_reports_dir.mkdir(parents=True, exist_ok=True)
        lighthouse_base_report_name = lh_reports_dir / f"lh_{self.output_dir_name_prefix}" # Use common prefix

        cmd = [self.lighthouse_path, url_to_check_lh, "--output=json", "--output=html",
               f"--output-path={lighthouse_base_report_name}", 
               "--quiet", "--throttling-method=simulate",
               # Using preset "desktop" or "mobile" implies certain flags.
               # Forcing headless: new, as older versions might behave differently.
               f"--chrome-flags=--headless=new --disable-gpu --no-sandbox --no-zygote", 
               f"--preset={self.current_viewport_name.lower()}", # Use viewport name if 'desktop' or 'mobile'
               "--only-categories=" + ",".join(LIGHTHOUSE_CATEGORIES)]
        
        # If viewport is not 'desktop' or 'mobile', Lighthouse might default or error.
        # For custom viewports, specific flags like --screenEmulation would be needed,
        # but --preset=desktop/mobile is simpler if viewports align.
        if self.current_viewport_name.lower() not in ['desktop', 'mobile']:
            print(f"  Lighthouse preset might not match viewport '{self.current_viewport_name}'. Using default behavior.")
            # remove preset if not desktop/mobile
            cmd = [c for c in cmd if not c.startswith("--preset")]


        print(f"  Running Lighthouse on {url_to_check_lh} ({self.current_viewport_name})...")
        try:
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False) # Increased timeout
            report_path_json = lighthouse_base_report_name.with_suffix(".report.json")
            # report_path_html = lighthouse_base_report_name.with_suffix(".report.html") # Name is already base

            if process.returncode != 0 or not report_path_json.exists():
                err_msg = f"Lighthouse CLI failed. Code: {process.returncode}. Stderr: {process.stderr[:500]}"
                for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                    self._add_finding(cat_key, "Lighthouse Execution", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], err_msg, "FAIL", data={"cmd": " ".join(cmd)})
                return

            with open(report_path_json, 'r', encoding='utf-8') as f: lh_results = json.load(f)
            if lh_results.get("runtimeError"):
                err_msg = f"Lighthouse runtime error: {lh_results['runtimeError'].get('code')} - {lh_results['runtimeError'].get('message')}"
                for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                     self._add_finding(cat_key, "Lighthouse Runtime Error", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], err_msg, "FAIL")
                return
            
            # Process scores for each category
            lh_category_map = {
                'performance': "Performance (Lighthouse)",
                'accessibility': "Accessibility (Lighthouse)",
                'best-practices': "Best Practices (Lighthouse)",
                'seo': "SEO (Lighthouse)"
            }
            for lh_cat_id, internal_cat_key in lh_category_map.items():
                max_cat_points = TECHNICAL_QUALITY_MAX_POINTS[internal_cat_key]
                cat_data = lh_results.get('categories', {}).get(lh_cat_id)
                if cat_data and cat_data.get('score') is not None:
                    score_0_1 = cat_data['score']
                    score_percent = int(score_0_1 * 100)
                    earned_points = round(score_0_1 * max_cat_points, 2)
                    status = "PASS" if score_percent >= 90 else "WARN" if score_percent >= 50 else "FAIL"
                    self._add_finding(internal_cat_key, f"Lighthouse Score", earned_points, max_cat_points, f"{score_percent}/100", status)
                else:
                    self._add_finding(internal_cat_key, f"Lighthouse Score", 0, max_cat_points, "Score not found in report.", "FAIL")
            print(f"  Lighthouse reports generated in: {lh_reports_dir.name}")

        except subprocess.TimeoutExpired:
            for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key, "Lighthouse Execution", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], "Lighthouse timed out (5 min).", "FAIL")
        except FileNotFoundError: # JSON report not found after run
             for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key, "Lighthouse Report", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], "Lighthouse JSON report not found.", "FAIL")
        except json.JSONDecodeError:
             for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key, "Lighthouse Report Parse", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], "Failed to parse Lighthouse JSON.", "FAIL")
        except Exception as e_lh_main:
            for cat_key in ["Performance (Lighthouse)", "Accessibility (Lighthouse)", "Best Practices (Lighthouse)", "SEO (Lighthouse)"]:
                self._add_finding(cat_key, "Lighthouse Main Error", 0, TECHNICAL_QUALITY_MAX_POINTS[cat_key], f"Error: {e_lh_main}", "FAIL")
        finally:
            self._stop_local_server()


    def check_javascript_health(self):
        category = "JavaScript Health"
        max_points = TECHNICAL_QUALITY_MAX_POINTS[category]
        print(f"\nRunning Checks: {category}")
        try:
            logs = self.driver.get_log('browser')
            js_errors = []
            # More specific filtering for JS errors
            # Common benign errors to ignore (can be expanded)
            ignore_patterns = [
                "favicon.ico", "extension", "custom-element", "deprecated",
                "banner", "adwarning", "news.google.com", "doubleclick.net",
                "googleads.g.doubleclick.net", "googlesyndication.com",
                "Failed to load resource: net::ERR_FAILED", # Often from adblockers or flaky network
                "awesomium", "Reading from 'showModalDialog' is deprecated", # older patterns
                "ResizeObserver loop limit exceeded" # Can be benign or indicate layout thrashing
            ]
            for entry in logs:
                message = entry.get('message', '')
                is_error = entry['level'] == 'SEVERE'
                
                # Check if any ignore pattern is in the message
                should_ignore = any(pattern.lower() in message.lower() for pattern in ignore_patterns)
                
                if is_error and not should_ignore:
                    # Further filter out some common Chrome internal/extension errors
                    if "Unchecked runtime.lastError" in message and "extension" in message: continue
                    if "Error in event handler for (unknown)" in message: continue # often extension related

                    js_errors.append(f"JS ERROR: {message.splitlines()[0][:200]}") # First line, limit length

            if not js_errors: 
                self._add_finding(category, "JS Console Errors", max_points, max_points, "No significant JS errors.", "PASS")
            else: 
                # Simple penalty: 0 points if any errors. Could be more granular.
                self._add_finding(category, "JS Console Errors", 0, max_points, f"{len(js_errors)} JS error(s).", "FAIL", data=js_errors[:5]) # Show first 5
        except WebDriverException: 
            self._add_finding(category, "JS Console Errors", 0, max_points, "Could not retrieve browser logs.", "WARN")


    # --- Prompt Adherence Checks ---
    def check_prompt_adherence(self):
        category = "Prompt Adherence"
        print(f"\n--- Running Checks: {category} (Viewport: {self.current_viewport_name}) ---")
        
        adherence_config = self.prompt_config.get("adherence_checks", [])
        if not adherence_config:
            self._add_finding(category, "Configuration", 0, 0, "No adherence checks defined in prompt config.", "INFO")
            return

        for check_item in adherence_config:
            check_type = check_item.get("type")
            check_name = check_item.get("name", f"Unnamed {check_type} check")
            points = check_item.get("points", 0)
            check_viewports = check_item.get("viewports") # List of viewports or None for all

            if check_viewports and self.current_viewport_name not in check_viewports:
                # self._add_finding(category, check_name, 0, 0, f"Skipped for viewport {self.current_viewport_name}.", "INFO") # Can be too noisy
                continue # Skip if check is not for the current viewport

            try:
                if check_type == "element_presence":
                    self._check_element_presence(check_item, category, check_name, points)
                elif check_type == "element_order":
                    self._check_element_order(check_item, category, check_name, points)
                elif check_type == "element_count":
                    self._check_element_count(check_item, category, check_name, points)
                elif check_type == "text_content":
                    self._check_text_content(check_item, category, check_name, points)
                elif check_type == "attribute_value":
                    self._check_attribute_value(check_item, category, check_name, points)
                elif check_type == "css_property":
                    self._check_css_property(check_item, category, check_name, points)
                elif check_type == "interaction":
                    self._check_interaction(check_item, category, check_name, points)
                else:
                    self._add_finding(category, check_name, 0, points, f"Unknown check type: {check_type}", "FAIL")
            except Exception as e: # Catch all for a single adherence check failure
                 self._add_finding(category, check_name, 0, points, f"Error during check execution: {e}", "FAIL")


    def _check_element_presence(self, config, cat, name, pts):
        element = self._find_element_by_config(config)
        is_present_and_visible = element and (element.is_displayed() if not isinstance(element, list) else any(el.is_displayed() for el in element))
        
        if config.get("should_not_exist", False):
            if not element : # Or not visible if that's the criteria
                 self._add_finding(cat, name, pts, pts, f"Element '{config.get('selector')}' correctly not present/visible.", "PASS")
            else:
                 self._add_finding(cat, name, 0, pts, f"Element '{config.get('selector')}' unexpectedly present/visible.", "FAIL")
        else:
            if is_present_and_visible:
                self._add_finding(cat, name, pts, pts, f"Element '{config.get('selector')}' present and visible.", "PASS")
            elif element : # Present but not visible
                self._add_finding(cat, name, 0, pts, f"Element '{config.get('selector')}' present but not visible.", "FAIL")
            else: # Not present at all
                self._add_finding(cat, name, 0, pts, f"Element '{config.get('selector')}' not found.", "FAIL")

    def _check_element_count(self, config, cat, name, pts):
        elements = self._find_element_by_config({**config, "find_multiple": True})
        actual_count = len(elements) if elements else 0
        expected_count = config.get("expected_count")
        min_count = config.get("min_count")
        max_count = config.get("max_count")

        if expected_count is not None:
            if actual_count == expected_count:
                self._add_finding(cat, name, pts, pts, f"Count is {actual_count} (expected {expected_count}).", "PASS")
            else:
                self._add_finding(cat, name, 0, pts, f"Count is {actual_count}, expected {expected_count}.", "FAIL")
        elif min_count is not None and max_count is not None:
            if min_count <= actual_count <= max_count:
                self._add_finding(cat, name, pts, pts, f"Count is {actual_count} (between {min_count}-{max_count}).", "PASS")
            else:
                self._add_finding(cat, name, 0, pts, f"Count is {actual_count}, expected between {min_count}-{max_count}.", "FAIL")
        elif min_count is not None:
            if actual_count >= min_count:
                self._add_finding(cat, name, pts, pts, f"Count is {actual_count} (>= {min_count}).", "PASS")
            else:
                self._add_finding(cat, name, 0, pts, f"Count is {actual_count}, expected >= {min_count}.", "FAIL")
        else:
            self._add_finding(cat, name, 0, pts, "Invalid count configuration (expected_count or min/max).", "FAIL")


    def _check_element_order(self, config, cat, name, pts):
        # Expects a list of selectors that should appear in order
        selectors_in_order = config.get("selectors_in_order", [])
        if len(selectors_in_order) < 2:
            self._add_finding(cat, name, 0, pts, "Element order check needs at least 2 selectors.", "FAIL"); return

        elements = []
        for sel_config in selectors_in_order: # sel_config can be a string (selector) or a dict for _find_element_by_config
            if isinstance(sel_config, str): sel_config = {"selector": sel_config}
            el = self._find_element_by_config(sel_config)
            if not el or not el.is_displayed():
                self._add_finding(cat, name, 0, pts, f"Required element for order check ('{sel_config.get('selector')}') not found or not visible.", "FAIL"); return
            elements.append(el)
        
        # Basic DOM order check: compare source index
        # A more robust check would use getBoundingClientRect().top for visual order
        is_ordered = True
        last_el_source_idx = -1
        try:
            # This script gets the order of elements as they appear in the source
            source_indices = self.driver.execute_script(
                "return Array.from(arguments).map(el => Array.from(document.querySelectorAll('*')).indexOf(el));",
                *elements # Pass all elements as arguments to the script
            )
            
            for i in range(len(source_indices)):
                if i > 0 and source_indices[i] < source_indices[i-1] : # Current element appears before previous in source
                     # Allow for elements to be the same if they are nested, e.g. checking parent then child
                    if source_indices[i] == source_indices[i-1] and elements[i-1] == elements[i].parent: # crude parent check
                        continue
                    is_ordered = False; break
        except WebDriverException:
            is_ordered = False # Fallback if script fails

        if is_ordered:
            self._add_finding(cat, name, pts, pts, "Elements are in the expected DOM order.", "PASS")
        else:
            self._add_finding(cat, name, 0, pts, "Elements are NOT in the expected DOM order.", "FAIL", data={"expected_sequence_selectors": [s.get("selector") for s in selectors_in_order]})

    def _check_text_content(self, config, cat, name, pts):
        element = self._find_element_by_config(config)
        if not element or not element.is_displayed():
            self._add_finding(cat, name, 0, pts, f"Element '{config.get('selector')}' not found or not visible for text check.", "FAIL"); return

        actual_text = element.text.strip()
        expected_text = config.get("expected_text", "").strip()
        match_type = config.get("match_type", "exact").lower() # exact, contains, similar
        min_similarity = config.get("min_similarity", 0.85)

        passed = False
        if match_type == "exact":
            passed = actual_text == expected_text
        elif match_type == "contains":
            passed = expected_text in actual_text
        elif match_type == "similar":
            passed = string_similarity(actual_text.lower(), expected_text.lower()) >= min_similarity
        
        if passed:
            self._add_finding(cat, name, pts, pts, f"Text content matches (type: {match_type}). Actual: '{actual_text[:50]}...'", "PASS")
        else:
            self._add_finding(cat, name, 0, pts, f"Text mismatch. Expected ('{expected_text[:50]}...'), Actual ('{actual_text[:50]}...'). Type: {match_type}.", "FAIL", data={"expected": expected_text, "actual": actual_text})

    def _check_attribute_value(self, config, cat, name, pts):
        element = self._find_element_by_config(config)
        if not element: # Visibility might not matter for attribute check
            self._add_finding(cat, name, 0, pts, f"Element '{config.get('selector')}' not found for attribute check.", "FAIL"); return

        attribute_name = config.get("attribute_name")
        expected_value = config.get("expected_value") # Can be string or boolean for boolean attributes
        
        actual_value = element.get_attribute(attribute_name)

        passed = False
        if isinstance(expected_value, bool): # Check for presence of boolean attribute
            passed = (actual_value is not None) == expected_value # e.g. if expected true, actual must not be None
        elif expected_value is None: # Check for attribute absence
            passed = actual_value is None
        else: # String comparison
            # For class, we might want to check if expected_value is *one of* the classes
            if attribute_name.lower() == "class" and config.get("class_contains", False):
                 passed = expected_value in (actual_value.split() if actual_value else [])
            else:
                 passed = str(actual_value) == str(expected_value)
        
        if passed:
            self._add_finding(cat, name, pts, pts, f"Attribute '{attribute_name}' value matches.", "PASS")
        else:
            self._add_finding(cat, name, 0, pts, f"Attribute '{attribute_name}'. Expected: '{expected_value}', Actual: '{actual_value}'.", "FAIL")

    def _check_css_property(self, config, cat, name, pts):
        element = self._find_element_by_config(config)
        if not element or not element.is_displayed():
            self._add_finding(cat, name, 0, pts, f"Element '{config.get('selector')}' not found/visible for CSS check.", "FAIL"); return

        property_name = config.get("property_name")
        expected_value_str = config.get("expected_value")
        
        try:
            actual_value_str = element.value_of_css_property(property_name)
        except WebDriverException as e_css:
            self._add_finding(cat, name, 0, pts, f"Error getting CSS property '{property_name}': {e_css}", "FAIL"); return


        passed = False
        # Special handling for colors
        if "color" in property_name.lower():
            actual_rgb = parse_color_string_to_rgb_tuple(actual_value_str) # Returns (r,g,b,a) or None
            expected_rgb = parse_color_string_to_rgb_tuple(expected_value_str)
            if actual_rgb and expected_rgb:
                 # Compare only RGB, ignore alpha for direct color match unless specified
                passed = actual_rgb[:3] == expected_rgb[:3]
            elif actual_rgb is None and expected_rgb is None and actual_value_str == expected_value_str : # e.g. 'transparent' vs 'transparent'
                passed = True

        else: # Generic string comparison
            # Normalize values if possible (e.g., font weights 'bold' vs '700') - complex
            # For now, direct string comparison after stripping
            passed = actual_value_str.strip() == expected_value_str.strip()
        
        if passed:
            self._add_finding(cat, name, pts, pts, f"CSS '{property_name}' matches '{expected_value_str}'.", "PASS")
        else:
            self._add_finding(cat, name, 0, pts, f"CSS '{property_name}'. Expected: '{expected_value_str}', Actual: '{actual_value_str}'.", "FAIL")

    def _check_interaction(self, config, cat, name, pts):
        trigger_element = self._find_element_by_config(config.get("trigger_element"))
        if not trigger_element or not trigger_element.is_displayed():
            self._add_finding(cat, name, 0, pts, "Interaction trigger element not found/visible.", "FAIL"); return

        action = config.get("action", "click").lower()
        
        try:
            if action == "click":
                trigger_element.click()
            elif action == "hover": # Requires ActionChains
                webdriver.ActionChains(self.driver).move_to_element(trigger_element).perform()
            # Add other actions like 'type', 'submit' as needed
            else:
                self._add_finding(cat, name, 0, pts, f"Unsupported interaction action: {action}", "FAIL"); return
            
            time.sleep(config.get("wait_after_action", 0.5)) # Wait for potential async updates

            # Verify outcome
            outcome_config = config.get("outcome")
            if not outcome_config or not outcome_config.get("type"):
                 self._add_finding(cat, name, 0, pts, "No outcome defined for interaction.", "FAIL"); return

            outcome_type = outcome_config["type"]
            # Re-use existing check logic for outcomes for simplicity
            # We wrap the outcome_config to look like a primary check_item for the sub-checker
            # The points for the sub-check within interaction are 0, main points are for interaction pass/fail
            temp_outcome_config = {**outcome_config, "name": f"Outcome of '{name}'", "points": 0} # Pass 0 points to sub-checker

            outcome_passed = False
            if outcome_type == "element_presence": # Check if another element's presence/visibility changes
                # This needs to be slightly different as it's validating state *after* action
                target_el_conf = {
                    "selector": outcome_config.get("selector"), 
                    "selector_type": outcome_config.get("selector_type"),
                    "should_not_exist": outcome_config.get("should_not_exist", False) # Important for visibility toggles
                }
                target_el = self._find_element_by_config(target_el_conf)
                
                is_visible_as_expected = False
                if outcome_config.get("expected_to_be_visible", True): # Default to expecting visibility
                    is_visible_as_expected = target_el and target_el.is_displayed()
                else: # Expecting it to be hidden or not present
                    is_visible_as_expected = not target_el or not target_el.is_displayed()

                if target_el_conf.get("should_not_exist"): # If it should NOT exist
                    outcome_passed = not target_el
                elif outcome_config.get("expected_to_be_visible", True):
                    outcome_passed = target_el and target_el.is_displayed()
                else: # Expected to be hidden
                    outcome_passed = not target_el or not target_el.is_displayed()

            elif outcome_type == "attribute_value":
                # Need to capture the result of this sub-check
                # This requires _check_attribute_value to return a boolean or status
                # For now, let's simplify: find the element and check its attribute directly
                target_el = self._find_element_by_config(outcome_config)
                if target_el:
                    attr_name = outcome_config.get("attribute_name")
                    expected_attr_val = outcome_config.get("expected_value")
                    actual_attr_val = target_el.get_attribute(attr_name)
                    if isinstance(expected_attr_val, bool):
                        outcome_passed = (actual_attr_val is not None) == expected_attr_val
                    else:
                        outcome_passed = str(actual_attr_val) == str(expected_attr_val)
            # Add more outcome types: text_content_change, css_property_change etc.

            if outcome_passed:
                self._add_finding(cat, name, pts, pts, f"Interaction '{action}' successful, outcome '{outcome_type}' verified.", "PASS")
            else:
                self._add_finding(cat, name, 0, pts, f"Interaction '{action}' outcome '{outcome_type}' FAILED verification.", "FAIL", data=outcome_config)

        except ElementNotInteractableException:
            self._add_finding(cat, name, 0, pts, f"Trigger element for '{action}' not interactable.", "FAIL")
        except Exception as e_interact:
            self._add_finding(cat, name, 0, pts, f"Error during interaction '{action}': {e_interact}", "FAIL")


    def run_all_checks(self):
        print(f"--- Starting UI Benchmark Analysis for: {self.file_path.name} ---")
        print(f"--- Prompt Config: {self.prompt_config_path.name} ---")
        print(f"--- Prompt Description: {self.prompt_config.get('prompt_description', 'N/A')} ---")

        # Store initial scores state to reset for each viewport if necessary for adherence
        # For now, technical quality checks are viewport-agnostic in scoring (first one counts)
        # Adherence checks can be viewport specific.

        technical_checks_done_for_viewport = {}

        for vp_name, (vp_width, vp_height) in self.viewports.items():
            try:
                self._load_page_at_viewport(vp_name, vp_width, vp_height)

                # Run Technical Quality checks (most run once, some per viewport like horiz_scroll)
                if not technical_checks_done_for_viewport.get("html_structure"):
                    self.check_html_structure_semantics()
                    technical_checks_done_for_viewport["html_structure"] = True
                if not technical_checks_done_for_viewport.get("axe"):
                    self.check_accessibility_axe() # Axe can be viewport dependent
                    technical_checks_done_for_viewport["axe"] = True # Or run per viewport if desired
                if not technical_checks_done_for_viewport.get("css_quality"):
                     self.check_css_quality_and_responsiveness()
                     technical_checks_done_for_viewport["css_quality"] = True
                
                # These are good to run per viewport
                self.check_rendered_color_contrast() # Contrast can change with layout
                self.check_responsiveness_no_horizontal_scroll() # Definitely per viewport

                if not technical_checks_done_for_viewport.get("js_health"):
                    self.check_javascript_health() # Usually viewport agnostic unless JS behaves differently
                    technical_checks_done_for_viewport["js_health"] = True

                # Lighthouse only for 'desktop' or 'mobile' named viewports by default
                # and only if not already run for that preset
                lh_preset_key = vp_name.lower() # desktop or mobile
                if lh_preset_key in ["desktop", "mobile"] and self.lighthouse_path and not technical_checks_done_for_viewport.get(f"lighthouse_{lh_preset_key}"):
                    self.check_performance_lighthouse() # This method now handles all LH cats
                    technical_checks_done_for_viewport[f"lighthouse_{lh_preset_key}"] = True
                
                # Run Prompt Adherence checks FOR THIS VIEWPORT
                self.check_prompt_adherence()

            except (TimeoutException, WebDriverException) as e_vp:
                print(f"  CRITICAL ERROR for viewport {vp_name}: {type(e_vp).__name__} - {e_vp}. Some checks might be skipped.")
                self._add_finding("Viewport Processing", vp_name, 0,0, f"Failed viewport processing: {type(e_vp).__name__}.", "FAIL")
                continue # Try next viewport
        
        self._stop_local_server()
        print(f"\n--- Analysis Finished for: {self.file_path.name} ---")


    def generate_report(self):
        # Consolidate scores
        final_report_data = {
            "file_analyzed": str(self.file_path),
            "prompt_config_file": str(self.prompt_config_path),
            "prompt_description": self.prompt_config.get("prompt_description", "N/A"),
            "page_title": self.page_title,
            "analysis_timestamp": self.run_timestamp,
            "technical_quality": {"earned": 0, "max": TOTAL_TECHNICAL_QUALITY_MAX_POINTS, "categories": {}},
            "prompt_adherence": {"earned": 0, "max": self.current_prompt_total_adherence_max_points, "categories": {}},
            "overall_score_earned": 0,
            "overall_score_max": 0,
            "overall_percentage": 0,
            "detailed_findings_by_category": {}
        }

        text_report_lines = [
            f"--- UI Benchmark Report ---",
            f"HTML File: {self.file_path.name}",
            f"Prompt Config: {self.prompt_config_path.name}",
            f"Prompt: {final_report_data['prompt_description']}",
            f"Page Title: {self.page_title}",
            f"Analyzed: {self.run_timestamp}\n"
        ]

        # Separate scores into technical and adherence
        for cat_name, cat_data in self.scores.items():
            final_report_data["detailed_findings_by_category"][cat_name] = cat_data # Store all details
            if cat_name in TECHNICAL_QUALITY_MAX_POINTS:
                final_report_data["technical_quality"]["categories"][cat_name] = cat_data
                final_report_data["technical_quality"]["earned"] += cat_data['earned']
            elif cat_name == "Prompt Adherence":
                final_report_data["prompt_adherence"]["categories"][cat_name] = cat_data # Should only be one "Prompt Adherence"
                final_report_data["prompt_adherence"]["earned"] += cat_data['earned']
            # Other categories like "Page Load", "Viewport Processing" are for info, not direct scoring
            # unless we assign them points.

        # Calculate overall scores
        final_report_data["overall_score_earned"] = final_report_data["technical_quality"]["earned"] + final_report_data["prompt_adherence"]["earned"]
        final_report_data["overall_score_max"] = final_report_data["technical_quality"]["max"] + final_report_data["prompt_adherence"]["max"]
        
        if final_report_data["overall_score_max"] > 0:
            final_report_data["overall_percentage"] = round(
                (final_report_data["overall_score_earned"] / final_report_data["overall_score_max"]) * 100, 2
            )

        # Build Text Report
        text_report_lines.append("--- Technical Quality Summary ---")
        text_report_lines.append(f"Score: {final_report_data['technical_quality']['earned']:.2f} / {final_report_data['technical_quality']['max']:.0f}")
        for cat_name, cat_data in final_report_data["technical_quality"]["categories"].items():
            text_report_lines.append(f"  {cat_name}: {cat_data['earned']:.2f} / {cat_data['max']:.0f}")
        
        text_report_lines.append("\n--- Prompt Adherence Summary ---")
        text_report_lines.append(f"Score: {final_report_data['prompt_adherence']['earned']:.2f} / {final_report_data['prompt_adherence']['max']:.0f}")
        # Adherence details are usually under one category 'Prompt Adherence', so list its checks
        if "Prompt Adherence" in final_report_data["prompt_adherence"]["categories"]:
             for detail in final_report_data["prompt_adherence"]["categories"]["Prompt Adherence"].get("details",[]):
                status_icon = "✅" if detail['status'] == "PASS" else "❌" if detail['status'] == "FAIL" else "⚠️"
                points_str = f"({detail['points_earned']:.1f}/{detail['max_points']:.0f} pts)" if detail['max_points'] > 0 else ""
                text_report_lines.append(f"  {status_icon} [{detail['viewport']}] {detail['check']}: {detail['message']} {points_str}")


        text_report_lines.extend([
            "\n--- Overall Score ---",
            f"Total Earned: {final_report_data['overall_score_earned']:.2f}",
            f"Total Max Possible: {final_report_data['overall_score_max']:.0f}",
            f"Overall Percentage: {final_report_data['overall_percentage']:.2f}%",
            f"\nDetailed JSON report: {self.output_dir / 'full_benchmark_report.json'}",
            f"Summary text report: {self.output_dir / 'summary_benchmark_report.txt'}",
            f"Screenshots in: {self.screenshots_dir.name}",
            f"Lighthouse/Axe reports in: {self.output_dir.name}"
        ])
        
        with open(self.output_dir / "full_benchmark_report.json", "w", encoding='utf-8') as f:
            json.dump(final_report_data, f, indent=2)
        with open(self.output_dir / "summary_benchmark_report.txt", "w", encoding='utf-8') as f:
            f.write("\n".join(text_report_lines))
            
        print("\n" + "\n".join(text_report_lines[-6:])) # Print last few lines of summary
        return final_report_data

    def close(self):
        self._stop_local_server()
        if hasattr(self, 'driver') and self.driver:
            self.driver.quit()
            print("\nWebDriver closed.")

# --- Main Execution ---
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ui_benchmark_analyzer.py <path_to_html_file> <path_to_prompt_config_json> [output_base_dir]")
        # Create dummy files for testing if not provided
        # (You would replace this with actual test case setup)
        EXAMPLE_HTML_NAME = "example_prompt.html"
        EXAMPLE_CONFIG_NAME = "example_prompt_config.json"

        if not Path(EXAMPLE_HTML_NAME).exists():
            dummy_html_content = """
            <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Comprehensive Test Page</title><style>body{font-family:sans-serif; background-color: white; color: #333;} nav{background-color: #f0f0f0; padding: 10px;} #hero{padding:20px; background-color: #e0e0e0;} .gallery img{border:1px solid #ccc; margin:5px;} .bad-contrast{color: #777; background-color: #eee;} footer{text-align:center; padding:10px; background-color:#333; color:white;}</style></head>
            <body><nav data-testid="navbar"><a href="#">Home</a></nav><section id="hero" data-testid="hero-section"><h1>Hero Title</h1><p>Hero text here.</p><button data-testid="hero-cta">Click Me</button></section>
            <section class="gallery" data-testid="image-gallery"><h2 data-testid="gallery-title">Gallery</h2><img src="https://via.placeholder.com/100?text=1" alt="Image 1"><img src="https://via.placeholder.com/100?text=2" alt="Image 2"></section>
            <p class="bad-contrast" data-testid="lowcontrast-text">Low contrast text.</p>
            <form data-testid="test-form"><label for="name">Name:</label><input type="text" id="name" name="name"><button type="submit">Submit</button></form>
            <footer data-testid="footer"><p>© 2024</p></footer>
            <script>console.error("A minor JS test error!");</script></body></html>"""
            with open(EXAMPLE_HTML_NAME, "w", encoding="utf-8") as f: f.write(dummy_html_content)
            print(f"Created dummy HTML: {EXAMPLE_HTML_NAME}")

        if not Path(EXAMPLE_CONFIG_NAME).exists():
            dummy_config_content = {
                "prompt_id": "example_001",
                "prompt_description": "A simple page with navbar, hero, gallery, and footer.",
                "adherence_checks": [
                    {"type": "element_presence", "name": "Navbar Presence", "selector": "nav[data-testid='navbar']", "points": 5},
                    {"type": "element_presence", "name": "Hero Section Presence", "selector": "[data-testid='hero-section']", "points": 5},
                    {"type": "element_presence", "name": "Gallery Presence", "selector": "[data-testid='image-gallery']", "points": 5},
                    {"type": "element_presence", "name": "Footer Presence", "selector": "footer[data-testid='footer']", "points": 5},
                    {"type": "element_order", "name": "Basic Layout Order", "selectors_in_order": ["nav[data-testid='navbar']", "[data-testid='hero-section']", "[data-testid='image-gallery']", "footer[data-testid='footer']"], "points": 10},
                    {"type": "element_count", "name": "Gallery Image Count", "selector": "[data-testid='image-gallery'] img", "expected_count": 2, "points": 3},
                    {"type": "text_content", "name": "Hero Title Text", "selector": "[data-testid='hero-section'] h1", "expected_text": "Hero Title", "match_type":"exact", "points": 2},
                    {"type": "attribute_value", "name": "Hero CTA Test ID", "selector": "[data-testid='hero-section'] button", "attribute_name": "data-testid", "expected_value": "hero-cta", "points": 2},
                    {"type": "css_property", "name": "Footer Background Color", "selector": "footer[data-testid='footer']", "property_name": "background-color", "expected_value": "rgb(51, 51, 51)", "points": 3}
                ]
            } # Total adherence points: 40
            with open(EXAMPLE_CONFIG_NAME, "w", encoding="utf-8") as f: json.dump(dummy_config_content, f, indent=2)
            print(f"Created dummy JSON config: {EXAMPLE_CONFIG_NAME}")
        print("\nTo run with dummies (if in current dir):")
        print(f"python {sys.argv[0]} {EXAMPLE_HTML_NAME} {EXAMPLE_CONFIG_NAME}\n")
        sys.exit(1)
    
    html_file_arg = sys.argv[1]
    prompt_config_arg = sys.argv[2]
    output_dir_arg = sys.argv[3] if len(sys.argv) > 3 else "ui_benchmark_runs"
    
    analyzer_instance = None
    try:
        analyzer_instance = UIBenchmarkAnalyzer(html_file_arg, prompt_config_arg, output_base_dir=output_dir_arg)
        analyzer_instance.run_all_checks()
        analyzer_instance.generate_report()
    except FileNotFoundError as e_fnf: print(f"Error: Input file not found. {e_fnf}")
    except ValueError as e_val: print(f"Error: Configuration or value issue. {e_val}") # For JSON errors etc.
    except WebDriverException as e_wd_main: 
        print(f"Critical WebDriver error: {e_wd_main}")
        print("Ensure Chrome browser is installed and a compatible ChromeDriver is in your PATH or installable by webdriver-manager.")
    except Exception as e_main: 
        import traceback
        print(f"An unexpected critical error occurred: {e_main}")
        traceback.print_exc()
    finally:
        if analyzer_instance: 
            analyzer_instance.close()