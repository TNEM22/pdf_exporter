import io
import os
import time
import json
import copy
import base64
import threading
from datetime import datetime
from PIL import Image
from selenium import webdriver
from dataclasses import dataclass
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter
from typing import Dict, List, Tuple, Any
from reportlab.lib.utils import ImageReader
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from flask import Flask, request, jsonify, send_file
import requests  # For token if needed, but assuming passed

# Assuming these are defined elsewhere or mocked
# from ss.infrastructure.storage.Helpers import PathAccessLayer
# from ss.automation.storage.Abstractions import IAwsPrivateStorageAccessLayer as StorageAccessLayer
# For demo, we'll mock paths and upload

MAX_PDF_HEIGHT_PT = 14400.0
MAX_WAIT_TIME = 330 # 5.5 MINUTES
SCROLL_OFFSET = 20

# Global semaphore to limit concurrent PDF generations to 10 (like max tabs)
sem = threading.Semaphore(10)

@dataclass
class WidgetBoundary:
    top: int
    height: int

@dataclass
class PDFGenerationResult:
    widgets: List[WidgetBoundary]
    pdf_bytes: bytes

class ExportDashboardError(Exception):
    pass

def format_camel_case(obj: Any) -> Any:
    """Simple recursive camelCase formatter for dicts. Mimics VzHelper.FormatInCamelCase."""
    if isinstance(obj, dict):
        return {format_camel_case(k): format_camel_case(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [format_camel_case(item) for item in obj]
    elif isinstance(obj, str):
        if '_' in obj and len(obj) > 1:
            parts = obj.split('_')
            return parts[0] + ''.join(word.capitalize() for word in parts[1:])
        return obj
    else:
        return obj

def scroll_to_load_all_content(driver):
    """Scroll to load all content in #elementToExport until .all-widgets-loaded appears."""
    try:
        while True:
            WebDriverWait(driver, MAX_WAIT_TIME).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".report-loaded"))
            )
            for _ in range(3):
                try:
                    WebDriverWait(driver, MAX_WAIT_TIME).until(
                        EC.presence_of_element_located((By.ID, "elementToExport"))
                    )
                    break
                except TimeoutException:
                    time.sleep(1)
            element = driver.find_element(By.ID, "elementToExport")
            
            driver.execute_script(f"arguments[0].scrollTo(0, arguments[0].scrollHeight+{SCROLL_OFFSET});", element)
            time.sleep(1.5)
            try:
                driver.find_element(By.CSS_SELECTOR, ".all-widgets-loaded")
                return
            except:
                pass
    except Exception as ex:
        print(f"Error in scroll_to_load_all_content: {ex}")

def generate_pdf(path: str, dynamic_filter: Dict[str, Any], headless: bool = True) -> PDFGenerationResult:
    """Generate PDF screenshot and widget boundaries using Selenium."""
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    driver = None
    try:
        print("Generating PDF")
        driver = webdriver.Chrome(options=options)
        driver.set_window_size(1920, 1080)
        driver.get(path)
        
        wait = WebDriverWait(driver, timeout=MAX_WAIT_TIME)
        wait.until(EC.presence_of_element_located((By.ID, "elementToExport")))
        
        print("Scrolling")
        scroll_to_load_all_content(driver)
        
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".report-loaded")))
        time.sleep(4)
        
        # Apply dynamic filter
        # camel_filter = format_camel_case(dynamic_filter)
        # script = f"""
        # let event = new CustomEvent('applyDynamicFilter', {{
        #     detail: {{ dynamicFilter: {json.dumps(camel_filter)} }}
        # }});
        # document.dispatchEvent(event);
        # """
        # driver.execute_script(script)
        
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".report-loaded")))
        time.sleep(4)
        
        # Hide Zoho plugin if present
        try:
            zoho = driver.find_element(By.ID, "zsiq_float")
            driver.execute_script("arguments[0].style.display = 'none';", zoho)
        except:
            pass
        time.sleep(0.5)
        
        # Hide intro ripple if present
        try:
            ripple = driver.find_element(By.CSS_SELECTOR, ".questions-and-filters app-intro-ripple")
            driver.execute_script("arguments[0].setAttribute('style', 'display: none');", ripple)
        except:
            pass
        time.sleep(0.5)
        
        element = driver.find_element(By.ID, "elementToExport")
        scroll_height = driver.execute_script("return arguments[0].scrollHeight;", element)
        # Bounding box simulation - assume element is at (0,0) for simplicity; adjust if needed
        bounding_box = {"x": 0, "y": 0, "width": 1920, "height": scroll_height + 220}
        
        driver.set_window_size(1920, bounding_box["height"])
        time.sleep(0.5)

        print(f"Current Height: {bounding_box['height']}")
        pdfHeight = int((bounding_box["height"] / 100) + 5)
        print(f"Pdf Height: {pdfHeight}")
        pdf_bytes = driver.execute_cdp_cmd("Page.printToPDF", {
            "printBackground": True,
            "displayHeaderFooter": False,
            "preferCSSPageSize": False,  # ignore CSS page sizes
            "scale": 1,
            # "landscape": False,               # Puppeteer default
            "paperWidth": 20,
            "paperHeight": pdfHeight,
            "marginTop": 0,
            "marginBottom": 0,
            "marginLeft": 0,
            "marginRight": 0,
        })

        pdf_raw = base64.b64decode(pdf_bytes["data"])  # convert base64 → bytes

        # 1 inch -> 72 px
        cropped_widget_pdf_bytes = crop_widgets_pdf(
            pdf_raw,
            left=170,
            bottom=0,
            top=150,        # crop 100 px from top (convert to pts if needed)
            right=0
        )
        
        # Extract widget boundaries via JS
        widgets_script = """
        return Array.from(document.querySelectorAll('.grid-widget-wrapper')).map(el => {
            const transformY = el.style.transform.match(/translate3d\\(\\d+px, (\\d+)px, \\d+px\\)/);
            const top = transformY ? parseInt(transformY[1]) : el.offsetTop;
            return { top: top, height: el.offsetHeight };
        });
        """
        widgets_data = driver.execute_script(widgets_script)
        widgets = [WidgetBoundary(top=w["top"], height=w["height"]) for w in widgets_data]
        
        # return PDFGenerationResult(image_bytes=image_bytes, widgets=widgets, pdf_bytes=pdf_bytes, cropped_pdf_bytes=cropped_pdf_bytes)
        return PDFGenerationResult(
            widgets=widgets,
            pdf_bytes=cropped_widget_pdf_bytes,
        )
    
    except Exception as ex:
        print(f"Error in generate_pdf: {ex}")
        raise ExportDashboardError(f"Failed to generate PDF: {ex}")
    
    finally:
        if driver:
            driver.quit()

