# Revolut to PIT-8C Converter

Aplikacja webowa do automatycznej konwersji wyciągów z Revolut na raporty PIT-8C dla polskiego rozliczenia podatkowego.

## 🚀 Funkcje

- ✅ Konwersja wyciągów Revolut (CSV) na format Excel dla PIT-8C
- ✅ Automatyczne pobieranie kursów NBP zgodnie z prawem podatkowym
- ✅ Obsługa akcji, dywidend, kryptowalut i odsetek
- ✅ Rozliczenie T+1/T+2 dla rynków USA i europejskich
- ✅ Interfejs webowy - wgraj plik, pobierz raport
- ✅ Deployment z Docker i docker-compose

## 📋 Wymagania

### Dla Docker (zalecane):
- Docker Desktop lub Docker Engine + Docker Compose

### Dla uruchomienia lokalnego:
- Python 3.12+
- pip

## 🐳 Szybki start z Docker

```bash
# Sklonuj repozytorium
git clone <repository-url>
cd revolut-pit8c

# Uruchom aplikację
docker-compose up -d

# Aplikacja dostępna na http://localhost:8000
```

### Zatrzymanie:
```bash
docker-compose down
```

### Sprawdzenie logów:
```bash
docker-compose logs -f
```

## 💻 Instalacja lokalna

```bash
# Sklonuj repozytorium
git clone <repository-url>
cd revolut-pit8c

# Utwórz środowisko wirtualne
python3 -m venv .venv
source .venv/bin/activate  # Na Windows: .venv\Scripts\activate

# Zainstaluj zależności
pip install -r requirements.txt

# Uruchom aplikację
uvicorn main:app --host 0.0.0.0 --port 8000

# Aplikacja dostępna na http://localhost:8000
```

## 📖 Jak używać

1. Wejdź na http://localhost:8000
2. Pobierz wyciąg z Revolut (CSV) za dany rok podatkowy
3. Wgraj plik przez formularz na stronie
4. Pobierz wygenerowany raport Excel z danymi dla PIT-8C

## 📂 Struktura projektu

```
revolut-pit8c/
├── main.py                 # Aplikacja FastAPI
├── revolut_to_pit8c.py    # Logika konwersji i obliczeń
├── requirements.txt        # Zależności Python
├── Dockerfile             # Obraz Docker
├── docker-compose.yml     # Konfiguracja docker-compose
├── .dockerignore          # Pliki ignorowane przez Docker
├── .gitignore             # Pliki ignorowane przez Git
├── README.md              # Ta dokumentacja
├── SETTLEMENT_RULES.md    # Zasady rozliczenia T+1/T+2
└── example_revolut_statement.csv  # Przykładowy plik CSV
```

## 🔧 Konfiguracja

### Zmienne środowiskowe

Możesz skonfigurować aplikację przez zmienne środowiskowe w `docker-compose.yml`:

```yaml
environment:
  - PORT=8000
  - LOG_LEVEL=INFO
```

### Security Features

- **In-memory storage**: Pliki nigdy nie są zapisywane na dysku
- **Secure tokens**: Każdy raport ma unikalny, kryptograficzny token (32 bajty)
- **Single-use downloads**: Raport można pobrać tylko raz
- **Auto-expiration**: Pliki wygasają po 10 minutach
- **Rate limiting**: Maksymalnie 10 zapytań na minutę na IP (z X-Forwarded-For support)
- **File size limit**: Maksymalny rozmiar pliku 10MB
- **Path traversal protection**: Sanityzacja nazw plików
- **Security headers**: X-Frame-Options, CSP, X-Content-Type-Options
- **Privacy logging**: Hash nazw plików w logach

Szczegóły: [SECURITY.md](SECURITY.md)

## 📊 Metodologia obliczeń

Aplikacja stosuje polskie prawo podatkowe:

1. **Kursy walut**: Pobierane z NBP zgodnie z art. 11a ustawy o PIT
   - Kurs z dnia roboczego poprzedzającego datę przychodu/kosztu
   
2. **Data powstania przychodu** (akcje):
   - Europa: T+2 (dzień rozliczenia transakcji)
   - USA: T+1 od 28 maja 2024, wcześniej T+2
   
3. **Kategorie dochodów**:
   - Sprzedaż akcji/ETF: kapitałowe 19%
   - Dywidendy: 19% z odliczeniem podatku zagranicznego
   - Kryptowaluty: kapitałowe 19%
   - Odsetki: podatek Belki 19%

Szczegóły w [SETTLEMENT_RULES.md](SETTLEMENT_RULES.md)

## 🛡️ Bezpieczeństwo

- Pliki CSV są przetwarzane lokalnie, nie są przechowywane
- Brak połączenia z zewnętrznymi serwisami (poza API NBP)
- Raporty Excel generowane lokalnie
- Zalecane używanie HTTPS w środowisku produkcyjnym

## 🚀 Deployment produkcyjny

### Nginx Reverse Proxy

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### SSL/HTTPS

Zalecane użycie Let's Encrypt + Certbot:

```bash
certbot --nginx -d yourdomain.com
```

### Limity zasobów

W `docker-compose.yml`:

```yaml
deploy:
  resources:
    limits:
      cpus: '1.0'
      memory: 512M
```

## ⚠️ Zastrzeżenia

- Aplikacja ma charakter pomocniczy
- Użytkownik odpowiada za weryfikację danych
- Zalecana konsultacja z doradcą podatkowym
- Nie jest to oficjalne narzędzie skarbowe

## 🤝 Wsparcie

W razie problemów:
1. Sprawdź logi: `docker-compose logs`
2. Sprawdź health: `curl http://localhost:8000/health`
3. Zobacz dokumentację metodologii w aplikacji webowej

## 📅 Historia zmian

### v1.0.0 (styczeń 2026)
- ✅ Pierwsza wersja produkcyjna
- ✅ Obsługa T+1 dla USA (od 28.05.2024)
- ✅ Interfejs webowy
- ✅ Docker deployment
- ✅ Automatyczne pobieranie kursów NBP
