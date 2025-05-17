#!/usr/bin/env python3

import sys
import os
import re
import json
import subprocess
import time
import shutil
from urllib.parse import urlparse, unquote, quote # Added quote for file names in URL
from pathlib import Path
import threading
import socket
import http.server
import socketserver
from functools import partial # For http.server handler

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, StaleElementReferenceException
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from webdriver_manager.chrome import ChromeDriverManager
import wcag_contrast_ratio as contrast_lib
from axe_selenium_python import Axe

# --- Configuration ---
DEFAULT_VIEWPORTS = {
    "mobile": (375, 667),
    "desktop": (1920, 1080)
}
LIGHTHOUSE_CATEGORIES = ['performance', 'accessibility', 'best-practices', 'seo']

# --- Helper Functions ---

def parse_color_string_to_rgb_tuple(color_str):
    if not color_str: return None
    color_str = color_str.lower().strip()
    if color_str == 'transparent': return None
    match_rgba = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", color_str)
    if match_rgba:
        r, g, b, a_str = match_rgba.groups()
        a = float(a_str) if a_str else 1.0
        if a == 0: return None
        return (int(r), int(g), int(b))
    match_rgb = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color_str)
    if match_rgb:
        return tuple(int(c) for c in match_rgb.groups())
    if color_str.startswith('#'):
        hex_color = color_str[1:]
        if len(hex_color) == 3: return tuple(int(c * 2, 16) for c in hex_color)
        if len(hex_color) == 6: return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        if len(hex_color) == 8: return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    named = {"white": (255,255,255), "black": (0,0,0), "red":(255,0,0), "green":(0,128,0), "blue":(0,0,255)}
    return named.get(color_str)

def normalize_rgb(rgb_0_255):
    if not rgb_0_255 or len(rgb_0_255) != 3: return None
    return tuple(c / 255.0 for c in rgb_0_255)

def blend_colors(fg_rgb_alpha, bg_rgb):
    fg_r, fg_g, fg_b, alpha = fg_rgb_alpha
    bg_r, bg_g, bg_b = bg_rgb
    r = int(fg_r * alpha + bg_r * (1 - alpha))
    g = int(fg_g * alpha + bg_g * (1 - alpha))
    b = int(fg_b * alpha + bg_b * (1 - alpha))
    return (r, g, b)

def get_effective_background_rgb(element, driver):
    current_el = element
    path_colors = [] 
    while current_el:
        try:
            bg_color_str = driver.execute_script("return arguments[0] && getComputedStyle(arguments[0]).backgroundColor;", current_el)
            parsed_rgba = None
            if bg_color_str and bg_color_str.lower().strip() != 'transparent':
                match_rgba_val = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", bg_color_str.lower().strip())
                if match_rgba_val:
                    r_val, g_val, b_val, a_val = map(float, match_rgba_val.groups())
                    parsed_rgba = (int(r_val), int(g_val), int(b_val), a_val)
                else: 
                    rgb_only = parse_color_string_to_rgb_tuple(bg_color_str)
                    if rgb_only: parsed_rgba = (*rgb_only, 1.0)
            if parsed_rgba:
                if parsed_rgba[3] == 1.0: 
                    effective_color = (parsed_rgba[0], parsed_rgba[1], parsed_rgba[2])
                    for layer_rgba in reversed(path_colors): effective_color = blend_colors(layer_rgba, effective_color)
                    return effective_color
                elif parsed_rgba[3] > 0: path_colors.append(parsed_rgba)
            if not hasattr(current_el, 'tag_name') or current_el.tag_name.lower() == 'html': break
            parent = driver.execute_script("return arguments[0].parentElement;", current_el)
            if not parent: break
            current_el = parent
        except (WebDriverException, StaleElementReferenceException): break
    effective_color = (255, 255, 255)
    for layer_rgba in reversed(path_colors): effective_color = blend_colors(layer_rgba, effective_color)
    return effective_color

def get_element_desc(element):
    try:
        tag = element.tag_name
        el_id = element.get_attribute('id')
        el_class = element.get_attribute('class')
        desc = f"<{tag}"
        if el_id: desc += f" id='{el_id}'"
        if el_class: desc += f" class='{el_class[:30]}{'...' if len(el_class)>30 else ''}'"
        desc += ">"
        return desc
    except StaleElementReferenceException: return "<stale_element>"
    except WebDriverException: return "<invalid_element_for_desc>"

