# esp32_modbus_bench

O ESP32 funciona como servidor Modbus TCP («sensor») usando o [esp-modbus](https://github.com/espressif/esp-modbus) v2.
Um cliente no RPi ([rpi/modbus_bench.py](rpi/modbus_bench.py), Python 3.11+, sem dependências) faz o polling via FC03.
Tamanho, janela in-flight e taxa são definidos pelo cliente; o firmware não tem lógica de teste própria.

O Wi-Fi e o overlay de performance são compartilhados com o benchmark de MQTT ([../common](../common)), então as condições são idênticas nos dois.

## Mapa de registradores
| área | endereços | conteúdo |
|---|---|---|
| holding (FC03) | 0..`BENCH_DATA_REGS`-1 (padrão 1000) | payload, aparece como `abcd…` no fio |
| input (FC04) | 0-1 | uptime, s |
| | 2-3 | requisições FC03 atendidas |
| | 4-5 | registradores retornados via FC03 |
| | 6-7 / 8-9 | free heap / menor free heap desde o boot |
| | 10 | `BENCH_DATA_REGS` |

Valores de 32 bits ficam com a word mais significativa primeiro. As estatísticas são atualizadas a cada 100 ms.

## Build do ESP
```
idf.py set-target esp32
idf.py menuconfig                # Bench Wi-Fi → SSID/senha; Modbus bench → porta, unit id, tamanho da área
idf.py build flash monitor       # o log vai mostrar "got IP x.x.x.x"
```
Com tuning de lwIP/Wi-Fi:
```
idf.py -B build_perf -D SDKCONFIG=sdkconfig.perf_build \
       -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;../common/sdkconfig.perf" build flash
```
Vale reservar um IP fixo para o ESP no DHCP do roteador.

## Cliente no RPi
```
./rpi/modbus_bench.py <esp_ip> --size 250 --count 2000
./rpi/modbus_bench.py <esp_ip> --size 16,64,250,1000,4000 --inflight 1,4,16 --csv results.csv
./rpi/modbus_bench.py <esp_ip> --size 64 --rate 100 --count 1000
```
- `--size`: bytes por «mensagem». Uma requisição FC03 retorna no máximo 125 registradores (250 bytes), então mensagens maiores são divididas em várias transações (`tx_per_msg`). Tamanhos ímpares são arredondados para cima até um registrador inteiro.
- `--inflight`: quantas transações podem aguardar resposta ao mesmo tempo. 1 é o Modbus clássico: a próxima requisição só sai depois da resposta à anterior.
- `--rate`: mensagens por segundo. 0 = o mais rápido possível dentro da janela.
- Listas separadas por vírgula geram todas as combinações. Cada execução usa sua própria conexão TCP, com uma pausa de `--gap` entre elas.

Para cada execução é impressa uma linha. Principais campos:
- `msg_per_s`, `tx_per_s`, `payload_Bps`;
- `rtt_*_ms` — tempo entre o envio da requisição e o recebimento da resposta, medido no RPi;
- `exceptions` — número de exceções Modbus nas respostas;
- `esp_reqs` — quantas requisições FC03 o próprio ESP contou. Deve ser igual a `tx_per_msg × count`.

## Wireshark
Captura no RPi, filtro de captura `tcp port 502`. O Modbus/TCP na porta 502 é decodificado automaticamente.
Como capturar o tráfego no RPi sem GUI e abri-lo no PC: [../README.md](../README.md#captura-de-tráfego-no-rpi-e-visualização-no-pc).

| o quê | display filter |
|---|---|
| uma execução | `tcp.stream == N` (o número aparece em Statistics → Conversations → TCP) |
| requisições/respostas com dados | `modbus.func_code == 3` |
| leitura das estatísticas | `modbus.func_code == 4` |
| par requisição–resposta | `mbtcp.trans_id == X` |

Throughput de uma execução: Statistics → Conversations (bytes e duração do fluxo) ou IO Graphs com o filtro `tcp.stream == N`.

## Particularidades do esp-modbus tratadas no firmware
- **Fila de notificações.** A cada acesso a registradores, a stack coloca uma notificação numa fila. Quando ela enche, a task do Modbus espera até 10 ticks por requisição. O timeout está fixo no código como `MB_PAR_INFO_TOUT` e não pode ser alterado pelo Kconfig. Para que isso não entre na medição, a `stats_task` esvazia a fila continuamente e, de quebra, mantém os contadores.
- **Pipeline de requisições.** Com `inflight > 1`, a stack coloca as requisições numa fila de transações e responde estritamente em ordem. O ganho com a janela vem apenas da sobreposição do RTT da rede com o processamento. Para cada uma dessas requisições a stack escreve um WARN, por isso o log da tag `mb_port.tcp.slave` foi reduzido para ERROR.
- **Nagle.** O esp-modbus não ativa `TCP_NODELAY` nos seus sockets. Com `inflight > 1`, várias respostas podem sair num único segmento TCP, o que aparece no Wireshark. O cliente no RPi ativa `TCP_NODELAY`.
