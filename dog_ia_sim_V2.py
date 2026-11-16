#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DOG-IA v2 — Cão-guia robô (simulação em pygame)

Principais mudanças em relação à v1
-----------------------------------
 * Arquitetura HÍBRIDA: planejador deliberativo (A* em grid de ocupação, com
   custo menor sobre o piso tátil) + camada reativa (LiDAR) + máquina de estados.
 * Máquina de estados: PARADO, SEGUINDO, DESVIANDO, AGUARDANDO_SINAL,
   ATRAVESSANDO, FREIO_EMERGENCIA, SEM_ROTA, CHEGOU.
 * Freio de emergência independente da zona (inclusive na faixa).
 * Travessia só inicia se o verde de pedestre tiver tempo suficiente; semáforo com
   fases de segurança (todos vermelhos) antes/depois do verde de pedestre.
 * Tutor (pessoa guiada) modelado como seguidor com guia (leash), com colisão
   própria; obstáculos suspensos bloqueiam o tutor (cabeça), não o cão.
 * Carros com aceleração/frenagem, distância de seguimento, parada suave na
   linha de retenção e frenagem para pessoas na pista. Tecla T cria "infrator".
 * LiDAR com ruído gaussiano + falhas esporádicas + filtro de mediana (3 varreduras),
   raycast analítico (Rect.clipline).
 * Movimento independente do FPS (dt), pedestres NPC, vários destinos (teclas 1-4),
   modo manual ASSISTIDO (mantém a camada de segurança), áudio estéreo direcional,
   métricas (tempo, distância, alertas, quase-colisões, colisões, freios).
 * Modo benchmark headless:  python dog_ia_v2.py --benchmark 20 --seed 1

