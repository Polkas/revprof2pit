#!/usr/bin/env python3
"""
FastAPI aplikacja do konwersji raportów Revolut na dane do PIT-38
"""

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
import os
import tempfile
import shutil
from pathlib import Path
from revolut_to_pit8c import RevolutToPIT38
import secrets
from datetime import datetime, timedelta
import io
from collections import defaultdict
import logging
from logging.handlers import RotatingFileHandler
import re
import hashlib
import signal
from contextlib import contextmanager

app = FastAPI(title="Revolut → PIT-38")

# Configure logging
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "app.log"

logger = logging.getLogger("revolut_pit8c")
logger.setLevel(logging.INFO)

# Rotating file handler - 10MB max, keep 5 backup files
file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(file_handler)

# Console handler for development
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(console_handler)

# In-memory storage for temporary files with expiration
# Format: {token: {"file": bytes, "filename": str, "expires": datetime}}
temporary_files = {}

# Rate limiting: track requests per IP
# Format: {ip: [timestamp1, timestamp2, ...]}
request_counts = defaultdict(list)

# Concurrent processing limiter
import asyncio
processing_semaphore = asyncio.Semaphore(5)  # Max 5 concurrent file processings

# Configuration
MAX_FILE_SIZE_MB = 10
MAX_REQUESTS_PER_MINUTE = 10
MAX_FILES_IN_MEMORY = 50
FILE_EXPIRATION_MINUTES = 10
MAX_CONCURRENT_PROCESSING = 5

def get_client_ip(request: Request) -> str:
    """Get real client IP from headers (for reverse proxy setup)"""
    # Check X-Forwarded-For header (DigitalOcean, Cloudflare, etc)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, take the first (client)
        return forwarded_for.split(",")[0].strip()
    
    # Check X-Real-IP header (nginx)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Fallback to direct connection IP
    return request.client.host

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and other attacks"""
    # Remove path components
    filename = os.path.basename(filename)
    # Remove dangerous characters, keep only alphanumeric, dash, underscore, dot
    filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:195] + ext
    return filename

def cleanup_old_ips():
    """Remove IP addresses with no recent requests (prevent memory leak)"""
    now = datetime.now()
    old_ips = [
        ip for ip, timestamps in request_counts.items()
        if not timestamps or (now - max(timestamps)).total_seconds() > 3600  # 1 hour
    ]
    for ip in old_ips:
        del request_counts[ip]

@contextmanager
def timeout_context(seconds: int):
    """Context manager for timeout (Unix only, graceful fallback on Windows)"""
    def timeout_handler(signum, frame):
        raise TimeoutError("Operation timed out")
    
    # Try to set signal alarm (works on Unix)
    try:
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    except (AttributeError, ValueError):
        # Windows doesn't support SIGALRM, just yield without timeout
        yield

def validate_csv_content(content_preview: str) -> bool:
    """Validate CSV content with simple string matching (no regex to prevent ReDoS)"""
    keywords = ['Summary', 'Transactions', 'Date', 'Balance']
    # Simple O(n) check - no regex
    for keyword in keywords:
        if keyword in content_preview:
            return True
    return False

# Wspólne style CSS
COMMON_STYLES = """
    * {
        margin: 0;
        padding: 0;
        box-sizing: border-box;
    }
    
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
        background: #f5f7fa;
        min-height: 100vh;
        color: #333;
    }
    
    .navbar {
        background: #1a1a2e;
        padding: 15px 40px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    }
    
    .navbar-brand {
        color: white;
        font-size: 20px;
        font-weight: 600;
        text-decoration: none;
    }
    
    .navbar-links {
        display: flex;
        gap: 30px;
    }
    
    .navbar-links a {
        color: #b0b0b0;
        text-decoration: none;
        font-size: 14px;
        transition: color 0.3s;
    }
    
    .navbar-links a:hover {
        color: white;
    }
    
    .container {
        max-width: 900px;
        margin: 40px auto;
        padding: 0 20px;
    }
    
    .card {
        background: white;
        border-radius: 8px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        padding: 30px;
        margin-bottom: 20px;
    }
    
    h1 {
        color: #1a1a2e;
        margin-bottom: 10px;
        font-size: 28px;
    }
    
    h2 {
        color: #1a1a2e;
        margin-bottom: 15px;
        font-size: 22px;
        border-bottom: 2px solid #e0e0e0;
        padding-bottom: 10px;
    }
    
    h3 {
        color: #333;
        margin-bottom: 10px;
        font-size: 18px;
    }
    
    .subtitle {
        color: #666;
        margin-bottom: 30px;
        font-size: 16px;
    }
    
    .btn {
        display: inline-block;
        padding: 12px 24px;
        background: #1a1a2e;
        color: white;
        border: none;
        border-radius: 6px;
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        text-decoration: none;
        transition: all 0.3s;
    }
    
    .btn:hover {
        background: #2d2d44;
        transform: translateY(-1px);
    }
    
    .btn-success {
        background: #28a745;
    }
    
    .btn-success:hover {
        background: #218838;
    }
    
    .upload-area {
        border: 2px dashed #ccc;
        border-radius: 8px;
        padding: 40px;
        text-align: center;
        background: #fafafa;
        margin-bottom: 20px;
        transition: all 0.3s;
    }
    
    .upload-area:hover {
        border-color: #1a1a2e;
        background: #f5f5f5;
    }
    
    input[type="file"] {
        display: none;
    }
    
    .file-label {
        display: inline-block;
        padding: 12px 30px;
        background: #1a1a2e;
        color: white;
        border-radius: 6px;
        cursor: pointer;
        font-weight: 500;
        transition: all 0.3s;
    }
    
    .file-label:hover {
        background: #2d2d44;
    }
    
    .file-name {
        margin-top: 15px;
        color: #666;
    }
    
    .submit-btn {
        width: 100%;
        padding: 15px;
        background: #1a1a2e;
        color: white;
        border: none;
        border-radius: 6px;
        font-size: 16px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.3s;
    }
    
    .submit-btn:hover:not(:disabled) {
        background: #2d2d44;
    }
    
    .submit-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }
    
    .loading {
        display: none;
        text-align: center;
        margin-top: 20px;
        padding: 20px;
    }
    
    .spinner {
        border: 3px solid #f3f3f3;
        border-top: 3px solid #1a1a2e;
        border-radius: 50%;
        width: 40px;
        height: 40px;
        animation: spin 1s linear infinite;
        margin: 15px auto;
    }
    
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    
    .result {
        display: none;
        margin-top: 20px;
        padding: 20px;
        background: #f8f9fa;
        border-radius: 8px;
        border: 1px solid #e0e0e0;
    }
    
    .result h3 {
        color: #28a745;
        margin-bottom: 15px;
    }
    
    .error {
        display: none;
        margin-top: 20px;
        padding: 20px;
        background: #fff5f5;
        border-radius: 8px;
        border: 1px solid #f5c6cb;
        color: #721c24;
    }
    
    .warning-box {
        background: #fff3cd;
        border: 1px solid #ffc107;
        border-radius: 6px;
        padding: 15px;
        margin: 20px 0;
        font-size: 14px;
    }
    
    .info-list {
        margin-left: 20px;
        line-height: 1.8;
        color: #555;
    }
    
    .info-list li {
        margin-bottom: 5px;
    }
    
    table {
        width: 100%;
        border-collapse: collapse;
        margin: 15px 0;
    }
    
    th, td {
        padding: 12px;
        text-align: left;
        border-bottom: 1px solid #e0e0e0;
    }
    
    th {
        background: #f8f9fa;
        font-weight: 600;
    }
    
    .text-right {
        text-align: right;
    }
    
    .text-muted {
        color: #666;
    }
    
    .footer {
        text-align: center;
        padding: 30px;
        color: #666;
        font-size: 13px;
        border-top: 1px solid #e0e0e0;
        margin-top: 40px;
    }
