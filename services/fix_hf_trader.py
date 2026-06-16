with open('/var/www/virtuosocrypto.com/polyclawd/services/hf_paper_trader.py', 'r') as f:
    content = f.read()

# Fix the VIRTUOSO_EDGE call - disable it
content = content.replace(
    'return requests.get(VIRTUOSO_EDGE_URL, timeout=3).json()',
    'return None  # VIRTUOSO_EDGE disabled - port 8002 is separate service'
)

with open('/var/www/virtuosocrypto.com/polyclawd/services/hf_paper_trader.py', 'w') as f:
    f.write(content)

print('Fixed VIRTUOSO_EDGE')
