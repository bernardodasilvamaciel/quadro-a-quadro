#!/usr/bin/env python3
"""Interface web para medir a latência à mão, quadro a quadro.

Abre no navegador uma página onde você escolhe o vídeo, navega pelos quadros e marca, em cada
acionamento, o início e o quadro em que a lâmpada acende ou apaga. O tempo de cada quadro vem do
carimbo de tempo gravado no vídeo, e as latências são salvas em <video>_latencias.csv, na pasta
do vídeo, a cada marcação.

Uso:  python interface_web.py [--pasta PASTA_DOS_VIDEOS] [--porta 8765]

Roda só neste computador (endereço 127.0.0.1). Ctrl+C encerra.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import threading
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

import latencia as L

PAGINA = Path(__file__).with_name("interface_web.html")
CAMPOS = ["video", "acionamento", "comando", "quadro_inicio", "t_inicio_ms", "quadro_lampada",
          "t_lampada_ms", "quadros", "latencia_ms"]
COMANDOS = ("ligar", "desligar")


class Sessao:
    """Um vídeo aberto: tempos dos quadros, leitor com cache e detecção da lâmpada."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.info = L.ler_info(caminho)
        self.k_rot = L.k_da_rotacao(self.info.rotacao)
        self.t, self.modo_tempo = L.calcular_tempos(self.info)
        self.leitor = L.LeitorQuadros(self.info, self.k_rot, 1.0, capacidade=16)
        self.jpegs: OrderedDict[int, bytes] = OrderedDict()
        self.trava = threading.Lock()
        self.arquivo = caminho.parent / f"{caminho.stem}_latencias.csv"
        self.config = caminho.parent / f"{caminho.stem}_manual.json"
        self.luz = {"estado": "nenhuma", "progresso": 0.0, "roi": None, "mudancas": []}
        if self.config.exists():
            dados = json.loads(self.config.read_text(encoding="utf-8"))
            self.luz["roi"] = dados.get("luz")
            if "mudancas" in dados:
                self.luz.update(estado="pronto", progresso=1.0, mudancas=dados["mudancas"])

    @property
    def fps(self) -> float:
        return 1.0 / float(np.median(np.diff(self.t)))

    def quadro_jpeg(self, k: int) -> bytes:
        k = min(self.info.n_quadros - 1, max(0, k))
        with self.trava:
            if k in self.jpegs:
                self.jpegs.move_to_end(k)
                return self.jpegs[k]
            ok, dados = cv2.imencode(".jpg", self.leitor.quadro(k), [cv2.IMWRITE_JPEG_QUALITY, 90])
            self.jpegs[k] = dados.tobytes()
            while len(self.jpegs) > 600:
                self.jpegs.popitem(last=False)
            return self.jpegs[k]

    def registros(self) -> list[dict]:
        if not self.arquivo.exists():
            return []
        with self.arquivo.open(encoding="utf-8-sig", newline="") as f:
            return [{"q0": int(l["quadro_inicio"]), "q1": int(l["quadro_lampada"]),
                     "tipo": l.get("comando") or "ligar"}
                    for l in csv.DictReader(f, delimiter=";")]

    def salvar(self, registros: list[dict]) -> None:
        linhas = []
        for n, r in enumerate(sorted(registros, key=lambda r: r["q0"]), 1):
            q0, q1 = int(r["q0"]), int(r["q1"])
            comando = r.get("tipo", "ligar")
            if comando not in COMANDOS:
                raise ValueError(f"comando inválido: {comando}")
            t0, t1 = float(self.t[q0] * 1000), float(self.t[q1] * 1000)
            linhas.append({"video": self.caminho.name, "acionamento": str(n), "comando": comando,
                           "quadro_inicio": str(q0), "t_inicio_ms": L.formatar(t0, 1, True),
                           "quadro_lampada": str(q1), "t_lampada_ms": L.formatar(t1, 1, True),
                           "quadros": str(q1 - q0), "latencia_ms": L.formatar(t1 - t0, 1, True)})
        L.escrever_csv(self.arquivo, CAMPOS, linhas, True)

    def limpar(self, o_que: str) -> None:
        """Apaga a marcação da lâmpada ("lampada"), os acionamentos ("marcacoes") ou os dois ("tudo")."""
        if o_que not in ("lampada", "marcacoes", "tudo"):
            raise ValueError(f"não sei limpar: {o_que}")
        if o_que in ("lampada", "tudo"):
            if self.luz["estado"] == "medindo":
                raise ValueError("espere a medição da lâmpada terminar")
            self.config.unlink(missing_ok=True)
            self.luz = {"estado": "nenhuma", "progresso": 0.0, "roi": None, "mudancas": []}
        if o_que in ("marcacoes", "tudo"):
            self.arquivo.unlink(missing_ok=True)

    def medir_lampada(self, roi: list[int]) -> None:
        """Roda em segundo plano: mede a região da lâmpada em todos os quadros."""
        self.luz.update(estado="medindo", progresso=0.0, roi=roi, mudancas=[], erro=None)
        try:
            cont, fluxo = L.abrir(self.caminho)
            valores = []
            for i, q in enumerate(cont.decode(fluxo)):
                img = L.girar(q.to_ndarray(format="bgr24"), self.k_rot)
                valores.append(L.medir(L.recortar(img, roi)))
                if i % 120 == 0:
                    self.luz["progresso"] = i / self.info.n_quadros
            cont.close()
            s = L.separar_dois_estados(np.asarray(valores, dtype=np.float64), "lampada")
            if not s.ativo:
                raise ValueError("a região marcada não mostra a lâmpada acendendo e apagando")
            lacuna = max(1, math.ceil(L.LACUNA_LUZ_S * self.fps))
            mudancas = [[int(q), int(d)] for q, d in L.transicoes(s, lacuna, L.QUADROS_MINIMOS, 0.5)]
            self.config.write_text(json.dumps({"luz": roi, "rot90_k": self.k_rot, "mudancas": mudancas}),
                                   encoding="utf-8")
            self.luz.update(estado="pronto", progresso=1.0, mudancas=mudancas)
        except Exception as erro:  # noqa: BLE001 - o erro vai para a página
            self.luz.update(estado="erro", erro=str(erro))