"""

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Rate limiting middleware to prevent abuse"""
    # Skip rate limiting for health check
    if request.url.path == "/health":
        return await call_next(request)
    
    client_ip = get_client_ip(request)
    now = datetime.now()
    
    # Clean old requests (older than 1 minute)
    request_counts[client_ip] = [
        req_time for req_time in request_counts[client_ip]
        if now - req_time < timedelta(minutes=1)
    ]
    
    # Periodic cleanup of old IPs (every ~100 requests)
    if len(request_counts) % 100 == 0:
        cleanup_old_ips()
    
    # Check if exceeded limit
    if len(request_counts[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        raise HTTPException(
            status_code=429, 
            detail=f"Zbyt wiele zapytan. Maksymalnie {MAX_REQUESTS_PER_MINUTE} zapytan na minute."
        )
    
    request_counts[client_ip].append(now)
    response = await call_next(request)
    
    # Add security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
    
    return response

@app.get("/health")
async def health_check():
    """Health check endpoint for Docker"""
    return {"status": "ok", "service": "revolut-pit8c"}

@app.get("/", response_class=HTMLResponse)
async def home():
    """Strona glowna z formularzem uploadowania pliku"""
    return f"""
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Revolut → PIT-38</title>
    <style>{COMMON_STYLES}</style>
</head>
<body>
    <nav class="navbar">
        <a href="/" class="navbar-brand">Revolut → PIT-38</a>
        <div class="navbar-links">
            <a href="/">Konwerter</a>
            <a href="/metodologia">Metodologia obliczen</a>
            <a href="/zastrzezenia">Zastrzezenia prawne</a>
        </div>
    </nav>
    
    <div class="container">
        <div class="card">
            <h1>Konwersja raportu Revolut na dane do PIT-38</h1>
            <p class="subtitle">Automatyczne przeliczenie transakcji wedlug kursow NBP zgodnie z polskim prawem podatkowym</p>
            
            <div class="info-box" style="background: #e3f2fd; border-left: 3px solid #2196f3; padding: 15px; margin: 20px 0;">
                <strong>Cel aplikacji:</strong> Narzedzie sluzy jako <strong>benchmark</strong> do weryfikacji 
                wlasnych obliczen lub wynikow z innych aplikacji. Umozliwia sprawdzenie poprawnosci 
                przeliczania transakcji zagranicznych zgodnie z aktualnymi przepisami podatkowymi.
            </div>
            
            <div class="warning-box">
                <strong>Wazne:</strong> Przed uzyciem aplikacji prosimy o zapoznanie sie z 
                <a href="/metodologia">metodologia obliczen</a> oraz 
                <a href="/zastrzezenia">zastrzezeniami prawnymi</a>.
                Wyniki maja charakter informacyjny i nie stanowia porady podatkowej.
            </div>
            
            <h3>Instrukcja</h3>
            <ol class="info-list">
                <li>Pobierz raport CSV z aplikacji Revolut (Documents & statements > Consolidated statements)</li>
                <li>Wgraj plik ponizej</li>
                <li>System przetworzy transakcje i wygeneruje raport Excel</li>
            </ol>
            
            <div style="background: #fff3cd; border-left: 3px solid #ffc107; padding: 12px; margin: 15px 0; font-size: 14px;">
                <strong>Limity techniczne:</strong>
                <ul style="margin: 8px 0 0 20px; line-height: 1.6;">
                    <li>Maksymalny rozmiar pliku: <strong>10 MB</strong></li>
                    <li>Format: tylko pliki <strong>CSV</strong> z Revolut</li>
                    <li>Wygenerowane raporty wygasają po <strong>10 minutach</strong></li>
                    <li>Limit zapytań: <strong>10 na minutę</strong> z jednego IP</li>
                </ul>
            </div>
            
            <div style="background: #e8f5e9; border-left: 3px solid #4caf50; padding: 12px; margin: 15px 0; font-size: 14px;">
                <strong>Prywatność:</strong> Twoje dane <strong>NIE są gromadzone</strong> - przetwarzanie odbywa się 
                wyłącznie w pamięci serwera, a pliki są automatycznie usuwane po pobraniu lub po 10 minutach.
                <br><br>
                <strong>Open Source:</strong> Chcesz pełnej prywatności? Uruchom aplikację lokalnie! 
                Kod źródłowy dostępny na: 
                <a href="https://github.com/polkas/revprof2pit" target="_blank" style="color: #2196f3; text-decoration: underline;">
                    https://github.com/polkas/revprof2pit
                </a>
            </div>
        </div>
        
        <div class="card">
            <form id="uploadForm" enctype="multipart/form-data">
                <div class="upload-area">
                    <label for="fileInput" class="file-label">
                        Wybierz plik CSV
                    </label>
                    <input type="file" id="fileInput" name="file" accept=".csv" required>
                    <div class="file-name" id="fileName">Nie wybrano pliku</div>
                </div>
                
                <button type="submit" class="submit-btn" id="submitBtn" disabled style="margin-top: 15px;">
                    Generuj raport PIT-38
                </button>
                
                <div style="text-align: center; margin-top: 10px;">
                    <button type="button" class="btn" id="exampleBtn" style="background: transparent; color: #6c757d; border: 1px solid #dee2e6; padding: 8px 16px; font-size: 13px;">
                        Załaduj przykład
                    </button>
                </div>
            </form>
            
            <div class="loading" id="loading">
                <div class="spinner"></div>
                <p>Przetwarzanie danych i pobieranie kursow NBP...</p>
                <p class="text-muted">Moze to potrwac 5-10 sekund</p>
            </div>
            
            <div class="error" id="error"></div>
            
            <div class="result" id="result">
                <h3>Raport wygenerowany pomyslnie</h3>
                <div id="explanation"></div>
                <br>
                <a href="#" class="btn btn-success" id="downloadBtn">Pobierz raport Excel</a>
            </div>
        </div>
    </div>
    
    <div class="footer">
        Aplikacja ma charakter informacyjny. Nie stanowi porady podatkowej ani prawnej.
    </div>
    
    <script>
        const fileInput = document.getElementById('fileInput');
        const fileName = document.getElementById('fileName');
        const submitBtn = document.getElementById('submitBtn');
        const exampleBtn = document.getElementById('exampleBtn');
        const uploadForm = document.getElementById('uploadForm');
        const loading = document.getElementById('loading');
        const result = document.getElementById('result');
        const error = document.getElementById('error');
        const downloadBtn = document.getElementById('downloadBtn');
        const explanation = document.getElementById('explanation');
        
        fileInput.addEventListener('change', (e) => {{
            if (e.target.files.length > 0) {{
                fileName.textContent = e.target.files[0].name;
                submitBtn.disabled = false;
            }} else {{
                fileName.textContent = 'Nie wybrano pliku';
                submitBtn.disabled = true;
            }}
        }});
        
        exampleBtn.addEventListener('click', async () => {{
            loading.style.display = 'block';
            result.style.display = 'none';
            error.style.display = 'none';
            exampleBtn.disabled = true;
            submitBtn.disabled = true;
            
            try {{
                const response = await fetch('/upload-example', {{
                    method: 'POST'
                }});
                
                const data = await response.json();
                
                if (!response.ok) {{
                    throw new Error(data.detail || 'Wystapil blad podczas przetwarzania');
                }}
                
                explanation.innerHTML = data.explanation;
                downloadBtn.href = `/download/${{data.token}}`;
                downloadBtn.download = data.filename;
                result.style.display = 'block';
                fileName.textContent = 'Przetworzono plik przykladowy';
                
            }} catch (err) {{
                error.textContent = 'Blad: ' + err.message;
                error.style.display = 'block';
            }} finally {{
                loading.style.display = 'none';
                exampleBtn.disabled = false;
                submitBtn.disabled = false;
            }}
        }});
        
        uploadForm.addEventListener('submit', async (e) => {{
            e.preventDefault();
            
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            
            loading.style.display = 'block';
            result.style.display = 'none';
            error.style.display = 'none';
            submitBtn.disabled = true;
            
            try {{
                const response = await fetch('/upload', {{
                    method: 'POST',
                    body: formData
                }});
                
                const data = await response.json();
                
                if (!response.ok) {{
                    throw new Error(data.detail || 'Wystapil blad podczas przetwarzania');
                }}
                
                explanation.innerHTML = data.explanation;
                downloadBtn.href = `/download/${{data.token}}`;
                downloadBtn.download = data.filename;
                result.style.display = 'block';
                
            }} catch (err) {{
                error.textContent = 'Blad: ' + err.message;
                error.style.display = 'block';
            }} finally {{
                loading.style.display = 'none';
                submitBtn.disabled = false;
            }}
        }});
    </script>
</body>
</html>
    """

@app.get("/metodologia", response_class=HTMLResponse)
async def metodologia():
    """Strona z metodologia obliczen"""
    return f"""
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Metodologia obliczen - Revolut → PIT-38</title>
    <style>{COMMON_STYLES}</style>
</head>
<body>
    <nav class="navbar">
        <a href="/" class="navbar-brand">Revolut → PIT-38</a>
        <div class="navbar-links">
            <a href="/">Konwerter</a>
            <a href="/metodologia">Metodologia obliczen</a>
            <a href="/zastrzezenia">Zastrzezenia prawne</a>
        </div>
    </nav>
    
    <div class="container">
        <div class="card">
            <h1>Metodologia obliczen</h1>
            <p class="subtitle">Szczegolowy opis sposobu przeliczania transakcji na PLN zgodnie z polskim prawem podatkowym</p>
        </div>
        
        <div class="card">
            <h2>1. Podstawa prawna - kursy walut</h2>
            <p>Zgodnie z <strong>art. 11a ustawy o podatku dochodowym od osob fizycznych</strong>:</p>
            <blockquote style="background: #f8f9fa; padding: 15px; border-left: 3px solid #1a1a2e; margin: 15px 0; font-style: italic;">
                "Przychody w walutach obcych przelicza sie na zlote wedlug kursu sredniego ogłaszanego 
                przez Narodowy Bank Polski z <strong>ostatniego dnia roboczego poprzedzajacego dzien 
                uzyskania przychodu</strong> lub poniesienia kosztu."
            </blockquote>
            <p>Aplikacja pobiera kursy walut bezposrednio z oficjalnego API Narodowego Banku Polskiego.</p>
        </div>
        
        <div class="card">
            <h2>2. Cykl rozliczeniowy T+1 / T+2</h2>
            <p>Dla akcji gieldowych przychod powstaje nie w dniu zlozenia zlecenia, lecz w <strong>dniu rozliczenia transakcji</strong>.</p>
            
            <h3>Rynki amerykanskie (NYSE, NASDAQ)</h3>
            <table>
                <tr>
                    <th>Okres</th>
                    <th>Cykl rozliczeniowy</th>
                    <th>Zrodlo</th>
                </tr>
                <tr>
                    <td>Od 28 maja 2024</td>
                    <td><strong>T+1</strong> (1 dzien roboczy)</td>
                    <td>SEC Rule 15c6-1, DTCC</td>
                </tr>
                <tr>
                    <td>Przed 28 maja 2024</td>
                    <td>T+2 (2 dni robocze)</td>
                    <td>-</td>
                </tr>
            </table>
            
            <h3>Rynki europejskie (Euronext, XETRA, LSE)</h3>
            <table>
                <tr>
                    <th>Okres</th>
                    <th>Cykl rozliczeniowy</th>
                    <th>Zrodlo</th>
                </tr>
                <tr>
                    <td>Aktualnie</td>
                    <td><strong>T+2</strong> (2 dni robocze)</td>
                    <td>ESMA, CSDR</td>
                </tr>
            </table>
            
            <div class="warning-box">
                <strong>Uwaga:</strong> Dni robocze nie obejmuja sobot, niedziel ani swiat gieldowych.
            </div>
        </div>
        
        <div class="card">
            <h2>3. Schemat obliczen dla akcji</h2>
            <p>Przyklad dla akcji amerykanskiej sprzedanej w piatek 10.01.2025:</p>
            
            <table>
                <tr>
                    <th>Krok</th>
                    <th>Opis</th>
                    <th>Wynik</th>
                </tr>
                <tr>
                    <td>1</td>
                    <td>Data transakcji sprzedazy</td>
                    <td>10.01.2025 (piatek)</td>
                </tr>
                <tr>
                    <td>2</td>
                    <td>Dodaj T+1 (1 dzien roboczy)</td>
                    <td>13.01.2025 (poniedzialek)</td>
                </tr>
                <tr>
                    <td>3</td>
                    <td>Poprzedni dzien roboczy</td>
                    <td>10.01.2025 (piatek)</td>
                </tr>
                <tr>
                    <td>4</td>
                    <td>Kurs NBP USD z tego dnia</td>
                    <td>np. 4.0210 PLN</td>
                </tr>
                <tr>
                    <td>5</td>
                    <td>Przychod PLN = cena USD x kurs</td>
                    <td>Wartość w PLN</td>
                </tr>
            </table>
            
            <p style="margin-top: 15px;">Analogiczny schemat stosowany jest dla kosztu nabycia (z data zakupu i jej rozliczenia).</p>
        </div>
        
        <div class="card">
            <h2>4. Kryptowaluty</h2>
            <p>Dla kryptowalut nie stosuje sie cyklu T+1/T+2. Rozliczenie nastepuje <strong>natychmiastowo</strong>.</p>
            <p>Kurs NBP pobierany jest z dnia roboczego poprzedzajacego dzien transakcji.</p>
            <div class="warning-box">
                <strong>PIT-38 Sekcja E:</strong> Przychody z kryptowalut wykazuje sie w <strong>Sekcji E</strong> formularza PIT-38, 
                oddzielnie od akcji (Sekcja C). Strata z kryptowalut nie moze pomniejszyc zysku z akcji i odwrotnie.
            </div>
        </div>
        
        <div class="card">
            <h2>5. Dywidendy</h2>
            <p>Przychod z dywidend powstaje w dniu ich wyplaty. Kurs NBP pobierany jest z dnia roboczego 
            poprzedzajacego dzien wyplaty.</p>
            <p>Od dywidend zagranicznych czesto pobierany jest podatek u zrodla (np. 15% w USA przy wypelnionym 
            formularzu W-8BEN, 30% bez formularza). Podatek ten mozna odliczyc od podatku naleznego w Polsce (19%).</p>
            <div class="warning-box">
                <strong>Zalacznik PIT/ZG:</strong> Dla dochodow zagranicznych (dywidend) wymagany jest zalacznik PIT/ZG, 
                skladany <strong>osobno dla kazdego kraju</strong> pochodzenia dochodu.
            </div>
        </div>
        
        <div class="card">
            <h2>6. Odsetki z lokat (podatek Belki)</h2>
            <p>Odsetki z lokat podlegaja opodatkowaniu stawka 19% (tzw. podatek Belki).</p>
            <p>Dla odsetek w EUR stosowany jest kurs NBP z dnia roboczego poprzedzajacego dzien naliczenia odsetek.</p>
        </div>
        
        <div class="card">
            <h2>7. Referencje i zrodla prawne</h2>
            
            <h3>Polskie przepisy podatkowe</h3>
            <table>
                <tr>
                    <th>Dokument</th>
                    <th>Opis</th>
                    <th>Link</th>
                </tr>
                <tr>
                    <td>Ustawa o PIT - art. 11a</td>
                    <td>Przeliczanie przychodow w walutach obcych na PLN</td>
                    <td><a href="https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU19910800350" target="_blank">isap.sejm.gov.pl</a></td>
                </tr>
                <tr>
                    <td>Ustawa o PIT - art. 30b</td>
                    <td>Opodatkowanie dochodow kapitalowych (19%)</td>
                    <td><a href="https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU19910800350" target="_blank">isap.sejm.gov.pl</a></td>
                </tr>
                <tr>
                    <td>Ustawa o PIT - art. 30a</td>
                    <td>Podatek od odsetek i dywidend (podatek Belki)</td>
                    <td><a href="https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU19910800350" target="_blank">isap.sejm.gov.pl</a></td>
                </tr>
            </table>
            
            <h3 style="margin-top: 25px;">Cykl rozliczeniowy T+1 (USA)</h3>
            <table>
                <tr>
                    <th>Dokument</th>
                    <th>Opis</th>
                    <th>Link</th>
                </tr>
                <tr>
                    <td>SEC Rule 15c6-1</td>
                    <td>Standard Settlement Cycle - zmiana na T+1</td>
                    <td><a href="https://www.sec.gov/newsroom/press-releases/2023-29" target="_blank">sec.gov</a></td>
                </tr>
                <tr>
                    <td>DTCC - US T+1</td>
                    <td>Oficjalna strona DTCC o przejsciu na T+1</td>
                    <td><a href="https://www.dtcc.com/ust1" target="_blank">dtcc.com/ust1</a></td>
                </tr>
                <tr>
                    <td>SEC Final Rule 34-96930</td>
                    <td>Data wejscia w zycie: 28 maja 2024</td>
                    <td><a href="https://www.federalregister.gov/documents/2023/03/06/2023-03566/shortening-the-securities-transaction-settlement-cycle" target="_blank">federalregister.gov</a></td>
                </tr>
            </table>
            
            <h3 style="margin-top: 25px;">Cykl rozliczeniowy T+2 (Europa)</h3>
            <table>
                <tr>
                    <th>Dokument</th>
                    <th>Opis</th>
                    <th>Link</th>
                </tr>
                <tr>
                    <td>CSDR - Regulation (EU) 909/2014</td>
                    <td>Central Securities Depositories Regulation</td>
                    <td><a href="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32014R0909" target="_blank">eur-lex.europa.eu</a></td>
                </tr>
                <tr>
                    <td>ESMA Guidelines</td>
                    <td>Wytyczne dot. cyklu rozliczeniowego</td>
                    <td><a href="https://www.esma.europa.eu/policy-activities/post-trading/settlement" target="_blank">esma.europa.eu</a></td>
                </tr>
            </table>
            
            <h3 style="margin-top: 25px;">Kursy walut NBP</h3>
            <table>
                <tr>
                    <th>Dokument</th>
                    <th>Opis</th>
                    <th>Link</th>
                </tr>
                <tr>
                    <td>API NBP</td>
                    <td>Oficjalne API do pobierania kursow walut</td>
                    <td><a href="https://api.nbp.pl/" target="_blank">api.nbp.pl</a></td>
                </tr>
                <tr>
                    <td>Tabela A NBP</td>
                    <td>Srednie kursy walut obcych</td>
                    <td><a href="https://nbp.pl/statystyka-i-sprawozdawczosc/kursy/tabela-a/" target="_blank">nbp.pl</a></td>
                </tr>
            </table>
            
            <h3 style="margin-top: 25px;">Interpretacje podatkowe</h3>
            <table>
                <tr>
                    <th>Dokument</th>
                    <th>Opis</th>
                    <th>Link</th>
                </tr>
                <tr>
                    <td>Interpretacje KIS</td>
                    <td>Baza interpretacji indywidualnych</td>
                    <td><a href="https://eureka.mf.gov.pl/" target="_blank">eureka.mf.gov.pl</a></td>
                </tr>
                <tr>
                    <td>Informacje PIT-38</td>
                    <td>Instrukcja wypelniania zeznania PIT-38</td>
                    <td><a href="https://www.podatki.gov.pl/pit/twoj-e-pit/" target="_blank">podatki.gov.pl</a></td>
                </tr>
            </table>
        </div>
    </div>
    
    <div class="footer">
        Aplikacja ma charakter informacyjny. Nie stanowi porady podatkowej ani prawnej.
    </div>
</body>
</html>
    """

@app.get("/zastrzezenia", response_class=HTMLResponse)
async def zastrzezenia():
    """Strona z zastrzezeniami prawnymi"""
    return f"""
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zastrzezenia prawne - Revolut → PIT-38</title>
    <style>{COMMON_STYLES}</style>
</head>
<body>
    <nav class="navbar">
        <a href="/" class="navbar-brand">Revolut → PIT-38</a>
        <div class="navbar-links">
            <a href="/">Konwerter</a>
            <a href="/metodologia">Metodologia obliczen</a>
            <a href="/zastrzezenia">Zastrzezenia prawne</a>
        </div>
    </nav>
    
    <div class="container">
        <div class="card">
            <h1>Zastrzezenia prawne</h1>
            <p class="subtitle">Prosimy o uwaznie zapoznanie sie z ponizszymi informacjami przed skorzystaniem z aplikacji</p>
        </div>
        
        <div class="card">
            <h2>1. Charakter informacyjny</h2>
            <p>Niniejsza aplikacja ma wylacznie <strong>charakter informacyjny i pomocniczy</strong>. 
            Wyniki generowane przez aplikacje:</p>
            <ul class="info-list">
                <li>Nie stanowia porady podatkowej</li>
                <li>Nie stanowia porady prawnej</li>
                <li>Nie stanowia porady inwestycyjnej</li>
                <li>Nie zastepuja konsultacji z doradca podatkowym lub ksiegowym</li>
            </ul>
        </div>
        
        <div class="card">
            <h2>2. Brak odpowiedzialnosci</h2>
            <p>Tworcy aplikacji <strong>nie ponosza odpowiedzialnosci</strong> za:</p>
            <ul class="info-list">
                <li>Poprawnosc wygenerowanych obliczen</li>
                <li>Zgodnosc wynikow z obowiazujacymi przepisami prawa</li>
                <li>Jakiekolwiek szkody wynikajace z uzycia aplikacji</li>
                <li>Decyzje podatkowe podjete na podstawie wygenerowanych raportow</li>
                <li>Ewentualne kary lub odsetki naliczone przez organy podatkowe</li>
            </ul>
        </div>
        
        <div class="card">
            <h2>3. Weryfikacja wynikow</h2>
            <p>Uzytkownik jest <strong>zobowiazany do samodzielnej weryfikacji</strong> wygenerowanych wynikow przed 
            ich wykorzystaniem w rozliczeniu podatkowym.</p>
            <p>Zalecamy:</p>
            <ul class="info-list">
                <li>Sprawdzenie obliczen z oryginalnym raportem Revolut</li>
                <li>Weryfikacje kursow NBP na oficjalnej stronie nbp.pl</li>
                <li>Konsultacje z doradca podatkowym w przypadku watpliwosci</li>
                <li>Porownanie wynikow z innymi narzedziami lub obliczeniami recznymi</li>
            </ul>
        </div>
        
        <div class="card">
            <h2>4. Ograniczenia techniczne</h2>
            <p>Aplikacja moze nie obslugiwac wszystkich typow transakcji lub sytuacji podatkowych, w tym:</p>
            <ul class="info-list">
                <li>Niestandardowych formatow plikow CSV</li>
                <li>Transakcji na rynkach innych niz USA i Europa</li>
                <li>Szczegolnych sytuacji podatkowych (np. rezydencja podatkowa w innym kraju)</li>
                <li>Transakcji denominowanych w walutach innych niz USD, EUR, PLN</li>
            </ul>
        </div>
        
        <div class="card">
            <h2>5. Dane uzytkownika</h2>
            <p>Przeslane pliki CSV sa przetwarzane wylacznie w celu wygenerowania raportu i sa 
            <strong>automatycznie usuwane</strong> po zakonczeniu przetwarzania.</p>
            <p>Aplikacja nie przechowuje danych osobowych ani finansowych uzytkownikow.</p>
        </div>
        
        <div class="card">
            <h2>6. Zmiany w przepisach</h2>
            <p>Przepisy podatkowe moga ulec zmianie. Aplikacja zostala przygotowana na podstawie przepisow 
            obowiazujacych w momencie jej tworzenia i moze nie uwzgledniac pozniejszych zmian w prawie.</p>
            <p>Uzytkownik jest odpowiedzialny za sprawdzenie aktualnosci stosowanych przepisow.</p>
        </div>
        
        <div class="card">
            <h2>7. Akceptacja warunkow</h2>
            <p>Korzystajac z aplikacji, uzytkownik potwierdza, ze:</p>
            <ul class="info-list">
                <li>Zapoznal sie z powyzszymi zastrzezeniami</li>
                <li>Rozumie ograniczenia aplikacji</li>
                <li>Akceptuje brak odpowiedzialnosci tworcow za wyniki</li>
                <li>Bedzie weryfikowal wygenerowane dane przed ich wykorzystaniem</li>
            </ul>
        </div>
    </div>
    
    <div class="footer">
        Aplikacja ma charakter informacyjny. Nie stanowi porady podatkowej ani prawnej.
    </div>
</body>
</html>
    """

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Endpoint do uploadowania pliku CSV i generowania raportu"""
    # Hash filename for logging (privacy)
    file_hash = hashlib.sha256(file.filename.encode()).hexdigest()[:8]
    logger.info(f"Upload started: {file_hash}")
    
    # Sanitize filename to prevent path traversal
    safe_filename = sanitize_filename(file.filename)
    
    if not safe_filename.endswith('.csv'):
        logger.warning(f"Invalid file format: {file_hash}")
        raise HTTPException(status_code=400, detail="Plik musi byc w formacie CSV")
    
    # Read and validate file size
    contents = await file.read()
    file_size_mb = len(contents) / (1024 * 1024)
    
    logger.info(f"File size: {file_size_mb:.2f} MB")
    
    if file_size_mb > MAX_FILE_SIZE_MB:
        logger.warning(f"File too large: {file_size_mb:.2f} MB (max {MAX_FILE_SIZE_MB} MB)")
        raise HTTPException(
            status_code=413, 
            detail=f"Plik jest za duzy ({file_size_mb:.1f}MB). Maksymalny rozmiar: {MAX_FILE_SIZE_MB}MB"
        )
    
    # Basic CSV validation (with timeout to prevent ReDoS)
    try:
        with timeout_context(5):  # 5 second timeout
            csv_preview = contents[:1000].decode('utf-8', errors='ignore')
            if not validate_csv_content(csv_preview):
                raise HTTPException(
                    status_code=400, 
                    detail="Plik nie wyglada jak raport Revolut. Sprawdz czy to wlasciwy plik CSV."
                )
    except TimeoutError:
        logger.warning(f"CSV validation timeout for file: {file_hash}")
        raise HTTPException(status_code=400, detail="Walidacja pliku trwala zbyt dlugo")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Nie mozna odczytac pliku CSV")
    
    # Limit concurrent processing to prevent memory exhaustion
    async with processing_semaphore:
        temp_dir = tempfile.mkdtemp()
        
        try:
            input_path = os.path.join(temp_dir, safe_filename)
            with open(input_path, 'wb') as buffer:
                buffer.write(contents)
            
            output_filename = f"raport_pit38_{safe_filename.replace('.csv', '')}.xlsx"
            output_path = os.path.join(temp_dir, output_filename)
            
            converter = RevolutToPIT38(input_path)
            converter.parse_file()
            converter.preload_nbp_rates()
            results = converter.calculate_pit38_data()
            converter.generate_report(output_path, results)
            
            # Read file into memory
            with open(output_path, 'rb') as f:
                file_bytes = f.read()
            
            # Generate secure token
            token = secrets.token_urlsafe(32)
            
            # Store in memory with 10 minute expiration
            temporary_files[token] = {
                "file": file_bytes,
                "filename": output_filename,
                "expires": datetime.now() + timedelta(minutes=FILE_EXPIRATION_MINUTES)
            }
            
            logger.info(f"Report generated successfully: {file_hash} (token: {token[:8]}...)")
            logger.info(f"Files in memory: {len(temporary_files)}")
            
            # Clean expired files
            cleanup_expired_files()
            cleanup_if_memory_high()
            
            explanation = generate_explanation(converter, results)
            
            return {
                "status": "success",
                "token": token,
                "filename": output_filename,
                "explanation": explanation
            }
            
        except HTTPException:
            raise
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Error processing file {file_hash}: {type(e).__name__}: {str(e)}\n{tb}")
            # Don't expose internal error details to users
            raise HTTPException(status_code=500, detail="Wystapil blad podczas przetwarzania pliku. Sprawdz format pliku i sprobuj ponownie.")
        
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

@app.post("/upload-example")
async def upload_example():
    """Endpoint do przetwarzania przykladowego pliku CSV"""
    logger.info("Processing example file")
    example_file = Path("example_revolut_statement.csv")
    
    if not example_file.exists():
        logger.error("Example file not found")
        raise HTTPException(status_code=404, detail="Plik przykladowy nie zostal znaleziony")
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        output_filename = "raport_pit38_przyklad.xlsx"
        output_path = os.path.join(temp_dir, output_filename)
        
        converter = RevolutToPIT38(str(example_file))
        converter.parse_file()
        converter.preload_nbp_rates()
        results = converter.calculate_pit38_data()
        converter.generate_report(output_path, results)
        
        # Read file into memory
        with open(output_path, 'rb') as f:
            file_bytes = f.read()
        
        # Generate secure token
        token = secrets.token_urlsafe(32)
        
        # Store in memory with 10 minute expiration
        temporary_files[token] = {
            "file": file_bytes,
            "filename": output_filename,
            "expires": datetime.now() + timedelta(minutes=FILE_EXPIRATION_MINUTES)
        }
        
        logger.info(f"Example report generated: {output_filename} (token: {token[:8]}...)")
        
        # Clean expired files
        cleanup_expired_files()
        cleanup_if_memory_high()
        
        explanation = generate_explanation(converter, results)
        
        return {
            "status": "success",
            "token": token,
            "filename": output_filename,
            "explanation": explanation
        }
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error processing example file: {type(e).__name__}: {str(e)}\n{tb}")
        # Don't expose internal error details to users
        raise HTTPException(status_code=500, detail="Wystapil blad podczas przetwarzania przykladowego pliku.")
    
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.get("/download/{token}")
async def download_file(token: str):
    """Endpoint do pobierania wygenerowanego raportu przez token"""
    logger.info(f"Download request for token: {token[:8]}...")
    cleanup_expired_files()
    
    if token not in temporary_files:
        logger.warning(f"Token not found or expired: {token[:8]}...")
        raise HTTPException(status_code=404, detail="Plik wygasl lub nie zostal znaleziony")
    
    file_data = temporary_files[token]
    filename = file_data["filename"]
    
    logger.info(f"Download successful: token {token[:8]}...")
    
    # Sanitize filename for download (prevent header injection)
    safe_download_filename = sanitize_filename(filename)
    
    # Remove after download (single use)
    del temporary_files[token]
    
    return StreamingResponse(
        io.BytesIO(file_data["file"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_download_filename}"',
            "X-Content-Type-Options": "nosniff"
        }
    )

def cleanup_expired_files():
    """Remove expired files from memory"""
    now = datetime.now()
    expired = [token for token, data in temporary_files.items() if data["expires"] < now]
    if expired:
        logger.info(f"Cleaning up {len(expired)} expired files")
    for token in expired:
        del temporary_files[token]

def cleanup_if_memory_high():
    """Remove oldest files if memory limit exceeded"""
    if len(temporary_files) > MAX_FILES_IN_MEMORY:
        # Sort by expiration time and remove oldest
        sorted_files = sorted(temporary_files.items(), key=lambda x: x[1]['expires'])
        files_to_remove = len(temporary_files) - MAX_FILES_IN_MEMORY
        
        logger.warning(f"Memory limit reached. Removing {files_to_remove} oldest files")
        
        for i in range(files_to_remove):
            del temporary_files[sorted_files[i][0]]

def generate_explanation(converter: RevolutToPIT38, results: dict) -> str:
    """Generuje profesjonalne podsumowanie obliczen w formacie HTML"""
    html = []
    
    html.append('<div style="font-family: inherit; line-height: 1.6;">')
    html.append('<h2 style="color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; margin-bottom: 20px;">Podsumowanie obliczen</h2>')
    
    # Informacja o kursach
    html.append(f'''<div style="background: #f8f9fa; padding: 15px; border-radius: 6px; margin-bottom: 20px; border: 1px solid #e0e0e0;">
        <strong>Kursy walut NBP</strong><br>
        Pobrano {len(converter.converter.cache)} kursow z API Narodowego Banku Polskiego.<br>
        <span style="color: #666;">Kursy z dnia roboczego poprzedzajacego dzien rozliczenia transakcji (art. 11a ustawy o PIT)</span>
    </div>''')
    
    # Tabela zbiorcza
    total_sells = (len(converter.transactions['brokerage_sells_eur']) + 
                   len(converter.transactions['brokerage_sells_usd']))
    total_dividends = (len(converter.transactions['brokerage_dividends_eur']) + 
                       len(converter.transactions['brokerage_dividends_usd']))
    total_interest = (len(converter.transactions['interest_eur']) + 
                      len(converter.transactions['interest_pln']))
    total_crypto = len(converter.transactions['crypto_sells'])
    
    html.append('<h3 style="margin-top: 20px;">Przetworzone transakcje</h3>')
    html.append('<table style="width: 100%; border-collapse: collapse; margin: 15px 0;">')
    html.append('<tr style="background: #f8f9fa;"><th style="padding: 10px; text-align: left; border-bottom: 1px solid #e0e0e0;">Kategoria</th><th style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">Liczba</th></tr>')
    
    if total_sells > 0:
        eur_count = len(converter.transactions['brokerage_sells_eur'])
        usd_count = len(converter.transactions['brokerage_sells_usd'])
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Sprzedaz akcji EUR (T+2)</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{eur_count}</td></tr>')
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Sprzedaz akcji USD (T+1)</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{usd_count}</td></tr>')
    
    if total_crypto > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Sprzedaz kryptowalut</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{total_crypto}</td></tr>')
    
    if total_dividends > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Dywidendy</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{total_dividends}</td></tr>')
    
    if total_interest > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Odsetki z lokat</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{total_interest}</td></tr>')
    
    html.append('</table>')
    
    # Wyniki finansowe
    html.append('<h3 style="margin-top: 30px;">Wyniki finansowe</h3>')
    html.append('<table style="width: 100%; border-collapse: collapse; margin: 15px 0;">')
    html.append('<tr style="background: #f8f9fa;"><th style="padding: 10px; text-align: left; border-bottom: 1px solid #e0e0e0;">Pozycja</th><th style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">Kwota PLN</th></tr>')
    
    if total_sells > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Przychod ze sprzedazy akcji</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_income_brokerage"]:,.2f}</td></tr>')
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Koszty uzyskania przychodu (akcje)</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_cost_brokerage"]:,.2f}</td></tr>')
        html.append(f'<tr style="background: #f0f0f0;"><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;"><strong>Zysk/strata z akcji</strong></td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;"><strong>{results["summary"]["total_profit_brokerage"]:,.2f}</strong></td></tr>')
    
    if total_crypto > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Przychod ze sprzedazy kryptowalut</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_income_crypto"]:,.2f}</td></tr>')
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Koszty uzyskania przychodu (krypto)</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_cost_crypto"]:,.2f}</td></tr>')
        html.append(f'<tr style="background: #f0f0f0;"><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;"><strong>Zysk/strata z kryptowalut</strong></td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;"><strong>{results["summary"]["total_profit_crypto"]:,.2f}</strong></td></tr>')
    
    if total_dividends > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Dywidendy brutto</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_dividends_gross"]:,.2f}</td></tr>')
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Podatek pobrany za granica</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_dividends_tax_paid"]:,.2f}</td></tr>')
    
    if total_interest > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Odsetki z lokat</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{results["summary"]["total_interest"]:,.2f}</td></tr>')
    
    # Uwaga o strukturze PIT-38
    html.append('</table>')
    
    html.append('''<div style="background: #e3f2fd; padding: 12px; border-radius: 6px; margin: 15px 0; border-left: 3px solid #2196f3; font-size: 14px;">
        <strong>Struktura PIT-38:</strong> Akcje wykazuje sie w <strong>Sekcji C</strong> (Inne przychody), 
        kryptowaluty w <strong>Sekcji E</strong>. Strata z kryptowalut NIE pomniejsza zysku z akcji (i odwrotnie).
        Dla dochodow zagranicznych wymagany jest zalacznik <strong>PIT/ZG</strong> (osobny dla kazdego kraju).
    </div>''')
    
    # Obliczenie podatkow (oddzielnie dla akcji i krypto zgodnie z PIT-38)
    tax_stocks = max(0, results['summary']['total_profit_brokerage']) * 0.19
    tax_crypto = max(0, results['summary']['total_profit_crypto']) * 0.19
    tax_interest = results['summary']['total_interest'] * 0.19
    
    if total_dividends > 0:
        tax_dividends = max(0, results['summary']['total_dividends_gross'] * 0.19 - 
                           results['summary']['total_dividends_tax_paid'])
    else:
        tax_dividends = 0
    
    total_tax = tax_stocks + tax_crypto + tax_interest + tax_dividends
    
    html.append('<h3 style="margin-top: 30px;">Szacunkowe zobowiazanie podatkowe</h3>')
    html.append('<table style="width: 100%; border-collapse: collapse; margin: 15px 0;">')
    html.append('<tr style="background: #f8f9fa;"><th style="padding: 10px; text-align: left; border-bottom: 1px solid #e0e0e0;">Podatek</th><th style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">Kwota PLN</th></tr>')
    
    if results['summary']['total_profit_brokerage'] != 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Podatek 19% od akcji (Sekcja C PIT-38)</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{tax_stocks:,.2f}</td></tr>')
    
    if results['summary']['total_profit_crypto'] != 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Podatek 19% od kryptowalut (Sekcja E PIT-38)</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{tax_crypto:,.2f}</td></tr>')
    
    if total_interest > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Podatek Belki 19% od odsetek</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{tax_interest:,.2f}</td></tr>')
    
    if total_dividends > 0:
        html.append(f'<tr><td style="padding: 10px; border-bottom: 1px solid #e0e0e0;">Podatek od dywidend do doplaty</td><td style="padding: 10px; text-align: right; border-bottom: 1px solid #e0e0e0;">{tax_dividends:,.2f}</td></tr>')
    
    html.append(f'<tr style="background: #1a1a2e; color: white;"><td style="padding: 12px;"><strong>RAZEM SZACUNKOWY PODATEK</strong></td><td style="padding: 12px; text-align: right;"><strong>{total_tax:,.2f}</strong></td></tr>')
    html.append('</table>')
    
    # Ostrzezenie i przypomnienie
    html.append('''<div style="background: #fff3cd; padding: 15px; border-radius: 6px; margin-top: 20px; border: 1px solid #ffc107;">
        <strong>Uwaga:</strong> Powyzsze obliczenia maja charakter szacunkowy i informacyjny. 
        Prosimy o weryfikacje wynikow przed zlozeniem zeznania podatkowego. 
        Szczegolowe dane znajduja sie w wygenerowanym pliku Excel.
    </div>''')
    
    html.append('''<div style="background: #e8f5e9; padding: 15px; border-radius: 6px; margin-top: 10px; border: 1px solid #4caf50; font-size: 14px;">
        <strong>Przypomnienie:</strong>
        <ul style="margin: 8px 0 0 20px; line-height: 1.8;">
            <li>Termin zlozenia PIT-38: <strong>30 kwietnia 2026 r.</strong></li>
            <li>Zlozenie PIT-38 jest <strong>obowiazkowe nawet przy stracie</strong></li>
            <li>Strata moze byc odliczana przez <strong>5 kolejnych lat</strong></li>
            <li>Dla dochodow zagranicznych wymagany <strong>zalacznik PIT/ZG</strong> (osobny dla kazdego kraju)</li>
        </ul>
    </div>''')
    
    html.append('</div>')
    
    return ''.join(html)

if __name__ == "__main__":
    import uvicorn
    print("\nUruchamianie serwera...")
    print("Aplikacja dostepna pod adresem: http://localhost:8000")
    print("Dokumentacja API: http://localhost:8000/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
