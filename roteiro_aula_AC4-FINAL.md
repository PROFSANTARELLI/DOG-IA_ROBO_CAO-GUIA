# Guia Completo da Aula: Engenharia de Robôs Autônomos com DOG-IA (V1 $\rightarrow$ V2)

## 1. Visão Geral da Aula
* **Tema:** Navegação Autônoma Híbrida, Sensoriamento Ruidoso, Segurança Preditiva e Controle de Robôs Assistivos.
* **Ponto de Partida:** Código base funcional **DOG-IA V1** (navegação puramente reativa em calçada estática).
* **Objetivo Final:** Os alunos devem evoluir a arquitetura do código até alcançar os requisitos da **DOG-IA V2** e responder às **5 Questões Técnicas** preparatórias para a avaliação.

---

## 2. Roteiro de Desenvolvimento Prático (Evolução V1 $\rightarrow$ V2)

Divida a turma em equipes. Os alunos recebem o código da **V1** e devem implementar progressivamente os **4 Marcos de Engenharia** abaixo:

### Marco 1 — Ambiente Urbano Dinâmico, Semáforo e Barreira de Meio-Fio
**Problema da V1:** O robô atravessa a avenida diretamente em direção ao alvo sem respeitar a faixa de pedestres, semáforos ou veículos.
* **Requisitos Técnicos:**
  1. Implementar o ciclo semafórico completo com fases de segurança **All-Red** (`VERDE` $\rightarrow$ `AMARELO` $\rightarrow$ `TODOS_VERM_1` $\rightarrow$ `PEDESTRE` $\rightarrow$ `TODOS_VERM_2`) utilizando tempo simulado (`dt` em segundos, independente do FPS).
  2. Criar veículos em duas mãos opostas com cinemática realista: aceleração (`ACEL`), desaceleração (`DESACEL`), distância de seguimento entre carros e frenagem antes da linha de retenção da faixa.
  3. Adicionar **veículos infratores** que ignoram o sinal vermelho para testar a segurança ativa do robô.
  4. Implementar uma barreira física (`aplicar_barreira`) que impeça o robô de descer o meio-fio fora da faixa de pedestres ou quando a travessia não estiver autorizada.

### Marco 2 — Arquitetura Híbrida: Planejamento Global (A*) + Evasão Local
**Problema da V1:** A navegação puramente reativa fica presa em mínimos locais (obstáculos em "U") e não otimiza o uso do piso tátil nem múltiplos destinos.
* **Requisitos Técnicos:**
  1. Discretizar o mapa em uma grade de ocupação (células de $10 \times 10\text{ px}$) e implementar o algoritmo de busca **A\*** com heurística octile/euclidiana.
  2. Aplicar o conceito de **Espaço de Configuração (*C-Space*)** com inflação assimétrica: inflar obstáculos de solo (`30 px`), edificações (`22 px`) e dar margem maior para obstáculos suspensos (`38 px`), protegendo a cabeça do tutor humano.
  3. Atribuir custo reduzido (`0.45`) às células sobre o **piso tátil amarelo**, fazendo o planejador preferir rotas acessíveis.
  4. Configurar pontos de destino selecionáveis (`1`: Mercado, `2`: Farmácia, `3`: Padaria, `4`: Casa) e replanejamento automático caso o robô fique preso em desvio local por mais de `3,5 s`.

### Marco 3 — Sensoriamento LiDAR Realista e Segurança Preditiva
**Problema da V1:** O sensor faz varredura perfeita sem ruído e o robô não antecipa trajetórias de veículos em movimento.
* **Requisitos Técnicos:**
  1. Substituir o loop discreto de passos por interseção analítica de segmentos (`rect.clipline`), adicionando **ruído gaussiano** ($\sigma = 1,5\text{ px}$) e taxa de falha (*dropout*) de $2\%$.
  2. Aplicar um **filtro de mediana temporal** sobre uma janela deslizante de 3 quadros (`deque(maxlen=3)`) para eliminar leituras fantasmas.
  3. Implementar predição de colisão por **zona varrida (*swept volume*)**: projetar o retângulo futuro de cada veículo em movimento ($\Delta t = 1,0\text{ s}$) e acionar `FREIO_EMERGENCIA` caso intercepte o robô ou o tutor.

