# Modo automático (experimental)

`latencia.py` lê o vídeo de câmera lenta, encontra sozinho os dois quadros de cada teste e
calcula a latência:

- **T0**: primeiro quadro em que o botão virtual do app muda (cor ou início da animação). Não é
  o contato do dedo.
- **T1**: primeiro quadro em que a lâmpada mostra luz.
- **Latência** = (número de quadros entre T0 e T1 / taxa de captura) × 1000.

> **Atenção:** muitos apps só mudam o botão depois que o dispositivo confirma o novo estado.
> Nesse caso o botão muda depois da lâmpada, e o T0 deste modo não marca o início do
> acionamento. Confira com o comando `ver` antes de confiar nos resultados. Se o botão mudar
> depois da lâmpada, use a interface web, descrita no [README](../README.md).

Também tem um modo de navegação quadro a quadro para conferir à mão.

Os comandos abaixo rodam da pasta do repositório, com o ambiente da
[instalação](../README.md#instalação). No Windows, troque `python` por
`.\.venv\Scripts\python.exe`; no Linux e no macOS, por `.venv/bin/python`.

## Passo 1: conferir a taxa de quadros do vídeo

```bash
python latencia.py info caminho/do/video.mp4
```

Mostra quantos quadros por segundo o arquivo realmente tem.

- Se a taxa pelos carimbos já for a da câmera lenta (ex.: 240 fps), **não use
  `--fps-captura`**. Os carimbos guardam o horário real de cada quadro e registram até os
  quadros que o celular descarta.
- Se aparecer o aviso de ~30 fps, o celular reescreveu os tempos para a reprodução lenta. Só
  nesse caso, acrescente `--fps-captura` com a taxa da câmera lenta (ex.: `--fps-captura 240`)
  em todos os comandos. O tempo passa a ser índice / taxa, e quadros descartados não entram
  na conta.

Para ter certeza, filme uma vez o cronômetro do computador por uns 10 s:

1. Abra o vídeo com o comando `ver`.
2. Marque a referência (tecla Z) num quadro e avance.
3. O `delta` mostrado precisa bater com o quanto o cronômetro andou.

## Passo 2: marcar as regiões (uma vez por posição da câmera)

```bash
python latencia.py marcar caminho/do/video.mp4
```

A janela pede uma região de cada vez; o nome aparece no alto.

1. Navegue até um quadro em que ela apareça bem.
2. Tecle **M** e arraste um retângulo.
3. ENTER confirma.

Por padrão, o script espera dois dispositivos, chamados `wifi` e `zigbee`. Para outros nomes,
ou para um só, use `--dispositivos` (ex.: `--dispositivos sala` ou
`--dispositivos sala cozinha`).

| Região | Onde marcar |
|---|---|
| `<dispositivo>/botao` | Um pedaço do botão do app que muda de cor e que **o dedo não cobre** (por exemplo, a metade de cima, se o dedo vem de baixo). Escolha um quadro sem o dedo na tela. |
| `<dispositivo>/lampada` | Justo em volta do bulbo da lâmpada. |

Os retângulos devem ser pequenos e justos.

Teclas:
- Navegação: setas ou A/D (1 quadro), W/S (24 quadros), F/R (240 quadros).
- Marcação: M (marcar), K (pular região), U (desfazer), ESC (salvar e sair).

As regiões vão para `regioes.json`, na pasta do vídeo (ou onde `--saida` mandar).

## Passo 3: analisar

```bash
python latencia.py analisar caminho/do/video.mp4 --regioes caminho/regioes.json --rotulo distancia_m=2 --anexar todos.csv --formato-br
```

Um vídeo pode ter quantos testes quiser, de todos os dispositivos: cada mudança do botão vira
um evento, pareado com a mudança da lâmpada do mesmo dispositivo.

- `--rotulo chave=valor` acrescenta colunas para identificar o teste.
- `--anexar` junta os eventos de todos os vídeos numa planilha só.
- `--formato-br` grava com `;` e vírgula decimal, para abrir direto no Excel.

Na pasta `<video>_analise` ficam:

- `eventos.csv`: uma linha por mudança do botão. As principais colunas são:
  - `quadro_t0` e `quadro_t1`: os quadros de T0 e T1.
  - `quadros`: T1 − T0 em quadros, o "número de quadros" da fórmula.
  - `latencia_ms`: a latência em milissegundos.
  - `quadros_perdidos`: tamanho, em quadros, de algum salto nos carimbos de tempo entre o T0 e
    o T1. Normalmente é 0. Se não for, refaça o teste: o salto pode ser um quadro perdido ou só
    um carimbo errado, e o vídeo não permite saber qual.
  - `comando`: `ligar` ou `desligar`, pelo sentido da mudança.
- `conferencia/evento_NNN.png`: o quadro anterior e o quadro detectado do T0 (botão) e do T1
  (lâmpada). **Olhe essas imagens**: são a prova de que a detecção está certa.
- `sinais.png`: o sinal de cada região ao longo do vídeo, para ver se a detecção faz sentido.
- `sinais.csv`: os sinais quadro a quadro.

A coluna `situacao` indica:

| Valor | Significado |
|---|---|
| `ok` | O botão mudou e a lâmpada respondeu depois. |
| `sem_resposta` | O botão mudou e a lâmpada não respondeu dentro de `--janela` (5 s). |
| `lampada_antes_do_botao` | A lâmpada mudou antes do botão do app. A latência sai negativa. |
| `sem_t0` | A lâmpada mudou sem o botão mudar. Em geral, a região do botão está coberta pelo dedo ou fora do lugar. |

## Passo 4: conferir à mão quando houver dúvida

```bash
python latencia.py ver caminho/do/video.mp4 --regioes caminho/regioes.json
```

- N/P pulam para o próximo e o anterior T0 ou T1 detectado.
- Z marca a referência. Daí em diante, o `delta` mostra quantos quadros e quantos ms se passaram
  desde ela.

## Como o script decide

1. Em cada quadro, mede a cor média de cada região. No botão, usa a cor relativa (cada canal
   dividido pela soma), que não muda quando o brilho geral varia (cintilação da tela, reflexo
   da lâmpada, exposição da câmera). Na lâmpada, usa o brilho.
2. Separa os quadros de cada região em dois estados (desligado/ligado, apagada/acesa) e
   normaliza o sinal: 0 = um estado, 1 = o outro. Regiões sem diferença clara entre os estados
   são ignoradas.
3. Uma mudança é confirmada quando o sinal passa de 0,5. O script então volta quadro a quadro
   até o **início** da mudança: o primeiro quadro que já percorreu 15% do caminho. Assim, o T0
   pega o começo da animação do botão e o T1 pega a primeira luz perceptível.
4. Apagões de até 25 ms com a lâmpada acesa são tratados como cintilação da rede de 60 Hz.
5. Cada mudança do botão é pareada com a primeira mudança da lâmpada do mesmo dispositivo, no
   mesmo sentido.
6. O tempo de cada quadro vem do carimbo de tempo gravado no arquivo, ou de índice / taxa de
   captura quando se usa `--fps-captura`. Saltos nos carimbos entre o T0 e o T1 são contados
   na coluna `quadros_perdidos`.
7. A incerteza é de ±1 quadro em cada ponta: ±4,2 ms a 240 fps, ±8,3 ms a 120 fps.

## Validação

```bash
python validar_sintetico.py
```

Gera vídeos sintéticos com T0 e T1 conhecidos e confere se o script os encontra. Os vídeos
simulam:

- o botão com animação de troca de cor, com o dedo cobrindo parte dele;
- a cintilação da tela e da lâmpada, o reflexo da lâmpada na tela e a rampa de acendimento;
- um teste sem resposta e um em que a lâmpada acende antes de o botão mudar;
- um vídeo longo com vários testes seguidos;
- um arquivo reescrito a 30 fps.

Os resultados ficam em `teste_sintetico/`. O esperado é terminar com `VALIDAÇÃO OK`, com erro
de no máximo 1 quadro.
