# esp32_mqtt_bench

Dois ESP32 (ESP-IDF + esp-mqtt) e o mosquitto no RPi4: o **pub** publica mensagens de tamanho definido no broker e o **sub** as recebe.
O mesmo código gera os dois firmwares; o papel da placa é escolhido no build (presets `pub` / `sub`).
Os parâmetros de cada execução são enviados via MQTT para as duas placas. Um clock de referência de 100 kHz gerado pelo RPi
dá às duas a mesma base de tempo, e o botão que liga o clock dá a partida no teste.

```
           Wi-Fi                          Wi-Fi
ESP32 pub ───────► mosquitto no RPi5 ───────► ESP32 sub
    ▲                      │                      ▲
    └──── clock 100 kHz ───┴──── clock 100 kHz ───┘
```

## Broker
Por padrão, o mosquitto 2.x escuta apenas em localhost. Em `/etc/mosquitto/conf.d/bench.conf`:
```
listener 1883 0.0.0.0
allow_anonymous true
```
Sem TLS, para que o Wireshark consiga decodificar o MQTT.

## Ligações
```
RPi 5 (conector de 40 pinos)                          ESP32 pub     ESP32 sub
pino 32  GPIO12 (PWM0, clock) ──┬──[100 Ω]──────────── GPIO4
                                └──[100 Ω]─────────────────────────── GPIO4
pino 34  GND ───────────────────────────────────────── GND ────────── GND

pino 11  GPIO17 ──┐
               [botão]
pino 9   GND ─────┘
```
- 3,3 V dos dois lados, ligação direta. O GND comum é obrigatório.
- Um resistor série por placa, perto do RPi (33–220 Ω servem): amortece as reflexões nos fios. Fios curtos (até ~30 cm), de preferência cada sinal trançado com um GND.
- O botão usa o pull-up interno do RPi. A entrada do clock no ESP tem pull-down interno, então a linha fica em nível baixo enquanto o RPi não a aciona.
- O GPIO do ESP é configurável: `Bench clock → Clock input GPIO` (padrão 4).

## Build
```
idf.py --preset pub build
idf.py --preset pub -p /dev/ttyUSB0 flash monitor
idf.py --preset sub -p /dev/ttyUSB1 flash monitor
idf.py --preset sub menuconfig
```
| preset | papel | sdkconfig | diretório de build |
|---|---|---|---|
| `pub` | publisher | `sdkconfig.pub` | `build/pub` |
| `sub` | subscriber | `sdkconfig.sub` | `build/sub` |
| `pub-perf`, `sub-perf` | o mesmo + tuning de lwIP/Wi-Fi ([../common/sdkconfig.perf](../common/sdkconfig.perf)) | `build/<preset>/sdkconfig` | `build/<preset>` |

- Sem `--preset`, o `idf.py` usa o primeiro preset (`pub`). Para não repetir a opção: `export IDF_PRESET=sub`.
- O papel fica em `MQTT bench → Board role`, definido por `sdkconfig.defaults.pub` / `sdkconfig.defaults.sub`. O resto é comum: [sdkconfig.defaults](sdkconfig.defaults) e os padrões do Kconfig (SSID, senha, URI do broker). O que se muda no `menuconfig` vale só para o sdkconfig daquele preset.
- O target padrão é `esp32s3` (em `sdkconfig.defaults`). Outro chip: `idf.py --preset sub set-target esp32`. A entrada do clock usa o periférico PCNT, que o ESP32-C3 não tem.
- Depois de usar o `menuconfig`, confira `git diff sdkconfig.pub sdkconfig.sub`: as opções de *Security features* (flash encryption, secure boot) gravam eFuses de forma irreversível no primeiro boot.
- Os presets estão em [CMakePresets.json](CMakePresets.json). O `idf.py` não expande macros (`${sourceDir}`) nem suporta `inherits`, por isso os caminhos são relativos e repetidos.

O código de Wi-Fi é compartilhado entre todos os benchmarks: [../common/bench_wifi](../common/bench_wifi); a entrada do clock: [../common/bench_clock](../common/bench_clock).

## Clock no RPi
```
# /boot/firmware/config.txt, depois reboot
dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4

pinctrl get 12                                # GPIO12 deve estar na função PWM0
./rpi/clock_button.py --log clock_log.csv
```
[rpi/clock_button.py](rpi/clock_button.py) usa o PWM de hardware do RP1 (push-pull, o período não depende da carga da CPU) e o `gpiozero` para o botão,
que já vem no Raspberry Pi OS. Cada aperto liga ou desliga o clock. O acesso a `/sys/class/pwm` exige o grupo `gpio`; sem ele, rode com `sudo`.

## Execução
1. Com o clock parado, envie o comando. As duas placas respondem `armed`: o contador foi zerado e elas aguardam o clock.
   ```
   mosquitto_sub -h 192.168.4.1 -t bench/status -v                 # terminal 1
   mosquitto_sub -h 192.168.4.1 -t bench/sub/log > run1.csv         # terminal 2, opcional
   mosquitto_pub -h 192.168.4.1 -t bench/ctrl -m "size=1024 count=2000 qos=1 inflight=8 rate=0"
   ```