### Marco 4 — Acoplamento Físico do Tutor, Áudio Estéreo e Telemetria
**Problema da V1:** O usuário acompanhante é desenhado por deslocamento rígido fixo e não há feedback espacial nem métricas de desempenho.
* **Requisitos Técnicos:**
  1. Modelar o **Tutor** como entidade independente conectada pelo arnês (`GUIA = 42 px`). Se a distância entre o robô e o tutor ultrapassar a folga (`tensão > 10 px`), reduzir proporcionalmente a velocidade do robô para não puxar o usuário.
  2. Adicionar alertas sonoros sintéticos em **estéreo (`pan` $-1$ a $+1$)** para indicar auditivamente o lado do obstáculo.
  3. Registrar métricas da viagem (tempo, distância percorrida, alertas vermelhos, freios de emergência e viagens concluídas) e suporte a execução *headless* (`--benchmark`).

---

## 3. Lista Preparatória para a Prova (Versão do Aluno — Sem Gabarito)

**Instruções:** Responda de forma objetiva e fundamentada nos conceitos de robótica móvel aplicados durante o desenvolvimento do **DOG-IA V2**.

1. **Arquitetura Híbrida de Navegação (Global vs. Local):**
   Na Versão 1, o robô utiliza apenas leitura imediata do LiDAR para desviar de obstáculos e apontar para o alvo. Na Versão 2, o sistema combina um planejador **A\*** em grade com a evasão reativa local. Qual limitação crítica da navegação puramente reativa é resolvida por essa arquitetura híbrida e por que o planejador global sozinho também não basta em um ambiente urbano?

2. **Espaço de Configuração (*C-Space*) e Inflação Assimétrica:**
   Ao construir a malha de navegação do algoritmo A*, os obstáculos sofrem uma inflação antes da busca (`INFL_SOLO = 30 px` e `INFL_SUSPENSO = 38 px`). Explique o conceito de **Espaço de Configuração (*C-Space*)** por trás dessa técnica e justifique tecnicamente por que obstáculos suspensos exigem uma margem maior do que obstáculos no solo.

3. **Filtragem de Ruído em Sensores (Mediana vs. Média Móvel):**
   Sensores LiDAR reais sofrem com ruído térmico e falhas esporádicas de retorno (*dropouts*, quando o feixe retorna alcance máximo). Por que um **filtro de mediana temporal** sobre as últimas 3 amostras é tecnicamente superior a uma **média móvel simples** para tratar leituras de LiDAR em robôs móveis?

4. **Segurança Preditiva e Cinemática (Zona Varrida):**
   Em cruzamentos com veículos em alta velocidade (incluindo infratores que avançam o sinal vermelho), verificar apenas a sobreposição da posição atual do carro com a posição atual do robô é insuficiente. Como o cálculo da **zona varrida (*swept volume*)** resolve esse risco e quais variáveis físicas do veículo são indispensáveis para sua formulação?

5. **Máquina de Estados Finitos (FSM) e Prevenção de *Deadlocks*:**
   A tomada de decisão do robô é estruturada como uma Máquina de Estados Finitos hierárquica (`FREIO_EMERGENCIA` > `ATRAVESSANDO` > `AGUARDANDO_SINAL` > `DESVIANDO` > `SEGUINDO`). Explique:
   * a) Como o sistema evita ficar preso indefinidamente em oscilação ou *deadlock* no estado `DESVIANDO`;
   * b) Por que o semáforo exige uma fase intermediária de **todos vermelhos (*All-Red*)** entre o fechamento para os carros e a abertura para os pedestres.

