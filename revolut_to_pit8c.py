#!/usr/bin/env python3
"""
Skrypt do przetwarzania raportu z Revoluta na dane do PIT-38
z przeliczeniem transakcji według kursów NBP
"""

import pandas as pd
import numpy as np
from datetime import datetime
import requests
from typing import Dict, Optional
import time
import re

def sanitize_excel_value(value):
    """
    Sanitize value to prevent Excel formula injection.
    Prefixes dangerous characters with single quote.
    """
    if isinstance(value, str):
        # Characters that could start a formula in Excel
        dangerous_chars = ('=', '+', '-', '@', '\t', '\r', '\n')
        if value.startswith(dangerous_chars):
            return "'" + value
    return value

def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Sanitize all string values in DataFrame to prevent Excel injection"""
    return df.applymap(sanitize_excel_value) if not df.empty else df

class NBPCurrencyConverter:
    """Klasa do pobierania kursów walut z API NBP"""
    
    def __init__(self):
        self.cache = {}
        self.base_url = "https://api.nbp.pl/api/exchangerates/rates/a/{currency}/{date}/?format=json"
    
    def get_rate(self, currency: str, date: str) -> Optional[float]:
        """
        Pobiera kurs waluty z NBP dla danej daty
        
        Args:
            currency: Kod waluty (np. 'USD', 'EUR')
            date: Data w formacie 'YYYY-MM-DD'
        
        Returns:
            Kurs waluty lub None jeśli nie znaleziono
        """
        # Sprawdź cache
        cache_key = f"{currency}_{date}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        # OPTYMALIZACJA: Jeśli brak kursu (weekend/święto), najpierw sprawdź cache dla poprzednich dni
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        for i in range(1, 5):  # Sprawdź 4 poprzednie dni w cache
            prev_date = (date_obj - pd.Timedelta(days=i)).strftime('%Y-%m-%d')
            prev_cache_key = f"{currency}_{prev_date}"
            if prev_cache_key in self.cache:
                # Zapisz ten sam kurs dla requested date
                self.cache[cache_key] = self.cache[prev_cache_key]
                return self.cache[prev_cache_key]
        
        # Jeśli nie ma w cache, pobierz z API
        try:
            url = self.base_url.format(currency=currency.lower(), date=date)
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                rate = data['rates'][0]['mid']
                self.cache[cache_key] = rate
                return rate
            elif response.status_code == 404:
                # Jeśli brak kursu (weekend/święto), spróbuj poprzedni dzień
                for i in range(1, 4):  # Spróbuj 3 poprzednie dni
                    prev_date = (date_obj - pd.Timedelta(days=i)).strftime('%Y-%m-%d')
                    prev_rate = self.get_rate(currency, prev_date)
                    if prev_rate:
                        self.cache[cache_key] = prev_rate
                        return prev_rate
            
            print(f"Nie można pobrać kursu {currency} dla daty {date}")
            return None
            
        except Exception as e:
            print(f"Błąd przy pobieraniu kursu: {e}")
            return None
        finally:
            time.sleep(0.1)  # Opóźnienie, aby nie przeciążyć API

def parse_currency_value(value_str: str) -> tuple:
    """
    Parsuje wartość walutową z tekstu
    
    Args:
        value_str: String z wartością, np. "$1,234.56" lub "1 234,56€" (polski format)
    
    Returns:
        Tuple (wartość, waluta)
    """
    if pd.isna(value_str) or value_str == '':
        return 0.0, None
    
    value_str = str(value_str).strip()
    original_str = value_str
    
    # Wykryj walutę
    currency = None
    if '$' in value_str or 'US$' in value_str:
        currency = 'USD'
        value_str = value_str.replace('US$', '').replace('$', '')
    elif '€' in value_str:
        currency = 'EUR'
        value_str = value_str.replace('€', '')
    elif 'PLN' in value_str:
        currency = 'PLN'
        value_str = value_str.replace('PLN', '')
    
    # Usuń spacje
    value_str = value_str.replace(' ', '').strip()
    
    # Wykryj format: polski (1.234,56) vs angielski (1,234.56)
    # Polski: przecinek jako separator dziesiętny
    # Angielski: kropka jako separator dziesiętny
    if ',' in value_str and '.' in value_str:
        # Oba separatory - sprawdź który jest ostatni
        last_comma = value_str.rfind(',')
        last_dot = value_str.rfind('.')
        if last_comma > last_dot:
            # Polski format: 1.234,56
            value_str = value_str.replace('.', '').replace(',', '.')
        else:
            # Angielski format: 1,234.56
            value_str = value_str.replace(',', '')
    elif ',' in value_str:
        # Tylko przecinek - może być polski (dziesiętny) lub angielski (tysiące)
        # Jeśli są 3 cyfry po przecinku lub brak cyfr po przecinku -> tysiące
        parts = value_str.split(',')
        if len(parts[-1]) == 3 or len(parts[-1]) == 0:
            # Separator tysięcy
            value_str = value_str.replace(',', '')
        else:
            # Separator dziesiętny (polski)
            value_str = value_str.replace(',', '.')
    # Jeśli tylko kropka, zostaw bez zmian (angielski format)
    
    try:
        value = float(value_str)
        return value, currency
    except ValueError:
        return 0.0, currency

def get_previous_working_day(date_str: str) -> str:
    """
    Zwraca poprzedni dzień ROBOCZY przed podaną datą
    (zgodnie z art. 11a ustawy o PIT - kurs średni NBP z ostatniego dnia roboczego
    poprzedzającego dzień uzyskania przychodu lub poniesienia kosztu)
    
    WAŻNE: Funkcja pomija weekendy!
    - Jeśli podano poniedziałek → zwraca piątek (nie niedzielę!)
    - Jeśli podano wtorek-piątek → zwraca poprzedni dzień
    - Jeśli podano sobotę → zwraca piątek
    - Jeśli podano niedzielę → zwraca piątek
    
    Args:
        date_str: Data w formacie 'YYYY-MM-DD'
    
    Returns:
        Data poprzedniego dnia ROBOCZEGO w formacie 'YYYY-MM-DD'
    """
    if not date_str:
        return None
    
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    # Cofnij się o 1 dzień
    prev_date = date_obj - pd.Timedelta(days=1)
    
    # Jeśli to weekend, cofnij do piątku
    while prev_date.weekday() >= 5:  # 5=sobota, 6=niedziela
        prev_date = prev_date - pd.Timedelta(days=1)
    
    return prev_date.strftime('%Y-%m-%d')

def add_trading_days(date_str: str, days: int) -> str:
    """
    Dodaje określoną liczbę dni roboczych (pomijając weekendy) do daty
    Używane dla obliczenia daty rozliczenia dla akcji:
    - T+1 dla akcji USA (od 28 maja 2024)
    - T+2 dla akcji europejskich
    
    Args:
        date_str: Data w formacie 'YYYY-MM-DD'
        days: Liczba dni roboczych do dodania (1 dla USA, 2 dla EUR)
    
    Returns:
        Data po dodaniu dni roboczych w formacie 'YYYY-MM-DD'
    """
    if not date_str:
        return None
    
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    added = 0
    
    while added < days:
        date_obj += pd.Timedelta(days=1)
        # Pomiń weekendy (sobota=5, niedziela=6)
        if date_obj.weekday() < 5:
            added += 1
    
    return date_obj.strftime('%Y-%m-%d')

def parse_date(date_str: str) -> str:
    """
    Parsuje datę z różnych formatów do YYYY-MM-DD
    """
    if pd.isna(date_str):
        return None
    
    date_str = str(date_str).strip()
    
    # Mapowanie polskich nazw miesięcy
    polish_months = {
        'sty': 'Jan', 'lut': 'Feb', 'mar': 'Mar', 'kwi': 'Apr',
        'maj': 'May', 'cze': 'Jun', 'lip': 'Jul', 'sie': 'Aug',
        'wrz': 'Sep', 'paź': 'Oct', 'lis': 'Nov', 'gru': 'Dec'
    }
    
    try:
        # Format: "Jan 1, 2025" (angielski)
        date_obj = datetime.strptime(date_str, '%b %d, %Y')
        return date_obj.strftime('%Y-%m-%d')
    except:
        pass
    
    try:
        # Format: "1 sty 2025" (polski)
        parts = date_str.split()
        if len(parts) == 3:
            day, month_pl, year = parts
            if month_pl in polish_months:
                month_en = polish_months[month_pl]
                date_str_en = f"{month_en} {day}, {year}"
                date_obj = datetime.strptime(date_str_en, '%b %d, %Y')
                return date_obj.strftime('%Y-%m-%d')
    except:
        pass
    
    try:
        # Format: "YYYY-MM-DD"
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        return date_obj.strftime('%Y-%m-%d')
    except:
        pass
    
    return None

class RevolutToPIT38:
    """Główna klasa do przetwarzania danych Revolut na dane do PIT-38"""
    
    def __init__(self, csv_file: str):
        self.csv_file = csv_file
        self.converter = NBPCurrencyConverter()
        self.transactions = {
            'brokerage_sells_eur': [],
            'brokerage_sells_usd': [],
            'brokerage_dividends_eur': [],
            'brokerage_dividends_usd': [],
            'crypto_sells': [],
            'interest_eur': [],
            'interest_pln': []
        }
        
    def read_csv(self):
        """Wczytuje plik CSV z Revoluta"""
        with open(self.csv_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    
    def parse_brokerage_sells(self, lines: list, start_idx: int, currency: str) -> int:
        """Parsuje transakcje sprzedaży akcji"""
        import csv
        import io
        
        idx = start_idx + 1  # Pomiń "Portfolio created"
        
        # Pomiń linię nagłówka CSV
        if idx < len(lines) and 'Date acquired' in lines[idx]:
            idx += 1
        
        # Zbierz linie do parsowania - kontynuuj nawet przez puste linie aż do nowej sekcji
        csv_lines = []
        while idx < len(lines):
            line = lines[idx].strip()
            # Zatrzymaj się na nowej sekcji
            if 'Transactions for' in line or line.startswith('Summary for'):
                break
            # Dodaj niepuste linie
            if line:
                csv_lines.append(lines[idx])
            idx += 1
        
        if not csv_lines:
            return idx
        
        print(f"    Zebrano {len(csv_lines)} linii do parsowania CSV")
        
        # Parsuj jako CSV - łącz z zachowaniem nowych linii jeśli ich brak
        csv_text = ''.join(csv_lines)
        if not csv_text.endswith('\n'):
            # Dodaj \n między liniami jeśli ich nie ma
            csv_text = '\n'.join([line.rstrip('\n') for line in csv_lines]) + '\n'
        
        csv_reader = csv.reader(io.StringIO(csv_text))
        count = 0
        for row_num, row in enumerate(csv_reader):
            if len(row) < 14:
                continue
            
            # Pomiń nagłówki jeśli jeszcze się gdzieś znalazły
            if row[0] == 'Date acquired':
                continue
            
            date_acquired = parse_date(row[0])
            date_sold = parse_date(row[1])
            security_name = row[2]
            symbol = row[3]
            qty = row[6]
            cost_basis_str = row[7]
            gross_proceeds_str = row[10]
            gross_pnl_str = row[13]
            
            cost_basis, _ = parse_currency_value(cost_basis_str)
            gross_proceeds, _ = parse_currency_value(gross_proceeds_str)
            gross_pnl, _ = parse_currency_value(gross_pnl_str)
            
            self.transactions[f'brokerage_sells_{currency.lower()}'].append({
                'date_acquired': date_acquired,
                'date_sold': date_sold,
                'security_name': security_name,
                'symbol': symbol,
                'qty': qty,
                'cost_basis': cost_basis,
                'gross_proceeds': gross_proceeds,
                'gross_pnl': gross_pnl,
                'currency': currency
            })
            count += 1
        
        print(f"    Sparsowano {count} transakcji {currency}")
        return idx
    
    def parse_brokerage_dividends(self, lines: list, start_idx: int, currency: str) -> int:
        """Parsuje dywidendy z akcji"""
        import csv
        import io
        
        idx = start_idx + 1  # Pomiń "Portfolio created"
        
        # Pomiń linię nagłówka CSV
        if idx < len(lines) and 'Date' in lines[idx] and 'Security name' in lines[idx]:
            idx += 1
        
        # Zbierz linie do parsowania - kontynuuj nawet przez puste linie
        csv_lines = []
        while idx < len(lines):
            line = lines[idx].strip()
            # Zatrzymaj się na nowej sekcji
            if 'Transactions for' in line or line.startswith('Summary for'):
                break
            # Dodaj niepuste linie
            if line:
                csv_lines.append(lines[idx])
            idx += 1
        
        if not csv_lines:
            return idx
        
        # Parsuj jako CSV - łącz z zachowaniem nowych linii
        csv_text = ''.join(csv_lines)
        if not csv_text.endswith('\n'):
            csv_text = '\n'.join([line.rstrip('\n') for line in csv_lines]) + '\n'
        
        csv_reader = csv.reader(io.StringIO(csv_text))
        for row in csv_reader:
            if len(row) < 11:
                continue
            
            # Pomiń nagłówki
            if row[0] == 'Date':
                continue
            
            date = parse_date(row[0])
            security_name = row[1]
            symbol = row[2]
            gross_amount_str = row[5]
            withholding_tax_str = row[8]
            net_amount_str = row[10]
            
            gross_amount, _ = parse_currency_value(gross_amount_str)
            withholding_tax, _ = parse_currency_value(withholding_tax_str)
            net_amount, _ = parse_currency_value(net_amount_str)
            
            self.transactions[f'brokerage_dividends_{currency.lower()}'].append({
                'date': date,
                'security_name': security_name,
                'symbol': symbol,
                'gross_amount': gross_amount,
                'withholding_tax': withholding_tax,
                'net_amount': net_amount,
                'currency': currency
            })
        
        return idx
    
    def parse_interest(self, lines: list, start_idx: int, currency: str) -> int:
        """Parsuje odsetki z lokat oszczędnościowych"""
        import csv
        import io
        
        idx = start_idx
        
        # Pomiń linię nagłówka CSV
        if idx < len(lines) and 'Date' in lines[idx] and 'Description' in lines[idx]:
            idx += 1
        
        # Zbierz linie do parsowania
        csv_lines = []
        while idx < len(lines):
            line = lines[idx].strip()
            if 'Transactions for' in line or line.startswith('Summary for'):
                break
            if line:
                csv_lines.append(lines[idx])
            idx += 1
        
        if not csv_lines:
            return idx
        
        # Parsuj jako CSV
        csv_text = ''.join(csv_lines)
        if not csv_text.endswith('\n'):
            csv_text = '\n'.join([line.rstrip('\n') for line in csv_lines]) + '\n'
        
        csv_reader = csv.reader(io.StringIO(csv_text))
        for row in csv_reader:
            if len(row) < 4:
                continue
            
            # Pomiń nagłówki
            if row[0] == 'Date':
                continue
            
            date = parse_date(row[0])
            description = row[1]
            
            # Szukaj odsetek (Interest earned)
            if 'Interest earned' not in description:
                continue
            
            money_in_str = row[3] if len(row) > 3 else ''
            amount, _ = parse_currency_value(money_in_str)
            
            if amount > 0:
                self.transactions[f'interest_{currency.lower()}'].append({
                    'date': date,
                    'amount': amount,
                    'currency': currency
                })
        
        return idx
    
    def parse_crypto_sells(self, lines: list, start_idx: int) -> int:
        """Parsuje sprzedaż kryptowalut"""
        import csv
        import io
        
        idx = start_idx
        
        # Pomiń linię nagłówka CSV
        if idx < len(lines) and 'Date acquired' in lines[idx]:
            idx += 1
        
        # Zbierz linie do parsowania - kontynuuj nawet przez puste linie
        csv_lines = []
        while idx < len(lines):
            line = lines[idx].strip()
            # Zatrzymaj się na nowej sekcji lub końcu pliku
            if 'Transactions for' in line or line.startswith('Summary for') or 'Summary for' in line:
                break
            # Dodaj niepuste linie
            if line:
                csv_lines.append(lines[idx])
            idx += 1
        
        if not csv_lines:
            return idx
        
        # Parsuj jako CSV - łącz z zachowaniem nowych linii
        csv_text = ''.join(csv_lines)
        if not csv_text.endswith('\n'):
            csv_text = '\n'.join([line.rstrip('\n') for line in csv_lines]) + '\n'
        
        csv_reader = csv.reader(io.StringIO(csv_text))
        for row in csv_reader:
            if len(row) < 7:
                continue
            
            # Pomiń nagłówki
            if row[0] == 'Date acquired':
                continue
            
            date_acquired = parse_date(row[0])
            date_sold = parse_date(row[1])
            token_name = row[2]
            qty = row[3]
            cost_basis_str = row[4]
            gross_proceeds_str = row[5]
            gross_pnl_str = row[6]
            
            cost_basis, _ = parse_currency_value(cost_basis_str)
            gross_proceeds, _ = parse_currency_value(gross_proceeds_str)
            gross_pnl, _ = parse_currency_value(gross_pnl_str)
            
            self.transactions['crypto_sells'].append({
                'date_acquired': date_acquired,
                'date_sold': date_sold,
                'token_name': token_name,
                'qty': qty,
                'cost_basis': cost_basis,
                'gross_proceeds': gross_proceeds,
                'gross_pnl': gross_pnl,
                'currency': 'USD'
            })
        
        return idx
    
    def parse_file(self):
        """Parsuje cały plik CSV"""
        content = self.read_csv()
        lines = content.split('\n')
        
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            
            if 'Transactions for Brokerage Account sells - EUR' in line:
                print(f"  Znaleziono sekcję EUR sells w linii {idx}")
                idx = self.parse_brokerage_sells(lines, idx + 1, 'EUR')
            elif 'Transactions for Brokerage Account sells - USD' in line:
                print(f"  Znaleziono sekcję USD sells w linii {idx}")
                idx = self.parse_brokerage_sells(lines, idx + 1, 'USD')
            elif 'Transactions for Brokerage Account dividends - EUR' in line:
                print(f"  Znaleziono sekcję EUR dividends w linii {idx}")
                idx = self.parse_brokerage_dividends(lines, idx + 1, 'EUR')
            elif 'Transactions for Brokerage Account dividends - USD' in line:
                print(f"  Znaleziono sekcję USD dividends w linii {idx}")
                idx = self.parse_brokerage_dividends(lines, idx + 1, 'USD')
            elif line == 'Transactions for Crypto':
                print(f"  Znaleziono sekcję Crypto w linii {idx}")
                idx = self.parse_crypto_sells(lines, idx + 1)
            elif 'Transactions for Savings Accounts - EUR' in line:
                print(f"  Znaleziono sekcję EUR interest w linii {idx}")
                idx = self.parse_interest(lines, idx + 1, 'EUR')
            elif 'Transactions for Savings Accounts - PLN' in line:
                print(f"  Znaleziono sekcję PLN interest w linii {idx}")
                idx = self.parse_interest(lines, idx + 1, 'PLN')
            else:
                idx += 1
    
    def convert_to_pln(self, amount: float, currency: str, date: str, return_rate: bool = False):
        """
        Przelicza kwotę na PLN według kursu NBP z dnia roboczego poprzedzającego transakcję
        (zgodnie z art. 11a ustawy o PIT)
        
        Args:
            return_rate: Jeśli True, zwraca krotkę (kwota_pln, kurs, data_kursu)
        """
        if currency == 'PLN':
            if return_rate:
                return amount, 1.0, date
            return amount
        
        if not date:
            if return_rate:
                return 0.0, 0.0, None
            return 0.0
        
        # Użyj kursu z poprzedniego dnia roboczego
        prev_day = get_previous_working_day(date)
        if not prev_day:
            if return_rate:
                return 0.0, 0.0, None
            return 0.0
        
        rate = self.converter.get_rate(currency, prev_day)
        if rate:
            if return_rate:
                return amount * rate, rate, prev_day
            return amount * rate
        
        if return_rate:
            return 0.0, 0.0, prev_day
        return 0.0
    
    def preload_nbp_rates(self, year: int = 2025):
        """
        Pobiera kursy NBP dla całego roku jednorazowo
        Pobiera także kursy z końca poprzedniego roku (dla transakcji na początku stycznia)
        """
        print("\n=== POBIERANIE KURSÓW NBP ===\n")
        
        # API NBP pozwala pobrać wszystkie kursy dla danego roku
        url_template = "https://api.nbp.pl/api/exchangerates/rates/a/{currency}/{start_date}/{end_date}/?format=json"
        
        for currency in ['EUR', 'USD']:
            # Pobierz kursy z końca poprzedniego roku (ostatnie 10 dni)
            # To zapewni kursy dla transakcji na początku stycznia
            prev_year_start = f"{year-1}-12-20"
            prev_year_end = f"{year-1}-12-31"
            
            print(f"Pobieranie kursów {currency} z końca {year-1}...")
            try:
                url = url_template.format(currency=currency.lower(), start_date=prev_year_start, end_date=prev_year_end)
                response = requests.get(url, timeout=30)
                
                if response.status_code == 200:
                    data = response.json()
                    rates = data['rates']
                    
                    for rate_entry in rates:
                        date = rate_entry['effectiveDate']
                        rate = rate_entry['mid']
                        cache_key = f"{currency}_{date}"
                        self.converter.cache[cache_key] = rate
                    
                    print(f"  ✓ Pobrano {len(rates)} kursów {currency} z {year-1}")
            except Exception as e:
                print(f"  ✗ Błąd: {e}")
            
            time.sleep(0.1)
            
            # Pobierz kursy z całego roku
            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"
            
            print(f"Pobieranie kursów {currency} dla roku {year}...")
            try:
                url = url_template.format(currency=currency.lower(), start_date=start_date, end_date=end_date)
                response = requests.get(url, timeout=30)
                
                if response.status_code == 200:
                    data = response.json()
                    rates = data['rates']
                    
                    # Zapisz wszystkie kursy do cache
                    for rate_entry in rates:
                        date = rate_entry['effectiveDate']
                        rate = rate_entry['mid']
                        cache_key = f"{currency}_{date}"
                        self.converter.cache[cache_key] = rate
                    
                    print(f"  ✓ Pobrano {len(rates)} kursów {currency} z {year}")
                else:
                    print(f"  ✗ Błąd pobierania kursów {currency}: status {response.status_code}")
            except Exception as e:
                print(f"  ✗ Błąd: {e}")
            
            time.sleep(0.1)  # Krótkie wait między walutami
        
        print(f"\n✓ Łącznie w cache: {len(self.converter.cache)} kursów\n")
    
    def calculate_pit38_data(self) -> Dict:
        """Oblicza dane dla PIT-38"""
        print("\n=== OBLICZANIE DANYCH DLA PIT-38 ===\n")
        
        results = {
            'brokerage_sells': [],
            'dividends': [],
            'crypto_sells': [],
            'interest': [],
            'summary': {
                'total_income_brokerage': 0,
                'total_cost_brokerage': 0,
                'total_profit_brokerage': 0,
                'total_dividends_gross': 0,
                'total_dividends_tax_paid': 0,
                'total_income_crypto': 0,
                'total_cost_crypto': 0,
                'total_profit_crypto': 0,
                'total_interest': 0
            }
        }
        
        # Przetwarza sprzedaż akcji (EUR)
        print("Przetwarzanie sprzedaży akcji (EUR)...")
        for trans in self.transactions['brokerage_sells_eur']:
            date_sold = trans['date_sold']
            date_acquired = trans['date_acquired']
            if not date_sold:
                continue
            
            # WAŻNE: Dla akcji europejskich przychód powstaje w dniu rozliczenia T+2
            # (rynki europejskie nadal stosują T+2)
            settlement_date_sell = add_trading_days(date_sold, 2)
            
            # WAŻNE: Koszt nabycia też powstaje w dniu rozliczenia ZAKUPU (T+2)
            # Kurs NBP z dnia roboczego poprzedzającego datę rozliczenia zakupu
            settlement_date_buy = add_trading_days(date_acquired, 2) if date_acquired else None
            
            cost_basis_pln, rate_buy, rate_date_buy = self.convert_to_pln(trans['cost_basis'], 'EUR', settlement_date_buy, return_rate=True)
            gross_proceeds_pln, rate_sell, rate_date_sell = self.convert_to_pln(trans['gross_proceeds'], 'EUR', settlement_date_sell, return_rate=True)
            profit_pln = gross_proceeds_pln - cost_basis_pln
            
            results['brokerage_sells'].append({
                'data_sprzedazy': date_sold,
                'data_rozliczenia_sprzedazy': settlement_date_sell,
                'data_zakupu': date_acquired,
                'data_rozliczenia_zakupu': settlement_date_buy,
                'nazwa_papieru': trans['security_name'],
                'symbol': trans['symbol'],
                'ilosc': trans['qty'],
                'przychod_pln': round(gross_proceeds_pln, 2),
                'koszt_pln': round(cost_basis_pln, 2),
                'zysk_strata_pln': round(profit_pln, 2),
                'waluta_oryginalna': 'EUR',
                'przychod_waluta': trans['gross_proceeds'],
                'koszt_waluta': trans['cost_basis'],
                'kurs_sprzedaz': round(rate_sell, 4) if rate_sell else None,
                'data_kursu_sprzedaz': rate_date_sell,
                'kurs_zakup': round(rate_buy, 4) if rate_buy else None,
                'data_kursu_zakup': rate_date_buy
            })
            
            results['summary']['total_income_brokerage'] += gross_proceeds_pln
            results['summary']['total_cost_brokerage'] += cost_basis_pln
            results['summary']['total_profit_brokerage'] += profit_pln
        
        # Przetwarza sprzedaż akcji (USD)
        print("Przetwarzanie sprzedaży akcji (USD)...")
        for trans in self.transactions['brokerage_sells_usd']:
            date_sold = trans['date_sold']
            date_acquired = trans['date_acquired']
            if not date_sold:
                continue
            
            # WAŻNE: Od 28 maja 2024 w USA obowiązuje T+1 (wcześniej było T+2)
            # Dla transakcji przed 28.05.2024 używamy T+2, po tej dacie T+1
            date_sold_obj = datetime.strptime(date_sold, '%Y-%m-%d')
            t1_start_date = datetime(2024, 5, 28)
            
            # Ustal dni rozliczenia dla SPRZEDAŻY
            if date_sold_obj >= t1_start_date:
                settlement_days_sell = 1  # T+1 dla USA od 28.05.2024
            else:
                settlement_days_sell = 2  # T+2 dla starszych transakcji
            
            settlement_date_sell = add_trading_days(date_sold, settlement_days_sell)
            
            # Ustal dni rozliczenia dla ZAKUPU (też zależy od daty zakupu!)
            if date_acquired:
                date_acquired_obj = datetime.strptime(date_acquired, '%Y-%m-%d')
                if date_acquired_obj >= t1_start_date:
                    settlement_days_buy = 1  # T+1 dla zakupów od 28.05.2024
                else:
                    settlement_days_buy = 2  # T+2 dla starszych zakupów
                settlement_date_buy = add_trading_days(date_acquired, settlement_days_buy)
            else:
                settlement_date_buy = None
            
            cost_basis_pln, rate_buy, rate_date_buy = self.convert_to_pln(trans['cost_basis'], 'USD', settlement_date_buy, return_rate=True)
            gross_proceeds_pln, rate_sell, rate_date_sell = self.convert_to_pln(trans['gross_proceeds'], 'USD', settlement_date_sell, return_rate=True)
            profit_pln = gross_proceeds_pln - cost_basis_pln
            
            results['brokerage_sells'].append({
                'data_sprzedazy': date_sold,
                'data_rozliczenia_sprzedazy': settlement_date_sell,
                'data_zakupu': date_acquired,
                'data_rozliczenia_zakupu': settlement_date_buy,
                'nazwa_papieru': trans['security_name'],
                'symbol': trans['symbol'],
                'ilosc': trans['qty'],
                'przychod_pln': round(gross_proceeds_pln, 2),
                'koszt_pln': round(cost_basis_pln, 2),
                'zysk_strata_pln': round(profit_pln, 2),
                'waluta_oryginalna': 'USD',
                'przychod_waluta': trans['gross_proceeds'],
                'koszt_waluta': trans['cost_basis'],
                'kurs_sprzedaz': round(rate_sell, 4) if rate_sell else None,
                'data_kursu_sprzedaz': rate_date_sell,
                'kurs_zakup': round(rate_buy, 4) if rate_buy else None,
                'data_kursu_zakup': rate_date_buy
            })
            
            results['summary']['total_income_brokerage'] += gross_proceeds_pln
            results['summary']['total_cost_brokerage'] += cost_basis_pln
            results['summary']['total_profit_brokerage'] += profit_pln
        
        # Przetwarza dywidendy (EUR)
        print("Przetwarzanie dywidend (EUR)...")
        for trans in self.transactions['brokerage_dividends_eur']:
            date = trans['date']
            if not date:
                continue
            
            gross_pln, rate, rate_date = self.convert_to_pln(trans['gross_amount'], 'EUR', date, return_rate=True)
            tax_pln = self.convert_to_pln(trans['withholding_tax'], 'EUR', date)
            
            results['dividends'].append({
                'data': date,
                'nazwa_papieru': trans['security_name'],
                'symbol': trans['symbol'],
                'kwota_brutto_pln': round(gross_pln, 2),
                'podatek_pobrany_pln': round(tax_pln, 2),
                'kwota_netto_pln': round(gross_pln - tax_pln, 2),
                'waluta_oryginalna': 'EUR',
                'kwota_brutto_waluta': trans['gross_amount'],
                'podatek_waluta': trans['withholding_tax'],
                'kurs_nbp': round(rate, 4) if rate else None,
                'data_kursu': rate_date,
                'kraj': 'Europa'
            })
            
            results['summary']['total_dividends_gross'] += gross_pln
            results['summary']['total_dividends_tax_paid'] += tax_pln
        
        # Przetwarza dywidendy (USD)
        print("Przetwarzanie dywidend (USD)...")
        for trans in self.transactions['brokerage_dividends_usd']:
            date = trans['date']
            if not date:
                continue
            
            gross_pln, rate, rate_date = self.convert_to_pln(trans['gross_amount'], 'USD', date, return_rate=True)
            tax_pln = self.convert_to_pln(trans['withholding_tax'], 'USD', date)
            
            results['dividends'].append({
                'data': date,
                'nazwa_papieru': trans['security_name'],
                'symbol': trans['symbol'],
                'kwota_brutto_pln': round(gross_pln, 2),
                'podatek_pobrany_pln': round(tax_pln, 2),
                'kwota_netto_pln': round(gross_pln - tax_pln, 2),
                'waluta_oryginalna': 'USD',
                'kwota_brutto_waluta': trans['gross_amount'],
                'podatek_waluta': trans['withholding_tax'],
                'kurs_nbp': round(rate, 4) if rate else None,
                'data_kursu': rate_date,
                'kraj': 'USA'
            })
            
            results['summary']['total_dividends_gross'] += gross_pln
            results['summary']['total_dividends_tax_paid'] += tax_pln
        
        # Przetwarza odsetki EUR
        print("Przetwarzanie odsetek EUR...")
        for trans in self.transactions['interest_eur']:
            date = trans['date']
            if not date:
                continue
            
            amount_pln, rate, rate_date = self.convert_to_pln(trans['amount'], 'EUR', date, return_rate=True)
            
            results['interest'].append({
                'data': date,
                'kwota_pln': round(amount_pln, 2),
                'waluta_oryginalna': 'EUR',
                'kwota_waluta': trans['amount'],
                'kurs_nbp': round(rate, 4) if rate else None,
                'data_kursu': rate_date
            })
            
            results['summary']['total_interest'] += amount_pln
        
        # Przetwarza odsetki PLN
        print("Przetwarzanie odsetek PLN...")
        for trans in self.transactions['interest_pln']:
            date = trans['date']
            if not date:
                continue
            
            amount_pln = trans['amount']  # Już w PLN
            
            results['interest'].append({
                'data': date,
                'kwota_pln': round(amount_pln, 2),
                'waluta_oryginalna': 'PLN',
                'kwota_waluta': trans['amount'],
                'kurs_nbp': 1.0,
                'data_kursu': date
            })
            
            results['summary']['total_interest'] += amount_pln
        
        # Przetwarza kryptowaluty
        print("Przetwarzanie kryptowalut...")
        for trans in self.transactions['crypto_sells']:
            date_sold = trans['date_sold']
            if not date_sold:
                continue
            
            cost_basis_pln, rate_buy, rate_date_buy = self.convert_to_pln(trans['cost_basis'], 'USD', trans['date_acquired'], return_rate=True)
            gross_proceeds_pln, rate_sell, rate_date_sell = self.convert_to_pln(trans['gross_proceeds'], 'USD', date_sold, return_rate=True)
            profit_pln = gross_proceeds_pln - cost_basis_pln
            
            results['crypto_sells'].append({
                'data_sprzedazy': date_sold,
                'data_zakupu': trans['date_acquired'],
                'token': trans['token_name'],
                'ilosc': trans['qty'],
                'przychod_pln': round(gross_proceeds_pln, 2),
                'koszt_pln': round(cost_basis_pln, 2),
                'zysk_strata_pln': round(profit_pln, 2),
                'waluta_oryginalna': 'USD',
                'przychod_waluta': trans['gross_proceeds'],
                'koszt_waluta': trans['cost_basis'],
                'kurs_sprzedaz': round(rate_sell, 4) if rate_sell else None,
                'data_kursu_sprzedaz': rate_date_sell,
                'kurs_zakup': round(rate_buy, 4) if rate_buy else None,
                'data_kursu_zakup': rate_date_buy
            })
            
            results['summary']['total_income_crypto'] += gross_proceeds_pln
            results['summary']['total_cost_crypto'] += cost_basis_pln
            results['summary']['total_profit_crypto'] += profit_pln
        
        return results
    
    def generate_report(self, output_file: str = 'raport_pit38_2025.xlsx', results: dict = None):
        """Generuje raport w formacie Excel z danymi do PIT-38"""
        print("\n=== GENEROWANIE RAPORTU Z DANYMI DO PIT-38 ===\n")
        
        # Jeśli nie przekazano wyników, oblicz je
        if results is None:
            self.parse_file()
            self.preload_nbp_rates()
            results = self.calculate_pit38_data()
        
        # Utwórz Excel z wieloma arkuszami
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            # Arkusz 1: Sprzedaż akcji
            if results['brokerage_sells']:
                df_sells = sanitize_dataframe(pd.DataFrame(results['brokerage_sells']))
                df_sells.to_excel(writer, sheet_name='Sprzedaż akcji', index=False)
                print(f"✓ Zapisano {len(df_sells)} transakcji sprzedaży akcji")
            
            # Arkusz 2: Dywidendy
            if results['dividends']:
                df_dividends = sanitize_dataframe(pd.DataFrame(results['dividends']))
                df_dividends.to_excel(writer, sheet_name='Dywidendy', index=False)
                print(f"✓ Zapisano {len(df_dividends)} transakcji dywidend")
            
            # Arkusz 3: Kryptowaluty
            if results['crypto_sells']:
                df_crypto = sanitize_dataframe(pd.DataFrame(results['crypto_sells']))
                df_crypto.to_excel(writer, sheet_name='Kryptowaluty', index=False)
                print(f"✓ Zapisano {len(df_crypto)} transakcji kryptowalut")
            
            # Arkusz 4: Odsetki z lokat
            if results['interest']:
                df_interest = sanitize_dataframe(pd.DataFrame(results['interest']))
                df_interest.to_excel(writer, sheet_name='Odsetki z lokat', index=False)
                print(f"✓ Zapisano {len(df_interest)} transakcji odsetek")
            
            # Arkusz 5: PIT/ZG - Dywidendy wg krajów
            if results['dividends']:
                pit_zg_data = {}
                for div in results['dividends']:
                    kraj = div.get('kraj', 'Nieznany')
                    if kraj not in pit_zg_data:
                        pit_zg_data[kraj] = {'kwota_brutto_pln': 0, 'podatek_pobrany_pln': 0}
                    pit_zg_data[kraj]['kwota_brutto_pln'] += div['kwota_brutto_pln']
                    pit_zg_data[kraj]['podatek_pobrany_pln'] += div['podatek_pobrany_pln']
                
                pit_zg_rows = []
                for kraj, data in pit_zg_data.items():
                    podatek_pl = round(data['kwota_brutto_pln'] * 0.19, 2)
                    do_zaplaty = round(max(0, podatek_pl - data['podatek_pobrany_pln']), 2)
                    pit_zg_rows.append({
                        'Kraj': kraj,
                        'Dywidendy brutto PLN': round(data['kwota_brutto_pln'], 2),
                        'Podatek pobrany za granicą PLN': round(data['podatek_pobrany_pln'], 2),
                        'Podatek należny w PL (19%)': podatek_pl,
                        'Podatek do dopłaty w PL': do_zaplaty,
                    })
                
                df_pit_zg = sanitize_dataframe(pd.DataFrame(pit_zg_rows))
                df_pit_zg.to_excel(writer, sheet_name='PIT-ZG (wg krajów)', index=False)
                print(f"✓ Zapisano dane PIT/ZG dla {len(pit_zg_data)} krajów")
            
            # Arkusz 6: Podsumowanie PIT-38
            summary_data = {
                'Kategoria': [
                    '═══ PIT-38 SEKCJA C — AKCJE (Inne przychody) ═══',
                    'AKCJE — Przychód ze sprzedaży',
                    'AKCJE — Koszty uzyskania przychodu',
                    'AKCJE — Zysk/Strata',
                    '',
                    '═══ PIT-38 SEKCJA E — KRYPTOWALUTY ═══',
                    'KRYPTOWALUTY — Przychód ze sprzedaży',
                    'KRYPTOWALUTY — Koszty uzyskania przychodu',
                    'KRYPTOWALUTY — Zysk/Strata',
                    '',
                    '═══ DYWIDENDY (PIT-38 + załącznik PIT/ZG) ═══',
                    'DYWIDENDY — Przychód brutto',
                    'DYWIDENDY — Podatek pobrany za granicą',
                    'DYWIDENDY — Podatek należny w PL (19%)',
                    'DYWIDENDY — Podatek do zapłaty (po odliczeniu)',
                    '',
                    '═══ ODSETKI Z LOKAT (podatek Belki 19%) ═══',
                    'ODSETKI — Przychód',
                    '',
                    '═══ SZACUNKOWE ZOBOWIĄZANIE PODATKOWE ═══',
                    'Podatek 19% od akcji (Sekcja C)',
                    'Podatek 19% od kryptowalut (Sekcja E)',
                    'Podatek 19% od odsetek (Belki)',
                    'Podatek od dywidend (do dopłaty)',
                    'RAZEM SZACUNKOWY PODATEK',
                    '',
                    '═══ WAŻNE UWAGI ═══',
                    'Strata z kryptowalut NIE pomniejsza zysku z akcji (i odwrotnie)',
                    'Złożenie PIT-38 jest obowiązkowe nawet przy stracie',
                    'Strata może być odliczana przez 5 kolejnych lat (art. 9 ust. 3)',
                    'Dla dochodów zagranicznych wymagany załącznik PIT/ZG',
                    'PIT/ZG składa się osobno dla każdego kraju',
                    'Akcje → PIT-38 Sekcja C poz. Inne przychody',
                    'Kryptowaluty → PIT-38 Sekcja E',
                ],
                'Kwota PLN': [
                    '',
                    round(results['summary']['total_income_brokerage'], 2),
                    round(results['summary']['total_cost_brokerage'], 2),
                    round(results['summary']['total_profit_brokerage'], 2),
                    '',
                    '',
                    round(results['summary']['total_income_crypto'], 2),
                    round(results['summary']['total_cost_crypto'], 2),
                    round(results['summary']['total_profit_crypto'], 2),
                    '',
                    '',
                    round(results['summary']['total_dividends_gross'], 2),
                    round(results['summary']['total_dividends_tax_paid'], 2),
                    round(results['summary']['total_dividends_gross'] * 0.19, 2),
                    round(max(0, results['summary']['total_dividends_gross'] * 0.19 - results['summary']['total_dividends_tax_paid']), 2),
                    '',
                    '',
                    round(results['summary']['total_interest'], 2),
                    '',
                    '',
                    round(max(0, results['summary']['total_profit_brokerage']) * 0.19, 2),
                    round(max(0, results['summary']['total_profit_crypto']) * 0.19, 2),
                    round(results['summary']['total_interest'] * 0.19, 2),
                    round(max(0, results['summary']['total_dividends_gross'] * 0.19 - results['summary']['total_dividends_tax_paid']), 2),
                    round(max(0, results['summary']['total_profit_brokerage']) * 0.19 +
                          max(0, results['summary']['total_profit_crypto']) * 0.19 +
                          results['summary']['total_interest'] * 0.19 +
                          max(0, results['summary']['total_dividends_gross'] * 0.19 - results['summary']['total_dividends_tax_paid']), 2),
                    '',
                    '',
                    '',
                    '',
                    '',
                    '',
                    '',
                    '',
                    '',
                ]
            }
            
            df_summary = sanitize_dataframe(pd.DataFrame(summary_data))
            df_summary.to_excel(writer, sheet_name='Podsumowanie PIT-38', index=False)
        
        print(f"\n✓ Raport zapisany do pliku: {output_file}")
        print("\n=== PODSUMOWANIE PIT-38 ===")
        print(f"\n--- Sekcja C: Akcje (Inne przychody) ---")
        print(f"Przychód z akcji: {results['summary']['total_income_brokerage']:.2f} PLN")
        print(f"Koszty z akcji: {results['summary']['total_cost_brokerage']:.2f} PLN")
        print(f"Zysk/strata z akcji: {results['summary']['total_profit_brokerage']:.2f} PLN")
        print(f"\n--- Sekcja E: Kryptowaluty ---")
        print(f"Przychód z krypto: {results['summary']['total_income_crypto']:.2f} PLN")
        print(f"Koszty z krypto: {results['summary']['total_cost_crypto']:.2f} PLN")
        print(f"Zysk/strata z krypto: {results['summary']['total_profit_crypto']:.2f} PLN")
        print(f"\n--- Dywidendy (+ PIT/ZG) ---")
        print(f"Dywidendy brutto: {results['summary']['total_dividends_gross']:.2f} PLN")
        print(f"Podatek zagraniczny: {results['summary']['total_dividends_tax_paid']:.2f} PLN")
        print(f"Podatek do dopłaty w PL: {max(0, results['summary']['total_dividends_gross'] * 0.19 - results['summary']['total_dividends_tax_paid']):.2f} PLN")
        print(f"\n--- Odsetki ---")
        print(f"Odsetki z lokat: {results['summary']['total_interest']:.2f} PLN")
        
        tax_stocks = max(0, results['summary']['total_profit_brokerage']) * 0.19
        tax_crypto = max(0, results['summary']['total_profit_crypto']) * 0.19
        tax_interest = results['summary']['total_interest'] * 0.19
        tax_dividends = max(0, results['summary']['total_dividends_gross'] * 0.19 - results['summary']['total_dividends_tax_paid'])
        tax_total = tax_stocks + tax_crypto + tax_interest + tax_dividends
        
        print(f"\n--- Szacunkowy podatek ---")
        print(f"Podatek od akcji (19%): {tax_stocks:.2f} PLN")
        print(f"Podatek od krypto (19%): {tax_crypto:.2f} PLN")
        print(f"Podatek od odsetek (19%): {tax_interest:.2f} PLN")
        print(f"Podatek od dywidend (dopłata): {tax_dividends:.2f} PLN")
        print(f"\n*** RAZEM SZACUNKOWY PODATEK: {tax_total:.2f} PLN ***")
        print("\nUWAGI:")
        print("• Strata z krypto NIE pomniejsza zysku z akcji (i odwrotnie)")
        print("• Złożenie PIT-38 obowiązkowe nawet przy stracie")
        print("• Strata może być odliczana przez 5 kolejnych lat")
        print("• Wymagany załącznik PIT/ZG dla dochodów zagranicznych (osobny dla każdego kraju)")

def main():
    import sys
    
    if len(sys.argv) < 2:
        print("Użycie: python revolut_to_pit8c.py <plik_csv>")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    output_file = 'raport_pit38_2025.xlsx'
    
    if len(sys.argv) >= 3:
        output_file = sys.argv[2]
    
    converter = RevolutToPIT38(csv_file)
    converter.generate_report(output_file)

if __name__ == '__main__':
    main()
