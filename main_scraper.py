# main_scraper.py
import hashlib
import io
import logging
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, after_this_request, jsonify, request, send_file
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# --- Configuration ---
TEMP_DIR = Path("temp_scraper_work")
REQUEST_TIMEOUT = 20  # Timeout for downloading assets
# Time (in seconds) for the headless browser to wait for the page's JavaScript to load.
# Increase this for very slow or complex pages.
DYNAMIC_PAGE_LOAD_DELAY = 5
ALLOWED_SCHEMES = {"http", "https"}

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app, resources={r"/scrape*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Helper Functions ---
def create_project_dirs(project_id: str) -> Path:
    """Creates a unique temporary directory structure for a scraping job."""
    project_path = TEMP_DIR / project_id
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "assets").mkdir(exist_ok=True)
    return project_path

def download_and_save_asset(url: str, save_dir: Path) -> (str | None):
    """
    Downloads an asset, saves it with a unique name, and returns its new relative path.
    """
    if urlparse(url).scheme not in ALLOWED_SCHEMES:
        logging.warning("Skipping asset from disallowed scheme: %s", url)
        return None
    try:
        response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        original_filename = Path(urlparse(url).path).name
        extension = Path(original_filename).suffix or '.bin'
        url_hash = hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]
        new_filename = f"{url_hash}{extension}"
        dest_path = save_dir / new_filename

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        logging.info("Downloaded asset %s -> %s", url, dest_path.name)
        return dest_path.relative_to(save_dir.parent).as_posix()

    except requests.RequestException as e:
        logging.warning("Asset download failed: %s (%s)", url, e)
        return None

def scrape_single_page(page_url: str, page_title: str, project_path: Path) -> None:
    """
    Scrapes a single page using a headless browser to get dynamic content,
    then downloads its assets and rewrites local links.
    """
    logging.info("Scraping page '%s' from URL: %s", page_title, page_url)
    
    # --- Selenium Headless Browser Setup ---
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Run without opening a visible browser window
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    
    driver = None
    try:
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        logging.info("Browser session started. Navigating to %s", page_url)
        driver.get(page_url)
        
        # --- Wait for dynamic content to load ---
        logging.info("Waiting %d seconds for dynamic content...", DYNAMIC_PAGE_LOAD_DELAY)
        time.sleep(DYNAMIC_PAGE_LOAD_DELAY)
        
        # Get the final, JavaScript-rendered HTML
        final_html = driver.page_source
        logging.info("Successfully retrieved final HTML source.")

    finally:
        if driver:
            driver.quit() # Ensure the browser is closed
            logging.info("Browser session closed.")
    # --- End of Selenium Logic ---
    
    soup = BeautifulSoup(final_html, "html.parser")
    page_domain = urlparse(page_url).netloc

    asset_tags = {"link": "href", "script": "src", "img": "src", "source": "src"}
    
    for tag_name, attr in asset_tags.items():
        for tag in soup.find_all(tag_name):
            if not tag.has_attr(attr) or tag[attr].startswith(('data:', '#', 'javascript:')):
                continue

            asset_url = urljoin(page_url, tag[attr])
            
            if urlparse(asset_url).netloc != page_domain:
                continue

            asset_save_dir = project_path / "assets"
            new_asset_path = download_and_save_asset(asset_url, asset_save_dir)
            
            if new_asset_path:
                tag[attr] = new_asset_path

    safe_filename = "".join(c for c in page_title if c.isalnum() or c in (' ', '_')).rstrip()
    safe_filename = safe_filename.replace(' ', '_').lower() or "index"
    
    output_html_path = project_path / f"{safe_filename}.html"
    output_html_path.write_text(soup.prettify(), encoding="utf-8")
    logging.info("Saved final rendered page to %s", output_html_path)

# --- API Endpoint ---
@app.route("/scrape", methods=["POST"])
def scrape_endpoint():
    data = request.get_json()
    if not data or "pages" not in data or not isinstance(data["pages"], list):
        return jsonify({"error": "Invalid request. 'pages' array is required."}), 400

    pages = data["pages"]
    if not pages:
        return jsonify({"error": "No pages provided for scraping."}), 400

    project_id = str(uuid.uuid4())
    project_path = create_project_dirs(project_id)
    
    first_url = pages[0].get("url", "http://example.com")
    slug = urlparse(first_url).netloc.replace('.', '_') or "scraped_site"

    try:
        for page in pages:
            if "url" in page and "title" in page:
                scrape_single_page(page["url"], page["title"], project_path)
        
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in project_path.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(project_path)
                    zf.write(file_path, arcname)
        
        memory_file.seek(0)

        @after_this_request
        def cleanup(response):
            try:
                shutil.rmtree(project_path)
                logging.info("Cleaned up temporary directory: %s", project_path)
            except Exception as e:
                logging.error("Error during cleanup: %s", e)
            return response

        return send_file(
            memory_file,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{slug}_clone.zip"
        )

    except Exception as e:
        logging.error("An unexpected error occurred: %s", e, exc_info=True)
        shutil.rmtree(project_path, ignore_errors=True)
        # Provide a more user-friendly error
        error_message = f"An error occurred: {e.__class__.__name__}. Check logs for details."
        if "net::ERR_NAME_NOT_RESOLVED" in str(e):
             error_message = "The URL could not be found. Please check the address for typos."
        return jsonify({"error": error_message}), 500

if __name__ == "__main__":
    TEMP_DIR.mkdir(exist_ok=True)
    app.run(debug=True, port=5000)
