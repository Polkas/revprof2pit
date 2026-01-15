#!/usr/bin/env python3
"""
Generate large test file by multiplying transactions from example file
"""

import sys

def multiply_example_file(input_file="example_revolut_statement.csv", 
                          output_file="test_large_revolut.csv",
                          target_mb=9.5):
    """Multiply transactions to reach target file size"""
    
    print(f"Reading {input_file}...")
    
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Find transaction sections
    sections = {}
    current_section = None
    
    for i, line in enumerate(lines):
        if 'Transactions for' in line:
            current_section = line.strip()
            sections[current_section] = {'start': i, 'lines': []}
        elif current_section and line.strip() and not line.startswith('Summary for'):
            sections[current_section]['lines'].append(line)
    
    # Calculate how many times to multiply
    current_size_mb = len(''.join(lines).encode('utf-8')) / (1024 * 1024)
    multiplier = int((target_mb / current_size_mb) * 1.3) + 1  # Add 30% safety margin
    
    print(f"Original file: {current_size_mb:.2f} MB")
    print(f"Multiplying transactions by {multiplier}x to reach ~{target_mb} MB...")
    
    # Build output
    output_lines = []
    in_transaction_section = False
    transaction_header = None
    transaction_lines = []
    
    for line in lines:
        if 'Transactions for' in line:
            # Start of transaction section
            in_transaction_section = True
            transaction_header = None
            transaction_lines = []
            output_lines.append(line)
        elif in_transaction_section:
            if transaction_header is None and line.strip():
                # This is the header line (Date,Description,etc)
                transaction_header = line
                output_lines.append(line)
            elif line.strip() and not line.startswith('Summary for'):
                # Transaction data line
                transaction_lines.append(line)
            else:
                # End of section - multiply and add
                for _ in range(multiplier):
                    output_lines.extend(transaction_lines)
                # Add the empty line
                output_lines.append(line)
                in_transaction_section = False
                transaction_lines = []
        else:
            output_lines.append(line)
    
    # Write output
    with open(output_file, 'w', encoding='utf-8') as f:
        f.writelines(output_lines)
    
    output_size_mb = len(''.join(output_lines).encode('utf-8')) / (1024 * 1024)
    print(f"\n✓ Generated {output_file}")
    print(f"  File size: {output_size_mb:.2f} MB")
    print(f"  Transactions multiplied: {multiplier}x")

if __name__ == "__main__":
    multiply_example_file()