class UIBenchmarkAnalyzer:
    def __init__(self, html_file_path, output_base_dir="ui_benchmark_reports", viewports=None):
        self.file_path = Path(html_file_path).resolve()
        if not self.file_path.is_file():
            raise FileNotFoundError(f"HTML file not found: {self.file_path}")

        self.selenium_uri = self.file_path.as_uri() # For Selenium to load the page
        self.run_timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.output_dir = Path(output_base_dir) / f"{self.file_path.stem}_{self.run_timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.screenshots_dir = self.output_dir / "screenshots"
        self.screenshots_dir.mkdir(exist_ok=True)

        self.viewports = viewports or DEFAULT_VIEWPORTS
        self.scores = {} 
        self.page_title = "N/A"
        self.lighthouse_path = shutil.which("lighthouse")
        if not self.lighthouse_path:
            print("WARNING: Lighthouse CLI not found in PATH. Performance checks will be skipped.")

        # For local HTTP server
        self.http_server = None
        self.http_thread = None
        self.server_port = None
        self.local_server_url_for_lighthouse = None


        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu'); options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        options.add_argument('--log-level=3') 
        
        caps = DesiredCapabilities.CHROME.copy()
        caps['goog:loggingPrefs'] = {'browser': 'ALL', 'performance': 'ALL'}
        options.set_capability('goog:loggingPrefs', {'browser': 'ALL', 'performance': 'ALL'})

        try:
            try:
                driver_path = ChromeDriverManager().install()
                service = ChromeService(executable_path=driver_path)
            except Exception:
                service = ChromeService() 
            self.driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            print(f"Fatal WebDriver Error during setup: {e}")
            sys.exit(1)
        
        self.current_viewport_name = "initial"

    def _start_local_server(self):
        if self.http_server: # Already running
            return self.local_server_url_for_lighthouse

        # Find an available port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('localhost', 0))
        self.server_port = sock.getsockname()[1]
        sock.close()

        serve_directory = str(self.file_path.parent)
        # Handler needs to be created with the directory
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=serve_directory)
        
        # Allow address reuse
        socketserver.TCPServer.allow_reuse_address = True
        self.http_server = socketserver.TCPServer(("localhost", self.server_port), handler)
        
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        
        # URL-encode the filename part
        file_name_encoded = quote(self.file_path.name)
        self.local_server_url_for_lighthouse = f"http://localhost:{self.server_port}/{file_name_encoded}"
        print(f"  Local HTTP server started for Lighthouse at: {self.local_server_url_for_lighthouse}")
        time.sleep(0.5) # Give server a moment to start
        return self.local_server_url_for_lighthouse

    def _stop_local_server(self):
        if self.http_server:
            print("  Stopping local HTTP server for Lighthouse...")
            self.http_server.shutdown()
            self.http_server.server_close()
            if self.http_thread and self.http_thread.is_alive():
                 self.http_thread.join(timeout=2) # Wait for thread to finish
            self.http_server = None
            self.http_thread = None
            self.local_server_url_for_lighthouse = None
            print("  Local HTTP server stopped.")


    def _add_finding(self, category, check_name, points_earned, max_points, message, status="INFO", data=None):
        if category not in self.scores: self.scores[category] = {'earned': 0, 'max': 0, 'details': []}
        self.scores[category]['earned'] += points_earned
        self.scores[category]['max'] += max_points
        finding_detail = {"check": check_name, "status": status, "message": message,
                          "points_earned": points_earned, "max_points": max_points,
                          "viewport": self.current_viewport_name}
        if data: finding_detail["data"] = data
        self.scores[category]['details'].append(finding_detail)
        if status == "FAIL": print(f"  [FAIL] {category} - {check_name}: {message}")
        elif status == "WARN": print(f"  [WARN] {category} - {check_name}: {message}")

    def _load_page_at_viewport(self, viewport_name, width, height):
        self.current_viewport_name = viewport_name
        print(f"\nSetting viewport: {viewport_name} ({width}x{height}) and loading page...")
        self.driver.set_window_size(width, height)
        try:
            self.driver.get(self.selenium_uri) # Use file URI for Selenium
            WebDriverWait(self.driver, 15).until(lambda d: d.execute_script('return document.readyState') == 'complete')
            self.page_title = self.driver.title or "No Title"
            time.sleep(2.0) 
            screenshot_path = self.screenshots_dir / f"{viewport_name}.png"
            self.driver.save_screenshot(str(screenshot_path))
            print(f"  Screenshot saved to {screenshot_path}")
        except TimeoutException:
            self._add_finding("Page Load", "Page Completeness", 0, 0, f"Page timed out loading at {viewport_name} viewport.", "FAIL")
            raise 
        except WebDriverException as e:
            self._add_finding("Page Load", "Driver Operation", 0, 0, f"WebDriver error at {viewport_name}: {e}", "FAIL")
            raise

    def check_html_structure_semantics(self):
        category = "HTML Structure & Semantics"
        max_cat_points = {"HTML Lang": 2, "Page Title": 1, "Semantic Tags": 7, 
                          "Heading Presence": 1, "H1 Count": 2, "Heading Order Logic": 2, 
                          "Image Alts": 2, "Form Labels": 2}
        print(f"\nRunning Checks: {category}")
        try:
            html_tag = self.driver.find_element(By.TAG_NAME, "html")
            lang = html_tag.get_attribute("lang")
            if lang and lang.strip(): self._add_finding(category, "HTML Lang", max_cat_points["HTML Lang"], max_cat_points["HTML Lang"], f"HTML 'lang' attribute is set: '{lang}'.", "PASS")
            else: self._add_finding(category, "HTML Lang", 0, max_cat_points["HTML Lang"], "HTML 'lang' attribute is missing or empty.", "FAIL")
        except NoSuchElementException: self._add_finding(category, "HTML Lang", 0, max_cat_points["HTML Lang"], "<html> tag not found.", "FAIL")
        if self.page_title and self.page_title != "No Title" and len(self.page_title.strip()) > 0: self._add_finding(category, "Page Title", max_cat_points["Page Title"], max_cat_points["Page Title"], f"Page has a title: '{self.page_title}'.", "PASS")
        else: self._add_finding(category, "Page Title", 0, max_cat_points["Page Title"], "Page title is missing or empty.", "WARN")
        semantic_tags_to_check = ["header", "nav", "main", "footer", "aside", "article", "section"]
        for tag_name in semantic_tags_to_check:
            try:
                elements = self.driver.find_elements(By.TAG_NAME, tag_name)
                visible_elements = [el for el in elements if el.is_displayed()]
                if visible_elements: self._add_finding(category, f"Semantic <{tag_name}>", 1, 1, f"<{tag_name}> element(s) found and at least one is visible.", "PASS")
                elif elements: self._add_finding(category, f"Semantic <{tag_name}>", 0.5, 1, f"<{tag_name}> element(s) found but none are visible. Verify intentional.", "WARN")
                else:
                    status = "INFO" if tag_name in ["article", "section", "aside"] else "WARN"
                    self._add_finding(category, f"Semantic <{tag_name}>", 0, 1, f"No <{tag_name}> element found. Consider for page structure.", status)
            except WebDriverException: self._add_finding(category, f"Semantic <{tag_name}>", 0, 1, f"Error checking for <{tag_name}>.", "FAIL")
        try:
            headings = self.driver.find_elements(By.CSS_SELECTOR, "h1, h2, h3, h4, h5, h6")
            sorted_visible_headings = sorted([h for h in headings if h.is_displayed()], key=lambda x: (x.location['y'], x.location['x']))
            if not sorted_visible_headings: self._add_finding(category, "Heading Presence", 0, max_cat_points["Heading Presence"], "No visible headings (<h1>-<h6>) found.", "WARN")
            else:
                self._add_finding(category, "Heading Presence", max_cat_points["Heading Presence"], max_cat_points["Heading Presence"], f"{len(sorted_visible_headings)} visible headings found.", "PASS")
                h1s = [h for h in sorted_visible_headings if h.tag_name == 'h1']
                if len(h1s) == 1: self._add_finding(category, "H1 Count", max_cat_points["H1 Count"], max_cat_points["H1 Count"], "Exactly one visible <h1> found.", "PASS")
                elif len(h1s) == 0: self._add_finding(category, "H1 Count", 0, max_cat_points["H1 Count"], "No visible <h1> found.", "FAIL")
                else: self._add_finding(category, "H1 Count", 0, max_cat_points["H1 Count"], f"{len(h1s)} visible <h1>s found. Only one is recommended.", "FAIL")
                last_level = 0; order_ok_overall = True; first_heading_processed = False
                for i, h_el in enumerate(sorted_visible_headings):
                    try:
                        current_level = int(h_el.tag_name[1])
                        h_text_snippet = h_el.text.strip()[:30] if hasattr(h_el, 'text') else ""
                    except StaleElementReferenceException: continue
                    if not first_heading_processed:
                        if current_level != 1 and any(vh.tag_name == 'h1' for vh in sorted_visible_headings):
                            self._add_finding(category, "Heading Order Start", 0.5, 1, f"First visible heading is <h{current_level}> ('{h_text_snippet}...'), not <h1>. Consider visual flow.", "WARN"); order_ok_overall = False 
                        last_level = current_level; first_heading_processed = True; continue
                    if current_level > last_level + 1:
                        self._add_finding(category, "Heading Order Skipped", 0, 1, f"Skipped heading level: <h{last_level}> followed by <h{current_level}> ('{h_text_snippet}...'). Element: {get_element_desc(h_el)}", "WARN"); order_ok_overall = False
                    last_level = current_level
                if sorted_visible_headings and order_ok_overall: self._add_finding(category, "Heading Order Logic", max_cat_points["Heading Order Logic"], max_cat_points["Heading Order Logic"], "Visible headings generally follow logical order.", "PASS")
                elif sorted_visible_headings: self._add_finding(category, "Heading Order Logic", 0, max_cat_points["Heading Order Logic"], "One or more heading order issues detected.", "FAIL")
        except WebDriverException as e_head: self._add_finding(category, "Headings Processing", 0, sum(v for k,v in max_cat_points.items() if "Heading" in k or "H1" in k), f"Error processing headings: {e_head}", "FAIL")
        try:
            images = self.driver.find_elements(By.TAG_NAME, "img")
            visible_images = [img for img in images if img.is_displayed()]
            if not visible_images: self._add_finding(category, "Image Alts", max_cat_points["Image Alts"], max_cat_points["Image Alts"], "No visible <img> elements found.", "INFO")
            else:
                missing_alt_count = 0; presentational_alt_count = 0
                for img in visible_images:
                    try:
                        alt = img.get_attribute("alt")
                        if alt is None: missing_alt_count += 1
                        elif alt.strip() == "": presentational_alt_count +=1
                    except StaleElementReferenceException: continue
                if missing_alt_count > 0: self._add_finding(category, "Image Alts", 0, max_cat_points["Image Alts"], f"{missing_alt_count} visible images are missing 'alt' attributes entirely.", "FAIL")
                else: self._add_finding(category, "Image Alts", max_cat_points["Image Alts"], max_cat_points["Image Alts"], f"All {len(visible_images)} visible images have 'alt' attributes ({presentational_alt_count} presentational).", "PASS")
        except WebDriverException: self._add_finding(category, "Image Alts", 0, max_cat_points["Image Alts"], "Error processing images for alts.", "FAIL")
        try:
            inputs_selector = 'input:not([type="hidden"]):not([type="submit"]):not([type="reset"]):not([type="button"]):not([type="image"]), select, textarea'
            form_inputs = [inp for inp in self.driver.find_elements(By.CSS_SELECTOR, inputs_selector) if inp.is_displayed()]
            if not form_inputs: self._add_finding(category, "Form Labels", max_cat_points["Form Labels"], max_cat_points["Form Labels"], "No visible form inputs requiring labels found.", "INFO")
            else:
                unlabeled_inputs = 0
                for inp in form_inputs:
                    try:
                        inp_id = inp.get_attribute("id"); has_label = False
                        if inp_id:
                            try: self.driver.find_element(By.CSS_SELECTOR, f"label[for='{inp_id}']"); has_label = True
                            except NoSuchElementException: pass
                        if not has_label:
                            try: inp.find_element(By.XPATH, "ancestor::label"); has_label = True
                            except NoSuchElementException: pass
                        if not has_label and (inp.get_attribute("aria-label") or inp.get_attribute("aria-labelledby")): has_label = True
                        if not has_label: unlabeled_inputs += 1
                    except StaleElementReferenceException: continue
                if unlabeled_inputs > 0: self._add_finding(category, "Form Labels", 0, max_cat_points["Form Labels"], f"{unlabeled_inputs}/{len(form_inputs)} visible form inputs appear to be missing labels.", "FAIL", data={"unlabeled_count": unlabeled_inputs, "total_inputs": len(form_inputs)})
                else: self._add_finding(category, "Form Labels", max_cat_points["Form Labels"], max_cat_points["Form Labels"], "All visible form inputs appear to have associated labels.", "PASS")
        except WebDriverException: self._add_finding(category, "Form Labels", 0, max_cat_points["Form Labels"], "Error processing form inputs for labels.", "FAIL")

    def check_accessibility_axe(self):
        category = "Accessibility (Axe-core)"; max_axe_points = 20
        print(f"\nRunning Checks: {category}")
        try:
            axe = Axe(self.driver); axe.inject(); results = axe.run() 
            report_path_obj = self.output_dir / f"axe_report_{self.current_viewport_name}.json"
            axe.write_results(results, str(report_path_obj)) 
            self._add_finding(category, "Axe Report Generated", 0, 0, f"Axe-core report saved to {report_path_obj.name}", "INFO")
            violations = results.get("violations", []); passes_count = len(results.get("passes", [])); incomplete_count = len(results.get("incomplete", []))
            if not violations: self._add_finding(category, "Axe Violations", max_axe_points, max_axe_points, f"No accessibility violations by Axe-core. {passes_count} passed. {incomplete_count} incomplete.", "PASS")
            else:
                violation_summary = []; penalty = 0.0; critical_issues = 0; serious_issues = 0
                for v in violations:
                    v_desc = f"{v['id']}: {v['help']} ({len(v['nodes'])} node(s)). Impact: {v['impact']}"
                    violation_summary.append(v_desc)
                    if v['impact'] == 'critical': penalty += 2.0; critical_issues+=1
                    elif v['impact'] == 'serious': penalty += 1.0; serious_issues+=1
                    else: penalty += 0.5
                earned_points = max(0, int(max_axe_points - penalty))
                status = "FAIL" if critical_issues > 0 or serious_issues > 2 else "WARN"
                self._add_finding(category, "Axe Violations", earned_points, max_axe_points, f"{len(violations)} Axe-core violations. {critical_issues} critical, {serious_issues} serious. See {report_path_obj.name}.", status, data=violation_summary)
        except Exception as e_axe: self._add_finding(category, "Axe Execution", 0, max_axe_points, f"Error running Axe-core: {type(e_axe).__name__} - {e_axe}", "FAIL")

    def check_css_quality_responsiveness(self):
        category = "CSS & Responsiveness"; print(f"\nRunning Checks: {category}")
        try:
            viewport_tag = self.driver.find_element(By.CSS_SELECTOR, "meta[name='viewport']")
            content = viewport_tag.get_attribute("content").lower()
            vp_points = 2; vp_messages = []; status = "PASS"
            if "width=device-width" not in content: vp_points -= 1; vp_messages.append("'width=device-width' missing.")
            if "initial-scale=1" not in content and "initial-scale=1.0" not in content: vp_points -= 1; vp_messages.append("'initial-scale=1' missing.")
            if "user-scalable=no" in content or ("maximum-scale=1" in content and "initial-scale=1" in content and "minimum-scale=1" in content): self._add_finding(category, "Viewport Meta", 0, 2, f"CRITICAL: Viewport '{content}' restricts user scaling.", "FAIL")
            else:
                if vp_messages: status = "WARN"
                self._add_finding(category, "Viewport Meta", max(0,vp_points), 2, f"Viewport content: '{content}'. {', '.join(vp_messages) if vp_messages else 'Looks good.'}", status)
        except NoSuchElementException: self._add_finding(category, "Viewport Meta", 0, 2, "Viewport meta tag missing.", "FAIL")
        try:
            root_style_vars = self.driver.execute_script("return Object.keys(getComputedStyle(document.documentElement)).filter(k => k.startsWith('--'));")
            css_vars_count = len(root_style_vars) if isinstance(root_style_vars, list) else 0
            if css_vars_count > 5: self._add_finding(category, "CSS Variables (:root)", 2, 2, f"{css_vars_count} CSS variables on :root.", "PASS")
            elif css_vars_count > 0: self._add_finding(category, "CSS Variables (:root)", 1, 2, f"{css_vars_count} CSS variables on :root. Consider more.", "WARN")
            else: self._add_finding(category, "CSS Variables (:root)", 0, 2, "No CSS variables on :root found.", "WARN")
        except WebDriverException: self._add_finding(category, "CSS Variables (:root)", 0, 2, "Error checking CSS variables.", "FAIL")
        layout_score = 0
        try:
            body_display = self.driver.execute_script("return getComputedStyle(document.body).display;")
            if body_display in ['flex', 'grid']: layout_score += 1
            main_el_list = self.driver.find_elements(By.TAG_NAME, "main")
            if main_el_list and main_el_list[0].is_displayed() and self.driver.execute_script("return getComputedStyle(arguments[0]).display;", main_el_list[0]) in ['flex', 'grid']: layout_score +=1
            if layout_score >=1: self._add_finding(category, "Modern Layout (Flex/Grid)", 1, 1, "Flexbox or Grid on body/main.", "PASS")
            else: self._add_finding(category, "Modern Layout (Flex/Grid)", 0, 1, "Flexbox or Grid not readily detected on body/main.", "INFO")
        except WebDriverException: self._add_finding(category, "Modern Layout (Flex/Grid)", 0, 1, "Error checking flex/grid.", "FAIL")
        try:
            elements_with_style_attr = self.driver.find_elements(By.CSS_SELECTOR, "[style]")
            visible_elements_with_inline_styles_count = 0
            for el in elements_with_style_attr:
                try:
                    if el.is_displayed() and el.get_attribute("style").strip(): visible_elements_with_inline_styles_count += 1
                except StaleElementReferenceException: continue 
            threshold_high = 20; threshold_some = 5
            if visible_elements_with_inline_styles_count > threshold_high: self._add_finding(category, "Inline Styles", 0, 1, f"High usage of inline styles ({visible_elements_with_inline_styles_count} > {threshold_high}).", "WARN")
            elif visible_elements_with_inline_styles_count > threshold_some: self._add_finding(category, "Inline Styles", 0.5, 1, f"Some inline styles ({visible_elements_with_inline_styles_count} > {threshold_some}).", "INFO")
            else: self._add_finding(category, "Inline Styles", 1, 1, f"Minimal inline styles ({visible_elements_with_inline_styles_count} <= {threshold_some}).", "PASS")
        except WebDriverException as e_inline: self._add_finding(category, "Inline Styles", 0, 1, f"Error checking inline styles: {type(e_inline).__name__}", "FAIL")
        try:
            internal_styles_content = self.driver.execute_script("let c=''; document.querySelectorAll('style').forEach(s=>c+=s.textContent); return c;")
            important_count = internal_styles_content.lower().count("!important")
            if important_count > 5: self._add_finding(category, "!important Usage (Internal CSS)", 0, 1, f"High usage of '!important' ({important_count}) in internal <style> tags.", "WARN")
            elif important_count > 0: self._add_finding(category, "!important Usage (Internal CSS)", 0.5, 1, f"{important_count} instance(s) of '!important' in internal <style> tags.", "INFO")
            else: self._add_finding(category, "!important Usage (Internal CSS)", 1, 1, "No '!important' usage in internal <style> tags.", "PASS")
        except WebDriverException: self._add_finding(category, "!important Usage (Internal CSS)", 0, 1, "Error checking !important.", "FAIL")

    def check_rendered_color_contrast(self):
        category = "Rendered Color & Contrast"; max_points = 15
        print(f"\nRunning Checks: {category} for viewport: {self.current_viewport_name}")
        text_nodes_script = """
            const elementsData = [];
            const treeWalker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, {
                acceptNode: function (node) {
                    if (node.nodeName === 'SCRIPT' || node.nodeName === 'STYLE' || 
                        node.nodeName === 'NOSCRIPT' || node.nodeName === 'IFRAME' ||
                        node.nodeName === 'HEAD' || node.nodeName === 'META' || 
                        node.nodeName === 'LINK' || node.nodeName === 'TITLE' ||
                        node.nodeName === 'SVG' || (node.parentElement && node.parentElement.closest('svg'))) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    let hasDirectText = false;
                    for (let child = node.firstChild; child; child = child.nextSibling) {
                        if (child.nodeType === Node.TEXT_NODE && child.nodeValue.trim().length > 0) {
                            hasDirectText = true; break;
                        }
                    }
                    if (!hasDirectText && node.nodeName !== 'INPUT' && node.nodeName !== 'TEXTAREA') {
                         if (!((node.nodeName === 'INPUT' && node.placeholder && node.placeholder.trim().length >0) || 
                               (node.nodeName === 'TEXTAREA' && node.placeholder && node.placeholder.trim().length >0))) {
                            return NodeFilter.FILTER_SKIP;
                         }
                    }
                    const style = window.getComputedStyle(node);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' || 
                        parseFloat(style.opacity) === 0 || parseFloat(style.fontSize) < 4 ||
                        node.offsetWidth === 0 || node.offsetHeight === 0) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    return NodeFilter.FILTER_ACCEPT;
                }
            });
            let count = 0;
            while (treeWalker.nextNode() && count < 300) { elementsData.push(treeWalker.currentNode); count++;}
            return elementsData;
        """
        try: candidate_elements = self.driver.execute_script(text_nodes_script)
        except WebDriverException as e_fetch: self._add_finding(category, "Contrast Element Fetch", 0, max_points, f"Error fetching text elements: {e_fetch}", "FAIL"); return
        if not candidate_elements: self._add_finding(category, "Contrast Check", max_points, max_points, "No visible text elements by script to check.", "INFO"); return
        unique_web_elements_map = {}
        for el_cand in candidate_elements:
            try: unique_web_elements_map[el_cand.id] = el_cand
            except (StaleElementReferenceException, WebDriverException): continue 
        elements_to_check = list(unique_web_elements_map.values())
        if len(elements_to_check) > 250: 
             self._add_finding(category, "Contrast Element Count", 0,0, f"WARN: Checking contrast for {len(elements_to_check)} (capped at 250).", "INFO")
             elements_to_check = elements_to_check[:250]
        failure_count_aa = 0; checked_count = 0
        script_get_styles = """
            const el = arguments[0];
            if (!el || typeof el.getBoundingClientRect !== 'function') return null;
            if (el.offsetWidth === 0 || el.offsetHeight === 0) return {'error': 'not_visible_at_style_fetch'};
            const style = window.getComputedStyle(el); if (!style) return null;
            return {color: style.color, fontSize: style.fontSize, fontWeight: style.fontWeight};
        """
        for el_web_element in elements_to_check:
            try:
                if not el_web_element.is_displayed(): continue
                text_content_py = el_web_element.text.strip()
                placeholder_text_py = ""
                if not text_content_py and el_web_element.tag_name.lower() in ['input', 'textarea']:
                    placeholder_text_py = el_web_element.get_attribute("placeholder")
                    if placeholder_text_py: placeholder_text_py = placeholder_text_py.strip()
                if not text_content_py and not placeholder_text_py: continue
                effective_text_content = text_content_py if text_content_py else placeholder_text_py
                style_dict = self.driver.execute_script(script_get_styles, el_web_element)
                if not style_dict or style_dict.get('error'): continue
                fg_color_str = style_dict.get('color'); font_size_str = style_dict.get('fontSize'); font_weight_str = style_dict.get('fontWeight')
                if not fg_color_str or not font_size_str: continue
                fg_rgb = parse_color_string_to_rgb_tuple(fg_color_str) 
                bg_rgb = get_effective_background_rgb(el_web_element, self.driver) 
                if fg_rgb and bg_rgb: 
                    checked_count += 1
                    font_size_px = float(re.sub(r'[^\d.]', '', font_size_str))
                    is_large_text_wcag = (font_size_px >= 24) or (font_size_px >= 18.66 and (str(font_weight_str) == 'bold' or (str(font_weight_str).isdigit() and int(font_weight_str) >= 700)))
                    norm_fg_rgb = normalize_rgb(fg_rgb); norm_bg_rgb = normalize_rgb(bg_rgb)
                    if norm_fg_rgb and norm_bg_rgb:
                        actual_contrast_ratio = contrast_lib.rgb(norm_fg_rgb, norm_bg_rgb)
                        passes_aa = contrast_lib.passes_AA(actual_contrast_ratio, large=is_large_text_wcag)
                        passes_aaa = contrast_lib.passes_AAA(actual_contrast_ratio, large=is_large_text_wcag)
                        snippet = effective_text_content[:50].replace('\n', ' ') + ('...' if len(effective_text_content) > 50 else '')
                        if not passes_aa:
                            failure_count_aa += 1
                            self._add_finding(category, "Contrast Failure (AA)", 0, 0, f"AA FAIL Ratio {actual_contrast_ratio:.2f} for '{snippet}' in {get_element_desc(el_web_element)} (FG:{fg_color_str}, Eff.BG:rgb{bg_rgb}, Font:{font_size_str} {font_weight_str})", "FAIL", data={"ratio": actual_contrast_ratio, "text": snippet, "element": get_element_desc(el_web_element)})
                        elif not passes_aaa and failure_count_aa == 0: 
                            self._add_finding(category, "Contrast Suboptimal (AAA)", 0, 0, f"AAA WARN Ratio {actual_contrast_ratio:.2f} for '{snippet}' in {get_element_desc(el_web_element)} (FG:{fg_color_str}, Eff.BG:rgb{bg_rgb}, Font:{font_size_str} {font_weight_str})", "WARN", data={"ratio": actual_contrast_ratio, "text": snippet, "element": get_element_desc(el_web_element)})
            except (StaleElementReferenceException, WebDriverException, Exception): continue
        if checked_count == 0 and candidate_elements: self._add_finding(category, "Contrast Check Result", 0, max_points, f"Found {len(candidate_elements)} text candidates but successfully checked 0. Review script or page.", "FAIL")
        elif failure_count_aa > 0: earned_points = max(0, max_points - (failure_count_aa * 3)); self._add_finding(category, "Contrast Check Result", earned_points, max_points, f"{failure_count_aa} WCAG AA contrast failures on {checked_count} text instances.", "FAIL")
        elif checked_count > 0: self._add_finding(category, "Contrast Check Result", max_points, max_points, f"All {checked_count} checked text instances meet WCAG AA contrast.", "PASS")

    def check_performance_lighthouse(self):
        category = "Performance (Lighthouse)"; max_lh_points = 20
        print(f"\nRunning Checks: {category}")
        if not self.lighthouse_path:
            self._add_finding(category, "Lighthouse Execution", 0, max_lh_points, "Lighthouse CLI not found. Skipping.", "WARN"); return
        
        url_to_check_lh = None
        try:
            url_to_check_lh = self._start_local_server()
            if not url_to_check_lh:
                self._add_finding(category, "Lighthouse Server", 0, max_lh_points, "Failed to start local server for Lighthouse.", "FAIL"); return

            lighthouse_base_report_name = self.output_dir / f"lighthouse_report_{self.current_viewport_name}"
            cmd = [self.lighthouse_path, url_to_check_lh, "--output=json", "--output=html",
                   f"--output-path={lighthouse_base_report_name}", "--quiet", 
                   "--chrome-flags=--headless=new --disable-gpu --no-sandbox --no-zygote", # Removed file access flags as we use HTTP
                   "--only-categories=" + ",".join(LIGHTHOUSE_CATEGORIES)]
            
            print(f"  Running Lighthouse on {url_to_check_lh}: {' '.join(map(str,cmd))}")
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)
            report_path_json = lighthouse_base_report_name.with_suffix(".report.json")
            report_path_html = lighthouse_base_report_name.with_suffix(".report.html")

            if process.returncode != 0:
                error_message = f"Lighthouse CLI failed. Code: {process.returncode}. Error: {process.stderr[:1000]}"
                if report_path_json.exists(): error_message += f" Partial JSON report might exist: {report_path_json.name}"
                self._add_finding(category, "Lighthouse Execution", 0, max_lh_points, error_message, "FAIL", data={"stderr": process.stderr, "stdout": process.stdout})
            
            if not report_path_json.exists():
                self._add_finding(category, "Lighthouse Report File", 0, max_lh_points, f"Lighthouse JSON report not found at {report_path_json}. stdout: {process.stdout[:500]}", "FAIL"); return
            with open(report_path_json, 'r', encoding='utf-8') as f: lh_results = json.load(f)
            if lh_results.get("runtimeError"):
                lh_err_code = lh_results["runtimeError"].get("code"); lh_err_msg = lh_results["runtimeError"].get("message")
                self._add_finding(category, "Lighthouse Runtime Error", 0, max_lh_points, f"Lighthouse runtime error: {lh_err_code} - {lh_err_msg}", "FAIL", data=lh_results["runtimeError"]); return
            
            total_lh_earned_score = 0
            max_points_per_lh_category = max_lh_points / len(LIGHTHOUSE_CATEGORIES) if LIGHTHOUSE_CATEGORIES else max_lh_points
            for cat_id in LIGHTHOUSE_CATEGORIES:
                cat_data = lh_results.get('categories', {}).get(cat_id)
                if cat_data and cat_data.get('score') is not None:
                    score_0_1 = cat_data['score']; score_percent = int(score_0_1 * 100)
                    earned_for_this_cat = int(score_0_1 * max_points_per_lh_category)
                    total_lh_earned_score += earned_for_this_cat
                    status = "PASS" if score_percent >=90 else "WARN" if score_percent >=50 else "FAIL"
                    self._add_finding(category, f"Lighthouse: {cat_data.get('title', cat_id)}", earned_for_this_cat, int(max_points_per_lh_category), f"Score: {score_percent}/100", status)
                else: self._add_finding(category, f"Lighthouse: {cat_id}", 0, int(max_points_per_lh_category), "Score not found.", "WARN")
            summary_message = f"Lighthouse reports generated. HTML: {report_path_html.name if report_path_html.exists() else 'not found'}."
            self._add_finding(category, "Lighthouse Overall Score", 0, 0, summary_message + f" Aggregated: {total_lh_earned_score}/{max_lh_points}", "INFO" if total_lh_earned_score > max_lh_points * 0.5 else "WARN")
        except subprocess.TimeoutExpired: self._add_finding(category, "Lighthouse Execution", 0, max_lh_points, "Lighthouse timed out (4 min).", "FAIL")
        except FileNotFoundError: self._add_finding(category, "Lighthouse Report File", 0, max_lh_points, "Lighthouse JSON report open failed.", "FAIL")
        except json.JSONDecodeError: self._add_finding(category, "Lighthouse Report Parse", 0, max_lh_points, f"Failed to parse Lighthouse JSON.", "FAIL")
        except Exception as e_lh_main: self._add_finding(category, "Lighthouse Main Error", 0, max_lh_points, f"Unexpected Lighthouse error: {type(e_lh_main).__name__} - {e_lh_main}", "FAIL")
        finally:
            self._stop_local_server() # Ensure server is stopped

    def check_javascript_health(self):
        category = "JavaScript Health"; print(f"\nRunning Checks: {category}")
        try:
            logs = self.driver.get_log('browser'); js_errors = []
            for entry in logs:
                message = entry.get('message', '').lower()
                if entry['level'] == 'SEVERE' and not ("usb:" in message or "setupdigetdeviceproperty" in message or "ssl certificate" in message or "favicon.ico" in message or "deprecated" in message or "extension" in message):
                    js_errors.append(f"JS ERROR: {entry['message']}")
            if not js_errors: self._add_finding(category, "JS Console Errors", 5, 5, "No significant JS errors in console.", "PASS")
            else: self._add_finding(category, "JS Console Errors", 0, 5, f"{len(js_errors)} JS error(s) in console.", "FAIL", data=js_errors)
        except WebDriverException as e_jslog: self._add_finding(category, "JS Console Errors", 0, 5, f"Could not retrieve browser logs: {e_jslog}.", "WARN")

    def run_all_checks(self):
        print(f"--- Starting UI Benchmark Analysis for: {self.file_path.name} ---")
        try:
            for vp_name, (vp_width, vp_height) in self.viewports.items():
                try:
                    self._load_page_at_viewport(vp_name, vp_width, vp_height)
                    self.check_html_structure_semantics(); self.check_accessibility_axe()
                    self.check_css_quality_responsiveness(); self.check_rendered_color_contrast()
                    self.check_javascript_health() 
                    if vp_name.lower() in ["mobile", "desktop"] and self.lighthouse_path:
                        self.check_performance_lighthouse()
                except (TimeoutException, WebDriverException) as e_vp:
                    print(f"  CRITICAL ERROR for viewport {vp_name}: {e_vp}. Skipping."); self._add_finding("Viewport Processing", vp_name, 0,0, f"Failed viewport {vp_name}: {type(e_vp).__name__}.", "FAIL"); continue 
        finally:
            self._stop_local_server() # Ensure server is stopped if it was started and script exited early
            print(f"--- Analysis Finished for: {self.file_path.name} ---")

    def generate_report(self):
        report_data = {"file_analyzed": str(self.file_path), "page_title": self.page_title, 
                       "analysis_timestamp": self.run_timestamp, "overall_score": 0, 
                       "max_possible_score": 0, "categories": {}}
        text_report_lines = [f"--- UI Benchmark Report for: {self.file_path.name} ---",
                             f"Page Title: {self.page_title}", f"Analyzed: {self.run_timestamp}\n"]
        for cat_name, cat_data in self.scores.items():
            report_data["categories"][cat_name] = cat_data
            report_data["overall_score"] += cat_data['earned']
            report_data["max_possible_score"] += cat_data['max']
            text_report_lines.append(f"--- Category: {cat_name} (Score: {cat_data['earned']}/{cat_data['max']}) ---")
            for detail in sorted(cat_data['details'], key=lambda x: (x['viewport'], x['status'])): 
                 points_str = f"({detail['points_earned']}/{detail['max_points']} pts)" if detail['max_points'] > 0 else ""
                 text_report_lines.append(f"  [{detail['status']}] V:{detail['viewport']} | {detail['check']}: {detail['message']} {points_str}")
                 if detail.get('data'):
                     data_preview = str(detail['data'])
                     if isinstance(detail['data'], list) and len(detail['data']) > 3: data_preview = f"{str(detail['data'][:2])[:-1]} ... {str(detail['data'][-1:])}] ({len(detail['data'])} items)"
                     elif len(data_preview) > 200: data_preview = data_preview[:200] + "..."
                     text_report_lines.append(f"    Data: {data_preview}")
        overall_percentage = round((report_data["overall_score"] / report_data["max_possible_score"]) * 100) if report_data["max_possible_score"] > 0 else 0
        report_data["overall_percentage"] = overall_percentage
        text_report_lines.extend(["\n--- Overall Summary ---", f"Total Score: {report_data['overall_score']} / {report_data['max_possible_score']}",
                                  f"Overall Percentage: {overall_percentage}%", f"\nDetailed JSON report: {self.output_dir / 'full_report.json'}",
                                  f"Axe/Lighthouse reports in: {self.output_dir.name}", f"Screenshots in: {self.screenshots_dir.name}"])
        with open(self.output_dir / "full_report.json", "w", encoding='utf-8') as f: json.dump(report_data, f, indent=2)
        with open(self.output_dir / "summary_report.txt", "w", encoding='utf-8') as f: f.write("\n".join(text_report_lines))
        print("\n" + "\n".join(text_report_lines[-4:])); return report_data

    def close(self):
        self._stop_local_server() # Final attempt to stop server
        if hasattr(self, 'driver') and self.driver:
            self.driver.quit()
            print("\nWebDriver closed.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ui_benchmark_analyzer.py <path_to_html_file> [output_base_dir]")
        dummy_html_path = Path("1.html")
        if not dummy_html_path.exists():
            dummy_html_content = """
            <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Basic Test Page</title><style>body{font-family:sans-serif;margin:20px;background-color:#f0f0f0;color:#333;}h1{color:navy;}h3{color:darkgreen;margin-top:1em;}
            p{background-color:white;padding:10px;}.bad-contrast{color:#888;background-color:#aaa;padding:5px;}.good-contrast{color:#000;background-color:#fff;padding:5px;}
            nav{display:none;}.inline-style-example{color:purple;}</style></head><body><header><h1>Main Page Title (H1)</h1></header>
            <nav><ul><li><a href="#">Hidden Nav Link</a></li></ul></nav><main><p class="good-contrast">Good contrast paragraph.</p><h3>Subheading (H3) - Skipped H2</h3>
            <p class="bad-contrast">Bad contrast paragraph.</p><p style="font-style:italic;" class="inline-style-example">Inline style.</p>
            <img src="https://via.placeholder.com/150" alt="A test image"><img src="https://via.placeholder.com/100">
            <form><label for="name">Name:</label><input type="text" id="name"><input type="email" aria-label="Email"><input type="text" placeholder="Unlabeled"></form>
            </main><footer><p>© 2024 Test Suite</p></footer><script>console.error("JS test error!");</script></body></html>"""
            with open(dummy_html_path, "w", encoding="utf-8") as f_dummy: f_dummy.write(dummy_html_content)
            print(f"Created dummy '{dummy_html_path}' for testing.")
        sys.exit(1)
    
    html_file_arg = sys.argv[1]
    output_dir_arg = sys.argv[2] if len(sys.argv) > 2 else "ui_benchmark_reports"
    analyzer_instance = None
    try:
        analyzer_instance = UIBenchmarkAnalyzer(html_file_arg, output_base_dir=output_dir_arg)
        analyzer_instance.run_all_checks()
        analyzer_instance.generate_report()
    except FileNotFoundError as e_fnf: print(f"Error: {e_fnf}")
    except WebDriverException as e_wd_main: print(f"Critical WebDriver error: {e_wd_main}\nEnsure Chrome & compatible ChromeDriver are installed/in PATH.")
    except Exception as e_main: import traceback; print(f"Unexpected critical error: {e_main}"); traceback.print_exc()
    finally:
        if analyzer_instance: analyzer_instance.close()