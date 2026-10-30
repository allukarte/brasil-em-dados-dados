# -*- coding: utf-8 -*-
"""
Robô do Brasil em Dados
=======================
Busca dados oficiais por município e grava um arquivo por estado em  dados/UF.json
(por exemplo dados/PE.json). A página do Blogger lê esses arquivos.

Fontes:
  • IBGE: lista oficial de municípios (sem chave)
  • Tesouro Nacional: transferências constitucionais, como FPM e Fundeb (sem chave)
  • Portal da Transparência: Bolsa Família e BPC por município (precisa de chave gratuita)

Configuração (feita no GitHub, não é preciso editar este arquivo):
  PORTAL_CHAVE  → segredo com a chave da API do Portal da Transparência
  UFS           → opcional: estados a atualizar, ex. "PE" ou "PE,PB". Vazio = todos
  FONTES        → opcional: "todas", "tesouro", "portal", "pdde" ou "fundeb"
                  ("fundeb" = matrículas da rede municipal consideradas no Fundeb, planilha do FNDE)
                  ("pdde" = Dinheiro Direto na Escola e merenda, do arquivo mensal
                   "Recursos Transferidos" do Portal da Transparência; não precisa de chave)

Usa só bibliotecas que já vêm com o Python. Nada para instalar.
"""

import csv
import gzip
from html import unescape
import io
import json
import os
import re
import tempfile
import zipfile
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")
FUSO_BR = timezone(timedelta(hours=-3))

IBGE_MUNICIPIOS = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
# Cópia da lista de municípios mantida no GitHub (usada se o IBGE não responder)
LISTA_RESERVA = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"
UF_POR_CODIGO = {"11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
                 "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
                 "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
                 "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF"}
TESOURO = "https://apiapex.tesouro.gov.br/aria/v1/transferencias_constitucionais/custom"
PORTAL = "https://api.portaldatransparencia.gov.br/api-de-dados"

# Programas do Portal da Transparência. Para cada um, os endereços são testados em ordem.
PROGRAMAS = {
    "bolsa_familia": ["novo-bolsa-familia-por-municipio", "bolsa-familia-por-municipio"],
    "bpc": ["bpc-por-municipio"],
}

PAUSA_PORTAL = 0.75   # segundos entre consultas (≈ 80 por minuto; o limite do Portal é 90)
PAUSA_TESOURO = 0.3

UFS_NOMES = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí",
    "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul",
    "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
    "SE": "Sergipe", "TO": "Tocantins",
}


# ----------------------------------------------------------------- utilidades

def log(msg):
    print(datetime.now(FUSO_BR).strftime("%H:%M:%S"), msg, flush=True)


def normalizar(t):
    t = unicodedata.normalize("NFD", str(t or ""))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


def requisicao(url, cab, tempo=45):
    """GET simples com http.client: envia os cabeçalhos exatamente como escritos
    (o urllib troca 'chave-api-dados' por 'Chave-api-dados'). Devolve (status, corpo)."""
    import http.client
    u = urllib.parse.urlsplit(url)
    caminho = u.path + ("?" + u.query if u.query else "")
    con = http.client.HTTPSConnection(u.netloc, timeout=tempo)
    try:
        con.putrequest("GET", caminho, skip_accept_encoding=True)
        for k, v in cab.items():
            con.putheader(k, v)
        con.endheaders()
        r = con.getresponse()
        if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
            destino = urllib.parse.urljoin(url, r.getheader("Location"))
            con.close()
            return requisicao(destino, cab, tempo)
        return r.status, r.read()
    finally:
        con.close()


class ErroHTTP(Exception):
    def __init__(self, codigo, corpo):
        super().__init__(f"HTTP {codigo}")
        self.code, self.corpo = codigo, corpo


def baixar_json(url, cabecalhos=None, tentativas=4, espera_429=65, tempo=45):
    """Baixa um JSON. Repete em caso de falha temporária. Devolve None se não conseguir."""
    cab = {"Accept": "application/json", "User-Agent": "BrasilEmDados/1.0 (robô de dados públicos)"}
    cab.update(cabecalhos or {})
    for tentativa in range(1, tentativas + 1):
        try:
            status, corpo = requisicao(url, cab, tempo)
            if status >= 400:
                raise ErroHTTP(status, corpo)
            texto = corpo.decode("utf-8", errors="replace").strip()
            return json.loads(texto) if texto else None
        except ErroHTTP as e:
            if e.code == 429:
                log(f"   limite de consultas atingido; aguardando {espera_429}s…")
                time.sleep(espera_429)
                continue
            if e.code in (401, 403):
                msg = e.corpo.decode("utf-8", errors="replace").strip()[:200]
                log(f"   acesso negado ({e.code}) em {url.split('?')[0]} — resposta: {msg or '(vazia)'}")
                return None
            if e.code in (400, 404):
                return None
            log(f"   erro {e.code} (tentativa {tentativa}/{tentativas})")
        except Exception as e:  # rede, tempo esgotado, JSON inválido
            log(f"   falha: {e.__class__.__name__} (tentativa {tentativa}/{tentativas})")
        time.sleep(5 * tentativa)
    return None


def chave_ci(d, *nomes):
    """Lê um campo de um dicionário sem diferenciar maiúsculas/minúsculas."""
    if not isinstance(d, dict):
        return None
    baixo = {str(k).lower(): v for k, v in d.items()}
    for n in nomes:
        if n.lower() in baixo:
            return baixo[n.lower()]
    return None


