"""
Alerta de Enchente com Previsão de Transbordamento - medir o nível da água, o sistema calcula a VELOCIDADE de subida
e estima em quantos minutos o canal vai transbordar, antecipando o alerta.

Entradas : HC-SR04 (nível), potenciômetro (chuva), 2 botões (silenciar / teste)
Saídas   : 3 LEDs, buzzer (padrões por estado), display OLED SSD1306
"""
from machine import Pin, PWM, ADC, I2C, time_pulse_us
from utime import ticks_ms, ticks_diff, sleep_ms
import ssd1306

ALTURA_SENSOR_CM = 200      # distância do sensor até o fundo do canal
NIVEL_TRANSBORDO = 150      # nível (cm) em que a água transborda
LIMITES = (80, 110, 140)    # ATENÇÃO, ALERTA, EMERGÊNCIA (cm)
HISTERESE_CM = 5            # folga para descer de estado
TEMPO_DESCIDA_MS = 5000     # tempo mínimo abaixo do limite para descer
CHUVA_MAX_MM_H = 100        # fundo de escala do potenciômetro
CHUVA_FORTE = 60            # mm/h considerado chuva forte
JANELA_AMOSTRAS = 20        # amostras usadas no cálculo da tendência
INTERVALO_LEITURA_MS = 500
SILENCIO_MS = 30000         # duração do botão silenciar
DURACAO_TESTE_MS = 3000

NORMAL, ATENCAO, ALERTA, EMERGENCIA = 0, 1, 2, 3
NOMES = ("NORMAL", "ATENCAO", "ALERTA", "EMERGENCIA")
# (período total, tempo ligado, frequência) do buzzer por estado
PADROES_BUZZER = {
    ATENCAO: (2000, 100, 1000),
    ALERTA: (1000, 300, 1500),
    EMERGENCIA: (400, 200, 2000),
}

# HARDWARE 
trig = Pin(3, Pin.OUT)
echo = Pin(2, Pin.IN)
chuva_adc = ADC(26)
led_verde = Pin(10, Pin.OUT)
led_amarelo = Pin(11, Pin.OUT)
led_vermelho = Pin(12, Pin.OUT)
buzzer = PWM(Pin(15))
buzzer.duty_u16(0)
btn_teste = Pin(13, Pin.IN, Pin.PULL_UP)
btn_silencio = Pin(14, Pin.IN, Pin.PULL_UP)
i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=400000)
oled = ssd1306.SSD1306_I2C(128, 64, i2c)

# LEITURAS
def ler_distancia_cm():
    """Distância medida pelo HC-SR04 em cm (None se falhar)."""
    trig.low()
    sleep_ms(2)
    trig.high()
    sleep_ms(1)
    trig.low()
    duracao = time_pulse_us(echo, 1, 30000)
    if duracao < 0:
        return None
    return duracao / 58.0

def ler_chuva_mm_h():
    return chuva_adc.read_u16() * CHUVA_MAX_MM_H / 65535

# TENDÊNCIA E PREVISÃO 
historico = []  # lista de (tempo_ms, nivel_cm)

def registrar(nivel, agora):
    historico.append((agora, nivel))
    if len(historico) > JANELA_AMOSTRAS:
        historico.pop(0)


def media_movel(valores):
    return sum(valores) / len(valores)


