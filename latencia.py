#!/usr/bin/env python3
"""Medição automática de latência em vídeos de câmera lenta (experimental).

Encontra sozinho os dois quadros e calcula a latência:
  T0 = primeiro quadro em que o botão virtual do app muda (cor ou animação)
  T1 = primeiro quadro em que a lâmpada LED mostra luz
  Latência = (número de quadros entre T0 e T1 / taxa de captura) x 1000

Subcomandos:
  info      mostra a taxa de quadros real e os metadados de tempo do vídeo
  marcar    abre o vídeo para marcar as regiões (botão do app e lâmpada de cada interruptor)
  analisar  encontra T0 e T1 e gera CSV, gráfico e imagens de conferência
  ver       navega quadro a quadro mostrando o tempo exato

O passo a passo está em docs/modo-automatico.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path

import av
import cv2
import numpy as np

TIPOS_REGIAO = ("botao", "lampada")
DICAS = {"botao": "um pedaco do botao do app que o dedo nao cobre",
         "lampada": "justo em volta do bulbo da lampada"}

# Abaixo desta separação (em desvios do ruído) a região é tratada como "sem mudança".
SEPARACAO_MINIMA = 4.0
# Diferença mínima entre os dois estados, para variações lentas da luz do ambiente não virarem
# mudança: na lâmpada, em níveis de brilho (0 a 255); no botão, em distância de cor relativa
# (cinza para azul dá uns 0,3).
AMPLITUDE_MINIMA = {"lampada": 15.0, "botao": 0.02}
# Apagões de até 25 ms dentro de um trecho aceso são cintilação da rede de 60 Hz, não desligamento.
LACUNA_LUZ_S = 0.025
# O botão fica no mesmo estado por segundos; mudanças de até 50 ms são ruído da tela.
LACUNA_BOTAO_S = 0.05
# Trechos acesos mais curtos que isto são ruído.
QUADROS_MINIMOS = 2
# O início de uma mudança é o primeiro quadro que já percorreu 15% do caminho entre os dois
# estados (ou mais, se o ruído da região for maior).
INICIO_MINIMO = 0.15

PESOS_CINZA = np.array([0.114, 0.587, 0.299], dtype=np.float32)  # ordem BGR


# ---------------------------------------------------------------------------
# Leitura do vídeo e tempo de cada quadro
# ---------------------------------------------------------------------------

@dataclass
class InfoVideo:
    caminho: Path
    pts_brutos: list[int]
    base_tempo: float
    rotacao: int          # graus anti-horários indicados pela matriz de exibição
    largura: int
    altura: int
    metadados: dict[str, str]

    @property
    def n_quadros(self) -> int:
        return len(self.pts_brutos)

    @property
    def pts_s(self) -> np.ndarray:
        return np.asarray(self.pts_brutos, dtype=np.float64) * self.base_tempo

    @property
    def fps_carimbos(self) -> float:
        intervalos = np.diff(self.pts_s)
        if intervalos.size == 0 or np.median(intervalos) <= 0:
            return 0.0
        return 1.0 / float(np.median(intervalos))

    @property
    def fps_captura_tag(self) -> float | None:
        """Valor de com.android.capture.fps, que alguns celulares gravam no slow motion."""
        for chave, valor in self.metadados.items():
            if "capture.fps" in chave:
                try:
                    return float(valor)
                except ValueError:
                    pass
        return None


def abrir(caminho: Path):
    cont = av.open(str(caminho))
    fluxo = cont.streams.video[0]
    fluxo.thread_type = "AUTO"
    return cont, fluxo


def coletar_metadados(cont, fluxo) -> dict[str, str]:
    meta = {f"arquivo:{k}": str(v) for k, v in cont.metadata.items()}
    meta.update({f"fluxo:{k}": str(v) for k, v in fluxo.metadata.items()})
    return meta


def pts_do_quadro(quadro, anteriores: list[int]) -> int:
    if quadro.pts is not None:
        return int(quadro.pts)
    return anteriores[-1] + 1 if anteriores else 0


def girar(img: np.ndarray, k: int) -> np.ndarray:
    """Gira a imagem k x 90 graus no sentido anti-horário."""
    return np.ascontiguousarray(np.rot90(img, k)) if k % 4 else img


def k_da_rotacao(graus_anti_horarios: int) -> int:
    return int(round(graus_anti_horarios / 90)) % 4


def k_do_argumento(graus_horarios: int | None, info: InfoVideo) -> int:
    if graus_horarios is None:
        return k_da_rotacao(info.rotacao)
    return (-int(graus_horarios) // 90) % 4


def ler_info(caminho: Path) -> InfoVideo:
    """Lista o carimbo de tempo de cada quadro.

    Lê só os pacotes do arquivo, sem decodificar a imagem: num vídeo de minutos a 240 fps isso
    leva segundos em vez de minutos. Se algum pacote vier sem carimbo, decodifica tudo.
    """
    if not caminho.exists():
        raise SystemExit(f"Vídeo não encontrado: {caminho}")
    cont, fluxo = abrir(caminho)
    primeiro = next(cont.decode(fluxo), None)
    if primeiro is None:
        raise SystemExit(f"O vídeo {caminho.name} não tem quadros.")
    rotacao = int(getattr(primeiro, "rotation", 0) or 0)
    largura, altura, base = primeiro.width, primeiro.height, float(fluxo.time_base)
    metadados = coletar_metadados(cont, fluxo)
    cont.close()

    cont, fluxo = abrir(caminho)
    pacotes = [p.pts for p in cont.demux(fluxo) if p.size > 0]
    cont.close()
    if pacotes and None not in pacotes:
        pts = sorted(int(p) for p in pacotes)
    else:
        cont, fluxo = abrir(caminho)
        pts = []
        for quadro in cont.decode(fluxo):
            pts.append(pts_do_quadro(quadro, pts))
        cont.close()
    info = InfoVideo(caminho, pts, base, rotacao, largura, altura, metadados)
    if info.n_quadros < 2:
        raise SystemExit(f"O vídeo {caminho.name} tem menos de 2 quadros.")
    return info


def calcular_tempos(info: InfoVideo, fps_captura: float | None = None) -> tuple[np.ndarray, str]:
    """Tempo de cada quadro em segundos (o primeiro quadro é zero) e como ele foi obtido."""
    indices = np.arange(info.n_quadros, dtype=np.float64)
    if fps_captura:
        return indices / fps_captura, f"índice do quadro / {fps_captura:g} fps (informado em --fps-captura)"
    tag = info.fps_captura_tag
    if tag and abs(info.fps_carimbos - tag) / tag > 0.10:
        return indices / tag, f"índice do quadro / {tag:g} fps (metadado de captura do celular)"
    pts = info.pts_s
    return pts - pts[0], "carimbo de tempo (PTS) gravado em cada quadro"


def diagnostico(info: InfoVideo) -> tuple[list[str], list[str]]:
    pts = info.pts_s
    intervalos = np.diff(pts) * 1000
    mediana = float(np.median(intervalos))
    fps = info.fps_carimbos
    tag = info.fps_captura_tag
    linhas = [
        f"Arquivo: {info.caminho.name}",
        f"Resolução: {info.largura}x{info.altura} (rotação de exibição: {info.rotacao} graus)",
        f"Quadros: {info.n_quadros}   duração pelos carimbos de tempo: {pts[-1] - pts[0]:.3f} s",
        f"Taxa pelos carimbos (1 / mediana do intervalo): {fps:.2f} fps",
        f"Intervalo entre quadros: mín {intervalos.min():.3f} ms | mediana {mediana:.3f} ms"
        f" | máx {intervalos.max():.3f} ms",
    ]
    if tag:
        linhas.append(f"Metadado com.android.capture.fps: {tag:g}")
    avisos = []
    irregulares = float(np.mean(np.abs(intervalos - mediana) > 0.2 * mediana))
    if irregulares > 0.01:
        avisos.append(f"{irregulares:.1%} dos intervalos fogem mais de 20% da mediana: taxa variável "
                      "ou quadros perdidos. Confira filmando um cronômetro.")
    if fps < 100 and not tag:
        avisos.append(f"O arquivo declara ~{fps:.0f} fps. Se ele foi gravado em câmera lenta, o celular "
                      "reescreveu os tempos para a reprodução lenta. Use --fps-captura com a taxa da "
                      "câmera lenta (ex.: 240 ou 120) e confira filmando um cronômetro.")
    if tag and abs(fps - tag) / tag > 0.10:
        avisos.append(f"A taxa dos carimbos ({fps:.0f} fps) difere da taxa de captura ({tag:g} fps). "
                      f"O tempo será calculado por índice / {tag:g}.")
    return linhas, avisos


# ---------------------------------------------------------------------------
# Regiões de interesse
# ---------------------------------------------------------------------------

def carregar_regioes(caminho: Path) -> dict:
    if not caminho.exists():
        raise SystemExit(f"Arquivo de regiões não encontrado: {caminho}\n"
                         "Rode antes: python latencia.py marcar <video>")
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    dispositivos = dados.setdefault("dispositivos", {})
    if "ponteiro" in dados or any("sinaleiro" in tipos for tipos in dispositivos.values()):
        raise SystemExit(f"{caminho} é do formato antigo (ponteiro e sinaleiro). "
                         "Marque as regiões de novo com o comando marcar.")
    if not any(all(tp in tipos for tp in TIPOS_REGIAO) for tipos in dispositivos.values()):
        raise SystemExit(f"{caminho} não tem nenhum interruptor com o botão e a lâmpada marcados.")
    return dados


def achatar_regioes(dados: dict) -> dict[str, tuple[int, int, int, int]]:
    """{"wifi/botao": r, "wifi/lampada": r, ...} com r = (x, y, largura, altura)."""
    planas = {}
    for dispositivo, tipos in dados["dispositivos"].items():
        if all(tp in tipos for tp in TIPOS_REGIAO):
            for tipo in TIPOS_REGIAO:
                planas[f"{dispositivo}/{tipo}"] = tuple(tipos[tipo])
    return planas


def recortar(img: np.ndarray, r) -> np.ndarray:
    x, y, w, h = r
    alt, larg = img.shape[:2]
    return img[max(0, y):min(alt, y + h), max(0, x):min(larg, x + w)]


def medir(recorte: np.ndarray) -> tuple[float, float, float, float]:
    """Média de B, G, R e desvio do cinza."""
    px = recorte.reshape(-1, 3).astype(np.float32)
    if px.size == 0:
        return 0.0, 0.0, 0.0, 0.0
    media = px.mean(axis=0)
    return float(media[0]), float(media[1]), float(media[2]), float((px @ PESOS_CINZA).std())


def extrair_medidas(video: Path, regioes: dict[str, tuple], k_rot: int, mostrar: bool = True):
    """Lê o vídeo uma vez e mede cada região em cada quadro."""
    if not video.exists():
        raise SystemExit(f"Vídeo não encontrado: {video}")
    cont, fluxo = abrir(video)
    pts: list[int] = []
    medidas: dict[str, list] = {nome: [] for nome in regioes}
    rotacao, largura, altura, dimensoes = 0, 0, 0, None
    for i, quadro in enumerate(cont.decode(fluxo)):
        if i == 0:
            rotacao = int(getattr(quadro, "rotation", 0) or 0)
            largura, altura = quadro.width, quadro.height
        img = girar(quadro.to_ndarray(format="bgr24"), k_rot)
        if dimensoes is None:
            dimensoes = (img.shape[1], img.shape[0])
        pts.append(pts_do_quadro(quadro, pts))
        for nome, r in regioes.items():
            medidas[nome].append(medir(recortar(img, r)))
        if mostrar and i % 600 == 0:
            print(f"\r  {i} quadros lidos", end="", flush=True)
    if mostrar:
        print(f"\r  {len(pts)} quadros lidos")
    info = InfoVideo(video, pts, float(fluxo.time_base), rotacao, largura, altura,
                     coletar_metadados(cont, fluxo))
    cont.close()
    if info.n_quadros < 2:
        raise SystemExit(f"O vídeo {video.name} tem menos de 2 quadros.")
    return info, {nome: np.asarray(v, dtype=np.float64) for nome, v in medidas.items()}, dimensoes


# ---------------------------------------------------------------------------
# De medidas a estados (desligado/ligado, apagada/acesa)
# ---------------------------------------------------------------------------

def ruido_dos_estados(p: np.ndarray) -> tuple[float, float]:
    """Desvio robusto (MAD) de p no estado de base e no ativo."""
    def mad(v):
        return 1.4826 * float(np.median(np.abs(v - np.median(v)))) if v.size else 1.0
    return mad(p[p <= 0.5]), mad(p[p > 0.5])


@dataclass
class Sinal:
    nome: str
    tipo: str
    p: np.ndarray | None                 # 0 = estado de base, 1 = estado ativo
    ruido: tuple[float, float] = (1.0, 1.0)
    amplitude: float = 0.0               # diferença entre os estados na unidade de AMPLITUDE_MINIMA

    @property
    def separacao(self) -> float:
        """Distância entre os estados, em desvios do ruído do estado de base."""
        return 1.0 / max(self.ruido[0], 1e-3)

    @property
    def ativo(self) -> bool:
        return (self.p is not None and self.separacao >= SEPARACAO_MINIMA
                and self.amplitude >= AMPLITUDE_MINIMA[self.tipo])

    def inverter(self) -> None:
        self.p = 1.0 - self.p
        self.ruido = ruido_dos_estados(self.p)


def caracteristicas(X: np.ndarray, tipo: str) -> np.ndarray:
    """Medidas usadas para separar os estados de cada tipo de região.

    No botão, usa só a cor relativa (cada canal dividido pela soma), que não muda quando o
    brilho varia: faixas do PWM da tela OLED, reflexo da lâmpada, exposição da câmera.
    Na lâmpada, a própria intensidade é o que interessa.
    """
    if tipo == "lampada":
        return X
    return X[:, :3] / (X[:, :3].sum(axis=1, keepdims=True) + 1e-6)


def otsu(v: np.ndarray) -> float:
    lo, hi = np.percentile(v, [0.1, 99.9])
    v = np.clip(v, lo, hi)
    hist, bordas = np.histogram(v, bins=256)
    centros = (bordas[:-1] + bordas[1:]) / 2
    w0 = np.cumsum(hist).astype(np.float64)
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * centros)
    m0 = s0 / np.maximum(w0, 1)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1)
    entre = w0 * w1 * (m0 - m1) ** 2
    return float(bordas[int(np.argmax(entre[:-1])) + 1])


def separar_dois_estados(X: np.ndarray, tipo: str) -> Sinal:
    """Projeta as medidas da região num eixo em que 0 = estado de base e 1 = estado ativo.

    Agrupa os quadros em dois estados (limiar de Otsu na componente principal), refina a
    direção que melhor separa os grupos e normaliza pelas medianas de cada grupo. Assim o
    limiar de 0,5 significa "meio caminho entre apagada e acesa", seja qual for a cor.
    """
    vazio = Sinal("", tipo, None)
    F = caracteristicas(X, tipo)
    desvio = F.std(axis=0)
    desvio[desvio < 1e-9] = 1.0
    Z = (F - F.mean(axis=0)) / desvio
    _, _, vt = np.linalg.svd(Z, full_matrices=False)
    grupo = (Z @ vt[0]) > otsu(Z @ vt[0])
    p = None
    for _ in range(10):
        if grupo.all() or not grupo.any():
            return vazio
        c0, c1 = Z[~grupo].mean(axis=0), Z[grupo].mean(axis=0)
        var = (Z[~grupo].var(axis=0) * (~grupo).sum() + Z[grupo].var(axis=0) * grupo.sum()) / len(Z)
        q = Z @ ((c1 - c0) / (var + 1e-6))
        m0, m1 = np.median(q[~grupo]), np.median(q[grupo])
        if m1 == m0:
            return vazio
        p = (q - m0) / (m1 - m0)
        novo = p > 0.5
        if np.array_equal(novo, grupo):
            break
        grupo = novo
    if p is None or grupo.all() or not grupo.any():
        return vazio

    # Polaridade: na lâmpada, o estado ativo é o mais claro. No botão, por enquanto, é o oposto
    # do estado do começo do vídeo; analisar_video confere com a lâmpada do mesmo interruptor.
    if tipo == "lampada":
        brilho = X[:, :3].mean(axis=1)
        inverter = brilho[grupo].mean() < brilho[~grupo].mean()
        amplitude = abs(float(np.median(brilho[grupo]) - np.median(brilho[~grupo])))
    else:
        inverter = np.median(p[:12]) > 0.5
        amplitude = float(np.linalg.norm(np.median(F[grupo], axis=0) - np.median(F[~grupo], axis=0)))
    if inverter:
        p = 1.0 - p
    return Sinal("", tipo, p, ruido_dos_estados(p), amplitude)


def mediana3(p: np.ndarray) -> np.ndarray:
    """Filtro de mediana de 3 quadros: remove picos isolados sem deslocar as bordas."""
    if p.size < 3:
        return p
    return np.median(np.stack([np.r_[p[0], p[:-1]], p, np.r_[p[1:], p[-1]]]), axis=0)


def segmentos(ativo: np.ndarray, lacuna: int, minimo: int = QUADROS_MINIMOS) -> list[tuple[int, int]]:
    """Trechos (primeiro, último quadro) ativos, juntando interrupções de até `lacuna` quadros."""
    idx = np.flatnonzero(ativo)
    if idx.size == 0:
        return []
    quebras = np.flatnonzero(np.diff(idx) > lacuna + 1)
    inicios = np.r_[idx[0], idx[quebras + 1]]
    fins = np.r_[idx[quebras], idx[-1]]
    return [(int(a), int(b)) for a, b in zip(inicios, fins) if b - a + 1 >= minimo]


def recuar(p: np.ndarray, q: int, limite: int, direcao: int, inicio: float) -> int:
    """Volta do quadro em que o sinal cruzou 0,5 até o primeiro quadro em que ele já tinha
    saído do estado anterior (mais de `inicio` do caminho), sem passar de `limite`."""
    while q - 1 > limite and (p[q - 1] > inicio if direcao > 0 else p[q - 1] < 1 - inicio):
        q -= 1
    return q


def transicoes(s: Sinal, lacuna: int, minimo: int, limiar: float) -> list[tuple[int, int]]:
    """(primeiro quadro da mudança, +1 = para o estado ativo, -1 = de volta à base)."""
    p = mediana3(s.p) if s.tipo == "botao" else s.p
    n = len(p)
    inicio_subida = min(limiar, max(INICIO_MINIMO, 4 * s.ruido[0]))
    inicio_descida = min(limiar, max(INICIO_MINIMO, 4 * s.ruido[1]))
    lista = []
    trechos = segmentos(p > limiar, lacuna, minimo)
    for i, (ini, fim) in enumerate(trechos):
        if ini > 0:
            anterior = trechos[i - 1][1] if i > 0 else -1
            lista.append((recuar(s.p, ini, anterior, +1, inicio_subida), +1))
        if fim + 1 < n:
            lista.append((recuar(s.p, fim + 1, ini, -1, inicio_descida), -1))
    return lista


# ---------------------------------------------------------------------------
# Eventos: cada mudança do botão (T0) com a mudança da lâmpada que veio com ela (T1)
# ---------------------------------------------------------------------------

@dataclass
class Evento:
    dispositivo: str
    comando: str                     # ligar ou desligar, pelo sentido da mudança
    situacao: str = "ok"
    quadro_t0: int | None = None     # primeiro quadro com o botão do app mudando
    quadro_t1: int | None = None     # primeiro quadro com a lâmpada mudando
    lat_ms: float | None = None
    perdidos: int | None = None      # salto nos carimbos de tempo entre T0 e T1, em quadros
    n: int = 0

    @property
    def quadros(self) -> int | None:
        """Número de quadros entre T0 e T1: o numerador da fórmula."""
        if self.quadro_t0 is None or self.quadro_t1 is None:
            return None
        return self.quadro_t1 - self.quadro_t0


def parear(t: np.ndarray, mudancas: dict[str, dict[str, list[tuple[int, int]]]],
           janela_s: float) -> list[Evento]:
    """Associa a cada mudança do botão a primeira mudança da lâmpada do mesmo interruptor, no
    mesmo sentido, entre a mudança anterior e a próxima do botão. A lâmpada pode vir antes do
    botão: isso é registrado, não escondido."""
    eventos = []
    for dispositivo, tipos in mudancas.items():
        botao, lampada = tipos.get("botao", []), tipos.get("lampada", [])
        usadas = set()
        for i, (q0, direcao) in enumerate(botao):
            antes = botao[i - 1][0] if i > 0 else -1
            depois = botao[i + 1][0] if i + 1 < len(botao) else len(t)
            ev = Evento(dispositivo, "ligar" if direcao > 0 else "desligar", quadro_t0=q0)
            for j, (q1, d) in enumerate(lampada):
                if j in usadas or d != direcao or not antes < q1 < depois:
                    continue
                if t[q1] - t[q0] > janela_s:
                    break
                ev.quadro_t1, ev.lat_ms = q1, float((t[q1] - t[q0]) * 1000)
                usadas.add(j)
                break
            if ev.quadro_t1 is None:
                ev.situacao = "sem_resposta"
            elif ev.quadro_t1 < q0:
                ev.situacao = "lampada_antes_do_botao"
            eventos.append(ev)
        for j, (q1, d) in enumerate(lampada):
            if j not in usadas:
                eventos.append(Evento(dispositivo, "ligar" if d > 0 else "desligar", "sem_t0",
                                      quadro_t1=q1))
    eventos.sort(key=lambda e: e.quadro_t0 if e.quadro_t0 is not None else e.quadro_t1)
    for n, ev in enumerate(eventos, 1):
        ev.n = n
    return eventos


@dataclass
class Resultado:
    info: InfoVideo
    t: np.ndarray
    modo_tempo: str
    fps: float
    limiar: float
    k_rot: int
    regioes: dict[str, tuple[int, int, int, int]]
    sinais: dict[str, Sinal]
    eventos: list[Evento]
    avisos: list[str]


def analisar_video(video: Path, dados_regioes: dict, fps_captura: float | None = None,
                   limiar: float = 0.5, janela_s: float = 5.0, mostrar: bool = True) -> Resultado:
    regioes = achatar_regioes(dados_regioes)
    k_rot = int(dados_regioes.get("rot90_k", 0))
    info, medidas, dimensoes = extrair_medidas(video, regioes, k_rot, mostrar)
    t, modo = calcular_tempos(info, fps_captura)
    _, avisos = diagnostico(info)
    esperado = (dados_regioes.get("largura"), dados_regioes.get("altura"))
    if None not in esperado and tuple(esperado) != dimensoes:
        avisos.append(f"As regiões foram marcadas num vídeo {esperado[0]}x{esperado[1]}, mas este é "
                      f"{dimensoes[0]}x{dimensoes[1]}. Marque as regiões de novo.")

    fps = 1.0 / float(np.median(np.diff(t)))
    lacuna_luz = max(1, math.ceil(LACUNA_LUZ_S * fps))
    lacuna_botao = max(1, math.ceil(LACUNA_BOTAO_S * fps))

    sinais = {}
    for nome, X in medidas.items():
        s = separar_dois_estados(X, nome.split("/")[1])
        s.nome = nome
        sinais[nome] = s
    # O botão "ligado" é o estado que acompanha a lâmpada acesa.
    for nome, s in sinais.items():
        lampada = sinais[nome.replace("/botao", "/lampada")]
        if s.tipo == "botao" and s.p is not None and lampada.ativo:
            if np.corrcoef(s.p, lampada.p)[0, 1] < 0:
                s.inverter()

    mudancas: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for nome, s in sinais.items():
        if not s.ativo:
            continue
        dispositivo, tipo = nome.split("/")
        if tipo == "lampada":
            lista = transicoes(s, lacuna_luz, QUADROS_MINIMOS, limiar)
        else:
            lista = transicoes(s, lacuna_botao, lacuna_botao, limiar)
        mudancas.setdefault(dispositivo, {})[tipo] = lista
    if not any("botao" in tipos for tipos in mudancas.values()):
        avisos.append("Nenhuma mudança detectada no botão do app. Confira se a região do botão "
                      "não está coberta pelo dedo e se o botão muda de cor ao ligar.")

    eventos = parear(t, mudancas, janela_s)
    # Um salto nos carimbos de tempo pode ser um quadro perdido ou só um carimbo errado. O vídeo
    # não diz qual, então o teste com salto entre T0 e T1 deve ser refeito.
    pts = info.pts_s
    passo = float(np.median(np.diff(pts)))
    for ev in eventos:
        if ev.quadro_t0 is not None and ev.quadro_t1 is not None:
            a, b = sorted((ev.quadro_t0, ev.quadro_t1))
            ev.perdidos = int(round((pts[b] - pts[a]) / passo)) - (b - a)
    com_perda = [ev.n for ev in eventos if ev.perdidos]
    if com_perda:
        avisos.append(f"Há um salto nos carimbos de tempo entre o T0 e o T1 dos eventos {com_perda} "
                      "(coluna quadros_perdidos): quadro perdido ou carimbo errado. Refaça esses testes.")
    return Resultado(info, t, modo, fps, limiar, k_rot, regioes, sinais, eventos, avisos)


# ---------------------------------------------------------------------------
# Saídas: relatório, CSV, gráfico e imagens de conferência
# ---------------------------------------------------------------------------

CAMPOS = ["video", "n", "dispositivo", "comando", "situacao", "quadro_t0", "t0_s", "quadro_t1",
          "t1_s", "quadros", "latencia_ms", "quadros_perdidos", "fps_efetivo", "modo_tempo"]


def imprimir_relatorio(res: Resultado) -> None:
    linhas, _ = diagnostico(res.info)
    print()
    print("\n".join(linhas))
    print(f"Tempo calculado por: {res.modo_tempo}")
    print(f"Resolução temporal: {1000 / res.fps:.2f} ms por quadro (incerteza de ±1 quadro em cada ponta)")
    for aviso in res.avisos:
        print(f"AVISO: {aviso}")

    print("\nRegiões:")
    for nome, s in res.sinais.items():
        estado = "mudou" if s.ativo else "sem mudança"
        print(f"  {nome:<18} separação {s.separacao:7.1f}   amplitude {s.amplitude:7.3f}   {estado}")

    print(f"\nEventos: {len(res.eventos)}")
    for situacao, n in Counter(ev.situacao for ev in res.eventos).most_common():
        print(f"  {situacao:<24} {n}")

    grupos: dict[tuple[str, str], list[Evento]] = {}
    for ev in res.eventos:
        if ev.situacao == "ok":
            grupos.setdefault((ev.dispositivo, ev.comando), []).append(ev)
    if grupos:
        print("\nLatência T1 - T0 (eventos ok):")
        for (dispositivo, comando), evs in sorted(grupos.items()):
            v = np.array([e.lat_ms for e in evs])
            desvio = f"  desvio {v.std(ddof=1):6.1f}" if v.size >= 2 else ""
            print(f"  {dispositivo:<10} {comando:<9} n={v.size:<3} média {v.mean():7.1f} ms{desvio}"
                  f"  mín {v.min():7.1f}  máx {v.max():7.1f}")


def formatar(valor, casas: int, br: bool) -> str:
    if valor is None:
        return ""
    texto = f"{valor:.{casas}f}"
    return texto.replace(".", ",") if br else texto


def linha_evento(res: Resultado, ev: Evento, br: bool) -> dict[str, str]:
    def tempo(q):
        return formatar(None if q is None else float(res.t[q]), 4, br)

    return {
        "video": res.info.caminho.name,
        "n": str(ev.n),
        "dispositivo": ev.dispositivo,
        "comando": ev.comando,
        "situacao": ev.situacao,
        "quadro_t0": "" if ev.quadro_t0 is None else str(ev.quadro_t0),
        "t0_s": tempo(ev.quadro_t0),
        "quadro_t1": "" if ev.quadro_t1 is None else str(ev.quadro_t1),
        "t1_s": tempo(ev.quadro_t1),
        "quadros": "" if ev.quadros is None else str(ev.quadros),
        "latencia_ms": formatar(ev.lat_ms, 1, br),
        "quadros_perdidos": "" if ev.perdidos is None else str(ev.perdidos),
        "fps_efetivo": formatar(res.fps, 2, br),
        "modo_tempo": res.modo_tempo,
    }


def escrever_csv(caminho: Path, campos: list[str], linhas: list[dict], br: bool) -> None:
    with caminho.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";" if br else ",")
        w.writeheader()
        w.writerows(linhas)


def anexar_csv(caminho: Path, campos: list[str], linhas: list[dict], br: bool) -> None:
    """Acrescenta as linhas a uma planilha geral, criando-a se ainda não existir."""
    separador = ";" if br else ","
    if not caminho.exists():
        escrever_csv(caminho, campos, linhas, br)
        return
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        cabecalho = next(csv.reader(f, delimiter=separador), [])
    faltando = [c for c in campos if c not in cabecalho]
    if faltando:
        print(f"AVISO: {caminho.name} não tem as colunas {', '.join(faltando)}; elas não foram gravadas.")
    with caminho.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cabecalho, delimiter=separador, extrasaction="ignore")
        w.writerows(linhas)


def salvar_grafico(res: Resultado, caminho: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    nomes = list(res.sinais)
    fig, eixos = plt.subplots(len(nomes), 1, figsize=(14, 1.6 * len(nomes) + 0.8),
                              sharex=True, squeeze=False)
    for ax, nome in zip(eixos[:, 0], nomes):
        s = res.sinais[nome]
        ax.set_ylabel(nome if s.ativo else f"{nome}\n(sem mudança)", rotation=0, ha="right",
                      va="center", fontsize=8)
        ax.set_ylim(-0.4, 1.4)
        ax.set_yticks([0, 1])
        if s.p is None:
            continue
        ax.plot(res.t, np.clip(s.p, -0.4, 1.4), lw=0.6, color="#3a5a8c")
        ax.axhline(res.limiar, color="#999999", lw=0.6, ls="--")
        dispositivo, tipo = nome.split("/")
        for ev in res.eventos:
            if ev.quadro_t0 is not None:
                ax.axvline(res.t[ev.quadro_t0], color="#d0743c", lw=1.0)
            q = ev.quadro_t0 if tipo == "botao" else ev.quadro_t1
            if ev.dispositivo == dispositivo and q is not None:
                ax.plot(res.t[q], s.p[q], "o", ms=3, color="#2e8b57")
    eixos[-1, 0].set_xlabel("tempo (s)")
    fig.suptitle(f"{res.info.caminho.name}: sinais normalizados (0 = base, 1 = ativo). "
                 "Laranja = T0; verde = mudança detectada", fontsize=10)
    fig.tight_layout()
    fig.savefig(caminho, dpi=110)
    plt.close(fig)


def escrever(img: np.ndarray, texto: str, origem: tuple[int, int], escala: float = 0.5) -> None:
    """Texto branco sobre tarja preta (a fonte do OpenCV não tem acentos)."""
    fonte = cv2.FONT_HERSHEY_SIMPLEX
    (larg, alt), base = cv2.getTextSize(texto, fonte, escala, 1)
    x, y = origem
    cv2.rectangle(img, (x - 3, y - alt - 3), (x + larg + 3, y + base), (0, 0, 0), -1)
    cv2.putText(img, texto, origem, fonte, escala, (255, 255, 255), 1, cv2.LINE_AA)


def gravar_imagem(caminho: Path, img: np.ndarray) -> None:
    # imencode + tofile funciona em pastas com acento no Windows; imwrite não.
    ok, dados = cv2.imencode(caminho.suffix or ".png", img)
    if ok:
        dados.tofile(str(caminho))


def recorte_com_contexto(img: np.ndarray, r, fator: float = 0.8) -> np.ndarray:
    x, y, w, h = r
    m = int(max(w, h) * fator)
    alt, larg = img.shape[:2]
    x0, y0, x1, y1 = max(0, x - m), max(0, y - m), min(larg, x + w + m), min(alt, y + h + m)
    c = img[y0:y1, x0:x1].copy()
    cv2.rectangle(c, (x - x0, y - y0), (x - x0 + w - 1, y - y0 + h - 1), (0, 255, 255), 1)
    return c


def ladrilho(c: np.ndarray | None, legenda: str, tamanho=(300, 220)) -> np.ndarray:
    larg, alt = tamanho
    tela = np.full((alt, larg, 3), 32, np.uint8)
    if c is not None and c.size:
        h, w = c.shape[:2]
        e = min(larg / w, (alt - 24) / h)
        modo = cv2.INTER_NEAREST if e > 1 else cv2.INTER_AREA
        r = cv2.resize(c, (max(1, int(w * e)), max(1, int(h * e))), interpolation=modo)
        oy, ox = 24 + (alt - 24 - r.shape[0]) // 2, (larg - r.shape[1]) // 2
        tela[oy:oy + r.shape[0], ox:ox + r.shape[1]] = r
    escrever(tela, legenda, (6, 17), 0.45)
    return tela


def itens_conferencia(ev: Evento) -> list[tuple[str, int, str]]:
    itens = []
    if ev.quadro_t0 is not None:
        itens.append(("T0", ev.quadro_t0, f"{ev.dispositivo}/botao"))
    if ev.quadro_t1 is not None:
        itens.append(("T1", ev.quadro_t1, f"{ev.dispositivo}/lampada"))
    return itens


def salvar_conferencia(res: Resultado, pasta: Path) -> None:
    """Uma imagem por evento: o quadro anterior e o quadro detectado do T0 e do T1."""
    pasta.mkdir(parents=True, exist_ok=True)
    pedidos: dict[int, set[str]] = {}
    for ev in res.eventos:
        for _, q, regiao in itens_conferencia(ev):
            for qq in (q - 1, q):
                if qq >= 0:
                    pedidos.setdefault(qq, set()).add(regiao)
    if not pedidos:
        return
    recortes = {}
    ultimo = max(pedidos)
    cont, fluxo = abrir(res.info.caminho)
    for i, quadro in enumerate(cont.decode(fluxo)):
        if i in pedidos:
            img = girar(quadro.to_ndarray(format="bgr24"), res.k_rot)
            for regiao in pedidos[i]:
                recortes[(i, regiao)] = recorte_com_contexto(img, res.regioes[regiao])
        if i >= ultimo:
            break
    cont.close()

    for ev in res.eventos:
        referencia = res.t[ev.quadro_t0 if ev.quadro_t0 is not None else ev.quadro_t1]
        cabecalho = np.full((28, 600, 3), 20, np.uint8)
        escrever(cabecalho, f"evento {ev.n}: {ev.dispositivo} {ev.comando} | {ev.situacao}", (8, 19), 0.55)
        faixas = [cabecalho]
        for rotulo, q, regiao in itens_conferencia(ev):
            pares = []
            for qq, nome in ((q - 1, f"{rotulo}-1"), (q, rotulo)):
                legenda = f"{nome} q{qq} {(res.t[max(qq, 0)] - referencia) * 1000:+.1f}ms"
                pares.append(ladrilho(recortes.get((qq, regiao)), legenda))
            faixas.append(np.hstack(pares))
        gravar_imagem(pasta / f"evento_{ev.n:03d}.png", np.vstack(faixas))


def salvar_resultado(res: Resultado, pasta: Path, rotulos: dict[str, str], anexar: Path | None,
                     br: bool, imagens: bool = True) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    campos = CAMPOS + list(rotulos)
    linhas = [{**linha_evento(res, ev, br), **rotulos} for ev in res.eventos]
    escrever_csv(pasta / "eventos.csv", campos, linhas, br)

    nomes = list(res.sinais)
    sinais = []
    for i in range(res.info.n_quadros):
        linha = {"quadro": str(i), "tempo_s": formatar(res.t[i], 5, br)}
        for nome in nomes:
            p = res.sinais[nome].p
            linha[nome] = formatar(None if p is None else float(p[i]), 4, br)
        sinais.append(linha)
    escrever_csv(pasta / "sinais.csv", ["quadro", "tempo_s"] + nomes, sinais, br)

    salvar_grafico(res, pasta / "sinais.png")
    if imagens:
        salvar_conferencia(res, pasta / "conferencia")
    if anexar:
        anexar_csv(anexar, campos, linhas, br)
    print(f"\nResultados em: {pasta}")
    if anexar:
        print(f"Linhas acrescentadas em: {anexar}")


# ---------------------------------------------------------------------------
# Janela de navegação quadro a quadro (comandos marcar e ver)
# ---------------------------------------------------------------------------

def _teclas(*itens):
    mapa = {}
    for teclas, passo in itens:
        for tecla in teclas:
            mapa[tecla] = passo
            if isinstance(tecla, int) and ord("a") <= tecla <= ord("z"):
                mapa[tecla - 32] = passo  # mesma tecla com Caps Lock
    return mapa


# Códigos das setas: Windows (waitKeyEx) e Linux.
PASSOS = _teclas(((ord("d"), 2555904, 65363), 1), ((ord("a"), 2424832, 65361), -1),
                 ((ord("w"), 2490368, 65362), 24), ((ord("s"), 2621440, 65364), -24),
                 ((ord("f"),), 240), ((ord("r"),), -240))


class LeitorQuadros:
    """Acesso a qualquer quadro pelo índice, com cache dos quadros recém-decodificados."""

    def __init__(self, info: InfoVideo, k_rot: int, escala: float, capacidade: int = 64):
        self.cont, self.fluxo = abrir(info.caminho)
        self.pts = info.pts_brutos
        self.indice = {p: i for i, p in enumerate(self.pts)}
        self.k_rot, self.escala, self.capacidade = k_rot, escala, capacidade
        self.cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self.gerador = None
        self.posicao = -1

    def quadro(self, k: int) -> np.ndarray:
        if k in self.cache:
            self.cache.move_to_end(k)
            return self.cache[k]
        if self.gerador is None or not (self.posicao < k <= self.posicao + 60):
            self.cont.seek(int(self.pts[k]), stream=self.fluxo, backward=True, any_frame=False)
            self.gerador = self.cont.decode(self.fluxo)
            self.posicao = -1
        for q in self.gerador:
            i = self.indice.get(q.pts)
            if i is None:
                continue
            self.posicao = i
            img = girar(q.to_ndarray(format="bgr24"), self.k_rot)
            if self.escala != 1.0:
                img = cv2.resize(img, None, fx=self.escala, fy=self.escala, interpolation=cv2.INTER_AREA)
            self.cache[i] = img
            while len(self.cache) > self.capacidade:
                self.cache.popitem(last=False)
            if i >= k:
                break
        if k in self.cache:
            return self.cache[k]
        return self.cache[min(self.cache, key=lambda i: abs(i - k))]


class Navegador:
    """Janela com navegação quadro a quadro e referência para medir intervalos à mão."""

    def __init__(self, info: InfoVideo, k_rot: int, t: np.ndarray, titulo: str, ajuda: str):
        larg, alt = (info.altura, info.largura) if k_rot % 2 else (info.largura, info.altura)
        self.escala = min(1.0, 1280 / larg, 760 / alt)
        self.leitor = LeitorQuadros(info, k_rot, self.escala)
        self.t, self.n, self.titulo = t, info.n_quadros, titulo
        self.ajuda = "setas ou A/D: 1 quadro | W/S: 24 | F/R: 240 | Z: referencia | " + ajuda
        self.atual, self.referencia, self.versao = 0, None, 0
        cv2.namedWindow(titulo, cv2.WINDOW_AUTOSIZE)
        cv2.createTrackbar("quadro", titulo, 0, max(1, self.n - 1), lambda _v: None)
        self._pos_barra = 0

    def imagem(self) -> np.ndarray:
        return self.leitor.quadro(self.atual).copy()

    def desenhar_regiao(self, img: np.ndarray, nome: str, r, cor=(0, 255, 255)) -> None:
        x, y, w, h = (int(round(v * self.escala)) for v in r)
        cv2.rectangle(img, (x, y), (x + w, y + h), cor, 1)
        escrever(img, nome, (x, max(12, y - 4)), 0.42)

    def hud(self, img: np.ndarray, extras=()) -> None:
        linhas = [f"quadro {self.atual}/{self.n - 1}   t = {self.t[self.atual] * 1000:.1f} ms"]
        if self.referencia is not None:
            delta = (self.t[self.atual] - self.t[self.referencia]) * 1000
            quadros = self.atual - self.referencia
            linhas.append(f"referencia q{self.referencia}   delta = {quadros:+d} quadros = {delta:+.1f} ms")
        linhas += list(extras)
        for i, linha in enumerate(linhas):
            escrever(img, linha, (8, 22 + 20 * i))
        escrever(img, self.ajuda, (8, img.shape[0] - 10), 0.4)

    def rodar(self, desenhar, teclar) -> None:
        """`desenhar(img)` acrescenta marcações; `teclar(tecla)` devolve False para sair."""
        exibido = None
        while True:
            if exibido is not None and cv2.getWindowProperty(self.titulo, cv2.WND_PROP_VISIBLE) < 1:
                return
            pos = cv2.getTrackbarPos("quadro", self.titulo)
            if pos != self._pos_barra:
                self.atual = self._pos_barra = pos
            estado = (self.atual, self.referencia, self.versao)
            if estado != exibido:
                img = self.imagem()
                desenhar(img)
                cv2.imshow(self.titulo, img)
                if self._pos_barra != self.atual:
                    cv2.setTrackbarPos("quadro", self.titulo, self.atual)
                    self._pos_barra = self.atual
                exibido = estado
            tecla = cv2.waitKeyEx(30)
            if tecla == -1:
                continue
            if tecla in PASSOS:
                self.atual = min(self.n - 1, max(0, self.atual + PASSOS[tecla]))
            elif tecla in (ord("z"), ord("Z")):
                self.referencia = self.atual
            elif tecla == 27:
                return
            elif teclar(tecla) is False:
                return
            else:
                self.versao += 1


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def cmd_info(a) -> None:
    info = ler_info(Path(a.video))
    linhas, avisos = diagnostico(info)
    print("\n".join(linhas))
    print(f"Tempo será calculado por: {calcular_tempos(info, a.fps_captura)[1]}")
    for aviso in avisos:
        print(f"AVISO: {aviso}")
    print("Metadados:")
    for chave, valor in sorted(info.metadados.items()):
        print(f"  {chave} = {valor[:80]}")


def cmd_marcar(a) -> None:
    video = Path(a.video)
    print("Lendo os quadros do vídeo...")
    info = ler_info(video)
    k_rot = k_do_argumento(a.girar, info)
    t, _ = calcular_tempos(info, a.fps_captura)
    pendentes = [f"{d}/{tp}" for d in a.dispositivos for tp in TIPOS_REGIAO]
    marcadas: dict[str, list[int]] = {}
    nav = Navegador(info, k_rot, t, f"Marcar regioes - {video.name}",
                    "M: marcar | K: pular | U: desfazer | ESC: salvar e sair")

    def desenhar(img):
        for nome, r in marcadas.items():
            nav.desenhar_regiao(img, nome, r)
        if pendentes:
            nav.hud(img, [f"Marque: {pendentes[0]} ({DICAS[pendentes[0].split('/')[1]]})"])
        else:
            nav.hud(img, ["Todas as regioes marcadas. ESC salva e sai."])

    def teclar(tecla):
        if tecla in (ord("u"), ord("U")) and marcadas:
            pendentes.insert(0, marcadas.popitem()[0])
        elif not pendentes:
            return True
        elif tecla in (ord("m"), ord("M")):
            janela = "Arraste o retangulo e tecle ENTER (C cancela)"
            x, y, w, h = cv2.selectROI(janela, nav.imagem(), True, False)
            cv2.destroyWindow(janela)
            if w > 0 and h > 0:
                e = nav.escala
                marcadas[pendentes.pop(0)] = [int(round(x / e)), int(round(y / e)),
                                              int(round(w / e)), int(round(h / e))]
        elif tecla in (ord("k"), ord("K")):
            pendentes.pop(0)
        return True

    nav.rodar(desenhar, teclar)
    cv2.destroyAllWindows()

    dispositivos: dict[str, dict[str, list[int]]] = {}
    for nome, r in marcadas.items():
        d, tp = nome.split("/")
        dispositivos.setdefault(d, {})[tp] = r
    for d in a.dispositivos:
        if not all(tp in dispositivos.get(d, {}) for tp in TIPOS_REGIAO):
            print(f"AVISO: o interruptor '{d}' ficou sem o botão ou sem a lâmpada; ele será ignorado.")
    if not any(all(tp in tipos for tp in TIPOS_REGIAO) for tipos in dispositivos.values()):
        raise SystemExit("Nada foi salvo: nenhum interruptor ficou com o botão e a lâmpada marcados.")
    larg, alt = (info.altura, info.largura) if k_rot % 2 else (info.largura, info.altura)
    destino = Path(a.saida) if a.saida else video.parent / "regioes.json"
    dados = {"versao": 2, "video_de_referencia": video.name, "rot90_k": k_rot, "largura": larg,
             "altura": alt, "dispositivos": dispositivos}
    destino.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Regiões salvas em: {destino}")


def carregar_marcos(pasta: Path) -> dict[int, list[str]]:
    arquivo = pasta / "eventos.csv"
    marcos: dict[int, list[str]] = {}
    if not arquivo.exists():
        return marcos
    with arquivo.open(encoding="utf-8-sig", newline="") as f:
        primeira = f.readline()
        f.seek(0)
        if primeira.startswith("\ufeff"):
            f.read(1)
        leitor = csv.DictReader(f, delimiter=";" if ";" in primeira else ",")
        for linha in leitor:
            for coluna, rotulo in (("quadro_t0", "T0 (botao)"), ("quadro_t1", "T1 (lampada)")):
                if linha.get(coluna):
                    texto = f"evento {linha['n']}: {rotulo}"
                    if coluna == "quadro_t1" and linha.get("latencia_ms"):
                        texto += f" ({linha['quadros']} quadros, {linha['latencia_ms']} ms)"
                    marcos.setdefault(int(linha[coluna]), []).append(texto)
    return marcos


TECLAS_MANUAIS = {ord("1"): "toque", ord("2"): "solta", ord("3"): "luz", ord("4"): "botao"}
CAMPOS_MANUAL = ["video", "evento", "comando", "quadro_toque", "t_toque_ms", "quadro_solta", "t_solta_ms",
                 "quadro_luz", "t_luz_ms", "quadro_botao", "t_botao_ms", "luz_menos_toque_ms",
                 "luz_menos_solta_ms", "botao_menos_luz_ms"]


class AnotacaoManual:
    """Instantes anotados à mão, um conjunto por mudança da lâmpada. Salva a cada tecla."""

    def __init__(self, video: Path, t: np.ndarray, mudancas: list[tuple[int, int]]):
        self.video, self.t = video, t
        self.arquivo = video.parent / f"{video.stem}_manual.csv"
        self.eventos = [{"comando": "ligar" if d > 0 else "desligar", "auto": q} for q, d in mudancas]
        self.atual = 0
        if self.arquivo.exists():
            with self.arquivo.open(encoding="utf-8-sig", newline="") as f:
                for linha in csv.DictReader(f, delimiter=";"):
                    n = int(linha["evento"]) - 1
                    if 0 <= n < len(self.eventos):
                        for campo in TECLAS_MANUAIS.values():
                            if linha.get(f"quadro_{campo}"):
                                self.eventos[n][campo] = int(linha[f"quadro_{campo}"])

    def anotar(self, campo: str, q: int) -> None:
        self.eventos[self.atual][campo] = q
        self.salvar()

    def ms(self, ev: dict, campo: str) -> float | None:
        return float(self.t[ev[campo]] * 1000) if campo in ev else None

    def diferenca(self, ev: dict, a: str, b: str) -> float | None:
        va, vb = self.ms(ev, a), self.ms(ev, b)
        return None if va is None or vb is None else va - vb

    def salvar(self) -> None:
        linhas = []
        for n, ev in enumerate(self.eventos, 1):
            linha = {"video": self.video.name, "evento": str(n), "comando": ev["comando"]}
            for campo in TECLAS_MANUAIS.values():
                linha[f"quadro_{campo}"] = str(ev[campo]) if campo in ev else ""
                linha[f"t_{campo}_ms"] = formatar(self.ms(ev, campo), 1, True)
            linha["luz_menos_toque_ms"] = formatar(self.diferenca(ev, "luz", "toque"), 1, True)
            linha["luz_menos_solta_ms"] = formatar(self.diferenca(ev, "luz", "solta"), 1, True)
            linha["botao_menos_luz_ms"] = formatar(self.diferenca(ev, "botao", "luz"), 1, True)
            linhas.append(linha)
        escrever_csv(self.arquivo, CAMPOS_MANUAL, linhas, True)

    def hud(self) -> list[str]:
        ev = self.eventos[self.atual]
        partes = []
        for tecla, campo in zip("1234", TECLAS_MANUAIS.values()):
            v = self.ms(ev, campo)
            partes.append(f"{tecla} {campo}: " + ("-" if v is None else f"{v:.1f}"))
        linhas = [f"evento {self.atual + 1}/{len(self.eventos)} ({ev['comando']})", " | ".join(partes)]
        lat = [(rotulo, self.diferenca(ev, "luz", campo)) for rotulo, campo in
               (("luz-toque", "toque"), ("luz-solta", "solta"))]
        lat.append(("botao-luz", self.diferenca(ev, "botao", "luz")))
        linhas.append("   ".join(f"{r} = {v:.1f} ms" for r, v in lat if v is not None))
        return linhas


def mudancas_da_lampada(video: Path, r: tuple[int, int, int, int], k_rot: int) -> list[tuple[int, int]]:
    print("Medindo a lâmpada em todos os quadros (leva cerca de 1 min por minuto de vídeo)...")
    info, medidas, _ = extrair_medidas(video, {"luz": r}, k_rot)
    s = separar_dois_estados(medidas["luz"], "lampada")
    if not s.ativo:
        raise SystemExit("A região marcada não mostra a lâmpada acendendo e apagando. Marque de novo.")
    t, _ = calcular_tempos(info)
    fps = 1.0 / float(np.median(np.diff(t)))
    return transicoes(s, max(1, math.ceil(LACUNA_LUZ_S * fps)), QUADROS_MINIMOS, 0.5)


def ver_manual(video: Path, info: InfoVideo, k_rot: int, t: np.ndarray) -> None:
    """Navegação com anotação: N/P pulam entre as mudanças da lâmpada e 1 a 4 anotam os instantes."""
    nav = Navegador(info, k_rot, t, f"Manual - {video.name}",
                    "N/P: proxima/anterior mudanca da lampada | 1 toque 2 solta 3 luz 4 botao | ESC: sair")
    config = video.parent / f"{video.stem}_manual.json"
    if config.exists():
        r = tuple(json.loads(config.read_text(encoding="utf-8"))["luz"])
    else:
        janela = "Arraste um retangulo em volta da lampada e tecle ENTER"
        x, y, w, h = cv2.selectROI(janela, nav.imagem(), True, False)
        cv2.destroyWindow(janela)
        if w <= 0 or h <= 0:
            raise SystemExit("Nenhuma região marcada.")
        e = nav.escala
        r = (int(round(x / e)), int(round(y / e)), int(round(w / e)), int(round(h / e)))
        config.write_text(json.dumps({"luz": list(r), "rot90_k": k_rot}), encoding="utf-8")
    mudancas = mudancas_da_lampada(video, r, k_rot)
    if not mudancas:
        raise SystemExit("A lâmpada não muda neste vídeo.")
    anot = AnotacaoManual(video, t, mudancas)
    print(f"{len(mudancas)} mudanças da lâmpada encontradas. Anotações em: {anot.arquivo}")
    nav.atual = anot.eventos[0]["auto"]

    def desenhar(img):
        nav.desenhar_regiao(img, "lampada", r)
        nav.hud(img, anot.hud())

    def teclar(tecla):
        if tecla in (ord("n"), ord("N"), ord("p"), ord("P")):
            passo = 1 if tecla in (ord("n"), ord("N")) else -1
            anot.atual = min(len(anot.eventos) - 1, max(0, anot.atual + passo))
            nav.atual = anot.eventos[anot.atual]["auto"]
        elif tecla in TECLAS_MANUAIS:
            anot.anotar(TECLAS_MANUAIS[tecla], nav.atual)
        return True

    nav.rodar(desenhar, teclar)
    cv2.destroyAllWindows()
    completos = sum(all(c in ev for c in ("toque", "luz")) for ev in anot.eventos)
    print(f"Eventos com toque e luz anotados: {completos} de {len(anot.eventos)}. Arquivo: {anot.arquivo}")


def cmd_ver(a) -> None:
    video = Path(a.video)
    print("Lendo os quadros do vídeo...")
    info = ler_info(video)
    if a.manual:
        k_rot = k_do_argumento(a.girar, info)
        ver_manual(video, info, k_rot, calcular_tempos(info, a.fps_captura)[0])
        return
    regioes, k_rot = {}, k_do_argumento(a.girar, info)
    arquivo_regioes = Path(a.regioes) if a.regioes else video.parent / "regioes.json"
    if arquivo_regioes.exists():
        dados = carregar_regioes(arquivo_regioes)
        regioes, k_rot = achatar_regioes(dados), int(dados.get("rot90_k", k_rot))
    t, modo = calcular_tempos(info, a.fps_captura)
    print(f"Tempo calculado por: {modo}")
    pasta = Path(a.resultado) if a.resultado else video.parent / f"{video.stem}_analise"
    marcos = carregar_marcos(pasta)
    ordenados = sorted(marcos)
    nav = Navegador(info, k_rot, t, f"Ver - {video.name}", "N/P: proximo/anterior evento | ESC: sair")

    def desenhar(img):
        for nome, r in regioes.items():
            nav.desenhar_regiao(img, nome, r)
        nav.hud(img, marcos.get(nav.atual, []))

    def teclar(tecla):
        if tecla in (ord("n"), ord("N")):
            nav.atual = next((q for q in ordenados if q > nav.atual), nav.atual)
        elif tecla in (ord("p"), ord("P")):
            nav.atual = next((q for q in reversed(ordenados) if q < nav.atual), nav.atual)
        return True

    nav.rodar(desenhar, teclar)
    cv2.destroyAllWindows()


def cmd_analisar(a) -> None:
    video = Path(a.video)
    arquivo_regioes = Path(a.regioes) if a.regioes else video.parent / "regioes.json"
    rotulos = {}
    for item in a.rotulo:
        if "=" not in item:
            raise SystemExit(f"--rotulo precisa ser chave=valor (recebi '{item}')")
        chave, valor = item.split("=", 1)
        rotulos[chave.strip()] = valor.strip()
    print(f"Analisando {video.name}...")
    res = analisar_video(video, carregar_regioes(arquivo_regioes), a.fps_captura, a.limiar, a.janela)
    imprimir_relatorio(res)
    pasta = Path(a.saida) if a.saida else video.parent / f"{video.stem}_analise"
    salvar_resultado(res, pasta, rotulos, Path(a.anexar) if a.anexar else None, a.formato_br,
                     not a.sem_imagens)


def main(argv=None) -> None:
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)

    def comum(p):
        p.add_argument("video", help="arquivo de vídeo (mp4)")
        p.add_argument("--fps-captura", type=float, default=None,
                       help="força o tempo = índice do quadro / este valor (ex.: 240)")

    p = sub.add_parser("info", help="mostra a taxa de quadros real e os metadados de tempo")
    comum(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("marcar", help="marca as regiões de interesse com o mouse")
    comum(p)
    p.add_argument("--dispositivos", nargs="+", default=["wifi", "zigbee"],
                   help="nomes dos interruptores filmados (padrão: wifi zigbee)")
    p.add_argument("--saida", help="arquivo de regiões (padrão: regioes.json na pasta do vídeo)")
    p.add_argument("--girar", type=int, choices=[0, 90, 180, 270], default=None,
                   help="gira a imagem no sentido horário (padrão: segue o celular)")
    p.set_defaults(func=cmd_marcar)

    p = sub.add_parser("analisar", help="encontra T0 e T1 e calcula as latências")
    comum(p)
    p.add_argument("--regioes", help="arquivo de regiões (padrão: regioes.json na pasta do vídeo)")
    p.add_argument("--saida", help="pasta de resultados (padrão: <video>_analise)")
    p.add_argument("--limiar", type=float, default=0.5,
                   help="fração entre o estado de base (0) e o ativo (1) que confirma uma mudança")
    p.add_argument("--janela", type=float, default=5.0,
                   help="tempo máximo, em segundos, entre o T0 e a resposta da lâmpada")
    p.add_argument("--rotulo", action="append", default=[], metavar="CHAVE=VALOR",
                   help="coluna extra no CSV, ex.: --rotulo distancia_m=2 (pode repetir)")
    p.add_argument("--anexar", help="planilha geral onde acrescentar os eventos deste vídeo")
    p.add_argument("--formato-br", action="store_true",
                   help="CSV com ';' e vírgula decimal, para abrir direto no Excel em português")
    p.add_argument("--sem-imagens", action="store_true", help="não gera as imagens de conferência")
    p.set_defaults(func=cmd_analisar)

    p = sub.add_parser("ver", help="navega quadro a quadro com o tempo exato")
    comum(p)
    p.add_argument("--regioes", help="desenha as regiões deste arquivo")
    p.add_argument("--resultado", help="pasta de resultados para pular entre eventos")
    p.add_argument("--manual", action="store_true",
                   help="análise manual: N/P pulam entre as mudanças da lâmpada e as teclas 1 a 4 "
                        "anotam toque, soltura, luz e botão em <video>_manual.csv")
    p.add_argument("--girar", type=int, choices=[0, 90, 180, 270], default=None,
                   help="gira a imagem no sentido horário (padrão: segue o celular)")
    p.set_defaults(func=cmd_ver)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
