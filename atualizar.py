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
  FONTES        → opcional: "todas", "tesouro" ou "portal"

Usa só bibliotecas que já vêm com o Python. Nada para instalar.
"""

import json
import os
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

    with open(os.path.join(PASTA, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"ultima_execucao": datetime.now(FUSO_BR).isoformat(timespec="seconds"),
                   "estados": ufs, "fontes": fontes}, f, ensure_ascii=False, indent=1)
    log("Concluído.")


if __name__ == "__main__":
    main()