Bugs corrigidos da v1: giro "para longe da rua" dependente do heading, velocidade
assimétrica dos carros (truncamento de Rect), zonas do LiDAR 6/7/6, timers de áudio
por tipo, validação de cliques, carro parado na linha bloqueando a travessia.
"""

import argparse
import array
import heapq
import math
import os
import random
import statistics
import sys
from collections import deque
from dataclasses import dataclass, field

if "--benchmark" in sys.argv:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

# ==================== CONFIGURAÇÕES GERAIS ====================
LARGURA, ALTURA = 1050, 680
FPS = 60
AREA_MAPA_LARGURA = LARGURA - 260

RUA_Y = 250
RUA_ALTURA = 170
RUA_FIM = RUA_Y + RUA_ALTURA
FAIXA_X = 580
FAIXA_LARGURA = 95
CENTRO_FAIXA_X = FAIXA_X + FAIXA_LARGURA // 2
MEIO_FIO_CIMA = RUA_Y - 12
MEIO_FIO_BAIXO = RUA_FIM + 12

CASA_RECT = pygame.Rect(20, 38, 115, 68)
MERCADO_RECT = pygame.Rect(20, ALTURA - 106, 130, 68)
FARMACIA_RECT = pygame.Rect(690, 38, 84, 64)
PADARIA_RECT = pygame.Rect(690, ALTURA - 106, 84, 68)
PONTO_PARTIDA_CASA = (CASA_RECT.right + 28, CASA_RECT.centery + 18)

DESTINOS = {
    pygame.K_1: ("MERCADO", (MERCADO_RECT.right + 28, MERCADO_RECT.centery - 15)),
    pygame.K_2: ("FARMÁCIA", (732, 142)),
    pygame.K_3: ("PADARIA", (732, 546)),
    pygame.K_4: ("CASA", PONTO_PARTIDA_CASA),
}

# Folgas do planejador (px): o tutor/cabeça exigem mais margem em obstáculos suspensos
INFL_SOLO, INFL_SUSPENSO, INFL_EDIFICIO = 30, 38, 22

# Paleta
BRANCO = (245, 245, 245)
PRETO = (20, 22, 28)
ASFALTO = (42, 45, 52)
CALCADA_CLARA = (130, 134, 142)
MEIO_FIO = (175, 180, 188)
PISO_TATIL = (235, 190, 20)
GRAMA = (46, 115, 65)
COR_CASA = (65, 105, 155)
COR_TELHADO_CASA = (175, 70, 55)
COR_MERCADO = (38, 135, 115)
COR_TOLDO_MERCADO = (230, 145, 35)
COR_FARMACIA = (170, 60, 90)
COR_PADARIA = (150, 105, 55)
CORPO_ROBO = (30, 136, 229)
DETALHE_ROBO = (15, 76, 129)
CABECA_ROBO = (230, 236, 245)
ARNES_GUIA = (255, 215, 0)
VERDE_LIVRE = (46, 204, 113)
LARANJA_ALERTA = (255, 140, 0)
VERMELHO_PERIGO = (225, 55, 55)
ROXO_SUSPENSO = (155, 89, 182)
AZUL_INFO = (120, 170, 235)


# ==================== UTILIDADES ====================
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def angulo_diff(a, b):
    """Menor diferença angular a-b em [-pi, pi]."""
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def circulo_retangulo(cx, cy, r, rect):
    px = clamp(cx, rect.left, rect.right)
    py = clamp(cy, rect.top, rect.bottom)
    return (cx - px) ** 2 + (cy - py) ** 2 < r * r


def folga_circulo_retangulo(cx, cy, r, rect):
    px = clamp(cx, rect.left, rect.right)
    py = clamp(cy, rect.top, rect.bottom)
    return max(0.0, math.hypot(cx - px, cy - py) - r)


def em_rua(y, margem=0):
    return RUA_Y - margem <= y <= RUA_FIM + margem


def tateis():
    """Retângulos de piso tátil (conectam casa, mercado, farmácia e padaria)."""
    tx = CENTRO_FAIXA_X - 7
    return [
        pygame.Rect(CASA_RECT.right + 4, 120, 635 - (CASA_RECT.right + 4), 14),
        pygame.Rect(tx, 120, 14, RUA_Y - 120),
        pygame.Rect(tx, RUA_FIM, 14, 550 - RUA_FIM),
        pygame.Rect(MERCADO_RECT.right + 4, 536, 635 - (MERCADO_RECT.right + 4), 14),
        pygame.Rect(635, 120, 105, 14),   # ramo da farmácia
        pygame.Rect(635, 536, 105, 14),   # ramo da padaria
    ]


def aplicar_barreira(x, y, nx, ny, autorizado):
    """Meio-fio físico: só entra no asfalto se autorizado; dentro da rua, só na faixa."""
    bloqueado = False
    if not autorizado:
        if y <= MEIO_FIO_CIMA < ny:
            ny, bloqueado = MEIO_FIO_CIMA, True
        elif y >= MEIO_FIO_BAIXO > ny:
            ny, bloqueado = MEIO_FIO_BAIXO, True
    if RUA_Y - 4 <= ny <= RUA_FIM + 4:
        nx = clamp(nx, FAIXA_X + 12, FAIXA_X + FAIXA_LARGURA - 12)
    return nx, ny, bloqueado


@dataclass
class Obstaculo:
    rect: pygame.Rect
    tipo: str      # "solo" | "suspenso"
    rotulo: str


@dataclass
class Metricas:
    tempo: float = 0.0
    distancia: float = 0.0
    alertas: int = 0
    quase: int = 0
    colisoes: int = 0
    freios: int = 0
    sinal_fechou: int = 0
    viagens: int = 0

    def reset_viagem(self):
        v = self.viagens
        self.__init__()
        self.viagens = v


# ==================== ÁUDIO ====================
class AudioMudo:
    ativo = False

    def tocar(self, *a, **k):
        pass


class SistemaAudio:
    """Bipes sintéticos ESTÉREO; o lado do alerta indica a direção do perigo."""
    INTERVALOS = {"perigo": 0.75, "suspenso": 0.65, "sucesso": 0.0}

    def __init__(self):
        self.ativo = True
        self.disponivel = False
        self.ultimo = {}
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            self.sons = {
                "perigo": self._tom(980, 130, 0.35),
                "suspenso": self._tom(520, 180, 0.35),
                "sucesso": self._tom(784, 250, 0.30),
            }
            self.disponivel = True
        except pygame.error:
            self.disponivel = False

    @staticmethod
    def _tom(freq, dur_ms, vol):
        sr = 44100
        n = int(sr * dur_ms / 1000.0)
        buf = array.array("h", [0] * (2 * n))
        amp = int(32767 * vol)
        for i in range(n):
            v = int(amp * (1.0 - i / n) * math.sin(2 * math.pi * freq * i / sr))
            buf[2 * i] = v
            buf[2 * i + 1] = v
        return pygame.mixer.Sound(buffer=buf)

    def tocar(self, tipo, pan, agora, intervalo=None):
        if not (self.ativo and self.disponivel):
            return
        intervalo = self.INTERVALOS.get(tipo, 0.7) if intervalo is None else intervalo
        if agora - self.ultimo.get(tipo, -99.0) < intervalo:
            return
        self.ultimo[tipo] = agora
        canal = self.sons[tipo].play()
        if canal is not None:   # pan -1 (esquerda) .. +1 (direita)
            canal.set_volume(1.0 - max(0.0, pan) * 0.85, 1.0 - max(0.0, -pan) * 0.85)


# ==================== SEMÁFORO ====================
class Semaforo:
    """Ciclo com fases de segurança (todos vermelhos) e relógio simulado."""
    FASES = [("VERDE", 6.0), ("AMARELO", 2.0), ("TODOS_VERM_1", 1.5),
             ("PEDESTRE", 8.0), ("TODOS_VERM_2", 1.5)]

    def __init__(self):
        self.i = 0
        self.t = 0.0

    def atualizar(self, dt):
        self.t += dt
        while self.t >= self.FASES[self.i][1]:
            self.t -= self.FASES[self.i][1]
            self.i = (self.i + 1) % len(self.FASES)

    @property
    def fase(self):
        return self.FASES[self.i][0]

    @property
    def estado_carros(self):
        f = self.fase
        return f if f in ("VERDE", "AMARELO") else "VERMELHO"

    def pedestre_livre(self):
        return self.fase == "PEDESTRE"

    def tempo_restante_pedestre(self):
        return self.FASES[self.i][1] - self.t if self.pedestre_livre() else 0.0

    @staticmethod
    def _boneco(tela, cx, cy, cor, andando):
        pygame.draw.circle(tela, cor, (cx, cy - 8), 3)
        pygame.draw.line(tela, cor, (cx, cy - 5), (cx, cy + 3), 2)
        if andando:
            pygame.draw.line(tela, cor, (cx, cy - 3), (cx - 4, cy + 1), 2)
            pygame.draw.line(tela, cor, (cx, cy - 3), (cx + 4, cy), 2)
            pygame.draw.line(tela, cor, (cx, cy + 3), (cx - 4, cy + 9), 2)
            pygame.draw.line(tela, cor, (cx, cy + 3), (cx + 4, cy + 9), 2)
        else:
            pygame.draw.line(tela, cor, (cx - 3, cy - 3), (cx - 3, cy + 2), 2)
            pygame.draw.line(tela, cor, (cx + 3, cy - 3), (cx + 3, cy + 2), 2)
            pygame.draw.line(tela, cor, (cx - 1, cy + 3), (cx - 1, cy + 9), 2)
            pygame.draw.line(tela, cor, (cx + 1, cy + 3), (cx + 1, cy + 9), 2)

    def desenhar(self, tela, fonte):
        livre = self.pedestre_livre()
        resta = self.tempo_restante_pedestre()
        piscando = livre and resta < 3.0 and int(self.t * 4) % 2 == 0
        conjuntos = [
            (FAIXA_X - 54, RUA_Y - 58, FAIXA_X - 28, RUA_Y - 58),
            (FAIXA_X + FAIXA_LARGURA + 8, RUA_FIM + 8, FAIXA_X + FAIXA_LARGURA + 34, RUA_FIM + 8),
        ]
        est = self.estado_carros
        for cx, cy, px, py in conjuntos:
            pygame.draw.rect(tela, (25, 27, 33), (cx, cy, 22, 50), border_radius=5)
            pygame.draw.rect(tela, BRANCO, (cx, cy, 22, 50), 1, border_radius=5)
            pygame.draw.circle(tela, (240, 40, 40) if est == "VERMELHO" else (70, 20, 20), (cx + 11, cy + 10), 6)
            pygame.draw.circle(tela, (245, 210, 30) if est == "AMARELO" else (70, 60, 15), (cx + 11, cy + 25), 6)
            pygame.draw.circle(tela, (40, 230, 90) if est == "VERDE" else (20, 70, 30), (cx + 11, cy + 40), 6)

            pygame.draw.rect(tela, (25, 27, 33), (px, py, 22, 50), border_radius=5)
            pygame.draw.rect(tela, BRANCO, (px, py, 22, 50), 1, border_radius=5)
            self._boneco(tela, px + 11, py + 13, (255, 50, 50) if not livre else (65, 20, 20), False)
            pygame.draw.line(tela, (80, 85, 95), (px + 3, py + 25), (px + 19, py + 25), 1)
            cor_siga = (45, 245, 105) if (livre and not piscando) else (20, 65, 30)
            self._boneco(tela, px + 11, py + 37, cor_siga, True)
            if livre:
                tela.blit(fonte.render(f"{resta:0.0f}", True, BRANCO), (px + 26, py + 18))


# ==================== VEÍCULOS E PEDESTRES NPC ====================
class Carro:
    COMP, LARG = 75, 38
    ACEL, DECEL = 220.0, 380.0
    DECEL_PESSOA = 700.0

    def __init__(self, x, y, vmax, cor, direcao):
        self.x = float(x)
        self.rect = pygame.Rect(int(x), y, self.COMP, self.LARG)
        self.vmax = vmax            # px/s (módulo)
        self.v = vmax
        self.dir = direcao
        self.cor = cor
        self.infrator = False

    @property
    def right(self):
        return self.x + self.COMP

    def gap_linha(self):
        if self.dir > 0:
            return (FAIXA_X - 10) - self.right
        return self.x - (FAIXA_X + FAIXA_LARGURA + 10)

    def eta_faixa(self):
        """Segundos até a frente do carro alcançar a faixa (0 se já sobre ela)."""
        ini, fim = FAIXA_X - 6, FAIXA_X + FAIXA_LARGURA + 6
        if self.right > ini and self.x < fim:
            return 0.0
        gap = (ini - self.right) if self.dir > 0 else (self.x - fim)
        if gap < 0 or self.v < 40:
            return math.inf
        return gap / self.v

    def zona_varrida(self, t=1.0):
        ext = int(self.v * t + 30)
        r = self.rect.inflate(0, 16)
        if self.dir > 0:
            return pygame.Rect(r.left, r.top, r.width + ext, r.height)
        return pygame.Rect(r.left - ext, r.top, r.width + ext, r.height)

    def atualizar(self, dt, semaforo, outros, pessoas):
        alvo = self.vmax
        est = semaforo.estado_carros
        if not self.infrator and est != "VERDE":
            gap = self.gap_linha()
            if gap >= -1:
                d_freio = self.v ** 2 / (2 * self.DECEL)
                if est == "VERMELHO" or d_freio <= gap:
                    alvo = min(alvo, math.sqrt(2 * self.DECEL * max(gap - 3, 0)))
        for o in outros:   # distância de seguimento na mesma pista
            if o is self or o.dir != self.dir or abs(o.rect.y - self.rect.y) > 10:
                continue
            gap = (o.x - self.right) if self.dir > 0 else (self.x - o.right)
            if 0 <= gap < 250:
                alvo = min(alvo, math.sqrt(2 * self.DECEL * max(gap - 20, 0)))
        dec_p = 380.0 if self.infrator else self.DECEL_PESSOA
        for p in pessoas:   # freia para pessoas na pista
            gap = (p.x - p.raio - self.right) if self.dir > 0 else (self.x - (p.x + p.raio))
            if -4 <= gap <= 190 and abs(p.y - self.rect.centery) < self.LARG / 2 + p.raio + 8:
                alvo = min(alvo, math.sqrt(2 * dec_p * max(gap - 18, 0)))
        self.v += clamp(alvo - self.v, -max(self.DECEL, dec_p) * dt, self.ACEL * dt)
        self.v = max(0.0, self.v)
        self.x += self.dir * self.v * dt
        if self.dir > 0 and self.x > AREA_MAPA_LARGURA + 20:
            self.x = -self.COMP - 20
        elif self.dir < 0 and self.right < -20:
            self.x = AREA_MAPA_LARGURA + 20
        self.rect.x = int(self.x)

    def desenhar(self, tela):
        r = self.rect
        pygame.draw.rect(tela, (25, 28, 32), r.move(3, 4), border_radius=8)
        pygame.draw.rect(tela, self.cor, r, border_radius=8)
        pygame.draw.rect(tela, (255, 60, 60) if self.infrator else BRANCO, r, 2 if self.infrator else 1, border_radius=8)
        if self.dir > 0:
            vidro = pygame.Rect(r.right - 24, r.y + 5, 14, r.height - 10)
            f1, f2 = (r.right, r.y + 7), (r.right, r.bottom - 7)
        else:
            vidro = pygame.Rect(r.left + 10, r.y + 5, 14, r.height - 10)
            f1, f2 = (r.left, r.y + 7), (r.left, r.bottom - 7)
        pygame.draw.rect(tela, (40, 55, 75), vidro, border_radius=3)
        pygame.draw.circle(tela, (255, 245, 150), f1, 3)
        pygame.draw.circle(tela, (255, 245, 150), f2, 3)


class Pedestre:
    """Pedestre NPC que caminha na calçada e abre espaço quando o robô se aproxima."""
    raio = 7

    def __init__(self, x_min, x_max, y, vel, x_ini, recuo):
        self.x_min, self.x_max, self.y0, self.vel, self.recuo = x_min, x_max, y, vel, recuo
        self.x, self.y, self.dir = float(x_ini), float(y), 1

    @property
    def rect(self):
        return pygame.Rect(int(self.x) - 7, int(self.y) - 7, 14, 14)

    def atualizar(self, dt, outros):
        conflito = any(0 < (o.x - self.x) * self.dir < 90 and abs(o.y - self.y) < 40 for o in outros)
        alvo_y = self.y0 + (self.recuo if conflito else 0)
        self.y += clamp(alvo_y - self.y, -60 * dt, 60 * dt)
        self.x += self.dir * self.vel * dt
        if self.x > self.x_max:
            self.x, self.dir = self.x_max, -1
        elif self.x < self.x_min:
            self.x, self.dir = self.x_min, 1

    def desenhar(self, tela):
        cx, cy = int(self.x), int(self.y)
        pygame.draw.circle(tela, (240, 205, 160), (cx, cy - 6), 4)
        pygame.draw.rect(tela, (200, 90, 140), (cx - 4, cy - 2, 8, 10), border_radius=3)


# ==================== PLANEJADOR (A*) ====================
class Planejador:
    C = 10   # tamanho da célula (px)
    VIZ = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
           (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]

    def __init__(self):
        self.cols = AREA_MAPA_LARGURA // self.C
        self.rows = ALTURA // self.C
        self.bloq = []
        self.custo = []

    def construir(self, obstaculos, edificios, pisos):
        C = self.C
        obs = [o.rect.inflate(2 * (INFL_SUSPENSO if o.tipo == "suspenso" else INFL_SOLO),
                              2 * (INFL_SUSPENSO if o.tipo == "suspenso" else INFL_SOLO)) for o in obstaculos]
        edf = [e.inflate(2 * INFL_EDIFICIO, 2 * INFL_EDIFICIO) for e in edificios]
        self.bloq = [[False] * self.cols for _ in range(self.rows)]
        self.custo = [[1.0] * self.cols for _ in range(self.rows)]
        for r in range(self.rows):
            cy = r * C + C // 2
            for c in range(self.cols):
                cx = c * C + C // 2
                b = (cx < 22 or cx > AREA_MAPA_LARGURA - 22 or cy < 34 or cy > ALTURA - 34)
                if not b and RUA_Y - 6 <= cy <= RUA_FIM + 6 and not (FAIXA_X + 16 <= cx <= FAIXA_X + FAIXA_LARGURA - 16):
                    b = True
                if not b:
                    b = any(e.collidepoint(cx, cy) for e in edf) or any(o.collidepoint(cx, cy) for o in obs)
                self.bloq[r][c] = b
                if not b and any(p.collidepoint(cx, cy) for p in pisos):
                    self.custo[r][c] = 0.45

    def cel(self, p):
        return (clamp(int(p[0] // self.C), 0, self.cols - 1), clamp(int(p[1] // self.C), 0, self.rows - 1))

    def livre(self, c):
        return 0 <= c[0] < self.cols and 0 <= c[1] < self.rows and not self.bloq[c[1]][c[0]]

    def livre_proximo(self, c, raio=25):
        if self.livre(c):
            return c
        for k in range(1, raio + 1):
            melhor, md = None, 1e9
            for dr in range(-k, k + 1):
                for dc in range(-k, k + 1):
                    if max(abs(dc), abs(dr)) != k:
                        continue
                    n = (c[0] + dc, c[1] + dr)
                    if self.livre(n) and dc * dc + dr * dr < md:
                        melhor, md = n, dc * dc + dr * dr
            if melhor:
                return melhor
        return None

    def planejar(self, origem, destino):
        s = self.livre_proximo(self.cel(origem))
        g = self.livre_proximo(self.cel(destino))
        if s is None or g is None:
            return None

        def h(c):
            dx, dy = abs(c[0] - g[0]), abs(c[1] - g[1])
            return 0.45 * (max(dx, dy) + 0.4142 * min(dx, dy))

        abertos = [(h(s), 0, s)]
        gc = {s: 0.0}
        pai = {s: None}
        fechados = set()
        cont = 0
        while abertos:
            _, _, atual = heapq.heappop(abertos)
            if atual in fechados:
                continue
            if atual == g:
                break
            fechados.add(atual)
            for dc, dr, cm in self.VIZ:
                n = (atual[0] + dc, atual[1] + dr)
                if not self.livre(n):
                    continue
                if dc and dr and (not self.livre((atual[0] + dc, atual[1])) or not self.livre((atual[0], atual[1] + dr))):
                    continue
                novo = gc[atual] + cm * 0.5 * (self.custo[atual[1]][atual[0]] + self.custo[n[1]][n[0]])
                if novo < gc.get(n, 1e18):
                    gc[n] = novo
                    pai[n] = atual
                    cont += 1
                    heapq.heappush(abertos, (novo + h(n), cont, n))
        if g not in pai:
            return None
        cels = []
        c = g
        while c is not None:
            cels.append(c)
            c = pai[c]
        cels.reverse()
        C = self.C
        pts = [(c[0] * C + C / 2, c[1] * C + C / 2) for c in cels[1:]]
        if not pts:
            pts = [tuple(destino)]
        elif math.hypot(pts[-1][0] - destino[0], pts[-1][1] - destino[1]) < 25:
            pts[-1] = tuple(destino)
        return pts


# ==================== TUTOR (PESSOA GUIADA) ====================
class Tutor:
    raio = 12
    GUIA = 42.0

    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)
        self.angulo = 0.0

    def atualizar(self, robo, mundo, dt):
        dx, dy = robo.x - self.x, robo.y - self.y
        d = math.hypot(dx, dy)
        if d > 1e-6:
            self.angulo = math.atan2(dy, dx)
        if d > self.GUIA:
            passo = min(d - self.GUIA, robo.BASE * 1.3 * dt)
            nx, ny = self.x + dx / d * passo, self.y + dy / d * passo
            self.x, self.y, _ = aplicar_barreira(self.x, self.y, nx, ny, mundo.travessia_ativa)

    def tensao(self, robo):
        return max(0.0, math.hypot(robo.x - self.x, robo.y - self.y) - self.GUIA)

    def desenhar(self, tela, robo):
        pygame.draw.line(tela, ARNES_GUIA, (robo.x, robo.y), (self.x, self.y), 4)
        pygame.draw.circle(tela, (235, 235, 240), (int(self.x), int(self.y)), self.raio)
        pygame.draw.circle(tela, PRETO, (int(self.x), int(self.y)), self.raio, 2)
        fx = self.x + math.cos(self.angulo) * 7
        fy = self.y + math.sin(self.angulo) * 7
        pygame.draw.circle(tela, PRETO, (int(fx), int(fy)), 3)


# ==================== ROBÔ CÃO-GUIA ====================
class Robo:
    RAIO = 16
    BASE = 156.0
    ACEL = 320.0
    DESACEL = 900.0
    WMAX = 3.4

    def __init__(self, x, y, mundo):
        self.mundo = mundo
        self.x, self.y = float(x), float(y)
        self.angulo = 0.0
        self.v = 0.0
        self.alcance = 165
        self.n_raios = 19
        self.fov = math.radians(150)
        self.leituras = [(self.alcance, None, 0.0)] * self.n_raios
        self.hist = deque(maxlen=3)
        self.estado = "PARADO"
        self.status = "Escolha um destino (teclas 1-4 ou clique)."
        self.cor = AZUL_INFO
        self.passo = 0.0
        self.destino = None
        self.nome_destino = ""
        self.rota = None
        self.idx = 0
        self.versao_rota = -1
        self.t_desvio = 0.0
        self.travessia_autorizada = False

    @property
    def raio(self):
        return self.RAIO

    # ---------- destino / planejamento ----------
    def definir_destino(self, nome, ponto):
        self.nome_destino, self.destino = nome, tuple(ponto)
        self.rota = None
        self.mundo.metricas.reset_viagem()

    def replanejar(self):
        self.rota = self.mundo.planejador.planejar((self.x, self.y), self.destino)
        if self.rota is None:
            self.rota = []
        self.idx = 0
        self.versao_rota = self.mundo.versao
        self.t_desvio = 0.0

    # ---------- sensor ----------
    def varrer(self):
        """LiDAR 2D: raycast analítico + ruído gaussiano + falhas + filtro de mediana."""
        alvos = self.mundo.alvos_lidar()
        rng = self.mundo.rng
        a0 = self.angulo - self.fov / 2
        passo = self.fov / (self.n_raios - 1)
        bruto = []
        for i in range(self.n_raios):
            ang = a0 + i * passo
            x2 = self.x + math.cos(ang) * self.alcance
            y2 = self.y + math.sin(ang) * self.alcance
            d, tipo = float(self.alcance), None
            for rect, t in alvos:
                seg = rect.clipline(int(self.x), int(self.y), int(x2), int(y2))
                if seg:
                    dd = math.hypot(seg[0][0] - self.x, seg[0][1] - self.y)
                    if dd < d:
                        d, tipo = dd, t
            if tipo is not None:
                d = clamp(d + rng.gauss(0, 1.5), 4.0, self.alcance)
                if rng.random() < 0.02:       # falha esporádica do sensor
                    d, tipo = float(self.alcance), None
            bruto.append((d, tipo, ang))
        self.hist.append(bruto)
        filtrado = []
        for i in range(self.n_raios):
            amostras = (sorted((h[i][:2] for h in self.hist), key=lambda a: a[0])
                        if len(self.hist) == 3 else [bruto[i][:2]])
            d, t = amostras[len(amostras) // 2]
            filtrado.append((d, t, bruto[i][2]))
        self.leituras = filtrado

    def zonas(self, ignorar=("carro",)):
        L = self.leituras

        pl = self.mundo.planejador

        def relevante(d, t, a):
            if t in ignorar:
                return False
            if t in ("solo", "suspenso", "parede") and d >= 36:
                hx, hy = self.x + math.cos(a) * (d + 2), self.y + math.sin(a) * (d + 2)
                if not pl.livre(pl.cel((hx, hy))):
                    return False     # estático já no mapa: o A* cuida (hierarquia híbrida)
            return True

        def menor(s):
            v = [d for d, t, a in s if relevante(d, t, a)]
            return min(v) if v else float(self.alcance)

        esq, cen, dire = L[:6], L[7:12], L[13:]       # laterais 6+6; centro = 5 raios (±17°)
        susp = any(t == "suspenso" and d < 115 for d, t, a in L[6:13])
        return menor(esq), menor(cen), menor(dire), susp

    # ---------- segurança ----------
    def avaliar_perigo(self):
        m = self.mundo
        # 1) LiDAR: carro/pedestre muito perto na frente; qualquer coisa quase encostando
        lim_dyn = 20 + self.v * 0.25
        for d, t, a in self.leituras:
            if t is None or abs(angulo_diff(a, self.angulo)) > math.radians(22):
                continue
            if t in ("carro", "pedestre") and d < lim_dyn:   # estáticos: camada reativa (evita deadlock)
                return f"FREIO: {t} muito perto!", clamp(angulo_diff(a, self.angulo) / 1.3, -1, 1)
        # 2) zona varrida pelos carros (vale dentro e fora da faixa)
        frente = (self.x + math.cos(self.angulo) * 45, self.y + math.sin(self.angulo) * 45)
        for c in m.carros:
            if c.v < 25:
                continue
            z = c.zona_varrida(1.0)
            if (circulo_retangulo(self.x, self.y, self.RAIO, z) or circulo_retangulo(frente[0], frente[1], 10, z)
                    or circulo_retangulo(m.tutor.x, m.tutor.y, m.tutor.raio, z)):
                return "PARE! Veículo em rota de colisão.", 0.0
        return None

    def rota_entra_na_rua(self):
        if not self.rota:
            return False
        acum, px, py = 0.0, self.x, self.y
        for k in range(self.idx, len(self.rota)):
            wx, wy = self.rota[k]
            acum += math.hypot(wx - px, wy - py)
            px, py = wx, wy
            if acum > 130:
                break
            if RUA_Y <= wy <= RUA_FIM:
                return True
        return False

    # ---------- controle ----------
    def _set(self, estado, status, cor, som=None, pan=0.0, intervalo=None):
        m = self.mundo
        if cor == VERMELHO_PERIGO and self.cor != VERMELHO_PERIGO:
            m.metricas.alertas += 1
        if estado == "FREIO_EMERGENCIA" and self.estado != "FREIO_EMERGENCIA":
            m.metricas.freios += 1
        self.estado, self.status, self.cor = estado, status, cor
        if som:
            m.audio.tocar(som, pan, m.t, intervalo)

    def _seguir(self, v_max):
        r = self.rota
        # rastreia o ponto mais próximo da rota numa janela à frente (não trava se o waypoint for inalcançável)
        melhor, md = self.idx, 1e18
        for k in range(self.idx, min(len(r), self.idx + 25)):
            dk = math.hypot(r[k][0] - self.x, r[k][1] - self.y)
            if dk < md:
                melhor, md = k, dk
        self.idx = melhor
        while self.idx < len(r) - 1 and math.hypot(r[self.idx][0] - self.x, r[self.idx][1] - self.y) < 16:
            self.idx += 1
        j = self.idx
        while j < len(r) - 1 and math.hypot(r[j][0] - self.x, r[j][1] - self.y) < 34:
            j += 1
        tx, ty = r[j]
        diff = angulo_diff(math.atan2(ty - self.y, tx - self.x), self.angulo)
        w = clamp(diff * 4.0, -self.WMAX, self.WMAX)
        v = v_max * max(0.3, 1.0 - abs(diff) / 1.1)
        return v, w

    def _lado_desvio(self, esq, dire):
        """Sinal do giro (+1 = horário/direita, -1 = anti-horário/esquerda)."""
        ay = None
        if RUA_Y - 65 <= self.y < RUA_Y:
            ay = -1.0
        elif RUA_FIM < self.y <= RUA_FIM + 65:
            ay = 1.0
        if ay is not None and abs(math.cos(self.angulo)) > 0.25:
            # produto vetorial heading × (direção "para longe da rua"); corrige bug da v1
            return 1.0 if math.cos(self.angulo) * ay > 0 else -1.0
        return -1.0 if esq > dire else 1.0

    def decidir(self, dt):
        m = self.mundo
        sem = m.semaforo
        self.travessia_autorizada = False
        if self.destino is None:
            self._set("PARADO", "Escolha um destino (teclas 1-4 ou clique).", AZUL_INFO)
            return 0.0, 0.0
        if math.hypot(self.destino[0] - self.x, self.destino[1] - self.y) <= 25:
            if self.estado != "CHEGOU":
                m.audio.tocar("sucesso", 0.0, m.t, 0.0)
                m.metricas.viagens += 1
            self._set("CHEGOU", f"CHEGAMOS: {self.nome_destino}, com segurança!", VERDE_LIVRE)
            return 0.0, 0.0
        if self.rota is None or self.versao_rota != m.versao:
            self.replanejar()
        if not self.rota:
            self._set("SEM_ROTA", "Destino inalcançável com os obstáculos atuais.", VERMELHO_PERIGO, "perigo", 0.0, 2.0)
            return 0.0, 0.0
        perigo = self.avaliar_perigo()
        if perigo:
            self._set("FREIO_EMERGENCIA", perigo[0], VERMELHO_PERIGO, "perigo", perigo[1])
            return 0.0, 0.0

        if em_rua(self.y):       # corredor protegido: sem desvio lateral na pista
            resta = sem.tempo_restante_pedestre()
            apressar = (not sem.pedestre_livre()) or resta < 3.0
            v, w = self._seguir(self.BASE * (1.25 if apressar else 1.0))
            if apressar:
                self._set("ATRAVESSANDO", "Sinal fechando! Apressando a travessia.", LARANJA_ALERTA)
            else:
                self._set("ATRAVESSANDO", "Pedestre Verde! Atravessando na faixa.", VERDE_LIVRE)
            return v, w

        if self.rota_entra_na_rua():
            dist_cf = (RUA_Y - self.y) if self.y < RUA_Y else (self.y - RUA_FIM)
            if dist_cf < 80:
                ok, motivo = m.pode_iniciar_travessia(self)
                v, w = self._seguir(self.BASE)
                if not ok:
                    v = min(v, self.BASE * clamp((dist_cf - 12) / 45.0, 0.0, 1.0))
                    if abs(self.x - CENTRO_FAIXA_X) > 18:   # desliza pela calçada até o centro da faixa
                        alvo_a = 0.0 if CENTRO_FAIXA_X > self.x else math.pi
                        dif = angulo_diff(alvo_a, self.angulo)
                        w = clamp(dif * 4.0, -self.WMAX, self.WMAX)
                        v = self.BASE * 0.45 * max(0.15, 1.0 - abs(dif))
                    self._set("AGUARDANDO_SINAL", motivo, VERMELHO_PERIGO, "perigo", 0.0, 1.5)
                    return v, w
                self.travessia_autorizada = True
                self._set("ATRAVESSANDO", "Pedestre Verde! Iniciando travessia.", VERDE_LIVRE)
                return v, w
        return self._navegar_calcada(dt)

    def _navegar_calcada(self, dt):
        esq, cen, dire, susp = self.zonas()
        if cen < 82:
            lado = self._lado_desvio(esq, dire)
            self.t_desvio += dt
            if susp:
                self._set("DESVIANDO", "ALERTA CABEÇA: galho/toldo baixo! Desviando...", VERMELHO_PERIGO, "suspenso", 0.0)
            else:
                self._set("DESVIANDO", "OBSTÁCULO À FRENTE: contornando...", VERMELHO_PERIGO, "perigo", 0.0)
            v, w = self.BASE * 0.33, lado * 4.2
        else:
            v, w = self._seguir(self.BASE)
            if esq < 45:
                w += 2.2
                v *= 0.85
                self.t_desvio += dt
                self._set("DESVIANDO", "Obstáculo à esquerda. Deslocando à direita.", LARANJA_ALERTA, None)
            elif dire < 45:
                w -= 2.2
                v *= 0.85
                self.t_desvio += dt
                self._set("DESVIANDO", "Obstáculo à direita. Deslocando à esquerda.", LARANJA_ALERTA, None)
            else:
                self.t_desvio = 0.0
                self._set("SEGUINDO", f"Caminho livre. Guiando até: {self.nome_destino}.", VERDE_LIVRE)
        if self.t_desvio > 3.5:          # preso em mínimo local: replaneja
            self.rota = None
            self.t_desvio = 0.0
        return v, clamp(w, -self.WMAX * 1.3, self.WMAX * 1.3)

    def comando_manual(self, teclas):
        w = (teclas[pygame.K_RIGHT] - teclas[pygame.K_LEFT]) * 3.3
        v = self.BASE if teclas[pygame.K_UP] else 0.0
        ok, _ = self.mundo.pode_iniciar_travessia(self)
        self.travessia_autorizada = ok
        perigo = self.avaliar_perigo()
        if perigo:
            self._set("FREIO_EMERGENCIA", perigo[0], VERMELHO_PERIGO, "perigo", perigo[1])
            v = 0.0
        else:
            self._set("MANUAL", "Modo manual ASSISTIDO: segurança ativa.", AZUL_INFO)
        return v, w

    def atualizar(self, dt, autonomo, teclas):
        self.varrer()
        v_cmd, w_cmd = self.decidir(dt) if autonomo else self.comando_manual(teclas)
        tens = self.mundo.tutor.tensao(self)       # guia esticada reduz a velocidade
        if tens > 10:
            v_cmd *= max(0.0, 1.0 - (tens - 10) / 25.0)
        self.v += clamp(v_cmd - self.v, -self.DESACEL * dt, self.ACEL * dt)
        self.angulo += clamp(w_cmd, -self.WMAX * 1.3, self.WMAX * 1.3) * dt
        nx = self.x + math.cos(self.angulo) * self.v * dt
        ny = self.y + math.sin(self.angulo) * self.v * dt
        aut = self.mundo.travessia_ativa or self.travessia_autorizada
        nx, ny, bloqueado = aplicar_barreira(self.x, self.y, nx, ny, aut)
        if bloqueado:
            self.v = 0.0
        self.mundo.metricas.distancia += math.hypot(nx - self.x, ny - self.y)
        self.x, self.y = nx, ny
        self.passo += self.v * dt * 0.2

    # ---------- desenho ----------
    @staticmethod
    def _rot(cx, cy, px, py, a):
        s, c = math.sin(a), math.cos(a)
        dx, dy = px - cx, py - cy
        return (int(cx + dx * c - dy * s), int(cy + dx * s + dy * c))

    def desenhar(self, tela):
        for d, t, a in self.leituras:
            fx, fy = self.x + math.cos(a) * d, self.y + math.sin(a) * d
            pygame.draw.line(tela, VERDE_LIVRE if t is None else VERMELHO_PERIGO, (self.x, self.y), (fx, fy), 1)
            if t:
                pygame.draw.circle(tela, BRANCO, (int(fx), int(fy)), 3)
        bal = math.sin(self.passo) * 5
        for ox, oy in [(10 + bal, -14), (10 - bal, 14), (-12 - bal, -14), (-12 + bal, 14)]:
            px, py = self._rot(self.x, self.y, self.x + ox, self.y + oy, self.angulo)
            pygame.draw.circle(tela, DETALHE_ROBO, (px, py), 5)
        corpo = [self._rot(self.x, self.y, self.x + dx, self.y + dy, self.angulo)
                 for dx, dy in [(16, -11), (16, 11), (-18, 11), (-18, -11)]]
        pygame.draw.polygon(tela, CORPO_ROBO, corpo)
        pygame.draw.polygon(tela, BRANCO, corpo, 2)
        cab = self._rot(self.x, self.y, self.x + 21, self.y, self.angulo)
        pygame.draw.circle(tela, CABECA_ROBO, cab, 9)
        pygame.draw.circle(tela, DETALHE_ROBO, cab, 9, 2)
        for oy in (-10, 10):
            pygame.draw.circle(tela, DETALHE_ROBO, self._rot(self.x, self.y, self.x + 19, self.y + oy, self.angulo), 4)
        pygame.draw.circle(tela, LARANJA_ALERTA, self._rot(self.x, self.y, self.x + 3, self.y, self.angulo), 5)


# ==================== MUNDO ====================
class Mundo:
    def __init__(self, rng=None, audio=None, aleatorio=False):
        self.rng = rng or random.Random()
        self.audio = audio if audio is not None else AudioMudo()
        self.t = 0.0
        self.semaforo = Semaforo()
        self.edificios = [CASA_RECT, MERCADO_RECT, FARMACIA_RECT, PADARIA_RECT]
        self.paredes = [pygame.Rect(0, 0, AREA_MAPA_LARGURA, 15), pygame.Rect(0, ALTURA - 15, AREA_MAPA_LARGURA, 15),
                        pygame.Rect(0, 0, 15, ALTURA), pygame.Rect(AREA_MAPA_LARGURA - 15, 0, 15, ALTURA)]
        self.pisos = tateis()
        self.carros = [Carro(100, RUA_Y + 24, 204, (210, 60, 60), +1),
                       Carro(700, RUA_Y + 106, 186, (230, 165, 30), -1)]
        if aleatorio:
            self.carros[0].x = self.rng.uniform(0, 500)
            self.carros[1].x = self.rng.uniform(300, 700)
        self.pedestres = [Pedestre(170, 540, 190, 38, 300, -22), Pedestre(200, 540, 452, 34, 420, 20)]
        self.planejador = Planejador()
        self.versao = 0
        self.aviso = ("", 0.0)
        self.travessia_ativa = False
        self._sinal_flag = False
        self.metricas = Metricas()
        self.cooldown = {}
        self.obstaculos = []
        self.robo = Robo(PONTO_PARTIDA_CASA[0], PONTO_PARTIDA_CASA[1], self)
        self.tutor = Tutor(PONTO_PARTIDA_CASA[0], PONTO_PARTIDA_CASA[1] + Tutor.GUIA)  # fora da parede da casa
        if aleatorio:
            self._obstaculos_aleatorios()
        else:
            self.obstaculos = [
                Obstaculo(pygame.Rect(245, 65, 74, 48), "solo", "OBSTÁCULO"),
                Obstaculo(pygame.Rect(435, 110, 66, 44), "solo", "LIXEIRA"),
                Obstaculo(pygame.Rect(435, 535, 74, 48), "solo", "OBSTÁCULO"),
                Obstaculo(pygame.Rect(255, 485, 68, 44), "suspenso", "ALTO"),
            ]
        self.reconstruir()
        self.robo.definir_destino(*DESTINOS[pygame.K_1])

    # ----- cenário -----
    def reconstruir(self):
        self.versao += 1
        self.planejador.construir(self.obstaculos, self.edificios, self.pisos)

    def _obstaculos_aleatorios(self):
        pontos = [PONTO_PARTIDA_CASA] + [p for _, p in DESTINOS.values()]
        reservado = [pygame.Rect(FAIXA_X - 40, 28, FAIXA_LARGURA + 80, RUA_Y - 28),
                     pygame.Rect(FAIXA_X - 40, RUA_FIM, FAIXA_LARGURA + 80, ALTURA - RUA_FIM - 28)]
        for _ in range(60):
            alvo_n = self.rng.randint(4, 7)
            obs = []
            for _ in range(alvo_n * 12):
                if len(obs) >= alvo_n:
                    break
                topo = self.rng.random() < 0.5
                rect = pygame.Rect(self.rng.randint(190, 740), self.rng.randint(50, 200) if topo else self.rng.randint(450, 590), 68, 44)
                if rect.inflate(60, 60).collidelist(self.edificios) != -1:
                    continue
                if any(rect.colliderect(r) for r in reservado):
                    continue
                if any(rect.inflate(30, 30).colliderect(o.rect) for o in obs):
                    continue
                if any(math.hypot(rect.centerx - p[0], rect.centery - p[1]) < 90 for p in pontos):
                    continue
                tipo = self.rng.choice(["solo", "solo", "suspenso"])
                obs.append(Obstaculo(rect, tipo, "OBSTÁCULO" if tipo == "solo" else "ALTO"))
            self.obstaculos = obs
            self.reconstruir()
            if all(self.planejador.planejar(PONTO_PARTIDA_CASA, p) for _, p in DESTINOS.values()):
                return

    def alvos_lidar(self):
        return ([(w, "parede") for w in self.paredes] + [(e, "parede") for e in self.edificios]
                + [(o.rect, o.tipo) for o in self.obstaculos] + [(c.rect, "carro") for c in self.carros]
                + [(p.rect, "pedestre") for p in self.pedestres])

    # ----- regras de travessia -----
    def pode_iniciar_travessia(self, robo):
        sem = self.semaforo
        if not sem.pedestre_livre():
            return False, "PEDESTRE VERMELHO: aguardando sinal verde..."
        necessario = (RUA_ALTURA + 24 + Tutor.GUIA) / robo.BASE * 1.35 + 0.4
        if sem.tempo_restante_pedestre() < necessario:
            return False, "Sinal prestes a fechar: aguardando o próximo ciclo."
        if abs(robo.x - CENTRO_FAIXA_X) > 30:
            return False, "Alinhando com o centro da faixa..."
        for c in self.carros:
            if c.eta_faixa() < 2.2:
                return False, "PARE! Veículo em movimento perto da faixa..."
        return True, ""

    # ----- interação -----
    def _aviso(self, txt):
        self.aviso = (txt, self.t + 3.0)

    def clique_valido(self, pos, margem=30):
        x, y = pos
        if not (margem <= x <= AREA_MAPA_LARGURA - margem and 40 <= y <= ALTURA - 40):
            return False
        return not (RUA_Y - 6 <= y <= RUA_FIM + 6)

    def tentar_destino(self, pos):
        if not self.clique_valido(pos) or any(e.inflate(20, 20).collidepoint(pos) for e in self.edificios):
            self._aviso("Destino inválido: use apenas calçadas livres.")
            return
        if self.planejador.livre_proximo(self.planejador.cel(pos), 2) is None:
            self._aviso("Destino muito perto de obstáculo/edifício.")
            return
        self.robo.definir_destino("PONTO LIVRE", pos)
        self._aviso("Novo destino definido.")

    def tentar_obstaculo(self, pos):
        if not self.clique_valido(pos):
            self._aviso("Obstáculo só é permitido nas calçadas.")
            return
        rect = pygame.Rect(pos[0] - 34, pos[1] - 22, 68, 44)
        r, tu, dst = self.robo, self.tutor, self.robo.destino
        invalido = (rect.inflate(12, 12).collidelist(self.edificios) != -1
                    or rect.inflate(12, 12).colliderect(pygame.Rect(0, RUA_Y - 6, AREA_MAPA_LARGURA, RUA_ALTURA + 12))
                    or any(rect.colliderect(o.rect) for o in self.obstaculos)
                    or circulo_retangulo(r.x, r.y, r.RAIO + 30, rect)
                    or circulo_retangulo(tu.x, tu.y, tu.raio + 20, rect)
                    or (dst and circulo_retangulo(dst[0], dst[1], 40, rect)))
        if invalido:
            self._aviso("Posição inválida para obstáculo.")
            return
        tipo = self.rng.choice(["solo", "suspenso"])
        novo = Obstaculo(rect, tipo, "OBSTÁCULO" if tipo == "solo" else "ALTO")
        self.obstaculos.append(novo)
        self.reconstruir()
        if dst and self.planejador.planejar((r.x, r.y), dst) is None:
            self.obstaculos.remove(novo)
            self.reconstruir()
            self._aviso("Recusado: bloquearia o único caminho.")
        else:
            self._aviso("Obstáculo criado. Replanejando rota...")

    def remover_obstaculo(self, pos):
        for o in list(self.obstaculos):
            if o.rect.collidepoint(pos):
                self.obstaculos.remove(o)
                self.reconstruir()
                self._aviso("Obstáculo removido.")
                return

    # ----- simulação -----
    def _registrar(self, chave, tipo, cooldown):
        if self.t - self.cooldown.get(chave, -99.0) < cooldown:
            return
        self.cooldown[chave] = self.t
        if tipo == "colisao":
            self.metricas.colisoes += 1
            self._aviso(f"COLISÃO: {chave}")
        else:
            self.metricas.quase += 1

    def _checar_colisoes(self):
        r, tu = self.robo, self.tutor
        sol_robo = ([(o.rect, "robô×obstáculo") for o in self.obstaculos if o.tipo == "solo"]
                    + [(e, "robô×edifício") for e in self.edificios]
                    + [(c.rect, "robô×carro") for c in self.carros] + [(p.rect, "robô×pedestre") for p in self.pedestres])
        for rect, nome in sol_robo:
            if circulo_retangulo(r.x, r.y, r.RAIO, rect):
                self._registrar(nome, "colisao", 1.5)
            elif nome.endswith("carro") and folga_circulo_retangulo(r.x, r.y, r.RAIO, rect) < 14:
                self._registrar("qc_robo", "quase", 2.0)
        sol_tutor = ([(o.rect, "tutor×cabeça (suspenso)" if o.tipo == "suspenso" else "tutor×obstáculo") for o in self.obstaculos]
                     + [(e, "tutor×edifício") for e in self.edificios]
                     + [(c.rect, "tutor×carro") for c in self.carros] + [(p.rect, "tutor×pedestre") for p in self.pedestres])
        for rect, nome in sol_tutor:
            if circulo_retangulo(tu.x, tu.y, tu.raio, rect):
                self._registrar(nome, "colisao", 1.5)
            elif nome.endswith("carro") and folga_circulo_retangulo(tu.x, tu.y, tu.raio, rect) < 14:
                self._registrar("qc_tutor", "quase", 2.0)

    def step(self, dt, teclas=None, autonomo=True):
        self.t += dt
        self.semaforo.atualizar(dt)
        pessoas = [self.robo, self.tutor]
        for p in self.pedestres:
            p.atualizar(dt, pessoas)
        for c in self.carros:
            c.atualizar(dt, self.semaforo, self.carros, pessoas)
        self.travessia_ativa = em_rua(self.robo.y) or em_rua(self.tutor.y)
        if self.travessia_ativa and self.semaforo.fase == "TODOS_VERM_2" and not self._sinal_flag:
            self.metricas.sinal_fechou += 1
            self._sinal_flag = True
        if not self.travessia_ativa:
            self._sinal_flag = False
        self.robo.atualizar(dt, autonomo, teclas)
        self.tutor.atualizar(self.robo, self, dt)
        self._checar_colisoes()
        if self.robo.destino is not None and self.robo.estado != "CHEGOU":
            self.metricas.tempo += dt


# ==================== RENDERIZAÇÃO ====================
def quebrar_texto(texto, fonte, largura):
    linhas, atual = [], ""
    for pal in texto.split():
        teste = (atual + " " + pal).strip()
        if fonte.size(teste)[0] <= largura:
            atual = teste
        else:
            linhas.append(atual)
            atual = pal
    linhas.append(atual)
    return linhas


def desenhar_edificio(tela, rect, cor, rotulo, fonte, tipo):
    pygame.draw.rect(tela, (80, 84, 92), rect.move(4, 4), border_radius=6)
    pygame.draw.rect(tela, cor, rect, border_radius=6)
    pygame.draw.rect(tela, BRANCO, rect, 2, border_radius=6)
    if tipo == "casa":
        tel = [(rect.left - 4, rect.top + 6), (rect.centerx, rect.top - 12), (rect.right + 4, rect.top + 6)]
        pygame.draw.polygon(tela, COR_TELHADO_CASA, tel)
        pygame.draw.polygon(tela, BRANCO, tel, 1)
    else:
        pygame.draw.rect(tela, COR_TOLDO_MERCADO if tipo == "mercado" else (240, 240, 240),
                         (rect.left, rect.top, rect.width, 14), border_top_left_radius=6, border_top_right_radius=6)
    lbl = fonte.render(rotulo, True, BRANCO)
    tela.blit(lbl, (rect.centerx - lbl.get_width() // 2, rect.centery - 2))


def desenhar_ambiente(tela, mundo, fonte_peq):
    tela.fill(CALCADA_CLARA)
    pygame.draw.rect(tela, GRAMA, (0, 0, AREA_MAPA_LARGURA, 28))
    pygame.draw.rect(tela, GRAMA, (0, ALTURA - 28, AREA_MAPA_LARGURA, 28))
    pygame.draw.rect(tela, ASFALTO, (0, RUA_Y, AREA_MAPA_LARGURA, RUA_ALTURA))
    pygame.draw.line(tela, MEIO_FIO, (0, RUA_Y), (AREA_MAPA_LARGURA, RUA_Y), 4)
    pygame.draw.line(tela, MEIO_FIO, (0, RUA_FIM), (AREA_MAPA_LARGURA, RUA_FIM), 4)
    for x in range(15, AREA_MAPA_LARGURA - 20, 45):
        if not (FAIXA_X - 18 <= x <= FAIXA_X + FAIXA_LARGURA + 18):
            pygame.draw.line(tela, BRANCO, (x, RUA_Y + RUA_ALTURA // 2), (x + 22, RUA_Y + RUA_ALTURA // 2), 3)
    pygame.draw.line(tela, BRANCO, (FAIXA_X - 12, RUA_Y + 4), (FAIXA_X - 12, RUA_Y + RUA_ALTURA // 2 - 4), 4)
    pygame.draw.line(tela, BRANCO, (FAIXA_X + FAIXA_LARGURA + 12, RUA_Y + RUA_ALTURA // 2 + 4),
                     (FAIXA_X + FAIXA_LARGURA + 12, RUA_FIM - 4), 4)
    for y in range(RUA_Y + 12, RUA_FIM - 12, 22):
        pygame.draw.rect(tela, BRANCO, (FAIXA_X, y, FAIXA_LARGURA, 12))
    for p in mundo.pisos:
        pygame.draw.rect(tela, PISO_TATIL, p)
    desenhar_edificio(tela, CASA_RECT, COR_CASA, "CASA", fonte_peq, "casa")
    desenhar_edificio(tela, MERCADO_RECT, COR_MERCADO, "MERCADO", fonte_peq, "mercado")
    desenhar_edificio(tela, FARMACIA_RECT, COR_FARMACIA, "FARMÁCIA", fonte_peq, "outro")
    desenhar_edificio(tela, PADARIA_RECT, COR_PADARIA, "PADARIA", fonte_peq, "outro")


def desenhar_debug(tela, mundo):
    pl = mundo.planejador
    sup = pygame.Surface((AREA_MAPA_LARGURA, ALTURA), pygame.SRCALPHA)
    for r in range(pl.rows):
        for c in range(pl.cols):
            if pl.bloq[r][c]:
                sup.fill((255, 0, 0, 40), (c * pl.C, r * pl.C, pl.C, pl.C))
    tela.blit(sup, (0, 0))
    rota = mundo.robo.rota
    if rota and len(rota) > 1:
        pygame.draw.lines(tela, (255, 0, 255), False, [(mundo.robo.x, mundo.robo.y)] + [tuple(p) for p in rota[mundo.robo.idx:]], 2)


def desenhar_tudo(tela, mundo, fontes, estado_ui):
    f_tit, f_txt, f_peq = fontes
    desenhar_ambiente(tela, mundo, f_peq)
    if estado_ui["debug"]:
        desenhar_debug(tela, mundo)
    mundo.semaforo.desenhar(tela, f_peq)
    if mundo.robo.destino:
        pygame.draw.circle(tela, VERDE_LIVRE, (int(mundo.robo.destino[0]), int(mundo.robo.destino[1])), 20, 3)
        pygame.draw.circle(tela, VERDE_LIVRE, (int(mundo.robo.destino[0]), int(mundo.robo.destino[1])), 7)
    for o in mundo.obstaculos:
        r = o.rect
        if o.tipo == "suspenso":
            pygame.draw.rect(tela, (90, 95, 102), r.move(5, 5), border_radius=12)
            pygame.draw.rect(tela, ROXO_SUSPENSO, r, border_radius=12)
        elif o.rotulo == "LIXEIRA":
            pygame.draw.rect(tela, (45, 110, 75), r, border_radius=8)
        else:
            pygame.draw.rect(tela, (70, 74, 82), r, border_radius=5)
        pygame.draw.rect(tela, BRANCO, r, 2, border_radius=8)
        lbl = f_peq.render(o.rotulo, True, BRANCO)
        tela.blit(lbl, (r.centerx - lbl.get_width() // 2, r.centery - 6))
    for p in mundo.pedestres:
        p.desenhar(tela)
    for c in mundo.carros:
        c.desenhar(tela)
    mundo.tutor.desenhar(tela, mundo.robo)
    mundo.robo.desenhar(tela)

    # ----- painel lateral -----
    px = AREA_MAPA_LARGURA
    pygame.draw.rect(tela, PRETO, (px, 0, LARGURA - px, ALTURA))
    pygame.draw.line(tela, BRANCO, (px, 0), (px, ALTURA), 2)
    x0, y = px + 14, 12
    tela.blit(f_tit.render("TELEMETRIA DOG-IA v2", True, CORPO_ROBO), (x0, y))
    y += 30
    r, m, sem = mundo.robo, mundo.metricas, mundo.semaforo
    linhas = [
        (f"Modo: {'AUTÔNOMO (IA)' if estado_ui['autonomo'] else 'MANUAL assistido'}", BRANCO),
        (f"Estado: {r.estado}", AZUL_INFO),
        (f"Destino: {r.nome_destino or '-'}", BRANCO),
        (f"Pedestre: {'SIGA' if sem.pedestre_livre() else 'PARE'}  ({sem.fase})",
         VERDE_LIVRE if sem.pedestre_livre() else VERMELHO_PERIGO),
        (f"Áudio: {'LIGADO' if mundo.audio.ativo else 'MUDO'} (M)", VERDE_LIVRE if mundo.audio.ativo else LARANJA_ALERTA),
    ]
    for txt, cor in linhas:
        tela.blit(f_txt.render(txt, True, cor), (x0, y))
        y += 19
    y += 6
    caixa_h = 72
    pygame.draw.rect(tela, r.cor, (x0, y, 232, caixa_h), border_radius=8)
    yy = y + 8
    for ln in quebrar_texto(r.status, f_txt, 214)[:4]:
        tela.blit(f_txt.render(ln, True, PRETO), (x0 + 9, yy))
        yy += 16
    y += caixa_h + 6
    if mundo.t < mundo.aviso[1]:
        for ln in quebrar_texto(mundo.aviso[0], f_peq, 232)[:2]:
            tela.blit(f_peq.render(ln, True, (255, 230, 120)), (x0, y))
            y += 14
    y += 6
    tela.blit(f_txt.render("MÉTRICAS DA VIAGEM", True, BRANCO), (x0, y))
    y += 19
    for txt in [f"Tempo: {m.tempo:5.1f} s   Dist.: {m.distancia / 10:5.1f} m",
                f"Alertas: {m.alertas}   Freios: {m.freios}",
                f"Quase-colisões: {m.quase}   Colisões: {m.colisoes}",
                f"Sinal fechou na travessia: {m.sinal_fechou}",
                f"Viagens concluídas: {m.viagens}"]:
        cor = VERMELHO_PERIGO if ("Colisões: " in txt and m.colisoes) else BRANCO
        tela.blit(f_peq.render(txt, True, cor), (x0, y))
        y += 15
    y += 8
    ctrl = ["DESTINOS: 1 Mercado  2 Farmácia", "          3 Padaria  4 Casa",
            "Clique esq: novo destino (calçada)", "Clique dir: criar obstáculo",
            "Clique meio: remover obstáculo", "ESPAÇO: autônomo / manual (setas)",
            "M: áudio   P: pausa   G: debug A*", "T: carro infrator   R: reiniciar",
            "F11 / ESC: tela cheia"]
    tela.blit(f_txt.render("CONTROLES", True, BRANCO), (x0, y))
    y += 18
    for txt in ctrl:
        tela.blit(f_peq.render(txt, True, (200, 205, 215)), (x0, y))
        y += 14
    if estado_ui["pausado"]:
        lbl = f_tit.render("PAUSADO", True, LARANJA_ALERTA)
        tela.blit(lbl, (AREA_MAPA_LARGURA // 2 - lbl.get_width() // 2, 20))


# BENCHMARK HEADLESS
def benchmark(n, seed, limite=240.0):
    pygame.init()
    print(f"Benchmark: {n} episódios, seed={seed} (cenários aleatórios, destino aleatório)")
    res = []
    for ep in range(n):
        rng = random.Random(seed + ep)
        mundo = Mundo(rng, AudioMudo(), aleatorio=True)
        mundo.semaforo.i = rng.randrange(len(Semaforo.FASES))
        mundo.semaforo.t = rng.random() * Semaforo.FASES[mundo.semaforo.i][1] * 0.99
        if rng.random() < 0.3:
            for c in mundo.carros:
                c.infrator = True
        key = rng.choice([pygame.K_1, pygame.K_2, pygame.K_3])
        mundo.robo.definir_destino(*DESTINOS[key])
        while mundo.t < limite and mundo.robo.estado != "CHEGOU":
            mundo.step(1 / FPS)
        m = mundo.metricas
        ok = mundo.robo.estado == "CHEGOU"
        res.append((ok, m))
        print(f"  ep {ep:02d} -> {mundo.robo.nome_destino:9s} {'OK ' if ok else 'FALHA'} "
              f"t={m.tempo:6.1f}s colisões={m.colisoes} quase={m.quase} freios={m.freios} "
              f"sinal_fechou={m.sinal_fechou} infrator={mundo.carros[0].infrator}")
    ok = [r for r in res if r[0]]
    print("-" * 60)
    print(f"Sucesso: {len(ok)}/{n} ({100 * len(ok) / n:.0f}%)")
    if ok:
        print(f"Tempo médio (sucessos): {statistics.mean(r[1].tempo for r in ok):.1f} s")
    print(f"Colisões totais: {sum(r[1].colisoes for r in res)} | "
          f"quase-colisões: {sum(r[1].quase for r in res)} | "
          f"sinal fechou na travessia: {sum(r[1].sinal_fechou for r in res)}")
    pygame.quit()


# ==================== PROGRAMA PRINCIPAL ====================
def main():
    pygame.init()
    tela = pygame.display.set_mode((LARGURA, ALTURA), pygame.SCALED | pygame.RESIZABLE)
    pygame.display.set_caption("DOG-IA v2 — Cão-guia robô (F11: tela cheia)")
    clock = pygame.time.Clock()
    fontes = (pygame.font.SysFont("arial", 18, bold=True), pygame.font.SysFont("arial", 14),
              pygame.font.SysFont("arial", 11, bold=True))
    audio = SistemaAudio()
    mundo = Mundo(random.Random(), audio)
    ui = {"autonomo": True, "pausado": False, "debug": False}
    tela_cheia = False

    rodando = True
    while rodando:
        dt = min(clock.tick(FPS) / 1000.0, 0.05)
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                rodando = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.pos[0] < AREA_MAPA_LARGURA:
                if ev.button == 1:
                    mundo.tentar_destino(ev.pos)
                elif ev.button == 3:
                    mundo.tentar_obstaculo(ev.pos)
                elif ev.button == 2:
                    mundo.remover_obstaculo(ev.pos)
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_F11 or (ev.key == pygame.K_ESCAPE and tela_cheia):
                    pygame.display.toggle_fullscreen()
                    tela_cheia = not tela_cheia
                elif ev.key == pygame.K_SPACE:
                    ui["autonomo"] = not ui["autonomo"]
                    if ui["autonomo"]:
                        mundo.robo.rota = None
                elif ev.key == pygame.K_m:
                    audio.ativo = not audio.ativo
                elif ev.key == pygame.K_p:
                    ui["pausado"] = not ui["pausado"]
                elif ev.key == pygame.K_g:
                    ui["debug"] = not ui["debug"]
                elif ev.key == pygame.K_t:
                    novo = not mundo.carros[0].infrator
                    for c in mundo.carros:
                        c.infrator = novo
                    mundo._aviso("Carros INFRATORES ligados." if novo else "Carros respeitam o semáforo.")
                elif ev.key == pygame.K_r:
                    mundo = Mundo(random.Random(), audio)
                elif ev.key in DESTINOS:
                    mundo.robo.definir_destino(*DESTINOS[ev.key])
                    ui["autonomo"] = True
        if not ui["pausado"]:
            mundo.step(dt, pygame.key.get_pressed(), ui["autonomo"])
        desenhar_tudo(tela, mundo, fontes, ui)
        pygame.display.flip()
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DOG-IA v2 — cão-guia robô")
    ap.add_argument("--benchmark", type=int, default=0, help="roda N episódios headless e imprime métricas")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    if args.benchmark:
        benchmark(args.benchmark, args.seed)
    else:
        main()