2. Aperte o botão: o clock liga e o pub começa a publicar na primeira borda.
3. Depois de `pub done` e `sub done`, aperte de novo: o clock para. Só então envie o próximo comando; com o clock ligado, as placas respondem `error reason=clock_running`.

`stop` interrompe a execução nas duas placas, inclusive enquanto aguardam o clock.

| chave | significado | padrão |
|---|---|---|
| `size` | tamanho do payload MQTT, bytes (mín. 33, para caber o cabeçalho) | 256 |
| `count` | mensagens por execução | 1000 |
| `qos` | 0, 1 ou 2 (com 0 não há ack e `inflight` é ignorado) | 1 |
| `inflight` | janela de mensagens não confirmadas no pub (1 = stop-and-wait) | 8 |
| `rate` | msg/s no pub, 0 = o mais rápido possível dentro da janela | 0 |

O sub assina `bench/data` com QoS 2, então recebe com o mesmo QoS usado pelo pub (o broker entrega com o menor dos dois).

Respostas em `bench/status`, com o prefixo do papel:
- `pub armed run=N ...`, `pub start run=N tick=T`, `pub done run=N sent= acked= elapsed_us= payload_Bps= ack_min/avg/max_us= tick_start= tick_end= ...`, `pub error ...`.
  O pub mede o tempo de ack desde a chamada de publish até o PUBACK (QoS 1) ou o PUBCOMP (QoS 2). Com QoS 0: `acked=0`, tempos de ack 0 e `payload_Bps` é a taxa de envio.
- `sub armed ...`, `sub done run=N end= rx= unique= lost= dup= reordered= foreign= bad= lat_min/avg/max_us= span_us= payload_Bps= tick_first_tx= tick_last_rx= log= log_dropped=`, `sub error ...`.
  `end`: `complete` (chegaram todas), `idle` (10 s sem dados, mais que o timeout de ack de 5 s do pub, `CONFIG_BENCH_SUB_IDLE_MS`), `stopped` ou `rearmed` (chegou um novo comando).

## Base de tempo
- As duas placas contam as bordas de subida do clock com o PCNT, em hardware e sem carga de CPU. 1 tick = 10 µs.
  Como o contador é zerado com o clock parado, as duas começam na mesma borda, e não há deriva entre os cristais dos ESP: os dois contam o mesmo clock.
- Payload: `<run>:<seq, 8 dígitos>:<tx_tick, 10 dígitos>|abcdef…`, no tópico `bench/data`. `tx_tick` é lido no pub logo antes de `esp_mqtt_client_publish()`.
  O sub marca `rx_tick` no início do `MQTT_EVENT_DATA` (no último fragmento, se a mensagem for maior que `CONFIG_BENCH_MQTT_BUF_SIZE`).
- `lat = rx_tick − tx_tick` é a latência de ida pub → broker → sub, incluindo os dois trechos de Wi-Fi e o mosquitto. Resolução de ±1 tick.
- `span_us` vai do primeiro envio à última chegada; `payload_Bps = unique · size / span_us` é a vazão fim a fim.
- Log por mensagem em `bench/sub/log`, CSV `seq,tx_tick,rx_tick`, enviado depois do `sub done`, inclusive duplicatas.
  Termina com `# end run=N lines=M`. Até `CONFIG_BENCH_SUB_LOG_MAX` registros (10000 = 120 KB, na PSRAM se houver).
  ```python
  import pandas as pd
  d = pd.read_csv("run1.csv", comment="#")
  d["lat_us"] = (d.rx_tick - d.tx_tick) * 10
  ```
- Alinhamento com o Wireshark: `clock_button.py` registra `start_ns` no relógio do RPi (o mesmo dos timestamps do `dumpcap`).
  O tick N corresponde a `start_ns + N · 10 µs` e pode ser comparado com `frame.time_epoch`. A incerteza é `start_err_ns` (a escrita no sysfs, tipicamente dezenas de µs) mais 1 tick.

## Wireshark
Captura na interface do RPi por onde chega o tráfego dos ESP, filtro de captura `tcp port 1883`. A captura contém as duas conexões, pub → broker e broker → sub.
Como capturar o tráfego no RPi sem GUI e abri-lo no PC: [../README.md](../README.md#captura-de-tráfego-no-rpi-e-visualização-no-pc).

| o quê | display filter |
|---|---|
| dados | `mqtt.topic == "bench/data"` |
| só um lado | `ip.addr == <ip do pub>` / `ip.addr == <ip do sub>` |
| PUBLISH / PUBACK | `mqtt.msgtype == 3` / `mqtt.msgtype == 4` |
| PUBREC / PUBREL / PUBCOMP | `mqtt.msgtype == 5` / `6` / `7` |
| limites da execução | `mqtt.topic == "bench/status"` |
| log do sub (depois da execução) | `mqtt.topic == "bench/sub/log"` |

Throughput: Statistics → IO Graphs (bytes/s) ou Statistics → Conversations no intervalo entre `start` e `done`.
O tamanho do quadro no Wireshark é maior que `size`: ao payload somam-se o fixed header do MQTT, o tópico, o packet id, TCP/IP e, na `wlan0`, também o 802.11.
