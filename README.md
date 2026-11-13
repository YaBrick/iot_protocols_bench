# iot_protocols

Benchmarks de vazão (throughput) de protocolos IoT: ESP32 de um lado, RPi5 do outro.

| diretório | conteúdo |
|---|---|
| [esp32_mqtt_bench](esp32_mqtt_bench) | MQTT: um ESP32 publica e outro assina, via mosquitto no RPi, com clock de referência comum |
| [esp32_modbus_bench](esp32_modbus_bench) | Modbus TCP: o ESP32 é o servidor, um cliente no RPi faz o polling |
| [common](common) | componentes compartilhados (Wi-Fi, entrada do clock de referência) e overlay de performance do sdkconfig |

## Captura de tráfego no RPi e visualização no PC

No RPi o Wireshark roda só em CLI, então os pacotes são capturados lá e analisados no PC.
Em todos os métodos, os timestamps são gerados pelo RPi no momento da captura.

Portas: MQTT `1883`, Modbus TCP `502`. A interface é aquela por onde o tráfego do ESP chega ao RPi:
`wlan0` se o RPi está no Wi-Fi, ou `eth0` se está conectado por cabo. Lista de interfaces: `dumpcap -D`.

### Preparação no RPi (uma vez)
Captura sem sudo, para que a sessão SSH não peça senha:
```
sudo dpkg-reconfigure wireshark-common   # responder Yes: permitir captura para usuários não-root
sudo usermod -aG wireshark $USER
# fazer logout/login e depois verificar:
dumpcap -D                                # deve listar as interfaces sem sudo
```

### 1. Gravar em arquivo e depois copiar para o PC (para medições)
```
# no RPi, antes da execução (Ctrl+C ao terminar)
dumpcap -i wlan0 -f "tcp port 1883" -w /tmp/mqtt_run1.pcapng
dumpcap -i wlan0 -f "tcp port 502"  -w /tmp/modbus_run1.pcapng

# no PC
scp pi@<ip_rpi>:/tmp/mqtt_run1.pcapng . && wireshark mqtt_run1.pcapng
```

### 2. Captura em tempo real via SSH (para depuração)
```
ssh pi@<ip_rpi> 'dumpcap -i wlan0 -f "tcp port 1883 or tcp port 502" -q -w -' | wireshark -k -i -
```
O `dumpcap` grava a captura no stdout, o SSH a transmite para o PC e o Wireshark local lê o fluxo pelo stdin.
O filtro de captura é obrigatório: sem ele, a própria sessão SSH entra na captura e o tráfego passa a crescer sozinho.

### 3. sshdump pela interface gráfica do Wireshark (para depuração)
Na tela inicial, escolha a interface **SSH remote capture: sshdump**, clique na engrenagem e informe:
- endereço do RPi, usuário e chave SSH;
- interface remota (`wlan0`);
- filtro de captura (`tcp port 1883`).

O resultado é o mesmo do método 2, só que sem linha de comando, e as configurações ficam salvas.

### Por que usar o método 1 nas medições
Na transmissão ao vivo (métodos 2 e 3), cada pacote capturado é enviado de novo pela rede até o PC, ou seja, o tráfego no mínimo dobra.
Se o RPi estiver na mesma rede Wi-Fi que o ESP, essa transmissão ocupa o mesmo canal de rádio e a vazão medida fica abaixo da real.
Por isso é melhor gravar as medições em arquivo e copiá-las depois da execução. Se quiser acompanhar ao vivo, conecte o RPi ao roteador por cabo.
