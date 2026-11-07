"""Gera o código do gadget do Blogger a partir de modelo.html.

Uso: python montar.py saida.html
Troca __MAPA__ pelo mapa do Brasil e escapa os acentos dentro dos <script>
(o Blogger às vezes estraga caracteres não ASCII no JavaScript).
O trecho entre "INÍCIO DO C..." e "FIM" vai no gadget HTML/JavaScript (HTML1).
"""
import os, re, sys
aqui = os.path.dirname(os.path.abspath(__file__))
s = open(os.path.join(aqui, "modelo.html"), encoding="utf-8").read()
s = s.replace("__MAPA__", open(os.path.join(aqui, "mapa_js.txt"), encoding="utf-8").read().strip())
s = re.sub(r"<script>.*?</script>", lambda m: "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in m.group(0)), s, flags=re.S)
open(sys.argv[1], "w", encoding="utf-8").write(s)
