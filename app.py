import csv
import io
import json
import os
import random
import re
import sys
import threading
import webbrowser
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template, request, jsonify, Response
import requests
from bs4 import BeautifulSoup


def get_resource_path(relative_path):
    """Obtém o caminho absoluto do recurso, compatível com desenvolvimento e PyInstaller .exe"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath('.'), relative_path)


template_dir = get_resource_path('templates')
app = Flask(__name__, template_folder=template_dir)

HISTORY_FILE = 'historico_testes_telas.json'
TIMEOUT = 15

FIELDS_MAP = {
    'sku': 'SKU / Código do Produto',
    'produto_descricao': 'Produto / Modelo do Aparelho',
    'numero_nota': 'Número da Nota Fiscal',
    'cpf_consumidor': 'CPF do Cliente',
    'nome_consumidor': 'Nome do Cliente',
    'emitente': 'Loja / Filial (Emitente)',
    'data_emissao': 'Data/Hora Emissão',
    'chave_acesso': 'Chave de Acesso',
    'status_teste': 'Status do Teste de Tela',
    'observacoes_teste': 'Observações do Técnico',
    'scan_time': 'Data/Hora do Registro'
}


def normalize_spaces(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


class HistoryManager:
    """Gerencia a persistência do histórico de testes de telas em JSON."""
    def __init__(self, filename=HISTORY_FILE):
        self.filename = filename
        self.records = self.load_history()

    def load_history(self) -> list:
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_history(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Erro ao salvar histórico: {e}")

    def add_or_update_receipt(self, receipt_data: dict) -> bool:
        chave = receipt_data.get('chave_acesso', '')
        nota = receipt_data.get('numero_nota', '')
        sku = receipt_data.get('sku', '')

        for idx, rec in enumerate(self.records):
            rec_chave = rec.get('chave_acesso', '')
            rec_nota = rec.get('numero_nota', '')
            rec_sku = rec.get('sku', '')

            if (chave and rec_chave == chave) or (nota and rec_nota == nota and sku and rec_sku == sku):
                if not receipt_data.get('status_teste') and rec.get('status_teste'):
                    receipt_data['status_teste'] = rec.get('status_teste')
                if not receipt_data.get('observacoes_teste') and rec.get('observacoes_teste'):
                    receipt_data['observacoes_teste'] = rec.get('observacoes_teste')

                self.records[idx] = receipt_data
                self.save_history()
                return False

        self.records.insert(0, receipt_data)
        self.save_history()
        return True

    def update_test_status(self, index: int, status: str, obs: str) -> bool:
        if 0 <= index < len(self.records):
            self.records[index]['status_teste'] = status
            self.records[index]['observacoes_teste'] = obs
            self.save_history()
            return True
        return False

    def delete_receipt(self, index: int) -> bool:
        if 0 <= index < len(self.records):
            del self.records[index]
            self.save_history()
            return True
        return False

    def clear_all(self):
        self.records = []
        self.save_history()


history_mgr = HistoryManager()


class NFCeParser:
    @staticmethod
    def parse_qr_url(qr_url: str) -> dict:
        result = {
            'qr_url': qr_url,
            'sefaz_host': '',
            'chave_acesso': '',
            'versao_qrcode': '',
            'ambiente': '',
            'numero_nota': '',
            'cnpj_emitente': '',
            'serie': '',
            'sku': '',
            'emitente': ''
        }
        if not qr_url:
            return result

        parsed = urlparse(qr_url)
        result['sefaz_host'] = parsed.netloc

        # Procura por qualquer sequência de 44 dígitos numéricos na URL ou texto inserido
        match44 = re.search(r'(\d{44})', qr_url)
        if match44:
            chave = match44.group(1)
            result['chave_acesso'] = chave

            # CNPJ: dígitos 7 a 20 (índices 6 a 20)
            cnpj_raw = chave[6:20]
            result['cnpj_emitente'] = f"{cnpj_raw[:2]}.{cnpj_raw[2:5]}.{cnpj_raw[5:8]}/{cnpj_raw[8:12]}-{cnpj_raw[12:]}"

            # Série: dígitos 23 a 25 (índices 22 a 25)
            result['serie'] = chave[22:25].lstrip('0') or '0'

            # Número da Nota (nNF): dígitos 26 a 34 (índices 25 a 34)
            nnf_raw = chave[25:34].lstrip('0')
            result['numero_nota'] = nnf_raw or '0'

            # SKU prioritário derivado da Nota / Ref Interna (ex: 65041)
            result['sku'] = nnf_raw or 'N/A'

            # Identificação de filial Casas Bahia / Via Varejo
            if cnpj_raw.startswith('33041260'):
                result['emitente'] = 'Casas Bahia'

        return result

    @staticmethod
    def fetch_page(qr_url: str) -> tuple[dict, list]:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7'
        }
        resp = requests.get(qr_url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        return NFCeParser.extract_from_html(text)

    @staticmethod
    def extract_from_html(html: str) -> tuple[dict, list]:
        soup = BeautifulSoup(html, 'html.parser')
        page_text = normalize_spaces(soup.get_text(' ', strip=True))

        data = {
            'emitente': '',
            'cnpj_emitente': '',
            'numero_nota': '',
            'serie': '',
            'data_emissao': '',
            'cpf_consumidor': '',
            'nome_consumidor': '',
            'sku': '',
            'produto_descricao': '',
            'valor_total': '',
            'status_teste': 'Em Teste',
            'observacoes_teste': ''
        }

        title_candidates = [
            soup.select_one('#u20'), soup.select_one('.txtCenter'), soup.select_one('.txtTopo'),
            soup.select_one('.txtBox h2'), soup.select_one('h1')
        ]
        for c in title_candidates:
            if c:
                txt = normalize_spaces(c.get_text())
                if txt and len(txt) > 2 and 'consulta' not in txt.lower() and 'nota' not in txt.lower():
                    data['emitente'] = txt
                    break

        cnpj_match = re.search(r'CNPJ[:\s]*([\d\.\-/]+)', page_text, re.I)
        if cnpj_match:
            data['cnpj_emitente'] = cnpj_match.group(1)

        nota_match = re.search(r'(?:NFC-e|NFCE|Nota Fiscal).*?[Nn][ºo]?\s*([\d\.]+)', page_text)
        if nota_match:
            data['numero_nota'] = nota_match.group(1)
        else:
            n_match = re.search(r'Número[:\s]*([\d\.]+)', page_text, re.I)
            if n_match:
                data['numero_nota'] = n_match.group(1)

        dt_match = re.search(r'(?:Data de Emiss[aã]o|Emiss[aã]o)[:\s]*([\d/]{10}\s*[\d:]{5,8})', page_text, re.I)
        if dt_match:
            data['data_emissao'] = dt_match.group(1)

        cpf_match = re.search(r'CPF(?: do Consumidor)?[:\s]*([\d\.\-\*]+)', page_text, re.I)
        if cpf_match:
            data['cpf_consumidor'] = cpf_match.group(1)

        nome_match = re.search(r'Nome(?: do Consumidor)?[:\s]*([A-Za-zÀ-ÖØ-öø-ÿ\s\.\'-]+?)(?=\s+(?:Data|CPF|CNPJ|Endere[çc]o|Protocolo|Emiss[aã]o|Rua|Av|\d|$))', page_text, re.I)
        if nome_match and len(nome_match.group(1).strip()) > 3:
            raw_nome = nome_match.group(1).strip()
            data['nome_consumidor'] = re.sub(r'\s+Data(?:\s+de)?$', '', raw_nome, flags=re.I).strip()
        else:
            simple_nome = re.search(r'Nome(?: do Consumidor)?[:\s]*([A-Za-zÀ-ÖØ-öø-ÿ\s]+)', page_text, re.I)
            if simple_nome and len(simple_nome.group(1).strip()) > 3:
                raw_nome = simple_nome.group(1).strip()
                data['nome_consumidor'] = re.sub(r'\s+Data.*$', '', raw_nome, flags=re.I).strip()

        items_parsed = []
        possible_products = []

        for row in soup.select('table tr, #tabResult tr, .txtTit'):
            text_row = normalize_spaces(row.get_text(' ', strip=True))
            if text_row and 'código' not in text_row.lower() and 'descrição' not in text_row.lower():
                items_parsed.append(text_row)

                code_match = re.search(r'(?:C[óo]digo|Cod|SKU)[:\s]*(\d{5,12})', text_row, re.I)
                if code_match and not data['sku']:
                    data['sku'] = code_match.group(1)

                device_keywords = ['celular', 'smartphone', 'notebook', 'laptop', 'tablet', 'tv', 'smart tv', 'monitor', 'iphone', 'samsung', 'motorola', 'xiaomi', 'lg']
                if any(kw in text_row.lower() for kw in device_keywords):
                    possible_products.append(text_row)

        if possible_products:
            data['produto_descricao'] = possible_products[0]
        elif items_parsed:
            data['produto_descricao'] = items_parsed[0]

        if not data['sku'] and data['produto_descricao']:
            digit_match = re.search(r'\b(\d{5,10})\b', data['produto_descricao'])
            if digit_match:
                data['sku'] = digit_match.group(1)

        return data, items_parsed


# --- Rotas da Aplicação ---

@app.route('/')
def index():
    return render_template('index.html', fields_map=FIELDS_MAP)


@app.route('/api/consultar', methods=['POST'])
def api_consultar():
    payload = request.get_json() or {}
    qr_url = (payload.get('qr_url') or '').strip()

    if not qr_url:
        return jsonify({'success': False, 'error': 'URL do QR Code não informada.'}), 400

    is_http_url = qr_url.startswith('http://') or qr_url.startswith('https://')
    match44 = re.search(r'(\d{44})', qr_url)

    if not is_http_url and not match44:
        return jsonify({
            'success': False,
            'error': 'Link ou Chave inválida. Cole uma URL de QR Code (iniciando com http/https) ou uma Chave de Acesso de 44 dígitos.'
        }), 400

    try:
        base = NFCeParser.parse_qr_url(qr_url)
        page_data = {}
        items = []

        try:
            page_data, items = NFCeParser.fetch_page(qr_url)
        except Exception as net_err:
            print(f"Aviso: Não foi possível acessar Sefaz ({net_err}). Usando dados offline do QR Code.")

        if not match44 and not page_data.get('numero_nota') and not page_data.get('emitente'):
            return jsonify({
                'success': False,
                'error': 'Não foi possível consultar a nota. Verifique se o link ou a chave está correta.'
            }), 400

        merged_sku = page_data.get('sku') or base.get('sku') or base.get('numero_nota') or 'N/A'
        merged_nota = page_data.get('numero_nota') or base.get('numero_nota') or ''

        merged = {
            'sku': merged_sku,
            'produto_descricao': page_data.get('produto_descricao') or f"Aparelho / Tela (Nota {merged_nota})",
            'numero_nota': merged_nota,
            'cpf_consumidor': page_data.get('cpf_consumidor') or 'Não informado',
            'nome_consumidor': page_data.get('nome_consumidor') or 'Cliente',
            'emitente': page_data.get('emitente') or base.get('emitente') or 'Casas Bahia',
            'data_emissao': page_data.get('data_emissao') or '',
            'chave_acesso': base.get('chave_acesso') or qr_url,
            'status_teste': 'Em Teste',
            'observacoes_teste': '',
            'scan_time': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
            'qr_url': qr_url
        }

        history_mgr.add_or_update_receipt(merged)

        return jsonify({
            'success': True,
            'data': merged,
            'items': items,
            'fields_map': FIELDS_MAP
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/gerar-nota-teste', methods=['POST', 'GET'])
def api_gerar_nota_teste():
    """Gera uma nota fiscal fictícia aleatória da Casas Bahia para testes."""
    produtos_mock = [
        ("SKU-982415", "Smartphone Samsung Galaxy S24 Ultra 256GB 5G - Tela 6.8''"),
        ("SKU-774129", "Notebook Lenovo IdeaPad 1i Intel Core i5 8GB 256GB SSD 15.6'' Full HD"),
        ("SKU-312890", "Smart TV 55'' Crystal UHD 4K Samsung 55CU7700 Wi-Fi Bluetooth"),
        ("SKU-654123", "Tablet Apple iPad 10ª Geração 64GB Wi-Fi Tela 10.9'' Azul"),
        ("SKU-889104", "Smartphone Motorola Moto G84 5G 256GB 8GB RAM Grafite"),
        ("SKU-441208", "Notebook Gamer Acer Nitro 5 Intel Core i5 16GB 512GB SSD RTX 3050")
    ]
    nomes_mock = ["Gabriel Santos Silva", "Juliana Oliveira Ramos", "Carlos Eduardo Souza", "Mariana Costa Lima", "Fernanda Ribeiro", "Lucas Mendes Rocha"]
    lojas_mock = ["Casas Bahia - Filial 042 (SP Centro)", "Casas Bahia - Filial 118 (RJ Barra)", "Casas Bahia - Filial 085 (MG BH Shopping)", "Casas Bahia - Loja Online"]

    prod = random.choice(produtos_mock)
    nome = random.choice(nomes_mock)
    loja = random.choice(lojas_mock)
    nota_num = str(random.randint(100000, 999999))
    cpf_num = f"{random.randint(100, 999)}.{random.randint(100, 999)}.{random.randint(100, 999)}-{random.randint(10, 99)}"
    chave_simulada = "".join([str(random.randint(0, 9)) for _ in range(44)])

    merged = {
        'sku': prod[0],
        'produto_descricao': prod[1],
        'numero_nota': nota_num,
        'cpf_consumidor': cpf_num,
        'nome_consumidor': nome,
        'emitente': loja,
        'data_emissao': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
        'chave_acesso': chave_simulada,
        'status_teste': 'Em Teste',
        'observacoes_teste': 'Nota simulada gerada para teste no sistema.',
        'scan_time': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
        'qr_url': f"https://sefaz.sp.gov.br/nfce/qrcode?p={chave_simulada}|2|1|1"
    }

    history_mgr.add_or_update_receipt(merged)

    return jsonify({
        'success': True,
        'data': merged,
        'items': [prod[1], "Garantia Estendida 12 Meses", "Película Protetora"],
        'fields_map': FIELDS_MAP
    })


@app.route('/api/historico', methods=['GET'])
def api_historico():
    return jsonify({
        'success': True,
        'records': history_mgr.records,
        'count': len(history_mgr.records)
    })


@app.route('/api/historico/atualizar-status', methods=['POST'])
def api_atualizar_status():
    payload = request.get_json() or {}
    index = payload.get('index')
    status = payload.get('status', 'Em Teste')
    obs = payload.get('observacoes', '')

    if index is not None and history_mgr.update_test_status(int(index), status, obs):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Índice inválido.'}), 400


@app.route('/api/historico/deletar', methods=['POST'])
def api_deletar_historico():
    payload = request.get_json() or {}
    index = payload.get('index')
    if index is not None and history_mgr.delete_receipt(int(index)):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Índice inválido.'}), 400


@app.route('/api/historico/limpar', methods=['POST'])
def api_limpar_historico():
    history_mgr.clear_all()
    return jsonify({'success': True})


@app.route('/api/exportar-csv', methods=['GET'])
def api_exportar_csv():
    output = io.StringIO()
    headers = ['SKU', 'Produto / Aparelho', 'Número da Nota', 'CPF Cliente', 'Nome Cliente', 'Status do Teste', 'Observações', 'Loja Casas Bahia', 'Data/Hora Emissão', 'Chave de Acesso']
    writer = csv.writer(output, delimiter=';')
    writer.writerow(headers)

    for rec in history_mgr.records:
        writer.writerow([
            rec.get('sku', ''),
            rec.get('produto_descricao', ''),
            rec.get('numero_nota', ''),
            rec.get('cpf_consumidor', ''),
            rec.get('nome_consumidor', ''),
            rec.get('status_teste', ''),
            rec.get('observacoes_teste', ''),
            rec.get('emitente', ''),
            rec.get('data_emissao', ''),
            rec.get('chave_acesso', '')
        ])

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': 'attachment; filename=relatorio_testes_telas_casas_bahia.csv'}
    )


# --- Gerenciamento de Versão & Atualizações via GitHub ---
VERSION_FILE = 'version.json'
CURRENT_VERSION = "1.0.0"


def get_app_version_info():
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": CURRENT_VERSION, "github_repo": "MonoDim/andradecell-testes"}


def save_app_version_info(data):
    try:
        with open(VERSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Erro ao salvar info de versão: {e}")


@app.route('/api/versao', methods=['GET', 'POST'])
def api_versao():
    info = get_app_version_info()
    if request.method == 'POST':
        payload = request.get_json() or {}
        repo = payload.get('github_repo', '').strip()
        info['github_repo'] = repo
        save_app_version_info(info)
        return jsonify({'success': True, 'info': info})
    return jsonify({'success': True, 'info': info})


@app.route('/api/verificar-atualizacao', methods=['GET'])
def api_verificar_atualizacao():
    info = get_app_version_info()
    repo = info.get('github_repo', '').strip()

    if not repo:
        return jsonify({
            'success': False,
            'configured': False,
            'message': 'Repositório do GitHub não configurado. Clique no botão ⚙️ para informar o usuário/repositório.'
        })

    repo_clean = repo.replace('https://github.com/', '').replace('http://github.com/', '').strip('/')

    try:
        api_url = f"https://api.github.com/repos/{repo_clean}/releases/latest"
        headers = {'User-Agent': 'AndradeCell-AutoUpdater'}
        resp = requests.get(api_url, headers=headers, timeout=8)

        latest_version = ""
        download_url = ""
        release_notes = ""

        if resp.status_code == 200:
            rel_data = resp.json()
            latest_version = rel_data.get('tag_name', '').strip().lstrip('v')
            release_notes = rel_data.get('body', '')
            assets = rel_data.get('assets', [])
            for asset in assets:
                if asset.get('name', '').endswith('.exe') or asset.get('name', '').endswith('.zip'):
                    download_url = asset.get('browser_download_url', '')
                    break
            if not download_url:
                download_url = rel_data.get('html_url', f"https://github.com/{repo_clean}/releases")
        else:
            raw_url = f"https://raw.githubusercontent.com/{repo_clean}/main/version.json"
            raw_resp = requests.get(raw_url, headers=headers, timeout=8)
            if raw_resp.status_code == 200:
                raw_data = raw_resp.json()
                latest_version = raw_data.get('version', '').strip().lstrip('v')
                release_notes = raw_data.get('release_notes', '')
                download_url = f"https://github.com/{repo_clean}/archive/refs/heads/main.zip"

        if not latest_version:
            return jsonify({
                'success': False,
                'configured': True,
                'message': f'Nenhuma versão ou release público encontrado no repositório {repo_clean}.'
            })

        curr_v = info.get('version', CURRENT_VERSION).strip().lstrip('v')
        has_update = latest_version != curr_v

        return jsonify({
            'success': True,
            'configured': True,
            'current_version': curr_v,
            'latest_version': latest_version,
            'has_update': has_update,
            'download_url': download_url,
            'release_notes': release_notes,
            'repo': repo_clean
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'configured': True,
            'message': f'Falha ao conectar com o GitHub: {e}'
        })


def open_browser():
    webbrowser.open('http://127.0.0.1:5000/')


if __name__ == '__main__':
    threading.Timer(1.2, open_browser).start()
    app.run(host='127.0.0.1', port=5000, debug=False)