class Servidor(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, endereco, pasta: Path):
        super().__init__(endereco, Manipulador)
        self.pasta = pasta
        self.sessoes: dict[str, Sessao] = {}
        self.trava = threading.Lock()

    def videos(self) -> dict[str, Path]:
        return {p.relative_to(self.pasta).as_posix(): p for p in sorted(self.pasta.rglob("*.mp4"))}

    def sessao(self, nome: str) -> Sessao:
        caminho = self.videos().get(nome)
        if caminho is None:
            raise KeyError(nome)
        with self.trava:
            if nome not in self.sessoes:
                self.sessoes[nome] = Sessao(caminho)
            return self.sessoes[nome]


class Manipulador(BaseHTTPRequestHandler):
    server: Servidor

    def log_message(self, *args) -> None:
        pass

    def responder(self, corpo: bytes, tipo: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def json(self, dados, status: int = 200) -> None:
        self.responder(json.dumps(dados, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self.responder(PAGINA.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/videos":
                lista = []
                for nome, p in self.server.videos().items():
                    csv_lat = p.parent / f"{p.stem}_latencias.csv"
                    n = sum(1 for _ in csv_lat.open(encoding="utf-8-sig")) - 1 if csv_lat.exists() else 0
                    lista.append({"nome": nome, "mb": round(p.stat().st_size / 2**20), "marcados": max(0, n)})
                self.json({"pasta": str(self.server.pasta), "videos": lista})
            elif url.path == "/api/abrir":
                s = self.server.sessao(q["v"])
                self.json({"nome": q["v"], "n": s.info.n_quadros, "fps": round(s.fps, 2),
                           "modo_tempo": s.modo_tempo, "largura": s.info.altura if s.k_rot % 2 else s.info.largura,
                           "altura": s.info.largura if s.k_rot % 2 else s.info.altura,
                           "tempos": [round(float(x) * 1000, 3) for x in s.t],
                           "registros": s.registros(), "arquivo": str(s.arquivo), "luz": s.luz})
            elif url.path == "/api/quadro":
                self.responder(self.server.sessao(q["v"]).quadro_jpeg(int(q["i"])), "image/jpeg")
            elif url.path == "/api/lampada":
                self.json(self.server.sessao(q["v"]).luz)
            else:
                self.json({"erro": "não encontrado"}, 404)
        except KeyError:
            self.json({"erro": "vídeo não encontrado"}, 404)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        tamanho = int(self.headers.get("Content-Length", 0))
        corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        try:
            if url.path == "/api/salvar":
                s = self.server.sessao(q["v"])
                s.salvar(corpo["registros"])
                self.json({"arquivo": str(s.arquivo)})
            elif url.path == "/api/lampada":
                s = self.server.sessao(q["v"])
                if s.luz["estado"] != "medindo":
                    roi = [int(v) for v in corpo["roi"]]
                    threading.Thread(target=s.medir_lampada, args=(roi,), daemon=True).start()
                self.json({"estado": "medindo"})
            elif url.path == "/api/limpar":
                s = self.server.sessao(q["v"])
                s.limpar(corpo.get("o_que", ""))
                self.json({"luz": s.luz, "registros": s.registros()})
            elif url.path == "/api/exportar":
                self.json(exportar(self.server.pasta))
            else:
                self.json({"erro": "não encontrado"}, 404)
        except KeyError:
            self.json({"erro": "vídeo não encontrado"}, 404)
        except ValueError as erro:
            self.json({"erro": str(erro)}, 400)


def exportar(pasta: Path) -> dict:
    """Junta os *_latencias.csv da pasta numa planilha só."""
    linhas = []
    for arquivo in sorted(pasta.rglob("*_latencias.csv")):
        with arquivo.open(encoding="utf-8-sig", newline="") as f:
            linhas += list(csv.DictReader(f, delimiter=";"))
    destino = pasta / "latencias_todos.csv"
    L.escrever_csv(destino, CAMPOS, linhas, True)
    return {"arquivo": str(destino), "linhas": len(linhas)}


def main() -> None:
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pasta", default=".",
                    help="pasta com os vídeos (padrão: a pasta atual; procura também nas subpastas)")
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--sem-navegador", action="store_true", help="não abre o navegador sozinho")
    a = ap.parse_args()
    pasta = Path(a.pasta).resolve()
    if not pasta.is_dir():
        raise SystemExit(f"Pasta não encontrada: {pasta}")
    servidor = Servidor(("127.0.0.1", a.porta), pasta)
    url = f"http://127.0.0.1:{a.porta}/"
    print(f"Vídeos de: {pasta}\nInterface em: {url}\nCtrl+C encerra.")
    if not a.sem_navegador:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
