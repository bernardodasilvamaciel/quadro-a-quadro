#!/usr/bin/env python3
"""Validação do instrumento: gera vídeos sintéticos com T0 e T1 conhecidos e confere se
latencia.py os encontra dentro de um quadro.

A cena imita um teste filmado: a tela do celular com os botões do app dos dois interruptores e,
para cada um, uma lâmpada LED com rampa de acendimento e cintilação de 120 Hz. O botão muda
de cor com uma animação de 25 ms, o dedo cobre a parte de baixo do botão, a tela tem a
faixa do PWM do OLED e fica mais clara quando uma lâmpada acende (reflexo). Há vídeos com
um teste só, um caso sem resposta, um caso em que a lâmpada acende antes de o botão mudar e
um vídeo longo com vários testes seguidos, alternando os interruptores.
Também gera uma cópia "reescrita a 30 fps", como alguns celulares fazem no slow motion,
para testar a opção --fps-captura.

Uso:  python validar_sintetico.py [--pasta teste_sintetico]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import av
import cv2
import numpy as np

import latencia as L

FPS = 240
LARG, ALT = 640, 360
BOTAO = {"wifi": (50, 90), "zigbee": (130, 90)}      # canto superior esquerdo, 60 x 60 pixels
X_LAMPADA = {"wifi": 300, "zigbee": 480}
ANIMACAO_S = 0.025      # troca de cor do botão
RAMPA_S = 0.012         # subida do brilho da lâmpada ao ligar
TAU_S = 0.030           # decaimento ao desligar
NAN = math.nan

REGIOES = {
    "versao": 2, "rot90_k": 0, "largura": LARG, "altura": ALT,
    "dispositivos": {
        d: {"botao": [x + 5, y + 2, 50, 20], "lampada": [X_LAMPADA[d] - 30, 190, 60, 60]}
        for d, (x, y) in BOTAO.items()
    },
}


@dataclass
class Teste:
    dispositivo: str
    comando: str
    botao: float        # instante em que o botão começa a mudar (s)
    lampada: float      # instante em que a lâmpada começa a mudar (s); nan = não responde


VIDEOS = {
    "teste_01_wifi.mp4": [Teste("wifi", "ligar", 1.0021, 1.1873)],
    "teste_02_zigbee.mp4": [Teste("zigbee", "ligar", 0.9512, 1.2147)],
    "teste_03_sem_resposta.mp4": [Teste("wifi", "ligar", 1.0103, NAN)],
    "teste_04_varios.mp4": [
        Teste("wifi", "ligar", 1.0037, 1.1561),
        Teste("zigbee", "ligar", 2.5019, 2.8034),
        Teste("wifi", "desligar", 4.0082, 4.1290),
        Teste("zigbee", "desligar", 5.5003, 5.7121),
        Teste("wifi", "ligar", 7.0611, 7.0018),        # a lâmpada acende antes de o botão mudar
        Teste("zigbee", "ligar", 8.5047, 8.7466),
    ],
}
REESCRITO = "teste_04_varios.mp4"     # também gravado "reescrito a 30 fps"


def sequencia(n: int, semente: int = 3) -> list[Teste]:
    """Um vídeo por distância: n testes alternando Wi-Fi e Zigbee, cada um ligando e desligando."""
    rng = np.random.default_rng(semente)
    testes = []
    for k in range(n):
        d = ("wifi", "zigbee")[k % 2]
        lat = {"wifi": (0.10, 0.30), "zigbee": (0.18, 0.40)}[d]
        t0 = 1.0 + 3.0 * k + rng.uniform(0, 0.2)
        testes.append(Teste(d, "ligar", t0, t0 + rng.uniform(*lat)))
        t1 = t0 + 1.5 + rng.uniform(0, 0.2)
        testes.append(Teste(d, "desligar", t1, t1 + rng.uniform(*lat)))
    return testes


VIDEOS["teste_05_sequencia.mp4"] = sequencia(12)


def fracao_botao(t: float, testes: list[Teste], d: str) -> float:
    """0 = desligado, 1 = ligado, com a animação de troca de cor."""
    f = 0.0
    for x in testes:
        if x.dispositivo == d and t >= x.botao:
            k = min(1.0, (t - x.botao) / ANIMACAO_S)
            f = k if x.comando == "ligar" else 1.0 - k
    return f


def brilho_lampada(t: float, testes: list[Teste], d: str) -> float:
    b = 0.0
    for x in sorted(testes, key=lambda x: x.lampada if not math.isnan(x.lampada) else math.inf):
        if x.dispositivo == d and not math.isnan(x.lampada) and t >= x.lampada:
            b = min(1.0, (t - x.lampada) / RAMPA_S) if x.comando == "ligar" else math.exp(-(t - x.lampada) / TAU_S)
    return b


def quadro_esperado(f, inicio: float, comando: str) -> int:
    """Primeiro quadro que já percorreu INICIO_MINIMO do caminho entre os dois estados."""
    i = math.ceil(inicio * FPS)
    while not (f(i / FPS) >= L.INICIO_MINIMO if comando == "ligar" else f(i / FPS) <= 1 - L.INICIO_MINIMO):
        i += 1
    return i


def desenhar_cena(t: float, testes: list[Teste], ruido: np.ndarray) -> np.ndarray:
    img = np.full((ALT, LARG, 3), 40, np.float32)
    img[30:331, 30:211] = 235                                                # tela do celular
    acesas = 0.0
    for d, (x, y) in BOTAO.items():
        f = fracao_botao(t, testes, d)
        img[y:y + 60, x:x + 60] = [180 + 50 * f, 180 - 40 * f, 180 - 140 * f]
        b = brilho_lampada(t, testes, d)
        acesas += b
        b *= 1 + 0.08 * math.sin(2 * math.pi * 120 * t)
        cor = [c0 + (c1 - c0) * b for c0, c1 in zip((55, 55, 60), (200, 235, 255))]
        cv2.circle(img, (X_LAMPADA[d], 220), 28, cor, -1)
    for x in testes:                                                         # dedo
        if x.botao - 0.08 <= t < x.botao + 0.10:
            bx, by = BOTAO[x.dispositivo]
            cv2.circle(img, (bx + 30, by + 62), 22, (120, 160, 220), -1)
    tela = img[30:331, 30:211]
    tela *= 1 + 0.04 * acesas                                                # reflexo da lâmpada
    faixa = int((t * 900) % 300)                                             # PWM da tela OLED
    tela[faixa:faixa + 40] *= 0.88
    return np.clip(img + ruido, 0, 255).astype(np.uint8)


def gravar(pasta: Path, nome: str, testes: list[Teste], rng: np.random.Generator) -> list[Path]:
    fim = max(max(x.botao, 0 if math.isnan(x.lampada) else x.lampada) for x in testes) + 1.0
    ruidos = rng.normal(0, 2.5, (16, ALT, LARG, 3)).astype(np.float32)
    caminhos = [pasta / nome]
    if nome == REESCRITO:
        caminhos.append(pasta / nome.replace(".mp4", "_reescrito_30.mp4"))
    saidas = []
    for caminho, taxa in zip(caminhos, (FPS, 30)):
        cont = av.open(str(caminho), "w")
        fluxo = cont.add_stream("libx264", rate=taxa)
        fluxo.width, fluxo.height, fluxo.pix_fmt = LARG, ALT, "yuv420p"
        fluxo.options = {"crf": "12", "preset": "veryfast", "g": "240"}
        saidas.append((cont, fluxo))
    for i in range(int(fim * FPS)):
        img = desenhar_cena(i / FPS, testes, ruidos[i % 16])
        for cont, fluxo in saidas:
            quadro = av.VideoFrame.from_ndarray(img, format="bgr24")
            quadro.pts = i
            for pacote in fluxo.encode(quadro):
                cont.mux(pacote)
    for cont, fluxo in saidas:
        for pacote in fluxo.encode():
            cont.mux(pacote)
        cont.close()
    return caminhos


def esperados(testes: list[Teste]) -> list[tuple[str, str, str, int, int | None]]:
    lista = []
    for x in testes:
        q0 = quadro_esperado(lambda s: fracao_botao(s, testes, x.dispositivo), x.botao, x.comando)
        q1 = None
        if not math.isnan(x.lampada):
            q1 = quadro_esperado(lambda s: brilho_lampada(s, testes, x.dispositivo), x.lampada, x.comando)
        situacao = "sem_resposta" if q1 is None else "lampada_antes_do_botao" if q1 < q0 else "ok"
        lista.append((x.dispositivo, x.comando, situacao, q0, q1))
    return lista


def conferir(res: L.Resultado, testes: list[Teste], titulo: str) -> bool:
    print(f"\n=== {titulo} ===")
    print(f"tempo por: {res.modo_tempo}")
    certos = esperados(testes)
    if len(res.eventos) != len(certos):
        print(f"FALHA: {len(certos)} eventos esperados, {len(res.eventos)} detectados:")
        for ev in res.eventos:
            print(f"   {ev.dispositivo} {ev.comando} {ev.situacao} T0={ev.quadro_t0} T1={ev.quadro_t1}")
        return False
    print(f"{'n':>2} {'interruptor':<11} {'comando':<9} {'situacao':<24} {'T0 real/medido':>15} "
          f"{'T1 real/medido':>15} {'latência medida':>16}")
    ok = True
    for ev, (d, comando, situacao, q0, q1) in zip(res.eventos, certos):
        linha_ok = (ev.dispositivo, ev.comando, ev.situacao) == (d, comando, situacao)
        linha_ok &= ev.quadro_t0 is not None and abs(ev.quadro_t0 - q0) <= 1
        if q1 is None:
            linha_ok &= ev.quadro_t1 is None
        else:
            linha_ok &= ev.quadro_t1 is not None and abs(ev.quadro_t1 - q1) <= 1
        ok &= linha_ok
        lat = "-" if ev.lat_ms is None else f"{ev.lat_ms:.1f} ms"
        print(f"{ev.n:>2} {ev.dispositivo:<11} {ev.comando:<9} {ev.situacao:<24} {q0:>6} / {ev.quadro_t0!s:<6}"
              f" {q1!s:>6} / {ev.quadro_t1!s:<6} {lat:>16}{'' if linha_ok else '   <-- ERRO'}")
    print("RESULTADO:", "OK" if ok else "FALHOU")
    return ok


def main() -> None:
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pasta", default="teste_sintetico")
    ap.add_argument("--semente", type=int, default=7)
    a = ap.parse_args()

    pasta = Path(a.pasta)
    if pasta.exists():
        shutil.rmtree(pasta)
    pasta.mkdir(parents=True)
    (pasta / "regioes.json").write_text(json.dumps(REGIOES, indent=2), encoding="utf-8")
    regioes = L.carregar_regioes(pasta / "regioes.json")
    rng = np.random.default_rng(a.semente)

    ok = True
    for nome, testes in VIDEOS.items():
        print(f"\nGerando {nome}...")
        caminhos = gravar(pasta, nome, testes, rng)
        res = L.analisar_video(caminhos[0], regioes, mostrar=False)
        L.salvar_resultado(res, pasta / f"{caminhos[0].stem}_analise", {"teste": "sintetico"}, None, False)
        ok &= conferir(res, testes, f"{nome} (240 fps)")
        if len(caminhos) > 1:
            res30 = L.analisar_video(caminhos[1], regioes, fps_captura=FPS, mostrar=False)
            ok &= conferir(res30, testes, f"{caminhos[1].name} com --fps-captura {FPS}")
            sem_fps = L.analisar_video(caminhos[1], regioes, mostrar=False)
            avisou = any("--fps-captura" in aviso for aviso in sem_fps.avisos)
            print(f"\n{caminhos[1].name} sem --fps-captura: aviso emitido = {avisou}")
            ok &= avisou

    print("\nVALIDAÇÃO", "OK" if ok else "FALHOU")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