---

## 4. Gabarito Comentado e Critérios de Correção (Versão do Professor)

### Questão 1 — Arquitetura Híbrida de Navegação
* **Resposta Esperada:** A navegação puramente reativa (V1) sofre com **mínimos locais** (como obstáculos côncavos em formato de "U" ou corredores bloqueados), nos quais o robô oscila ou fica preso por buscar apenas a direção vetorial direta ao destino. O planejador global **A\*** resolve esse problema encontrando o caminho topológico completo ao redor de barreiras conhecidas. Contudo, o A* sozinho não basta porque opera sobre uma representação estática do mapa e não reage em tempo real a **agentes dinâmicos e imprevisíveis** (carros furando o sinal, pedestres ou novos obstáculos inseridos durante a execução), função desempenhada pela camada reativa local.

### Questão 2 — Espaço de Configuração (*C-Space*) e Inflação Assimétrica
* **Resposta Esperada:** No **Espaço de Configuração (*C-Space*)**, inflar os obstáculos pelo raio de segurança do robô permite reduzi-lo matematicamente a um **ponto adimensional** durante a busca em grafo, garantindo que qualquer caminho livre encontrado no grid mantenha o corpo físico do robô afastado de colisões. A margem maior para **obstáculos suspensos** (`38 px` contra `30 px`) justifica-se pela **ergonomia e cinemática do conjunto robô-tutor**: embora o cão-robô seja baixo e passe sob galhos ou toldos, ele guia uma pessoa acoplada atrás de si pelo arnês. Como o tutor segue com inércia/atraso nas curvas e tem altura elevada, uma folga lateral maior evita que a cabeça do tutor colida com o obstáculo aéreo.

### Questão 3 — Filtragem de Ruído em Sensores
* **Resposta Esperada:** A **média móvel** é sensível a valores extremos (*outliers*): um único quadro com falha de retorno (*dropout* marcando `165 px`) ou ruído impulsivo puxa a média para cima e mascara um obstáculo próximo, atrasando a frenagem. O **filtro de mediana temporal** ordena as amostras recentes e seleciona o valor central, rejeitando completamente falhas isoladas de 1 quadro sem suavizar/borrar as transições bruscas de distância nas bordas dos obstáculos.

### Questão 4 — Segurança Preditiva e Cinemática
* **Resposta Esperada:** A **zona varrida (*swept volume*)** projeta o volume espacial que o veículo ocupará ao longo de um horizonte futuro $\Delta t$ (ex.: $1,0\text{ s}$), permitindo detectar interseções futuras com o robô ou o tutor e acionar a frenagem de emergência **antes** da colisão física. Para calculá-la, são indispensáveis a **posição atual**, o **vetor direção/sentido**, a **velocidade escalar ($v$)** e a dinâmica de frenagem do veículo ($d_{\text{freio}} = \frac{v^2}{2a}$).

### Questão 5 — Máquina de Estados Finitos (FSM) e Prevenção de *Deadlocks*
* **Resposta Esperada:**
  * **a) Prevenção de *Deadlock*:** O robô monitora o tempo contínuo em estado de evasão (`t_desvio`). Ao ultrapassar o limite (`3,5 s`), ele assume que está preso em um mínimo local ou diante de um bloqueio não mapeado e dispara o **replanejamento global**, invalidando a rota atual e recalculando um novo caminho A* a partir de sua posição corrente.
  * **b) Fase *All-Red*:** Como veículos em movimento possuem inércia e distância de frenagem finita ($d = v^2 / 2a$), abrir o sinal de pedestres no instante exato em que o sinal veicular fecha faria com que carros já comprometidos com o cruzamento ou em desaceleração invadissem a faixa simultaneamente com o início da travessia. A fase *All-Red* garante a limpeza física da interseção antes de autorizar o avanço do robô.