def numero(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if "," in s:  # formato brasileiro 1.234,56
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def lista_de_registros(resp):
    """Encontra a lista de registros dentro de uma resposta, qualquer que seja o formato."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("registros", "items", "itens", "data", "dados", "content", "resultado"):
            v = chave_ci(resp, k)
            if isinstance(v, list):
                return v
        for v in resp.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
    return []


def ler_arquivo(uf):
    caminho = os.path.join(PASTA, f"{uf}.json")
    if os.path.exists(caminho):
        try:
            with open(caminho, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"uf": uf, "estado": UFS_NOMES[uf], "municipios": {}}


def gravar_arquivo(uf, dados):
    os.makedirs(PASTA, exist_ok=True)
    dados["atualizado_em"] = datetime.now(FUSO_BR).isoformat(timespec="seconds")
    caminho = os.path.join(PASTA, f"{uf}.json")
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))


# ----------------------------------------------------------------- IBGE

def municipios_ibge():
    log("IBGE: baixando a lista de municípios…")
    lista = baixar_json(IBGE_MUNICIPIOS, tentativas=2, tempo=30) or []
    por_uf = {}
    for m in lista:
        try:
            uf = m["microrregiao"]["mesorregiao"]["UF"]["sigla"]
        except (KeyError, TypeError):
            try:
                uf = m["regiao-imediata"]["regiao-intermediaria"]["UF"]["sigla"]
            except (KeyError, TypeError):
                uf = UF_POR_CODIGO.get(str(m.get("id", ""))[:2])
        if uf:
            por_uf.setdefault(uf, []).append({"id": str(m["id"]), "nome": m["nome"]})
    if not por_uf:
        log("IBGE não respondeu; usando a cópia da lista de municípios guardada no GitHub.")
        por_uf = municipios_reserva()
    log(f"Municípios: {sum(len(v) for v in por_uf.values())} em {len(por_uf)} estados.")
    return por_uf


def municipios_reserva():
    import csv, io
    try:
        req = urllib.request.Request(LISTA_RESERVA, headers={"User-Agent": "BrasilEmDados/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            texto = r.read().decode("utf-8-sig")
    except Exception as e:
        log(f"   a cópia também falhou: {e.__class__.__name__}")
        return {}
    por_uf = {}
    for linha in csv.DictReader(io.StringIO(texto)):
        uf = UF_POR_CODIGO.get(str(linha.get("codigo_uf", "")).strip())
        if uf and linha.get("codigo_ibge"):
            por_uf.setdefault(uf, []).append({"id": linha["codigo_ibge"].strip(), "nome": linha["nome"].strip()})
    return por_uf


def diagnostico(chave):
    """Teste rápido: mostra no registro se cada fonte responde aos servidores do GitHub."""
    testes = [
        ("IBGE", IBGE_MUNICIPIOS.replace("/municipios", "/estados"), {}),
        ("Tesouro Nacional", TESOURO + "/estados", {}),
        ("Portal da Transparência", PORTAL + "/bpc-por-municipio?mesAno=202401&codigoIbge=3550308&pagina=1",
         {"chave-api-dados": chave} if chave else {}),
    ]
    log("----- Teste de conexão com as fontes -----")
    if chave:
        estranho = any(c.isspace() for c in chave) or not chave.isalnum()
        log(f"   Chave do Portal: {len(chave)} caracteres" +
            (" — ATENÇÃO: contém espaços ou símbolos; confira se colou só a chave" if estranho else ""))
    else:
        log("   Chave do Portal: NÃO CONFIGURADA (segredo PORTAL_CHAVE vazio ou ausente)")
    resultado = {}
    for nome, url, cab in testes:
        cab = dict(cab, **{"Accept": "application/json", "User-Agent": "BrasilEmDados/1.0"})
        try:
            status, corpo = requisicao(url, cab, 25)
            if status < 400:
                resultado[nome] = f"OK (HTTP {status})"
            else:
                msg = corpo.decode("utf-8", errors="replace").strip()[:200]
                resultado[nome] = f"respondeu com erro HTTP {status} — resposta: {msg or '(vazia)'}"
        except Exception as e:
            resultado[nome] = f"SEM RESPOSTA ({e.__class__.__name__})"
        log(f"   {nome}: {resultado[nome]}")
    log("------------------------------------------")
    return resultado


# ----------------------------------------------------------------- Tesouro Nacional

def codigos_estados_tesouro():
    """Liga cada sigla (PE, SP…) ao código interno do Tesouro."""
    resp = baixar_json(TESOURO + "/estados")
    mapa = {}
    sigla_por_nome = {normalizar(n): s for s, n in UFS_NOMES.items()}
    for reg in lista_de_registros(resp):
        if not isinstance(reg, dict):
            continue
        sigla = None
        for v in reg.values():
            if isinstance(v, str):
                if v.strip().upper() in UFS_NOMES:
                    sigla = v.strip().upper()
                elif normalizar(v) in sigla_por_nome:
                    sigla = sigla_por_nome[normalizar(v)]
            if sigla:
                break
        codigo = None
        for k, v in reg.items():
            kl = str(k).lower()
            if ("cod" in kl or kl.startswith("id") or kl in ("co_uf", "uf_id")) and v not in (None, "") \
                    and str(v).strip().upper() not in UFS_NOMES:
                codigo = v
                break
        if sigla and codigo is not None:
            mapa[sigla] = codigo
    log(f"Tesouro: {len(mapa)} códigos de estado encontrados.")
    return mapa


def tesouro_registros(params):
    """Busca todas as páginas do endpoint por_estado_municipio."""
    vistos, linhas = set(), []
    tamanho = 1000
    pagina = 1
    while pagina <= 500:
        q = dict(params, page=pagina, pageSize=tamanho)
        resp = baixar_json(TESOURO + "/por_estado_municipio?" + urllib.parse.urlencode(q))
        regs = lista_de_registros(resp)
        novos = 0
        for r in regs:
            ident = (str(chave_ci(r, "co_ibge")), chave_ci(r, "ano"), chave_ci(r, "mes"),
                     chave_ci(r, "transferencia"))
            if ident not in vistos:
                vistos.add(ident)
                linhas.append(r)
                novos += 1
        proxima = chave_ci(resp, "next") if isinstance(resp, dict) else None
        if not regs or novos == 0 or len(regs) < tamanho or proxima is False:
            break
        pagina += 1
        time.sleep(PAUSA_TESOURO)
    return linhas


def resumir_tesouro(linhas, ano):
    """Agrupa as linhas por município: total por tipo de transferência e meses cobertos."""
    por_mun = {}
    for r in linhas:
        cod = "".join(ch for ch in str(chave_ci(r, "co_ibge") or "") if ch.isdigit())
        if not cod:
            continue
        nome = str(chave_ci(r, "transferencia") or "Outras").strip()
        mes = int(numero(chave_ci(r, "mes")))
        valor = numero(chave_ci(r, "valor", "total"))
        m = por_mun.setdefault(cod, {"ano": ano, "meses": set(), "itens": {}})
        if mes:
            m["meses"].add(mes)
        m["itens"][nome] = m["itens"].get(nome, 0.0) + valor
    saida = {}
    for cod, m in por_mun.items():
        itens = {k: round(v, 2) for k, v in sorted(m["itens"].items(), key=lambda kv: -kv[1])}
        fundeb = sum(v for k, v in itens.items() if "FUNDEB" in k.upper())
        fpm = sum(v for k, v in itens.items() if k.upper().startswith("FPM"))
        meses = sorted(m["meses"])
        saida[cod] = {
            "ano": ano,
            "mes_inicial": meses[0] if meses else None,
            "mes_final": meses[-1] if meses else None,
            "fundeb": round(fundeb, 2),
            "fpm": round(fpm, 2),
            "total": round(sum(itens.values()), 2),
            "por_tipo": itens,
        }
    return saida


_NACIONAL = {}


def resumo_nacional():
    """Plano B: se os códigos de estado não forem encontrados, baixa o país inteiro de uma vez."""
    if not _NACIONAL:
        hoje = date.today()
        for ano in (hoje.year, hoje.year - 1):
            linhas = tesouro_registros({"p_ano": ano})
            if linhas:
                _NACIONAL.update(resumir_tesouro(linhas, ano))
                break
    return _NACIONAL


def atualizar_tesouro(uf, municipios, dados, codigos):
    hoje = date.today()
    cod_uf = codigos.get(uf)
    resumo = {}
    if cod_uf is None:
        log(f"Tesouro: código de {uf} não encontrado; usando a consulta nacional.")
        resumo = resumo_nacional()
    else:
        for ano in (hoje.year, hoje.year - 1):
            linhas = tesouro_registros({"p_estado": cod_uf, "p_ano": ano})
            if linhas:
                resumo = resumir_tesouro(linhas, ano)
                break
    if not resumo:
        log(f"Tesouro: nenhuma transferência encontrada para {uf}.")
        return
    # O Tesouro pode usar o código IBGE com 6 ou 7 dígitos; os dois são aceitos.
    por6 = {m["id"][:6]: m["id"] for m in municipios}
    ids = {m["id"] for m in municipios}
    ligados = 0
    for cod, info in resumo.items():
        id7 = cod if len(cod) == 7 else por6.get(cod[:6])
        if id7 in ids:
            dados["municipios"].setdefault(id7, {})["transferencias"] = info
            ligados += 1
    log(f"Tesouro: {uf} — transferências de {ligados} municípios (ano {next(iter(resumo.values()))['ano']}).")


# ----------------------------------------------------------------- Portal da Transparência

def consultar_programa(caminho, codigo_ibge, mes_ano, chave):
    url = f"{PORTAL}/{caminho}?" + urllib.parse.urlencode(
        {"mesAno": mes_ano, "codigoIbge": codigo_ibge, "pagina": 1})
    resp = baixar_json(url, {"chave-api-dados": chave})
    time.sleep(PAUSA_PORTAL)
    if resp is None:
        return None
    regs = lista_de_registros(resp)
    if not regs:
        return {"valor": 0.0, "beneficiarios": 0, "vazio": True}
    valor = sum(numero(chave_ci(r, "valor")) for r in regs)
    benef = sum(int(numero(chave_ci(r, "quantidadeBeneficiados"))) for r in regs)
    return {"valor": round(valor, 2), "beneficiarios": benef, "vazio": False}


def descobrir_mes(programa, chave):
    """Descobre o mês mais recente com dados, testando a cidade de São Paulo."""
    hoje = date.today()
    for caminho in PROGRAMAS[programa]:
        a, m = hoje.year, hoje.month
        for _ in range(7):
            m -= 1
            if m == 0:
                a, m = a - 1, 12
            mes_ano = f"{a}{m:02d}"
            r = consultar_programa(caminho, "3550308", mes_ano, chave)
            if r is None:
                break  # endereço não existe ou não respondeu: tenta o próximo endereço
            if not r["vazio"]:
                log(f"Portal: {programa} → mês mais recente {m:02d}/{a} ({caminho}).")
                return caminho, mes_ano
    log(f"Portal: não encontrei dados recentes de {programa}.")
    return None, None


def atualizar_portal(uf, municipios, dados, chave, referencias):
    for programa, (caminho, mes_ano) in referencias.items():
        if not caminho:
            continue
        mes_txt = f"{mes_ano[:4]}-{mes_ano[4:]}"
        feitos = pulados = 0
        for i, m in enumerate(municipios, 1):
            atual = dados["municipios"].get(m["id"], {}).get(programa)
            if atual and atual.get("mes") == mes_txt:
                pulados += 1
                continue
            r = consultar_programa(caminho, m["id"], mes_ano, chave)
            if r is None:
                continue
            dados["municipios"].setdefault(m["id"], {})[programa] = {
                "mes": mes_txt, "valor": r["valor"], "beneficiarios": r["beneficiarios"]}
            feitos += 1
            if i % 100 == 0:
                log(f"   {uf} {programa}: {i}/{len(municipios)}")
                gravar_arquivo(uf, dados)  # salva o progresso
        log(f"Portal: {uf} {programa} — {feitos} atualizados, {pulados} já estavam em dia.")



# ----------------------------------------------------------------- PDDE e merenda (Recursos Transferidos)

DOWNLOAD_TRANSF = "https://portaldatransparencia.gov.br/download-de-dados/transferencias/{ano}{mes:02d}"
PASTA_PDDE = os.path.join(PASTA, "pdde")
PASTA_MENSAL = os.path.join(PASTA_PDDE, "mensal")

PREFIXOS_ESCOLA = [
    "UNIDADE EXECUTORA DO CONSELHO ESCOLAR DA", "UNIDADE EXECUTORA DO CONSELHO ESCOLAR DO",
    "UNIDADE EXECUTORA CONSELHO ESCOLAR DA", "UNIDADE EXECUTORA CONSELHO ESCOLAR DO",
    "UNIDADE EXECUTORA CONSELHO ESCOLAR", "UNIDADE EXECUTORA DA", "UNIDADE EXECUTORA DO", "UNIDADE EXECUTORA",
    "UEX DO CONSELHO ESCOLAR DA", "UEX DO CONSELHO ESCOLAR DO", "UEX CONSELHO ESCOLAR DA",
    "UEX CONSELHO ESCOLAR DO", "UEX CONS ESCOLAR DA", "UEX CONSELHO ESCOLAR", "UEX DA", "UEX DO", "UEX",
    "CONSELHO ESCOLAR DA", "CONSELHO ESCOLAR DO", "CONSELHO ESCOLAR DE", "CONSELHO ESCOLAR",
    "CONSELHO ECOLAR DA", "CONSELHO DA ESCOLA", "CONSELHO DO", "CONS ESCOLAR DA", "CONS ESCOLAR DO",
    "CONS.ESCOLAR DA", "CONS.ESCOLAR DO", "CONS. ESCOLAR DA", "CONS ESCOLAR",
    "CAIXA ESCOLAR DA", "CAIXA ESCOLAR DO", "CAIXA ESCOLAR",
    "ASSOCIACAO DE PAIS E MESTRES DA", "ASSOCIACAO DE PAIS E MESTRES DO", "ASSOCIACAO DE PAIS E MESTRES",
    "ASSOCIACAO DE PAIS MESTRES E FUNCIONARIOS DA", "APMF DA", "APMF DO", "APM DA", "APM DO", "APM",
    "ASSOCIACAO DE APOIO A ESCOLA", "ASSOCIACAO DE APOIO DA", "ASSOCIACAO DE APOIO",
]
ABREV = {"EM": "Escola Municipal", "EE": "Escola Estadual", "E M": "Escola Municipal", "E E": "Escola Estadual",
         "ESC MUN": "Escola Municipal", "ESC EST": "Escola Estadual", "ESC": "Escola", "CMEI": "CMEI",
         "CEMEI": "CEMEI", "EMEF": "EMEF", "EMEI": "EMEI"}
MINUSCULAS = {"da", "de", "do", "das", "dos", "e", "em"}


ENTIDADE = re.compile(
    r"^(?:"
    r"UNID(?:ADE)?\.?\s*E[XSC]+ECUTORA|U\.?\s?EX\.?|UEX|"
    r"CONS(?:ELHO)?\.?\s*(?:ESCOLAR|ECOLAR|ESC\.?)|CONSELHO\s+(?:DA|DE)\s+ESCOLA|CONSELHO\s+(?:DO|DA)|"
    r"CAIXA\s+ESCOLAR|"
    r"ASSOC\w*\s+(?:DE\s+)?PAIS(?:\s*,?\s*(?:E\s+)?MESTRES)?(?:\s*E\s+FUNCIONARIOS)?|APMF?|APM\b|"
    r"ASSOC\w*\s+(?:DE\s+)?APOIO(?:\s+(?:A|À)\s+ESCOLA)?|ASSOC\w*\s+ESCOLAR|ASSOC\w*\s+COMUNITARIA"
    r")\s*(?:D[AOE]S?\s+)?"
)
SIGLAS = {"CMEI", "CEMEI", "EMEF", "EMEI", "EREM", "ETE", "EJA", "II", "III", "IV", "VI", "VII", "CEI",
          "CAIC", "CIEP", "CAIC", "EEEP", "EEEFM", "EEF", "EEM", "CEU", "SESI", "UR", "CEJA", "IFPE", "CMEF"}
ABREVIACOES = [
    (r"^(?:ESC(?:OLA)?\.?\s+MUN(?:ICIPAL|IC)?\.?|E\.?\s?M\.?(?:\s|$)|EM\s)", "Escola Municipal "),
    (r"^(?:ESC(?:OLA)?\.?\s+EST(?:ADUAL)?\.?|E\.?\s?E\.?(?:\s|$)|EE\s)", "Escola Estadual "),
    (r"^ESC(?:OLA)?\.?\s", "Escola "),
    (r"^COL(?:EGIO)?\.?\s+MUN(?:ICIPAL)?\.?\s", "Colégio Municipal "),
    (r"^COL(?:EGIO)?\.?\s+EST(?:ADUAL)?\.?\s", "Colégio Estadual "),
]
MINUSCULAS = {"da", "de", "do", "das", "dos", "e", "em"}


def nome_escola(bruto):
    """Transforma 'CONSELHO ESCOLAR DA EM CHICO SCIENCE' em 'Escola Municipal Chico Science'."""
    original = re.sub(r"\s+", " ", bruto).strip()
    n = re.sub(r"\s+", " ", normalizar(original).upper().replace(".", " ")).strip()
    n = re.sub(r"\bMUL\b|\bMUNIC\b", "MUNICIPAL", n)
    for _ in range(3):   # remove "Conselho Escolar da", "Unidade Executora do"... (às vezes repetidos)
        novo = ENTIDADE.sub("", n).strip(" -.,")
        if novo == n:
            break
        n = novo
    if len(n) < 4:       # sobrou só o tipo da entidade: mantém o nome original
        n = re.sub(r"\s+", " ", normalizar(original).upper()).strip()
    prefixo = ""
    for padrao, troca in ABREVIACOES:
        m = re.match(padrao, n + " ")
        if m:
            prefixo, n = troca.strip(), (n + " ")[m.end():].strip()
            break
    palavras = []
    for i, w in enumerate(n.split(" ")):
        if not w:
            continue
        lw = w.lower()
        if lw in MINUSCULAS and (i > 0 or prefixo):
            palavras.append(lw)
        elif w.strip(".") in SIGLAS:
            palavras.append(w)
        elif w in ("PROF", "PROFA", "PROFª", "DR", "DRA", "PE", "STA", "STO"):
            palavras.append(lw.capitalize() + ".")
        else:
            palavras.append(lw.capitalize())
    nome = (prefixo + " " + " ".join(palavras)).strip()
    tipos = ("escola", "colégio", "colegio", "creche", "centro", "grupo", "instituto", "ginásio", "ginasio",
             "unidade", "núcleo", "nucleo", "liceu", "associ", "cmei", "cemei", "emef", "emei", "erem", "ete",
             "cei", "caic", "ciep", "eeep", "ceja", "cmef", "ifpe")
    if nome and not nome.lower().startswith(tipos):
        nome = "Escola " + nome
    return nome or original.title()


def eh_estadual(nome):
    n = normalizar(nome).upper()
    return any(t in n for t in ("SECRETARIA DE ESTADO", "SEC DE ESTADO", "SECRETARIA ESTADUAL", "SEC EST ",
                                "GOVERNO DO ESTADO", "ESTADO DE ", "ESTADO DO ", "ESTADO DA ", "SEDUC",
                                "SECRETARIA DA EDUCACAO DO ESTADO", "SECRETARIA DE EDUCACAO DO ESTADO",
                                "SECRETARIA DE EDUCACAO E ESPORTES", "DISTRITO FEDERAL"))


def mapa_siafi_ibge():
    """Liga o código SIAFI do município (usado pelo Portal) ao código IBGE."""
    try:
        req = urllib.request.Request(LISTA_RESERVA, headers={"User-Agent": "BrasilEmDados/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            texto = r.read().decode("utf-8-sig")
    except Exception as e:
        log(f"PDDE: não consegui baixar a tabela SIAFI→IBGE ({e.__class__.__name__}).")
        return {}
    mapa = {}
    for linha in csv.DictReader(io.StringIO(texto)):
        try:
            ibge = linha["codigo_ibge"].strip()
            mapa[int(linha["siafi_id"])] = ibge
            uf = UF_POR_CODIGO.get(str(linha.get("codigo_uf", "")).strip())
            if uf:   # plano B: reconhecer pela UF + nome do município
                mapa[(uf, normalizar(linha["nome"]).replace("-", " ").replace("'", " "))] = ibge
        except (KeyError, ValueError):
            continue
    log(f"PDDE: tabela SIAFI→IBGE com {sum(1 for k in mapa if isinstance(k, int))} municípios.")
    return mapa


def baixar_arquivo_mes(ano, mes, destino):
    """Baixa o ZIP mensal de Recursos Transferidos. Devolve True se baixou um ZIP válido."""
    url = DOWNLOAD_TRANSF.format(ano=ano, mes=mes)
    cab = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) BrasilEmDados/1.0", "Accept": "*/*"}
    for tentativa in range(1, 4):
        try:
            req = urllib.request.Request(url, headers=cab)
            with urllib.request.urlopen(req, timeout=300) as r, open(destino, "wb") as f:
                while True:
                    bloco = r.read(1 << 20)
                    if not bloco:
                        break
                    f.write(bloco)
            if zipfile.is_zipfile(destino):
                return True
            log(f"   {mes:02d}/{ano}: o Portal não devolveu um arquivo ZIP (mês ainda não publicado?).")
            return False
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                log(f"   {mes:02d}/{ano}: arquivo não disponível (HTTP {e.code}).")
                return False
            log(f"   {mes:02d}/{ano}: erro HTTP {e.code} (tentativa {tentativa}/3)")
        except Exception as e:
            log(f"   {mes:02d}/{ano}: falha {e.__class__.__name__} (tentativa {tentativa}/3)")
        time.sleep(10 * tentativa)
    return False


def coluna(cabecalho, *palavras):
    """Acha a coluna cujo nome contém todas as palavras (sem acento, minúsculas)."""
    for i, nome in enumerate(cabecalho):
        n = normalizar(nome)
        if all(p in n for p in palavras):
            return i
    return None


def processar_mes(caminho_zip, siafi_ibge):
    """Lê o CSV do mês e soma PDDE (por escola) e merenda por município."""
    resumo = {}   # uf -> ibge -> {"esc": {cnpj: [nome, valor]}, "pref": v, "pnae": v}
    linhas_pdde = linhas_pnae = linhas_saude = sem_mun = 0
    nao_achados = {}
    sem_local = {}   # uf -> cnpj -> [nome, valor]  (pagamentos a escolas sem município no arquivo)
    with zipfile.ZipFile(caminho_zip) as z:
        nome_csv = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if not nome_csv:
            return None
        with z.open(nome_csv) as bruto:
            texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
            leitor = csv.reader(texto, delimiter=";", quotechar='"')
            cab = next(leitor)
            c_uf = coluna(cab, "uf")
            c_mun = coluna(cab, "municipio", "siafi")
            c_nmun = coluna(cab, "nome", "municipio")
            c_acao = coluna(cab, "nome", "acao")
            c_cod = coluna(cab, "codigo", "favorecido")
            c_fav = coluna(cab, "nome", "favorecido")
            c_tipo = coluna(cab, "tipo", "favorecido")
            c_val = coluna(cab, "valor")
            c_func = next((i for i, c in enumerate(cab) if normalizar(c) in ("nome funcao", "funcao")), None)
            c_org = coluna(cab, "orgao", "superior")
            if None in (c_uf, c_mun, c_acao, c_fav, c_val):
                log(f"   colunas inesperadas no arquivo: {cab}")
                return None
            for row in leitor:
                if len(row) <= max(c_uf, c_mun, c_acao, c_fav, c_val):
                    continue
                acao = normalizar(row[c_acao])
                if "dinheiro direto na escola" in acao:
                    prog = "pdde"
                elif "alimentacao escolar" in acao:
                    prog = "pnae"
                elif "salario" in acao and "educacao" in acao:
                    prog = "se"
                elif "transporte escolar" in acao:
                    prog = "pnate"
                elif (normalizar(row[c_func]) == "saude" if c_func is not None and c_func < len(row) else
                      ("saude" in normalizar(row[c_org]) if c_org is not None and c_org < len(row) else
                       any(k in acao for k in ("atencao primaria", "media e alta complexidade",
                                               "agentes comunitarios de saude", "saude")))):
                    prog = "saude"
                else:
                    continue
                valor = numero(row[c_val])
                if not valor:
                    continue
                uf = row[c_uf].strip().upper()
                try:
                    ibge = siafi_ibge.get(int(row[c_mun]))
                except ValueError:
                    ibge = None
                if not ibge and c_nmun is not None:
                    chave = (uf, normalizar(row[c_nmun]).replace("-", " ").replace("'", " "))
                    ibge = siafi_ibge.get(chave)
                favorecido = row[c_fav].strip()
                tipo = normalizar(row[c_tipo]) if c_tipo is not None else ""
                cnpj = (row[c_cod].strip() if c_cod is not None else "") or favorecido
                eh_escola = prog == "pdde" and not ("administracao publica" in tipo or "orgao" in tipo
                                                    or favorecido.upper().startswith(("PREF", "MUNICIPIO")))
                if not ibge and uf in UFS_NOMES and eh_escola:
                    item = sem_local.setdefault(uf, {}).setdefault(cnpj, [nome_escola(favorecido), 0.0])
                    item[1] += valor
                    linhas_pdde += 1
                    continue
                if not ibge or uf not in UFS_NOMES:
                    sem_mun += 1
                    exemplo = (uf, row[c_mun].strip(), row[c_nmun].strip() if c_nmun is not None else "")
                    nao_achados[exemplo] = nao_achados.get(exemplo, 0) + 1
                    continue
                m = resumo.setdefault(uf, {}).setdefault(ibge, {"esc": {}, "pref": 0.0, "pnae": 0.0, "se": 0.0, "pnate": 0.0, "saude": {}})
                if prog == "saude":
                    if not eh_estadual(favorecido) and "estadual" not in tipo and "federal" not in tipo:
                        if "atencao primaria" in acao or "atencao basica" in acao:
                            cat = "primaria"
                        elif "media e alta complexidade" in acao:
                            cat = "mac"
                        elif "agentes comunitarios" in acao:
                            cat = "acs"
                        else:
                            cat = "outros"
                        m["saude"][cat] = m["saude"].get(cat, 0.0) + valor
                        linhas_saude += 1
                    continue
                if prog in ("se", "pnate"):
                    if not eh_estadual(favorecido):
                        m[prog] += valor
                    continue
                if prog == "pnae":
                    if not eh_estadual(favorecido):
                        m["pnae"] += valor
                    linhas_pnae += 1
                    continue
                linhas_pdde += 1
                if not eh_escola:
                    if not eh_estadual(favorecido):
                        m["pref"] += valor
                    continue
                item = m["esc"].setdefault(cnpj, [nome_escola(favorecido), 0.0])
                item[1] += valor
    log(f"   linhas: {linhas_pdde} de PDDE, {linhas_pnae} de merenda e {linhas_saude} de saúde "
        f"({sem_mun} sem município identificado). Coluna de função: {'sim' if c_func is not None else 'não'}.")
    if nao_achados:
        top = sorted(nao_achados.items(), key=lambda kv: -kv[1])[:6]
        log("   não identificados (UF, código, nome → linhas): " + "; ".join(f"{k} → {v}" for k, v in top))
    if sem_local:
        log(f"   {sum(len(v) for v in sem_local.values())} escolas vieram sem município; serão localizadas pelo CNPJ.")
    for uf in list(resumo.values()):
        for m in uf.values():
            m["pref"] = round(m["pref"], 2)
            m["pnae"] = round(m["pnae"], 2)
            m["se"] = round(m["se"], 2)
            m["pnate"] = round(m["pnate"], 2)
            m["saude"] = {k: round(v, 2) for k, v in m["saude"].items()}
            for v in m["esc"].values():
                v[1] = round(v[1], 2)
    for itens in sem_local.values():
        for v in itens.values():
            v[1] = round(v[1], 2)
    resumo["_sem_local"] = sem_local
    return resumo


VERSAO_CACHE = "v6"   # mudar este valor obriga o robô a reprocessar todos os meses


def caminho_mensal(ano, mes):
    return os.path.join(PASTA_MENSAL, f"{ano}-{mes:02d}.{VERSAO_CACHE}.json.gz")


def limpar_cache_antigo():
    for nome in os.listdir(PASTA_MENSAL):
        if nome.endswith(".json.gz") and f".{VERSAO_CACHE}." not in nome:
            os.remove(os.path.join(PASTA_MENSAL, nome))


def atualizar_pdde(ufs):
    os.makedirs(PASTA_MENSAL, exist_ok=True)
    limpar_cache_antigo()
    siafi_ibge = mapa_siafi_ibge()
    if not siafi_ibge:
        return
    hoje = date.today()
    meses_ok = {}
    for ano in (hoje.year,):
        for mes in range(1, 13):
            if (ano, mes) > (hoje.year, hoje.month):
                break
            destino = caminho_mensal(ano, mes)
            recente = (hoje.year * 12 + hoje.month) - (ano * 12 + mes) <= 2   # meses recentes podem ser revisados
            if os.path.exists(destino) and not recente:
                meses_ok[(ano, mes)] = destino
                continue
            log(f"PDDE: baixando Recursos Transferidos de {mes:02d}/{ano}…")
            with tempfile.TemporaryDirectory() as tmp:
                arq = os.path.join(tmp, "mes.zip")
                if not baixar_arquivo_mes(ano, mes, arq):
                    if os.path.exists(destino):
                        meses_ok[(ano, mes)] = destino
                    continue
                log(f"   arquivo com {os.path.getsize(arq) / 1e6:.0f} MB; lendo…")
                resumo = processar_mes(arq, siafi_ibge)
            if resumo is None:
                continue
            with gzip.open(destino, "wt", encoding="utf-8") as f:
                json.dump(resumo, f, ensure_ascii=False, separators=(",", ":"))
            meses_ok[(ano, mes)] = destino
    if not meses_ok:
        log("PDDE: nenhum mês disponível ainda.")
        return
    meses = sorted(meses_ok)
    ano = meses[-1][0]
    log(f"PDDE: somando {len(meses)} meses de {ano}.")
    total = {}
    mensais = []
    local_cnpj = {}   # cnpj da escola -> (uf, ibge), aprendido nos meses em que veio com município
    for chave in meses:
        with gzip.open(meses_ok[chave], "rt", encoding="utf-8") as f:
            mensal = json.load(f)
        mensais.append(mensal)
        for uf, muns in mensal.items():
            if uf.startswith("_"):
                continue
            for ibge, m in muns.items():
                for cnpj in m["esc"]:
                    local_cnpj[cnpj] = (uf, ibge)
    achados = perdidos = 0
    for mensal in mensais:
        for uf, itens in mensal.get("_sem_local", {}).items():
            for cnpj, (nome, valor) in itens.items():
                destino = local_cnpj.get(cnpj)
                if not destino or destino[0] != uf:
                    perdidos += 1
                    continue
                achados += 1
                m = mensal.setdefault(uf, {}).setdefault(destino[1], {"esc": {}, "pref": 0.0, "pnae": 0.0, "se": 0.0, "pnate": 0.0, "saude": {}})
                e = m["esc"].setdefault(cnpj, [nome, 0.0])
                e[1] += valor
    log(f"PDDE: {achados} pagamentos sem município foram localizados pelo CNPJ; {perdidos} ficaram sem localização.")
    for mensal in mensais:
        for uf, muns in mensal.items():
            if uf.startswith("_") or uf not in ufs:
                continue
            for ibge, m in muns.items():
                t = total.setdefault(uf, {}).setdefault(ibge, {"esc": {}, "pref": 0.0, "pnae": 0.0, "se": 0.0, "pnate": 0.0, "saude": {}})
                t["pref"] += m["pref"]
                t["pnae"] += m["pnae"]
                t["se"] += m.get("se", 0.0)
                t["pnate"] += m.get("pnate", 0.0)
                for k, v in m.get("saude", {}).items():
                    t["saude"][k] = t["saude"].get(k, 0.0) + v
                for cnpj, (nome, valor) in m["esc"].items():
                    e = t["esc"].setdefault(cnpj, [nome, 0.0])
                    e[1] += valor
    agora = datetime.now(FUSO_BR).isoformat(timespec="seconds")
    for uf in ufs:
        saida = {"uf": uf, "ano": ano, "mes_inicial": meses[0][1], "mes_final": meses[-1][1],
                 "atualizado_em": agora, "fonte": "Portal da Transparência — Recursos Transferidos",
                 "municipios": {}}
        for ibge, t in total.get(uf, {}).items():
            escolas = sorted(([polir_nome(n), round(v, 2)] for n, v in t["esc"].values() if v > 0), key=lambda x: -x[1])
            soma_esc = round(sum(v for _, v in escolas), 2)
            saida["municipios"][ibge] = {
                "pdde_total": round(soma_esc + t["pref"], 2),
                "pdde_escolas_total": soma_esc,
                "pdde_prefeitura": round(t["pref"], 2),
                "pdde_escolas": escolas,
                "pnae": round(t["pnae"], 2),
                "salario_educacao": round(t["se"], 2),
                "transporte_escolar": round(t["pnate"], 2),
                "saude": dict({k: round(v, 2) for k, v in t["saude"].items()},
                              total=round(sum(t["saude"].values()), 2)),
            }
        with open(os.path.join(PASTA_PDDE, f"{uf}.json"), "w", encoding="utf-8") as f:
            json.dump(saida, f, ensure_ascii=False, separators=(",", ":"))
        log(f"PDDE: {uf} — {len(saida['municipios'])} municípios gravados.")

# ----------------------------------------------------------------- PDDE: polimento final dos nomes

_TIPO = (r"(?:Escola|Esc\b|Creche|CMEI|CEMEI|EMEF|EMEI|EMEIEF|Emeief|Emef|Emei|Centro|Colegio|Col[eé]gio|Nucleo|N[uú]cleo|"
         r"Unidade Escolar|Grupo Escolar|Jardim|Instituto|E\.?\s?M\b|E\s?E\b|e e\b|e M\b|C M e I\b)")
_ENTIDADE = (r"(?:Associa|Assoc\b|Conselho|Caixa|Apm|A\s?P\s?M\b|A\s?P\s?P\b|Mestre|Metre|Meste|Pais\b|Uex|Unidade Executora|"
             r"Congrega|Propria|Pr[oó]pria|Funcionarios|Circulo|Comunidade Escolar|E Com\b|Colegiado|Recursos Financeiros|Educadores|Amigos|Appf?\b|Gestor|E Professores|E Func\b|Unid Exec|Cons Esc)")
_CORTE_PREFIXO = re.compile(r"^(?=.*?" + _ENTIDADE + r").*?\b(?:da|do|de|das|dos)\s+(?=" + _TIPO + ")", re.I)
_SUFIXO = re.compile(r"\s*[-–(]\s*(?:Apm|A\s?P\s?M|Uex|Aspas|Associa\w*[^)]*)\b[^)]*\)?\s*$|\s+-\s+[A-Z][a-z]\s*$|\s+-\s+[A-Z][a-z]+\s+-\s+[A-Z][a-z]\s*$", re.I)
_SIGLAS_EXP = [
    (re.compile(r"\bMunicipal e I e F\b"), "Municipal de Educação Infantil e Ensino Fundamental"),
    (re.compile(r"\bMunicipal e F\b"), "Municipal de Ensino Fundamental"),
    (re.compile(r"\bMunicipal e I\b"), "Municipal de Educação Infantil"),
    (re.compile(r"\bC M e I\b"), "Centro Municipal de Educação Infantil"),
    (re.compile(r"\b(\d)\s?(?:G|Grau)\b"), r"\1º Grau"),
    (re.compile(r"\bPre\s?-?\s?Escolar\b", re.I), "Pré-Escolar"),
]
_ACENTOS = {
    "educacao": "Educação", "basica": "Básica", "colegio": "Colégio", "nucleo": "Núcleo", "medio": "Médio",
    "indigena": "Indígena", "pre": "Pré", "joao": "João", "jose": "José", "antonio": "Antônio", "sebastiao": "Sebastião",
    "conceicao": "Conceição", "goncalves": "Gonçalves", "sao": "São", "assuncao": "Assunção", "graca": "Graça",
    "gracas": "Graças", "lucia": "Lúcia", "marcia": "Márcia", "patricia": "Patrícia", "fatima": "Fátima", "inacio": "Inácio",
    "vitoria": "Vitória", "gloria": "Glória", "julia": "Júlia", "cecilia": "Cecília", "emilia": "Emília", "otavio": "Otávio",
    "flavio": "Flávio", "fabio": "Fábio", "claudio": "Cláudio", "rogerio": "Rogério", "sergio": "Sérgio", "mario": "Mário",
    "vinicius": "Vinícius", "lazaro": "Lázaro", "anisio": "Anísio", "cassio": "Cássio", "tancredo": "Tancredo",
    "simoes": "Simões", "guimaraes": "Guimarães", "magalhaes": "Magalhães", "araujo": "Araújo", "conceicão": "Conceição",
    "frei": "Frei", "padre": "Padre", "irma": "Irmã", "crianca": "Criança", "criancas": "Crianças", "integracao": "Integração",
    "esperanca": "Esperança", "uniao": "União", "reuniao": "Reunião", "comunitaria": "Comunitária", "agricola": "Agrícola",
    "tecnica": "Técnica", "politecnica": "Politécnica", "rural": "Rural", "jesus": "Jesus", "mae": "Mãe", "maes": "Mães",
    "irmaos": "Irmãos", "familia": "Família", "patio": "Pátio", "historia": "História", "republica": "República",
    "nossa": "Nossa", "senhora": "Senhora", "cicero": "Cícero", "monica": "Mônica", "angela": "Ângela", "angelo": "Ângelo",
    "helio": "Hélio", "eugenio": "Eugênio", "jeronimo": "Jerônimo", "candido": "Cândido", "acacio": "Acácio",
    "silverio": "Silvério", "pedagogico": "Pedagógico", "ensino": "Ensino", "fundamental": "Fundamental",
    "infancia": "Infância", "rondonia": "Rondônia", "amapa": "Amapá", "para": None, "goias": "Goiás", "piaui": "Piauí",
    "associacao": "Associação", "principe": "Príncipe", "xodo": "Xodó", "vovo": "Vovó", "paraiso": "Paraíso",
    "infancias": "Infâncias", "cristovao": "Cristóvão", "mendonca": "Mendonça", "florencio": "Florêncio", "felix": "Félix",
    "goiania": "Goiânia", "goianesia": "Goianésia", "ceara": "Ceará", "paraiba": "Paraíba", "maranhao": "Maranhão", "parana": "Paraná", "brasilia": "Brasília",
}
_MINUSC = {"da", "de", "do", "das", "dos", "e", "em", "na", "no"}
_SIGLAS_ESCOLA = {"emei", "emef", "emeief", "emeif", "cmei", "cemei", "cei", "caic", "ciep", "apae", "eja", "cieja"}


def _palavra(w, primeira):
    base = w.lower()
    if base in _SIGLAS_ESCOLA:
        return w.upper()
    if w.isupper() and 2 <= len(w) <= 6 and w.lower() not in _MINUSC:
        return w
    if base in _MINUSC and not primeira:
        return base
    if base in _ACENTOS and _ACENTOS[base]:
        return _ACENTOS[base]
    if w.isupper() and len(w) <= 6 and len(w) >= 2 and not re.search(r"[AEIOU]{2}", w):
        return w                        # sigla (CMEI, EMEF…)
    return w[:1].upper() + w[1:]


def polir_nome(nome):
    """Arruma o nome já limpo: tira prefixos de associação/conselho, sufixos de sigla,
    aspas, expande abreviações comuns, põe acentos em palavras frequentes."""
    n = (nome or "").strip()
    original = n
    n = re.sub(r"['\"´`]+", " ", n)                                   # aspas
    n = re.sub(r"\(\s*(?:uex|apm|associa[^)]*)\)\s*,?", " ", n, flags=re.I)
    for _ in range(2):
        novo = _CORTE_PREFIXO.sub("", n)
        if novo == n:
            break
        n = novo
    n = re.sub(r"^(?=.*?" + _ENTIDADE + r")(?:Escola\s+)?.*?\b(?:Escolar\s+)?(?=(?:Escola (?:Municipal|Estadual|de)|Creche|CMEI|CEMEI|EMEF|EMEI|"
               r"Centro (?:Municipal|de Educa)|Colegio|Col[eé]gio)\b)", "", n, count=1, flags=re.I) if not re.match(
               r"(?:Escola (?:Municipal|Estadual|de)|Creche|CMEI|CEMEI|EMEF|EMEI|Centro|Col[eé]gio)\b", n, re.I) else n
    n = re.sub(r"^Escola\s+(?:da|do)\s+(?=" + _TIPO + ")", "", n, flags=re.I)
    n = re.sub(r"^(?:Escola\s+)?Caixa Escolar?\s+", "", n, flags=re.I)
    n = re.sub(r"^(?:Escola\s+)?E Amigos dos Excepcionais\b", "APAE", n, flags=re.I)
    if re.fullmatch(r"(?:Escola\s+)?(?:A\s?P\s?M|Apm|Associacao de Pais e Mestres)(?:\s*-\s*Apm)?", n, flags=re.I):
        return "Associação de Pais e Mestres (APM)"
    n = re.sub(r"^(?:Escola\s+)?(?:A\s?P\s?M|Apm|Uex)\s+(?:da\s+|do\s+)?", "", n, flags=re.I)
    n = _SUFIXO.sub("", n)
    n = re.sub(r"\s*-\s*Apm\w*\s*$", "", n, flags=re.I)
    for rx, rep in _SIGLAS_EXP:
        n = rx.sub(rep, n)
    n = re.sub(r"^(?:Escola\s+)?E\s?M e F\b", "Escola Municipal de Ensino Fundamental", n, flags=re.I)
    n = re.sub(r"^E\s?M\b\.?\s*", "Escola Municipal ", n)
    n = re.sub(r"^(?:e e|E E|EE)\b\.?\s*", "Escola Estadual ", n)
    n = re.sub(r"^Esc\.?\s+", "Escola ", n)
    n = re.sub(r"\bEscola Mun\.?\b", "Escola Municipal", n)
    n = re.sub(r"^(CMEI|CEMEI|EMEF|EMEI|EMEIEF)\s*-\s*", r"\1 ", n, flags=re.I)
    n = re.sub(r"^APAE\s*-\s*APAE\b", "APAE", n, flags=re.I)
    n = re.sub(r"^((?:Escola|Centro) Municipal de [^-]+?)\s*-\s*(?:EMEIEF|EMEF|EMEI|CMEI|CEMEI)\s+", r"\1 ", n, flags=re.I)
    n = re.sub(r"^Escola\s+(?=(?:Escola|Creche|CMEI|CEMEI|EMEF|EMEI|EMEIEF|Centro|Col[eé]gio|N[uú]cleo|Unidade Escolar|"
               r"Grupo Escolar|Instituto|Jardim de Inf)\b)", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" ,.-")
    partes = re.split(r"(\s+|-|\(|\)|/)", n)
    saida, primeira = [], True
    for p in partes:
        if p and re.match(r"\w", p):
            saida.append(_palavra(p, primeira))
            primeira = False
        else:
            saida.append(p)
    n = "".join(saida)
    n = re.sub(r"\((\w{2,8})\)", lambda m: "(" + m.group(1).upper() + ")" if not re.search(r"[aeiou]{2}", m.group(1).lower()) else m.group(0), n)
    if len(n) < 4 or not re.search(r"[A-Za-zÀ-ú]{3}", n):
        return original
    if not re.match(r"(Escola|Creche|CMEI|CEMEI|EMEF|EMEI|EMEIEF|Emeief|Centro|Colégio|Núcleo|Unidade|Grupo|Instituto|Jardim|"
                    r"Associação|Apae|APAE|CEI|CAIC|Ciep|CIEP|Casa|Lar|Educandário|Instituição|Projeto|Sociedade|Fundação|Obra)", n):
        n = "Escola " + n
    return n


# ----------------------------------------------------------------- Fundeb: matrículas (FNDE)

INDICE_MATRICULAS = ("https://www.gov.br/fnde/pt-br/acesso-a-informacao/acoes-e-programas/financiamento/"
                     "fundeb/matriculas-da-educacao-basica-1")
RESERVA_MATRICULAS = ("https://www.gov.br/fnde/pt-br/acesso-a-informacao/acoes-e-programas/financiamento/fundeb/"
                      "matriculas-da-educacao-basica/2026-com-base-na-portaria-interministerial-no-11-de-28-08-2026-1/"
                      "1-matriculas-coeficientes-e-estimativas-de-receita-do-fundeb-2026.xlsx/@@download/file")
PASTA_FUNDEB = os.path.join(PASTA, "fundeb")


def baixar_bytes(url, tempo=120):
    cab = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) BrasilEmDados/1.0", "Accept": "*/*"}
    for tentativa in range(1, 4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=tempo) as r:
                return r.read()
        except Exception as e:
            log(f"   falha ao baixar ({e.__class__.__name__}), tentativa {tentativa}/3")
            time.sleep(8 * tentativa)
    return None


def achar_planilha_matriculas():
    """Procura no site do FNDE a planilha mais recente de matrículas do Fundeb.
    Devolve (url, descrição da portaria)."""
    html = baixar_bytes(INDICE_MATRICULAS)
    if html:
        html = html.decode("utf-8", "ignore")
        ano_atual = datetime.now(FUSO_BR).year
        paginas = set(re.findall(r'href="([^"]*/matriculas-da-educacao-basica/(\d{4})-com-base-na-portaria[^"]*)"', html))
        candidatas = []
        for link, ano in paginas:
            if int(ano) > ano_atual:
                continue
            d = re.search(r"de-(\d{1,2})-(\d{1,2})-(\d{4})", link)
            data_portaria = (int(d.group(3)), int(d.group(2)), int(d.group(1))) if d else (0, 0, 0)
            candidatas.append(((int(ano), data_portaria), link))
        for _, link in sorted(candidatas, reverse=True)[:3]:
            pagina = baixar_bytes(urllib.parse.urljoin(INDICE_MATRICULAS, link))
            if not pagina:
                continue
            pagina = pagina.decode("utf-8", "ignore")
            m = re.search(r'href="([^"]*1-matriculas-coeficientes[^"]*?\.xlsx)(?:/@@download/file)?"', pagina)
            if m:
                url = urllib.parse.urljoin(INDICE_MATRICULAS, m.group(1)) + "/@@download/file"
                nome = re.search(r"(\d{4}-com-base[^/]*)", link)
                return url, (nome.group(1) if nome else link)
    log("Fundeb: não achei a página do FNDE; usando o endereço conhecido da planilha.")
    return RESERVA_MATRICULAS, "2026-com-base-na-portaria-interministerial-no-11-de-28-08-2026"


def ler_xlsx(conteudo):
    """Lê a primeira aba de um .xlsx (sem bibliotecas extras). Devolve lista de linhas (listas)."""
    z = zipfile.ZipFile(io.BytesIO(conteudo))
    textos = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = z.read("xl/sharedStrings.xml").decode("utf-8")
        for si in re.findall(r"<si>(.*?)</si>", ss, re.S):
            textos.append(unescape(re.sub(r"<[^>]+>", "", "".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))))
    aba = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    xml = z.read(aba).decode("utf-8")
    linhas = []
    for corpo in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
        linha = {}
        for ref, attrs, interno in re.findall(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', corpo, re.S):
            v = re.search(r"<v>(.*?)</v>", interno or "", re.S)
            if 't="s"' in attrs and v:
                val = textos[int(v.group(1))]
            elif 't="inlineStr"' in attrs:
                val = unescape(re.sub(r"<[^>]+>", "", interno or ""))
            else:
                val = v.group(1) if v else ""
            col = 0
            for ch in ref:
                col = col * 26 + ord(ch) - 64
            linha[col - 1] = val
        if linha:
            linhas.append([linha.get(i, "") for i in range(max(linha) + 1)])
    return linhas


def atualizar_fundeb(ufs):
    """Grava dados/fundeb/UF.json com as matrículas da rede de cada município consideradas
    no Fundeb e a receita prevista no ano (planilha oficial do FNDE)."""
    log("===== Fundeb: matrículas do FNDE =====")
    url, portaria = achar_planilha_matriculas()
    log(f"Fundeb: planilha {portaria}")
    conteudo = baixar_bytes(url, tempo=300)
    if not conteudo or not zipfile.is_zipfile(io.BytesIO(conteudo)):
        log("Fundeb: não consegui baixar a planilha do FNDE. Os dados antigos foram mantidos.")
        return
    linhas = ler_xlsx(conteudo)
    cab_i = next((i for i, l in enumerate(linhas[:10]) if any(normalizar(c) == "codigo ibge" for c in l)), None)
    if cab_i is None:
        log("Fundeb: a planilha mudou de formato (sem coluna 'Código IBGE'). Nada foi alterado.")
        return
    cab = [normalizar(c).replace("\n", " ") for c in linhas[cab_i]]

    def col(*palavras):
        for i, c in enumerate(cab):
            if all(p in c for p in palavras):
                return i
        return None

    c_ibge, c_mat, c_rec = col("codigo ibge"), col("matriculas totais"), col("total das receitas")
    if c_mat is None:
        log("Fundeb: coluna 'Matrículas Totais' não encontrada. Nada foi alterado.")
        return
    ano = re.match(r"(\d{4})", portaria)
    ano = int(ano.group(1)) if ano else datetime.now(FUSO_BR).year
    por_uf = {}
    for l in linhas[cab_i + 1:]:
        if len(l) <= max(c_ibge, c_mat):
            continue
        cod = str(l[c_ibge]).split(".")[0].strip()
        if len(cod) != 7 or not cod.isdigit():
            continue          # linhas dos estados (código de 2 dígitos) ficam de fora
        uf = UF_POR_CODIGO.get(cod[:2])
        try:
            mat = int(round(float(l[c_mat] or 0)))
            rec = round(float(l[c_rec] or 0), 2) if c_rec is not None and c_rec < len(l) else None
        except ValueError:
            continue
        por_uf.setdefault(uf, {})[cod] = {"matriculas": mat, "receita_prevista": rec}
    os.makedirs(PASTA_FUNDEB, exist_ok=True)
    agora = datetime.now(FUSO_BR).isoformat(timespec="seconds")
    total = 0
    for uf in ufs:
        ms = por_uf.get(uf)
        if not ms:
            log(f"Fundeb {uf}: nenhum município na planilha.")
            continue
        total += len(ms)
        with open(os.path.join(PASTA_FUNDEB, uf + ".json"), "w", encoding="utf-8") as f:
            json.dump({"uf": uf, "ano": ano, "portaria": portaria, "atualizado_em": agora,
                       "fonte": "FNDE — Matrículas, coeficientes e estimativas de receita do Fundeb",
                       "url": url, "municipios": ms}, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Fundeb: matrículas gravadas para {total} municípios.")


# ----------------------------------------------------------------- principal

def main():
    chave = os.environ.get("PORTAL_CHAVE", "").strip()
    fontes = (os.environ.get("FONTES") or "todas").strip().lower()
    pedidas = [u.strip().upper() for u in (os.environ.get("UFS") or "").replace(";", ",").split(",") if u.strip()]
    ufs = [u for u in (pedidas or sorted(UFS_NOMES)) if u in UFS_NOMES]
    if not ufs:
        log("Nenhum estado válido informado. Use siglas como PE ou SP.")
        sys.exit(1)

    os.makedirs(PASTA, exist_ok=True)
    nojekyll = os.path.join(os.path.dirname(PASTA), ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()

    if fontes == "pdde":
        atualizar_pdde(ufs)
        log("Concluído.")
        return
    if fontes == "fundeb":
        atualizar_fundeb(ufs)
        log("Concluído.")
        return

    diagnostico(chave)
    municipios = municipios_ibge()
    if not municipios:
        log("Não foi possível baixar a lista de municípios (IBGE e cópia). Tente de novo mais tarde.")
        sys.exit(1)

    usar_tesouro = fontes in ("todas", "tesouro")
    usar_portal = fontes in ("todas", "portal")
    codigos = codigos_estados_tesouro() if usar_tesouro else {}

    referencias = {}
    if usar_portal:
        if not chave:
            log("Portal: segredo PORTAL_CHAVE não configurado — Bolsa Família e BPC ficarão de fora.")
        else:
            for programa in PROGRAMAS:
                referencias[programa] = descobrir_mes(programa, chave)

    for uf in ufs:
        lista = municipios.get(uf, [])
        log(f"===== {uf} — {UFS_NOMES[uf]} ({len(lista)} municípios) =====")
        dados = ler_arquivo(uf)
        dados["estado"] = UFS_NOMES[uf]
        for m in lista:
            dados["municipios"].setdefault(m["id"], {})["nome"] = m["nome"]
        if usar_tesouro:
            atualizar_tesouro(uf, lista, dados, codigos)
            gravar_arquivo(uf, dados)
        if referencias:
            atualizar_portal(uf, lista, dados, chave, referencias)
        dados["fontes"] = {
            "transferencias": "Tesouro Nacional — Transferências Constitucionais",
            "bolsa_familia": "Portal da Transparência (CGU)",
            "bpc": "Portal da Transparência (CGU)",
            "municipios": "IBGE",
        }
        gravar_arquivo(uf, dados)

    if fontes in ("todas", "tesouro"):
        try:
            atualizar_fundeb(ufs)
        except Exception as e:
            log(f"Fundeb: erro inesperado ({e.__class__.__name__}: {e}). Os dados antigos foram mantidos.")
    if fontes == "todas":
        atualizar_pdde(ufs)

    with open(os.path.join(PASTA, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"ultima_execucao": datetime.now(FUSO_BR).isoformat(timespec="seconds"),
                   "estados": ufs, "fontes": fontes}, f, ensure_ascii=False, indent=1)
    log("Concluído.")


if __name__ == "__main__":
    main()
