"""Debug - inspeciona estrutura do pageProps.company do Reclame Aqui."""
import sys, json, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from curl_cffi import requests as cffi

headers = {'Accept': 'text/html', 'Accept-Language': 'pt-BR,pt;q=0.9', 'Referer': 'https://www.reclameaqui.com.br/'}

def buscar_empresa(slug):
    url = f'https://www.reclameaqui.com.br/empresa/{slug}/'
    r = cffi.get(url, headers=headers, impersonate='chrome124', timeout=20)
    print(f'[{slug}] status={r.status_code}')
    if r.status_code != 200:
        return None
    html = r.text
    match = re.search(r'id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html, re.I)
    if not match:
        print('  NO __NEXT_DATA__')
        return None
    nd = json.loads(match.group(1))
    pp = nd.get('props', {}).get('pageProps', {})
    company = pp.get('company', {})
    print('  company keys:', list(company.keys())[:20])
    return company

def buscar_full(slug):
    url = f'https://www.reclameaqui.com.br/empresa/{slug}/'
    r = cffi.get(url, headers=headers, impersonate='chrome124', timeout=20)
    print(f'[{slug}] status={r.status_code}')
    if r.status_code != 200:
        return None
    html = r.text
    match = re.search(r'id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html, re.I)
    if not match:
        print('  NO __NEXT_DATA__')
        return None
    nd = json.loads(match.group(1))
    pp = nd.get('props', {}).get('pageProps', {})
    return pp

# Testa betano (empresa conhecida)
c = buscar_empresa('betano')
if c:
    print('\n=== betano company keys (20) ===')
    print(json.dumps(list(c.keys())[:30], ensure_ascii=False))

pp = buscar_full('betano')
if pp:
    print('\n=== pageProps keys ===')
    print(json.dumps(list(pp.keys()), ensure_ascii=False))

    # companyVirtualFlags
    cvf = pp.get('companyVirtualFlags', {})
    print('\n=== companyVirtualFlags ===')
    print(json.dumps(cvf, ensure_ascii=False, indent=2)[:3000])

    # Full company object - all keys and nested
    co = pp.get('company', {})
    print('\n=== ALL company keys ===')
    print(json.dumps(list(co.keys()), ensure_ascii=False))
    # Look for score/reputation nested fields
    for k in ['score', 'rating', 'reputation', 'nota', 'status', 'ra1000', 'ra_1000', 'complains', 'statistics']:
        if k in co:
            print(f'  company.{k} = {json.dumps(co[k], ensure_ascii=False)[:500]}')

    # ssrHeroHTML might have the rendered stats (nota, reputacao)
    hero = pp.get('ssrHeroHTML', '')
    print('\n=== ssrHeroHTML snippet (500 chars) ===')
    print(str(hero)[:500])

    # Deep inspection of score-related fields
    for field in ['companyIndex12Months', 'companyIndex6Months', 'performanceData',
                  'companyFlags', 'companyPlan', 'complainCount']:
        val = co.get(field)
        print(f'\n=== company.{field} ===')
        print(json.dumps(val, ensure_ascii=False, indent=2)[:1500])

    # Full text search for reputacao context
    full_str = json.dumps(co, ensure_ascii=False)
    import re as re2
    # Find reputacao in context
    for pat in [r'.{0,20}"reputacao".{0,100}', r'.{0,20}"ra1000".{0,100}',
                r'.{0,20}"nota".{0,100}', r'.{0,20}"score".{0,100}']:
        hits = re2.findall(pat, full_str)[:3]
        if hits:
            print(f'\nPattern {repr(pat)}:')
            for h in hits:
                print(f'  {h}')
