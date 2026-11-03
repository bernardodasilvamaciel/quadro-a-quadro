# quadro-a-quadro

Mede a latência entre um toque na tela do celular e a resposta de uma lâmpada (acender ou
apagar), quadro a quadro, em vídeo de câmera lenta.

*Frame-by-frame latency measurement, from a tap on the phone to a lamp turning on or off, in
slow-motion video. The interface is in Portuguese.*

## Como funciona

1. Um segundo celular filma em câmera lenta (120 ou 240 fps), no mesmo quadro, a tela do
   celular que aciona o dispositivo e a lâmpada.
2. Você abre o vídeo numa página no navegador, anda quadro a quadro e marca, em cada
   acionamento:
   - o **início** do acionamento, por exemplo o primeiro quadro do toque;
   - o quadro em que a lâmpada **acende** ou **apaga**.
3. A latência é a diferença entre os tempos desses dois quadros. O tempo de cada quadro vem do
   carimbo de tempo (PTS) gravado no arquivo, e não de "número do quadro ÷ taxa". Assim a conta
   continua certa mesmo quando o celular varia o intervalo entre quadros.
4. As latências vão para uma planilha CSV que abre direto no Excel.

Os vídeos e as marcações ficam só no seu computador. A página é servida em `127.0.0.1` e não
envia nada para fora.

### Por que marcar à mão

Muitos apps só mudam o botão na tela depois que o dispositivo confirma o novo estado, então o
botão pode mudar depois da lâmpada e não serve como início. O toque precisa ser visto no
vídeo. No Android, a opção do desenvolvedor **Localização do ponteiro** desenha linhas na tela
no instante do toque, e isso facilita achar esse quadro.

## Instalação

Precisa de Python 3.12 ou mais novo. Foi testado no Windows com Python 3.14.

```powershell
git clone https://github.com/bernardodasilvamaciel/quadro-a-quadro.git
cd quadro-a-quadro
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No Linux ou no macOS, troque `.\.venv\Scripts\python.exe` por `.venv/bin/python`.

## Uso

```powershell
.\.venv\Scripts\python.exe interface_web.py --pasta C:\caminho\dos\videos
```

O navegador abre em `http://127.0.0.1:8765`, com a lista dos `.mp4` da pasta e das subpastas.
Deixe o terminal aberto enquanto usa; **Ctrl+C** encerra.

| Opção | Padrão | Faz |
|---|---|---|
| `--pasta` | a pasta atual | Onde procurar os vídeos |
| `--porta` | 8765 | Porta da página |
| `--sem-navegador` | | Não abre o navegador sozinho |

**Confira a taxa no topo da página.** Ela precisa ser a da câmera lenta (120, 240 fps). Se
aparecer cerca de 30 fps num vídeo gravado em câmera lenta, o celular reescreveu os tempos
para a reprodução lenta, e as latências saem erradas. O comando
`python latencia.py info VIDEO.mp4` mostra os detalhes de tempo do arquivo.

### Marcar um vídeo

1. Escolha o vídeo na lista do topo.
2. **Opcional, mas economiza tempo:** clique em **Marcar lâmpada** e arraste um retângulo em
   volta da lâmpada. A página acha sozinha cada vez que ela acende ou apaga, e daí em diante
   **N** e **P** pulam direto para a próxima e a anterior mudança.
3. Para cada acionamento:
   1. Ache o início do acionamento e tecle **1**.
   2. Avance até a lâmpada mudar:
      - no **primeiro quadro com luz**, tecle **2** (acendeu);
      - no **primeiro quadro em que ela começa a apagar**, tecle **3** (apagou).
   3. A latência entra na tabela, e o resumo (média, desvio, mínimo, máximo) se atualiza,
      separado em Acender e Apagar. Se a lâmpada tiver sido marcada no passo 2, a página avisa
      quando o tipo escolhido não bate com a mudança detectada.
4. A linha do tempo embaixo do vídeo mostra o que já foi feito:
   - blocos amarelos são os acendimentos marcados, e azuis os desligamentos;
   - laranja é um início ainda sem fim;
   - os tracinhos no alto são as mudanças da lâmpada detectadas.

   Clique em qualquer ponto ou bloco para ir até ele.
5. Tudo é salvo na hora. Se fechar e reabrir, as marcações voltam.

### Teclas

| Tecla | Faz |
|---|---|
| ← → ou A D | 1 quadro |
| ↑ ↓ ou W S | 24 quadros |
| F R | 240 quadros |
| 1 | Marca o início do acionamento |
| 2 | Lâmpada acendeu: salva o acionamento como `ligar` |
| 3 | Lâmpada apagou: salva o acionamento como `desligar` |
| N P | Próxima e anterior mudança da lâmpada (depois de Marcar lâmpada) |
| Z | Referência: mostra quantos quadros e ms se passaram desde ela |
| U | Desfaz o último início ou o último acionamento |

Na tabela, **ir** volta ao início de um acionamento e **×** apaga ele. Para ver detalhes
pequenos, use o seletor de zoom.

### Limpar

- **Limpar**, ao lado de Marcar lâmpada, apaga a região da lâmpada e as mudanças detectadas.
  As marcações continuam.
- **Apagar marcações**, embaixo do resumo, apaga todos os acionamentos do vídeo.

### Arquivos gerados

Ficam na pasta de cada vídeo:

| Arquivo | O que guarda |
|---|---|
| `NOME_DO_VIDEO_latencias.csv` | Os acionamentos marcados |
| `NOME_DO_VIDEO_manual.json` | A região da lâmpada e as mudanças detectadas |
| `latencias_todos.csv` | Na pasta dos vídeos: todos os acionamentos juntos, gerado pelo botão **Exportar tudo** |

Colunas do CSV:

| Coluna | O que é |
|---|---|
| `video` | Nome do arquivo de vídeo |
| `acionamento` | Número do acionamento, em ordem |
| `comando` | `ligar` ou `desligar` |
| `quadro_inicio`, `t_inicio_ms` | Quadro e tempo do início |
| `quadro_lampada`, `t_lampada_ms` | Quadro e tempo da mudança da lâmpada |
| `quadros` | Diferença em quadros |
| `latencia_ms` | Latência em milissegundos |

O CSV usa `;` como separador, vírgula decimal e UTF-8 com BOM, para abrir direto no Excel em
português. No pandas: `pd.read_csv(arquivo, sep=";", decimal=",")`.

### Precisão

Cada marcação tem incerteza de ±1 quadro em cada ponta: ±4,2 ms a 240 fps e ±8,3 ms a
120 fps. Grave na maior taxa que o celular permitir.

## Modo automático (experimental)

`latencia.py` tenta achar os dois quadros sozinho: o início pela mudança do botão do app e o
fim pela luz da lâmpada. Ele só serve quando o app muda o botão no instante do toque (veja
[Por que marcar à mão](#por-que-marcar-à-mão)). O passo a passo está em
[docs/modo-automatico.md](docs/modo-automatico.md).

`validar_sintetico.py` gera vídeos sintéticos com latências conhecidas e confere se o modo
automático as encontra com erro de no máximo um quadro.

## Arquivos

| Arquivo | O que é |
|---|---|
| `interface_web.py`, `interface_web.html` | A interface web de marcação |
| `latencia.py` | Leitura dos vídeos e dos tempos de cada quadro, e o modo automático |
| `validar_sintetico.py` | Validação do modo automático com vídeos sintéticos |
| `requirements.txt` | Dependências |

## Licença

[MIT](LICENSE).
