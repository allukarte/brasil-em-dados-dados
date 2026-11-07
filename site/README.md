# Código do site Repasse Aberto

Cópia de segurança do código que fica no Blogger (www.repasseaberto.com.br).

- `modelo.html`: página completa. A parte entre `INÍCIO DO C…` e `FIM` é o conteúdo do gadget
  HTML/JavaScript (Layout → Featured Post → HTML/JavaScript).
- `mapa_js.txt`: desenho do mapa do Brasil, inserido no lugar de `__MAPA__`.
- `montar.py`: gera a versão final (`python montar.py saida.html`).
- `notas-de-atualizacao.html`: conteúdo da postagem "Notas de Atualização".

Os dados ficam na pasta `dados/` deste repositório e são atualizados pelo robô (`atualizar.py`).