def crop_widgets_pdf(pdf_data: bytes, left: int = 0, right: int = 0,
             top: int = 0, bottom: int = 0):
    """
    Crop a PDF by adjusting mediabox, splitting pages vertically if the cropped height
    exceeds MAX_PDF_HEIGHT_PT using logic adapted from convert_to_pdf (slicing from top down
    into chunks of max height, handling potential widget-like spans by uniform chunking).
    Values must be in PDF points (1 inch = 72 points).
    """
    reader = PdfReader(io.BytesIO(pdf_data))
    writer = PdfWriter()

    for page in reader.pages:
        mediabox = page.mediabox

        # Current box
        x0 = float(mediabox.left)
        y0 = float(mediabox.bottom)
        x1 = float(mediabox.right)
        y1 = float(mediabox.top)

        # Apply cropping
        new_x0 = x0 + left
        new_y0 = y0 + bottom
        new_x1 = x1 - right
        new_y1 = y1 - top

        new_width = new_x1 - new_x0
        new_height = new_y1 - new_y0

        if new_height <= 0 or new_width <= 0:
            continue  # Skip invalid pages

        # Slice from top down, accumulating until max height per "page"
        slice_offset = 0.0  # Offset from the top of the cropped region
        while slice_offset < new_height:
            slice_h = min(MAX_PDF_HEIGHT_PT, new_height - slice_offset)

            # Compute slice bounds in original coordinates (top-down)
            slice_upper_y = new_y1 - slice_offset
            slice_lower_y = slice_upper_y - slice_h

            # Create a deep copy to avoid sharing page objects
            slice_page = copy.deepcopy(page)

            # Assign new crop region for this slice
            slice_page.mediabox.lower_left = (new_x0, slice_lower_y)
            slice_page.mediabox.upper_right = (new_x1, slice_upper_y)

            writer.add_page(slice_page)

            # Advance offset (like advancing widget index)
            slice_offset += slice_h

    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return output.getvalue()

def remove_extra_pages(pdf_bytes: bytes, keep_pages=1):
    """
    Keep only the first 'keep_pages' pages of the PDF.
    Default = keep only page 1.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for i in range(min(keep_pages, len(reader.pages))):
        writer.add_page(reader.pages[i])

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()

# Flask API
app = Flask(__name__)

@app.route('/', methods=['GET'])
@app.route('/check', methods=['GET'])
def check_health():
    return jsonify({'status': 'healthy'}), 200

@app.route('/generate_pdf', methods=['POST'])
def generate_pdf_endpoint():
    """Flask endpoint to generate PDF. Limits concurrent requests to 10 via semaphore."""
    try:
        token = None
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header.split(" ")[1]  # Extract token after 'Bearer '
        if not token:
            return jsonify({'message': 'Token is missing or invalid!'}), 401
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400
        dashboard_id = data.get('dashboard_id')
        if not dashboard_id:
            return jsonify({'error': 'Dashboard Id is required'}), 400
        dynamic_filter = data.get('dynamic_filter', {})

        url = f"https://uat-ss-portal.surveysensum.com/workspace/dashboards/{dashboard_id}?accessToken={token}"

        print(f"URL: https://uat-ss-portal.surveysensum.com/workspace/dashboards/{dashboard_id}")

        with sem:
            result = generate_pdf(url, dynamic_filter, headless=True)

        # Return the full multi-page PDF (no cropping to single page)
        pdf_io = io.BytesIO(remove_extra_pages(result.pdf_bytes, keep_pages=1))
        return send_file(
            pdf_io,
            mimetype='application/pdf',
            as_attachment=True,
            download_name='dashboard.pdf'
        )
    except ExportDashboardError as ex:
        return jsonify({'error': str(ex)}), 500
    except Exception as ex:
        return jsonify({'error': f'Unexpected error: {str(ex)}'}), 500

# --------------------------------------------------------
# Run
# --------------------------------------------------------
if __name__ == "__main__":
    # Run Flask API
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)