def taxa_cm_min():
    """Velocidade de subida (cm/min): média da 2ª metade da janela
    menos média da 1ª metade, dividido pelo tempo entre elas."""
    if len(historico) < JANELA_AMOSTRAS:
        return 0.0
    meio = JANELA_AMOSTRAS // 2
    ini = historico[:meio]
    fim = historico[meio:]
    dt_min = ticks_diff(fim[meio // 2][0], ini[meio // 2][0]) / 60000
    if dt_min <= 0:
        return 0.0
    return (media_movel([n for _, n in fim]) - media_movel([n for _, n in ini])) / dt_min

def tempo_ate_transbordar(nivel, taxa):
    """Minutos até o transbordo (None se estável/descendo)."""
    if taxa <= 0.2:
        return None
    return max(0.0, (NIVEL_TRANSBORDO - nivel) / taxa)

# MÁQUINA DE ESTADOS 
def estado_por_nivel(nivel):
    estado = NORMAL
    for i, lim in enumerate(LIMITES):
        if nivel >= lim:
            estado = i + 1
    return estado

def estado_por_antecipacao(eta, chuva, taxa):
    """Sobe o estado ANTES de o nível chegar ao limite."""
    estado = NORMAL
    if chuva >= 40 and taxa > 0.5:
        estado = ATENCAO
    if chuva >= CHUVA_FORTE and taxa > 1:
        estado = ALERTA
    if eta is not None:
        if eta <= 15:
            estado = max(estado, ALERTA)
        if eta <= 5:
            estado = max(estado, EMERGENCIA)
    return estado

estado = NORMAL
descendo_desde = None

def atualizar_estado(nivel, eta, chuva, taxa, agora):
    """Sobe na hora; desce só com histerese + tempo de espera."""
    global estado, descendo_desde
    antecip = estado_por_antecipacao(eta, chuva, taxa)
    bruto = max(estado_por_nivel(nivel), antecip)
    if bruto >= estado:
        estado = bruto
        descendo_desde = None
        return
    com_folga = max(estado_por_nivel(nivel + HISTERESE_CM), antecip)
    if com_folga < estado:
        if descendo_desde is None:
            descendo_desde = agora
        elif ticks_diff(agora, descendo_desde) >= TEMPO_DESCIDA_MS:
            estado = com_folga
            descendo_desde = None
    else:
        descendo_desde = None

# SAÍDAS
silencio_ate = 0
teste_ate = 0

def atualizar_leds(agora):
    if teste_ate and ticks_diff(teste_ate, agora) > 0:
        led_verde.on()
        led_amarelo.on()
        led_vermelho.on()
        return
    led_verde.value(estado == NORMAL)
    led_amarelo.value(estado == ATENCAO)
    if estado == EMERGENCIA:
        led_vermelho.value((agora // 200) % 2)  # pisca
    else:
        led_vermelho.value(estado == ALERTA)

def atualizar_buzzer(agora):
    em_teste = teste_ate and ticks_diff(teste_ate, agora) > 0
    mudo = ticks_diff(silencio_ate, agora) > 0
    if em_teste:
        buzzer.freq(1200)
        buzzer.duty_u16(20000 if (agora // 150) % 2 else 0)
        return
    if estado == NORMAL or mudo:
        buzzer.duty_u16(0)
        return
    periodo, ligado, freq = PADROES_BUZZER[estado]
    if agora % periodo < ligado:
        buzzer.freq(freq)
        buzzer.duty_u16(20000)
    else:
        buzzer.duty_u16(0)

def atualizar_display(nivel, chuva, taxa, eta, mudo):
    oled.fill(0)
    oled.text("NIVEL:%4d cm" % nivel, 0, 0)
    oled.text("CHUVA:%3d mm/h" % chuva, 0, 12)
    oled.text("TEND:%+5.1f cm/min" % taxa, 0, 24)
    oled.text("ETA: " + ("--" if eta is None else "%d min" % eta), 0, 36)
    oled.text(NOMES[estado] + (" (MUDO)" if mudo else ""), 0, 52)
    oled.show()

def tela_erro():
    oled.fill(0)
    oled.text("FALHA NO SENSOR", 0, 28)
    oled.show()

# BOTÕES
ultimo_btn = 0

def tratar_botoes(agora):
    """Leitura sem bloqueio, com debounce de 250 ms."""
    global silencio_ate, teste_ate, ultimo_btn
    if ticks_diff(agora, ultimo_btn) < 250:
        return
    if btn_silencio.value() == 0:
        silencio_ate = agora + SILENCIO_MS
        ultimo_btn = agora
    elif btn_teste.value() == 0:
        teste_ate = agora + DURACAO_TESTE_MS
        ultimo_btn = agora

# LOOP
ultima_leitura = ticks_ms()     
nivel = 0
chuva = 0
taxa = 0.0
eta = None
falha = False

print("Sistema de alerta de enchente iniciado")

while True:
    agora = ticks_ms()
    tratar_botoes(agora)

    if ticks_diff(agora, ultima_leitura) >= INTERVALO_LEITURA_MS:
        ultima_leitura = agora
        dist = ler_distancia_cm()
        chuva = ler_chuva_mm_h()
        if dist is None:
            falha = True
        else:
            falha = False
            estado_anterior = estado
            nivel = max(0, ALTURA_SENSOR_CM - dist)
            registrar(nivel, agora)
            taxa = taxa_cm_min()
            eta = tempo_ate_transbordar(nivel, taxa)
            atualizar_estado(nivel, eta, chuva, taxa, agora)
            if estado > estado_anterior:
                silencio_ate = 0  # novo agravamento reativa o alarme
                print("ESTADO:", NOMES[estado], "| nivel=%.0f taxa=%.1f" % (nivel, taxa))
        if falha:
            tela_erro()
        else:
            atualizar_display(nivel, chuva, taxa, eta, ticks_diff(silencio_ate, agora) > 0)

    atualizar_leds(agora)
    atualizar_buzzer(agora)
    sleep_ms(20)