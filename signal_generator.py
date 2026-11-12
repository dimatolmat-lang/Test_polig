# -*- coding: utf-8 -*-
"""
Генератор и проигрыватель тестового сигнала (tkinter).

Структура сигнала (все параметры — в файле config.json рядом со скриптом):
  1)  общая пауза
  2)  импульс СТАРТ
  3)  общая пауза
  4)  ГРУППА СИНУСОВ: частота от..до (шаг), длительность, пауза, уровни;
      внутри одной частоты перебираются уровни, затем следующая частота
  5)  общая пауза
  6)  ГРУППА СВИПОВ: частота от..до (round_trip = туда-обратно),
      длительность, пауза, уровни
  7)  общая пауза
  8)  ГРУППА ШУМА: тип (white/pink), длительность, пауза, уровни
  9)  общая пауза
  10) импульс СТОП

Группа отключается заданием "duration_s": 0.

Папка результатов: либо создаётся «Angle_<N>» рядом с программой,
либо выбирается уже существующая (лог + «Исходник.wav» кладутся туда).

Хуки: user_hook_on_start() / user_hook_on_stop() — см. внизу класса App.

Приём UDP-пакетов: при нажатии «СТАРТ» запускается приёмник, который
слушает указанный порт и сохраняет пакеты в бинарный файл в папке
результатов. Останавливается при нажатии «СТОП» или по завершении.

Зависимости:  pip install numpy sounddevice   (Python 3.7+)
"""

import bisect
import copy
import datetime
import json
import os
import queue
import socket
import subprocess                     # +++ для ping
import sys                            # +++ для определения заморозки PyInstaller
import threading
import time
import wave

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import numpy as np
    import sounddevice as sd
except ImportError as _err:
    _tmp = tk.Tk()
    _tmp.withdraw()
    messagebox.showerror(
        "Не установлены зависимости",
        "Установите библиотеки командой:\n\n"
        "    pip install numpy sounddevice\n\n"
        f"Подробности: {_err}",
    )
    raise SystemExit(1)


# ============================ ФАЙЛ НАСТРОЕК ====================================
CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "sample_rate": 46875,
    "start_silence_s": 1.0,                      # тишина ПЕРЕД импульсом СТАРТ
    "end_silence_s": 1.0,                        # тишина ПОСЛЕ импульса СТОП
    "general_pause_s": 2.0,                       # общие паузы (пп. 1,3,5,7,9)
    "impulse": {                                  # импульсы СТАРТ/СТОП
        "duration_ms": 5,                         # длительность, мс
        "level_percent": 100,                     # уровень, % полной шкалы
    },
    "sine": {                                     # группа синусов
        "freq_start_hz": 100,
        "freq_end_hz": 2000,
        "freq_step_hz": 100,
        "duration_s": 2.0,
        "pause_s": 1.0,
        "levels_percent": [25, 50, 75, 100],
    },
    "sweep": {                                    # группа свипов
        "freq_start_hz": 100,
        "freq_end_hz": 2000,
        "round_trip": True,                      # true = от->до->обратно
        "duration_s": 10.0,
        "pause_s": 1.0,
        "levels_percent": [25, 50, 75, 100],
    },
    "noise": {                                    # группа шума
        "type": "pink",                          # "white"/"белый" | "pink"/"розовый"
        "duration_s": 10.0,
        "pause_s": 1.0,
        "levels_percent": [25, 50, 75, 100],
        "seed": 12345,                            # зерно ГПСЧ: одна и та же
    },                                            # реализация шума при запусках
    # +++ секция приёмника UDP-пакетов +++
    "udp_receiver": {
        "enabled": False,                        # включить приём при старте
        "local_port": 45920,                     # локальный порт для прослушивания
        "source_ip": "192.168.0.9",             # IP источника ("" = любой)
        "packet_size": 1293,                     # ожидаемый размер пакета, байт
        "header_size": 13,                       # размер заголовка в пакете
        "filename": "data.bin",                  # имя файла (в папке результатов)
        "strip_header": True,                   # True = писать только данные без заголовка
    },
}


class AbortedError(Exception):
    """Воспроизведение остановлено пользователем."""


# ==============================================================================
#                          ЗАГРУЗКА И ПРОВЕРКА НАСТРОЕК
# ==============================================================================
def _merge_defaults(cfg, defaults):
    """Заполняет недостающие ключи значениями по умолчанию."""
    out = dict(defaults)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_defaults(v, out[k])
        else:
            out[k] = v
    return out


def _sect(cfg, name):
    v = cfg.get(name, {})
    if not isinstance(v, dict):
        raise ValueError(f"{name}: ожидается группа параметров {{...}}, получено {v!r}")
    return v


def validate_config(cfg):
    """Проверка значений настроек. Бросает ValueError с понятным описанием."""
    def num(v, path_, mn=None, mx=None):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{path_}: ожидается число, получено {v!r}")
        if mn is not None and v < mn:
            raise ValueError(f"{path_}: должно быть >= {mn} (сейчас {v})")
        if mx is not None and v > mx:
            raise ValueError(f"{path_}: должно быть <= {mx} (сейчас {v})")
        return v

    def levels(v, path_):
        if not isinstance(v, list) or not v:
            raise ValueError(f"{path_}: нужен непустой список, например [25, 50, 75, 100]")
        for i, x in enumerate(v):
            num(x, f"{path_}[{i}]", 0, 100)
        return v

    fs = num(cfg.get("sample_rate"), "sample_rate", 8000, 192000)
    num(cfg.get("general_pause_s"), "general_pause_s", 0)
    num(cfg.get("start_silence_s"), "start_silence_s", 0)
    num(cfg.get("end_silence_s"), "end_silence_s", 0)

    imp = _sect(cfg, "impulse")
    num(imp.get("duration_ms"), "impulse.duration_ms", 0)
    num(imp.get("level_percent"), "impulse.level_percent", 0, 100)

    s = _sect(cfg, "sine")
    f1 = num(s.get("freq_start_hz"), "sine.freq_start_hz", 1, fs / 2)
    f2 = num(s.get("freq_end_hz"), "sine.freq_end_hz", 1, fs / 2)
    num(s.get("freq_step_hz"), "sine.freq_step_hz", 1)
    num(s.get("duration_s"), "sine.duration_s", 0)
    num(s.get("pause_s"), "sine.pause_s", 0)
    if s.get("duration_s", 0) > 0:
        if f2 < f1:
            raise ValueError("sine.freq_end_hz должен быть >= sine.freq_start_hz")
        levels(s.get("levels_percent"), "sine.levels_percent")

    sw = _sect(cfg, "sweep")
    wf1 = num(sw.get("freq_start_hz"), "sweep.freq_start_hz", 1, fs / 2)
    wf2 = num(sw.get("freq_end_hz"), "sweep.freq_end_hz", 1, fs / 2)
    if not isinstance(sw.get("round_trip", False), bool):
        raise ValueError("sweep.round_trip: ожидается true/false")
    num(sw.get("duration_s"), "sweep.duration_s", 0)
    num(sw.get("pause_s"), "sweep.pause_s", 0)
    if sw.get("duration_s", 0) > 0:
        if wf1 == wf2:
            raise ValueError("sweep: freq_start_hz и freq_end_hz должны различаться")
        levels(sw.get("levels_percent"), "sweep.levels_percent")

    nz = _sect(cfg, "noise")
    typ = str(nz.get("type", "white")).strip().lower()
    if typ not in ("white", "pink", "белый", "розовый"):
        raise ValueError('noise.type: "white"/"белый" или "pink"/"розовый"')
    num(nz.get("duration_s"), "noise.duration_s", 0)
    num(nz.get("pause_s"), "noise.pause_s", 0)
    if nz.get("duration_s", 0) > 0:
        levels(nz.get("levels_percent"), "noise.levels_percent")
    seed = nz.get("seed", 12345)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("noise.seed: ожидается целое число")

    # +++ проверка секции udp_receiver +++
    udp = _sect(cfg, "udp_receiver")
    if not isinstance(udp.get("enabled", False), bool):
        raise ValueError("udp_receiver.enabled: ожидается true/false")
    num(udp.get("local_port"), "udp_receiver.local_port", 1, 65535)
    num(udp.get("packet_size"), "udp_receiver.packet_size", 1)
    num(udp.get("header_size", 13), "udp_receiver.header_size", 0)
    if not isinstance(udp.get("strip_header", False), bool):
        raise ValueError("udp_receiver.strip_header: ожидается true/false")


def create_default_config(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
        f.write("\n")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("Ожидается JSON-объект {...} верхнего уровня")
    cfg = _merge_defaults(raw, DEFAULT_CONFIG)
    validate_config(cfg)
    return cfg


# ==============================================================================
#                                ГЕНЕРАЦИЯ СИГНАЛА
# ==============================================================================
def _freq_list(f1, f2, step):
    f1, f2, step = int(f1), int(f2), max(1, int(step))
    if f2 < f1:
        f1, f2 = f2, f1
    freqs = list(range(f1, f2 + 1, step))
    if freqs[-1] != f2:
        freqs.append(f2)          # «до частоты» включительно
    return freqs


def _build_plan(cfg):
    """
    План сигнала по ТЗ: список шагов (тип, n_сэмплов, параметры, описание).
    Типы: pause | impulse | sine | sweep | noise.

    Структура:
      старт-тишина -> импульс СТАРТ -> общая пауза -> [синусы] -> общая пауза
      -> [свипы] -> общая пауза -> [шум] -> общая пауза -> импульс СТОП
      -> конечная тишина
    """
    fs = int(cfg["sample_rate"])
    steps = []

    def add(kind, n, params, desc):
        steps.append((kind, int(n), params, desc))

    gp = float(cfg["general_pause_s"])
    n_gp = round(gp * fs)

    imp = cfg["impulse"]
    n_imp = round(float(imp["duration_ms"]) * fs / 1000.0)
    imp_lvl = float(imp["level_percent"])

    # 1-3. старт: тишина -> импульс -> общая пауза
    n_start = round(float(cfg["start_silence_s"]) * fs)
    if n_start > 0:
        add("pause", n_start, None, f"Старт-тишина {float(cfg['start_silence_s']):g} с")
    if n_imp > 0:
        add("impulse", n_imp, imp_lvl,
            f"Импульс СТАРТ ({float(imp['duration_ms']):g} мс, {imp_lvl:g} %)")
    if n_gp > 0:
        add("pause", n_gp, None, f"Общая пауза {float(cfg['general_pause_s']):g} с")

    # 4. группа синусов: сначала уровни, потом следующая частота
    s = cfg["sine"]
    if float(s["duration_s"]) > 0 and s["levels_percent"]:
        for f in _freq_list(s["freq_start_hz"], s["freq_end_hz"], s["freq_step_hz"]):
            for lvl in s["levels_percent"]:
                add("sine", round(float(s["duration_s"]) * fs), (int(f), float(lvl)),
                    f"Синус {f} Гц, уровень {float(lvl):g} %, {float(s['duration_s']):g} с")
                add("pause", round(float(s["pause_s"]) * fs), None,
                    f"Пауза {float(s['pause_s']):g} с")
    # 5. общая пауза
    add("pause", n_gp, None, f"Общая пауза {gp:g} с")

    # 6. группа свипов
    sw = cfg["sweep"]
    rt = bool(sw.get("round_trip", False))
    if (float(sw["duration_s"]) > 0 and sw["levels_percent"]
            and sw["freq_start_hz"] != sw["freq_end_hz"]):
        f1, f2 = int(sw["freq_start_hz"]), int(sw["freq_end_hz"])
        arrow = f"{f1}\u2192{f2}" + (f"\u2192{f1}" if rt else "")
        for lvl in sw["levels_percent"]:
            add("sweep", round(float(sw["duration_s"]) * fs), (f1, f2, rt, float(lvl)),
                f"Свип {arrow} Гц (линейно), уровень {float(lvl):g} %, "
                f"{float(sw['duration_s']):g} с")
            add("pause", round(float(sw["pause_s"]) * fs), None,
                f"Пауза {float(sw['pause_s']):g} с")
    # 7. общая пауза
    add("pause", n_gp, None, f"Общая пауза {gp:g} с")

    # 8. группа шума
    nz = cfg["noise"]
    if float(nz["duration_s"]) > 0 and nz["levels_percent"]:
        typ = str(nz["type"]).strip().lower()
        typ = {"белый": "white", "розовый": "pink"}.get(typ, typ)
        typ_ru = {"white": "белый", "pink": "розовый"}.get(typ, typ)
        for lvl in nz["levels_percent"]:
            add("noise", round(float(nz["duration_s"]) * fs), (typ, float(lvl)),
                f"Шум {typ_ru}, уровень {float(lvl):g} %, {float(nz['duration_s']):g} с")
            add("pause", round(float(nz["pause_s"]) * fs), None,
                f"Пауза {float(nz['pause_s']):g} с")
    # 9. общая пауза
    if n_gp > 0:
        add("pause", n_gp, None, f"Общая пауза {float(cfg['general_pause_s']):g} с")
    # 10. импульс СТОП -> конечная тишина
    if n_imp > 0:
        add("impulse", n_imp, imp_lvl,
            f"Импульс СТОП ({float(imp['duration_ms']):g} мс, {imp_lvl:g} %)")
    n_end = round(float(cfg["end_silence_s"]) * fs)
    if n_end > 0:
        add("pause", n_end, None, f"Конец-тишина {float(cfg['end_silence_s']):g} с")

    return steps


def generation_info(cfg):
    """(длительность_с, размер_wav_МБ) по текущим настройкам."""
    steps = _build_plan(cfg)
    fs = int(cfg["sample_rate"])
    total = sum(n for _k, n, _p, _d in steps) / fs
    size_mb = total * fs * 2 / (1024.0 * 1024.0)
    return total, size_mb


def _pink_noise(n, rng):
    """Розовый шум (спектральная плотность ~ 1/f), FFT-метод."""
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, d=1.0)
    f[0] = f[1]                       # избежать деления на ноль
    spec = spec / np.sqrt(f)          # амплитуда ~ 1/sqrt(f)
    return np.fft.irfft(spec, n)


def build_test_signal(cfg, stop_flag=None):
    """
    Строит сигнал по плану _build_plan(cfg).

    Уровни синуса/свипа — доля амплитуды (level % полной шкалы).
    Уровень шума — по ПИКОВОМУ значению (пик = level % полной шкалы),
    чтобы гарантированно не было клиппинга; RMS белого шума при этом
    примерно в 4-5 раз ниже пика.

    Возвращает: (buf int16 моно, segments [(начало_с, длительность_с, описание)],
    total_с).
    """
    fs = int(cfg["sample_rate"])
    steps = _build_plan(cfg)
    n_total = sum(n for _k, n, _p, _d in steps)

    buf = np.zeros(n_total, dtype=np.int16)
    segments = []
    pos = 0

    def check_stop():
        if stop_flag is not None and stop_flag.is_set():
            raise AbortedError()

    silence_cache = {}
    sweep_cache = {}
    noise_cache = {}
    rng = np.random.default_rng(int(cfg.get("noise", {}).get("seed", 12345)))

    for kind, n, params, desc in steps:
        check_stop()

        if kind == "pause":
            arr = silence_cache.get(n)
            if arr is None:
                arr = np.zeros(n, dtype=np.int16)
                silence_cache[n] = arr

        elif kind == "impulse":
            lvl = float(params)
            arr = np.full(n, int(round(lvl / 100.0 * 32767.0)), dtype=np.int16)

        elif kind == "sine":
            f, lvl = params
            t = np.arange(n, dtype=np.float64) / fs
            arr = ((lvl / 100.0) * 32767.0 * np.sin(2.0 * np.pi * f * t)).astype(np.int16)

        elif kind == "sweep":
            f1, f2, rt, lvl = params
            key = (n, f1, f2, rt)
            wave = sweep_cache.get(key)
            if wave is None:
                if rt:                                    # от->да->обратно
                    half = n // 2
                    f_inst = np.concatenate((
                        np.linspace(f1, f2, half),
                        np.linspace(f2, f1, n - half)))
                else:                                     # от->да
                    f_inst = np.linspace(f1, f2, n)
                wave = np.sin(2.0 * np.pi * np.cumsum(f_inst) / fs)
                sweep_cache[key] = wave
            arr = ((lvl / 100.0) * 32767.0 * wave).astype(np.int16)

        else:  # noise
            typ, lvl = params
            base = noise_cache.get(n)
            if base is None:
                if typ == "pink":
                    base = _pink_noise(n, rng)
                else:
                    base = rng.standard_normal(n)
                base = base / np.max(np.abs(base))        # пик = 1.0
                noise_cache[n] = base
            arr = ((lvl / 100.0) * 32767.0 * base).astype(np.int16)

        segments.append((pos / fs, n / fs, desc))
        buf[pos:pos + n] = arr
        pos += n

    return buf[:pos], segments, pos / fs


# ==============================================================================
#                                     WAV I/O
# ==============================================================================
def load_wav(path):
    """Читает PCM WAV. Возвращает (данные float32 [N]/[N,каналы], Гц, длительность_с)."""
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        fs = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sampwidth == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v[v >= (1 << 23)] -= (1 << 24)
        data = v.astype(np.float32) / float(1 << 23)
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Неподдерживаемая разрядность WAV: {sampwidth * 8} бит")

    if n_channels > 1:
        data = data.reshape(-1, n_channels)
    return data, fs, data.shape[0] / fs


def save_wav_int16(path, buf, fs, stop_flag=None, chunk=1 << 20):
    """Сохраняет int16-моно буфер в WAV по частям (без раздувания памяти)."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(fs)
        for i in range(0, len(buf), chunk):
            if stop_flag is not None and stop_flag.is_set():
                raise AbortedError()
            wf.writeframes(buf[i:i + chunk].tobytes())

def convert_udp_bin_to_wav(bin_path, wav_path=None,
                            sample_rate=46875, n_channels=4,
                            channel_size=160, strip_header=True,
                            header_size=13):
    """
    Конвертирует бинарный файл от UDP-приёмника в многоканальный WAV.

    Структура одного пакета в файле:
      - заголовок header_size байт (только если strip_header=False)
      - данные: n_channels × channel_size семплов int16,
        уложенных блочно: [канал1(160), канал2(160), канал3(160), канал4(160)]

    WAV пишется в interleaved-формате:
      [ch1_s0, ch2_s0, ch3_s0, ch4_s0, ch1_s1, ch2_s1, ...]

    Возвращает: (wav_path, n_packets, n_total_samples)
    """
    if wav_path is None:
        base, _ = os.path.splitext(bin_path)
        wav_path = base + ".wav"

    samples_per_packet = n_channels * channel_size
    bytes_per_packet = samples_per_packet * 2 if strip_header \
        else header_size + samples_per_packet * 2

    raw = np.fromfile(bin_path, dtype=np.uint8)
    n_packets = len(raw) // bytes_per_packet
    if n_packets == 0:
        raise ValueError(
            f"Файл слишком короткий для конвертации: {len(raw)} байт, "
            f"нужно минимум {bytes_per_packet}")

    # Отрезаем возможный «хвост» некратной длины
    raw = raw[:n_packets * bytes_per_packet]
    packets = raw.reshape(n_packets, bytes_per_packet)

    # Убираем заголовок, если он есть в файле
    if not strip_header:
        packets = packets[:, header_size:]

    # Переинтерпретируем байты как int16: форма (n_packets, 640)
    samples = packets.view(np.int16)

    # Разбиваем по каналам и собираем матрицу (N_total, n_channels)
    channels = []
    for ch in range(n_channels):
        ch_data = samples[:, ch * channel_size:(ch + 1) * channel_size]
        channels.append(ch_data.reshape(-1))

    matrix = np.stack(channels, axis=1)         # (N_total, n_channels) int16

    # Interleave для WAV: (N_total, n_channels) -> (N_total * n_channels,)
    interleaved = matrix.reshape(-1)

    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(interleaved.tobytes())

    return wav_path, n_packets, matrix.shape[0]

# ==============================================================================
#                            КНОПКА «ХОТ-БАР» (п.3)
# ==============================================================================
class HotButton(tk.Canvas):
    """Покой: зелёная, «СТАРТ». Активна: синяя, «СТОП», заполняется прогрессом."""

    COLOR_IDLE = "#2ea44f"     # зелёный
    COLOR_ACTIVE = "#1f6feb"   # синий
    COLOR_FILL = "#79b8ff"     # заливка прогресса
    COLOR_TEXT = "#ffffff"

    def __init__(self, master, command, width=460, height=64):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bw, self._bh = width, height   # ВАЖНО: не self._w — занят tkinter'ом!
        self._command = command
        self._active = False
        self._progress = 0.0
        self.bind("<Button-1>", self._on_click)
        self._redraw()

    def _on_click(self, _event):
        if self._command:
            self._command()

    def set_active(self, active):
        self._active = bool(active)
        if not self._active:
            self._progress = 0.0
        self._redraw()

    def set_progress(self, fraction):
        self._progress = max(0.0, min(1.0, float(fraction)))
        if self._active:
            self._redraw()

    def _redraw(self):
        self.delete("all")
        base = self.COLOR_ACTIVE if self._active else self.COLOR_IDLE
        self.create_rectangle(0, 0, self._bw, self._bh, fill=base, outline="")
        if self._active and self._progress > 0.0:
            fill_w = int(self._bw * self._progress)
            self.create_rectangle(0, 0, fill_w, self._bh,
                                  fill=self.COLOR_FILL, outline="")
        self.create_text(self._bw // 2, self._bh // 2,
                         text="СТОП" if self._active else "СТАРТ",
                         fill=self.COLOR_TEXT, font=("Arial", 15, "bold"))

# ==============================================================================
#                       ИНДИКАТОР СВЯЗИ С ИСТОЧНИКОМ  (+++)
# ==============================================================================
class PingIndicator(tk.Canvas):
    """Индикатор связи с источником UDP (кликабельный кружок).

    Цвета:
      зелёный  — ping есть (источник доступен)
      красный  — ping отсутствует
      серый    — неизвестно / идёт проверка
    """

    COLOR_OK = "#22c55e"        # зелёный
    COLOR_FAIL = "#ef4444"      # красный
    COLOR_UNKNOWN = "#9ca3af"   # серый
    COLOR_OUTLINE = "#374151"

    def __init__(self, master, size=20, on_click=None):
        super().__init__(master, width=size, height=size,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._size = size
        self._on_click = on_click
        self._state = "unknown"
        self.bind("<Button-1>", self._handle_click)
        self._draw()

    def _handle_click(self, _event):
        if self._on_click:
            self._on_click()

    def set_state(self, state):
        """state: 'ok' | 'fail' | 'unknown'"""
        self._state = state
        self._draw()

    def _draw(self):
        self.delete("all")
        color = {
            "ok": self.COLOR_OK,
            "fail": self.COLOR_FAIL,
            "unknown": self.COLOR_UNKNOWN,
        }.get(self._state, self.COLOR_UNKNOWN)
        r = self._size // 2 - 2
        cx = cy = self._size // 2
        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                         fill=color, outline=self.COLOR_OUTLINE, width=1)


def check_ping(ip, timeout=2):
    """Пингует IP-адрес. Возвращает True при успехе, False при неудаче."""
    if not ip:
        return False
    is_windows = os.name == "nt"
    count_param = "-n" if is_windows else "-c"
    timeout_param = "-w" if is_windows else "-W"
    timeout_val = str(timeout * 1000) if is_windows else str(timeout)

    kwargs = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout + 3,
    )
    if is_windows:
        # CREATE_NO_WINDOW — не показывать консоль при запуске ping
        kwargs["creationflags"] = 0x08000000

    try:
        result = subprocess.run(
            ["ping", count_param, "1", timeout_param, timeout_val, ip],
            **kwargs,
        )
        return result.returncode == 0
    except Exception:
        return False

# ==============================================================================
#                       ПРИЁМНИК UDP-ПАКЕТОВ  (+++)
# ==============================================================================
class UdpReceiver:
    """Приёмник UDP-пакетов с сохранением в бинарный файл.

    Запускается в отдельном потоке. Слушает указанный локальный порт,
    опционально фильтрует пакеты по IP-адресу источника и записывает
    их в файл (сырые байты — как пришли, либо без заголовка).

    Структура пакета (согласно ТЗ):
      - общий размер:  1293 байта
      - заголовок:      13 байт
      - данные:         1280 байт = 640 семплов int16_t
      - частота:        46875 Гц
      - распределение по каналам: 1-й канал — 16 семплов,
        далее по 160 семплов на канал
    """

    def __init__(self, local_port, output_path, source_ip="",
                 packet_size=1293, strip_header=False, header_size=13,
                 status_callback=None):
        self._local_port = int(local_port)
        self._output_path = output_path
        self._source_ip = source_ip.strip() if source_ip else ""
        self._packet_size = int(packet_size)
        self._strip_header = bool(strip_header)
        self._header_size = int(header_size)
        self._status_callback = status_callback

        self._stop_flag = threading.Event()
        self._thread = None
        self._sock = None
        self._file = None

        self._packets_received = 0
        self._bytes_received = 0
        self._packets_bad_size = 0
        self._packets_bad_source = 0
        self._error = None
        self._lock = threading.Lock()
        self._running = False

    # ------------------------------------------------------------- API ----

    def start(self):
        """Запуск потока приёма."""
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        """Остановка потока приёма (блокирует до timeout секунд)."""
        self._stop_flag.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self._running = False

    def is_running(self):
        return self._running

    def get_stats(self):
        """Возвращает словарь со статистикой приёма."""
        with self._lock:
            return {
                "packets": self._packets_received,
                "bytes": self._bytes_received,
                "bad_size": self._packets_bad_size,
                "bad_source": self._packets_bad_source,
                "error": self._error,
                "running": self._running,
            }

    # -------------------------------------------------------- внутреннее ----

    def _set_status(self, text):
        if self._status_callback:
            try:
                self._status_callback(text)
            except Exception:
                pass

    def _run(self):
        """Рабочий цикл приёмника."""
        self._running = True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Увеличиваем приёмный буфер ядра до 4 МБ
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                      4 * 1024 * 1024)
            except Exception:
                pass
            self._sock.bind(("0.0.0.0", self._local_port))
            self._sock.settimeout(0.5)
        except Exception as exc:
            self._error = f"Не удалось открыть сокет: {exc}"
            self._set_status(f"Ошибка UDP: {exc}")
            self._running = False
            return

        try:
            self._file = open(self._output_path, "wb")
        except Exception as exc:
            self._error = f"Не удалось открыть файл: {exc}"
            self._set_status(f"Ошибка UDP: {exc}")
            try:
                self._sock.close()
            except Exception:
                pass
            self._running = False
            return

        self._set_status(
            f"Приём UDP: порт {self._local_port}"
            + (f", источник {self._source_ip}" if self._source_ip else " (любой)")
            + f"  ->  {os.path.basename(self._output_path)}")

        try:
            while not self._stop_flag.is_set():
                try:
                    data, addr = self._sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break

                # Фильтр по IP источника
                if self._source_ip and addr[0] != self._source_ip:
                    with self._lock:
                        self._packets_bad_source += 1
                    continue

                # Проверка размера пакета
                if len(data) != self._packet_size:
                    with self._lock:
                        self._packets_bad_size += 1
                    continue

                # Опционально отрезаем заголовок
                if self._strip_header and len(data) > self._header_size:
                    write_data = data[self._header_size:]
                else:
                    write_data = data

                try:
                    self._file.write(write_data)
                    self._file.flush()
                except Exception as exc:
                    self._error = f"Ошибка записи: {exc}"
                    break

                with self._lock:
                    self._packets_received += 1
                    self._bytes_received += len(write_data)
        finally:
            try:
                if self._file:
                    self._file.close()
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._running = False


# ==============================================================================
#                              ОСНОВНОЕ ПРИЛОЖЕНИЕ
# ==============================================================================
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Генератор тестового сигнала")
        self.resizable(False, False)

        # ---------- служебное состояние ----------
        self._active = False                 # идёт подготовка/воспроизведение
        self._stop_flag = threading.Event()  # взводится кнопкой «СТОП»
        self._worker = None
        self._ui_queue = queue.Queue()       # статусы: рабочий поток -> GUI
        self._done_flag = False
        self._done_reason = None             # "finished" | "manual" | "error"
        self._error_text = None
        self._playback_started = False
        self._play_t0 = None                 # time.monotonic() начала звука
        self._total_duration = None
        self._segments = None                # расписание сегментов генерации
        self._seg_offsets = None
        self._stop_hook_called = False
        self._click_guard_until = 0.0

        # +++ состояние UDP-приёмника +++
        self._udp_receiver = None
        self._last_udp_path = None

        # ---------- данные текущего запуска (видны в хуках) ----------
        self._last_angle = None
        self._last_folder = None
        self._last_log = None
        self._last_cfg = None

        # ---------- файл настроек ----------
        # +++ При запуске из .exe (PyInstaller) — папка рядом с .exe +++
        #     При запуске .py скрипта — папка со скриптом
        if getattr(sys, "frozen", False):
            self._base_dir = os.path.dirname(sys.executable)
        else:
            try:
                self._base_dir = os.path.dirname(os.path.abspath(__file__))
            except NameError:
                self._base_dir = os.getcwd()
        self._cfg_path = os.path.join(self._base_dir, CONFIG_FILE)

        if not os.path.exists(self._cfg_path):
            try:
                create_default_config(self._cfg_path)
                messagebox.showinfo(
                    "Файл настроек",
                    f"Создан файл настроек по умолчанию:\n{self._cfg_path}\n\n"
                    "Все параметры генерации (общие паузы, импульсы, синусы,\n"
                    "свипы, шум) задаются в нём — можно править любым\n"
                    "текстовым редактором и нажимать «Перечитать».")
            except OSError as exc:
                messagebox.showerror("Ошибка",
                                     f"Не удалось создать файл настроек:\n{exc}")
                raise SystemExit(1)

        self._cfg = None
        self._load_config(show_error=True)   # при ошибке — встроенные значения

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # +++ Состояние ping-проверки +++
        self._ping_in_progress = False

        # +++ Всегда работающий опросчик очереди + прогресс +++
        self.after(300, self._poll)

        # +++ Периодическая проверка связи (раз в 60 секунд) +++
        #     Первая проверка — через 1.5 с после старта
        self.after(1500, self._check_ping_periodic)


    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        frm = ttk.Frame(self, padding=(14, 10))
        frm.grid(sticky="nsew")
        frm.columnconfigure(2, weight=1)

        # ---- 1. Угол ----
        ttk.Label(frm, text="Угол, °:").grid(row=0, column=0, sticky="w", pady=4)
        self._angle_var = tk.StringVar(value="0")
        self._angle_spin = ttk.Spinbox(frm, from_=-90, to=90, increment=1,
                                       textvariable=self._angle_var, width=6,
                                       command=self._sync_angle)
        self._angle_spin.grid(row=0, column=1, sticky="w", padx=(6, 12), pady=4)
        self._angle_spin.bind("<KeyRelease>", lambda _e: self._sync_angle())
        self._angle_scale = ttk.Scale(frm, from_=-90, to=90, value=0,
                                      command=self._on_scale)
        self._angle_scale.grid(row=0, column=2, sticky="ew", pady=4)

        # ---- 1а. Папка результатов ----
        fold = ttk.LabelFrame(frm, text="Папка результатов (лог + «Исходник.wav»)",
                              padding=(10, 6))
        fold.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self._folder_mode_var = tk.StringVar(value="new")
        ttk.Radiobutton(fold, text="Создавать «Angle_<N>» рядом с программой",
                        variable=self._folder_mode_var, value="new",
                        command=self._update_folder_ui).pack(side="left")
        ttk.Radiobutton(fold, text="Существующая:",
                        variable=self._folder_mode_var, value="existing",
                        command=self._update_folder_ui).pack(side="left",
                                                             padx=(14, 0))
        self._folder_path_var = tk.StringVar()
        self._folder_entry = ttk.Entry(fold, textvariable=self._folder_path_var,
                                       width=34)
        self._folder_entry.pack(side="left", padx=(6, 0))
        self._folder_browse_btn = ttk.Button(fold, text="Обзор…", width=9,
                                             command=self._browse_folder)
        self._folder_browse_btn.pack(side="left", padx=(8, 0))

        # ---- 2. Источник звука ----
        src = ttk.LabelFrame(frm, text="Источник звука", padding=(10, 6))
        src.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self._source_var = tk.StringVar(value="gen")
        ttk.Radiobutton(src, text="WAV-файл", variable=self._source_var, value="wav",
                        command=self._update_source_ui).pack(side="left")
        ttk.Radiobutton(src, text="Генерация", variable=self._source_var, value="gen",
                        command=self._update_source_ui).pack(side="left", padx=(14, 0))
        self._wav_path_var = tk.StringVar()
        self._wav_entry = ttk.Entry(src, textvariable=self._wav_path_var, width=40)
        self._wav_entry.pack(side="left", padx=(14, 0))
        self._browse_btn = ttk.Button(src, text="Обзор…", width=9,
                                       command=self._browse_wav)
        self._browse_btn.pack(side="left", padx=(8, 0))

        # +++ 2б. Приём UDP-пакетов +++
        udp_frm = ttk.LabelFrame(frm, text="Приём UDP-пакетов", padding=(10, 6))
        udp_frm.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 4))

        udp_cfg = self._cfg.get("udp_receiver", {}) if self._cfg else {}
        self._udp_enabled_var = tk.BooleanVar(value = True)
        ttk.Checkbutton(udp_frm, text="Принимать и сохранять в файл",
                        variable=self._udp_enabled_var).pack(side="left")

        udp_fields = ttk.Frame(udp_frm)
        udp_fields.pack(side="left", padx=(14, 0))

        # IP источника + индикатор связи
        ttk.Label(udp_fields, text="IP источника:").grid(row=0, column=0,
                                                          sticky="w")
        self._udp_ip_var = tk.StringVar(
            value=str(udp_cfg.get("source_ip", "192.168.0.20")))
        self._udp_ip_entry = ttk.Entry(udp_fields, textvariable=self._udp_ip_var,
                                        width=15)
        self._udp_ip_entry.grid(row=0, column=1, padx=(4, 4))
        # При выходе из поля IP — перепроверить связь
        self._udp_ip_entry.bind("<FocusOut>", lambda _e: self._check_ping())

        # +++ Индикатор связи (кликабельный кружок) +++
        self._ping_indicator = PingIndicator(udp_fields, size=18,
                                              on_click=self._check_ping)
        self._ping_indicator.grid(row=0, column=2, padx=(2, 2))
        self._ping_status_var = tk.StringVar(value="?")
        ttk.Label(udp_fields, textvariable=self._ping_status_var,
                  width=5, foreground="#555").grid(row=0, column=3, padx=(0, 12))

        ttk.Label(udp_fields, text="Порт:").grid(row=0, column=4, sticky="w")
        self._udp_port_var = tk.StringVar(
            value=str(udp_cfg.get("local_port", 45920)))
        self._udp_port_entry = ttk.Entry(udp_fields, textvariable=self._udp_port_var,
                                         width=7)
        self._udp_port_entry.grid(row=0, column=5, padx=(4, 12))

        ttk.Label(udp_fields, text="Размер пакета:").grid(row=0, column=6,
                                                           sticky="w")
        self._udp_pkt_var = tk.StringVar(
            value=str(udp_cfg.get("packet_size", 1293)))
        self._udp_pkt_entry = ttk.Entry(udp_fields, textvariable=self._udp_pkt_var,
                                        width=6)
        self._udp_pkt_entry.grid(row=0, column=7, padx=(4, 0))

        # ---- 2в. Файл настроек генерации ----
        self._cfg_path_var = tk.StringVar(value=self._cfg_path)
        cfgf = ttk.Frame(frm)
        cfgf.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(cfgf, text="Настройки генерации:").pack(side="left")
        ttk.Label(cfgf, textvariable=self._cfg_path_var, foreground="#555").pack(
            side="left", padx=(6, 10))
        self._reload_btn = ttk.Button(cfgf, text="Перечитать", width=11,
                                       command=self._reload_config)
        self._reload_btn.pack(side="left")

        # информация о генерации
        self._info_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self._info_var, foreground="#555",
                  width=80, anchor="w").grid(row=5, column=0, columnspan=3,
                                             sticky="w", padx=4, pady=(0, 6))

        # ---- 3. Кнопка СТАРТ/СТОП ----
        self._hotbtn = HotButton(frm, command=self._on_hotbutton)
        self._hotbtn.grid(row=6, column=0, columnspan=3, pady=(2, 8))

        # ---- 4. Статус ----
        self._status_var = tk.StringVar(value="Готов к работе")
        ttk.Label(frm, textvariable=self._status_var, anchor="w", justify="left",
                  wraplength=620).grid(row=7, column=0, columnspan=3,
                                       sticky="w", padx=4, pady=(2, 4))

        self._update_source_ui()
        self._update_folder_ui()

    # ------------------------------------------------------------- угол ----
    def _on_scale(self, value):
        v = int(round(float(value)))
        if abs(float(value) - v) > 1e-6:
            self._angle_scale.set(v)          # «прилипание» к целым
        self._angle_var.set(str(v))

    def _sync_angle(self):
        try:
            v = int(self._angle_spin.get())
        except (ValueError, tk.TclError):
            return
        self._angle_scale.set(max(-90, min(90, v)))

    def _get_angle(self):
        try:
            v = int(float(self._angle_spin.get()))
        except (ValueError, tk.TclError):
            v = 0
        return max(-90, min(90, v))

    # --------------------------------------------------------- настройки ----
    def _load_config(self, show_error=True):
        try:
            self._cfg = load_config(self._cfg_path)
            return True
        except Exception as exc:
            if show_error:
                messagebox.showerror(
                    "Ошибка в файле настроек",
                    f"{self._cfg_path}\n\n{exc}\n\n"
                    "Используются встроенные настройки по умолчанию\n"
                    "(исправьте файл и нажмите «Перечитать»).")
            self._cfg = copy.deepcopy(DEFAULT_CONFIG)
            return False

    def _reload_config(self):
        if self._load_config(show_error=True):
            self._update_source_ui()
            # Обновляем поля UDP из файла настроек
            udp_cfg = self._cfg.get("udp_receiver", {})
            self._udp_enabled_var.set(True)
            self._udp_ip_var.set(str(udp_cfg.get("source_ip", "192.168.0.20")))
            self._udp_port_var.set(str(udp_cfg.get("local_port", 45920)))
            self._udp_pkt_var.set(str(udp_cfg.get("packet_size", 1293)))
            total, size_mb = generation_info(self._cfg)
            self._status_var.set(f"Настройки перечитаны: генерация "
                                 f"{self._fmt_time(total)} (~{size_mb:.0f} МБ)")

    # ---------------------------------------------------------- источник ----
    def _update_source_ui(self):
        is_wav = self._source_var.get() == "wav"
        state = "normal" if is_wav else "disabled"
        self._wav_entry.configure(state=state)
        self._browse_btn.configure(state=state)
        if is_wav:
            self._info_var.set("")
        else:
            try:
                total, size_mb = generation_info(self._cfg)
                self._info_var.set(f"Генерация: {self._fmt_time(total)} "
                                   f"(~{size_mb:.0f} МБ)")
            except Exception as exc:
                self._info_var.set(f"Ошибка в настройках: {exc}")

    def _update_folder_ui(self):
        is_new = self._folder_mode_var.get() == "new"
        state = "disabled" if is_new else "normal"
        self._folder_entry.configure(state=state)
        self._folder_browse_btn.configure(state=state)

    # ----------------------------------------------------------- выбор... ----
    def _browse_wav(self):
        path = filedialog.askopenfilename(
            title="Выбор WAV-файла",
            filetypes=[("WAV-файлы", "*.wav *.wave"), ("Все файлы", "*.*")])
        if path:
            self._wav_path_var.set(path)

    def _browse_folder(self):
        path = filedialog.askdirectory(title="Выбор папки для результатов")
        if path:
            self._folder_path_var.set(path)

    # ---------------------------------------------------- СТАРТ / СТОП ----
    def _on_hotbutton(self):
        if self._active:
            if time.monotonic() < self._click_guard_until:
                return                         # защита от двойного клика
            self._stop_playback()
        else:
            self._start()

    def _stop_playback(self):
        self._stop_flag.set()
        try:
            sd.stop()                          # прервать текущее воспроизведение
        except Exception:
            pass
        self._status_var.set("Остановка…")

    def _start(self):
        mode = self._source_var.get()
        wav_path = self._wav_path_var.get().strip()

        if mode == "wav":
            if not wav_path:
                messagebox.showwarning("Нет файла",
                                       "Выберите WAV-файл (кнопка «Обзор…»).")
                return
            if not os.path.isfile(wav_path):
                messagebox.showwarning("Файл не найден",
                                       f"Файл не найден:\n{wav_path}")
                return
        else:
            try:
                validate_config(self._cfg)
            except ValueError as exc:
                messagebox.showerror("Ошибка в настройках", str(exc))
                return

        angle = self._get_angle()

        # ---- папка результатов: новая или существующая ----
        if self._folder_mode_var.get() == "new":
            folder = os.path.join(self._base_dir, f"Angle_{angle}")
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Ошибка", f"Не удалось создать папку:\n{folder}\n\n{exc}")
                return
        else:
            folder = self._folder_path_var.get().strip()
            if not folder:
                messagebox.showwarning(
                    "Нет папки", "Выберите существующую папку (кнопка «Обзор…»).")
                return
            folder = os.path.abspath(folder)
            if not os.path.isdir(folder):
                messagebox.showwarning("Папка не найдена",
                                       f"Папка не найдена:\n{folder}")
                return

        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = os.path.join(folder, f"Лог_{stamp}.txt")
        source_wav = os.path.join(folder, "Исходник.wav")

        # данные текущего запуска (используются в хуках)
        self._last_angle = angle
        self._last_folder = folder
        self._last_log = log_path
        self._last_cfg = copy.deepcopy(self._cfg)

        # ==================================================================
        # +++ ЗАПУСК ПРИЁМНИКА UDP +++
        # ==================================================================
        self._udp_receiver = None
        self._last_udp_path = None

        if self._udp_enabled_var.get():
            udp_cfg = self._cfg.get("udp_receiver", {})
            try:
                udp_port = int(self._udp_port_var.get())
            except ValueError:
                messagebox.showerror("Ошибка UDP",
                                     f"Неверный порт: {self._udp_port_var.get()}")
                return
            try:
                udp_pkt_size = int(self._udp_pkt_var.get())
            except ValueError:
                messagebox.showerror("Ошибка UDP",
                                     f"Неверный размер пакета: "
                                     f"{self._udp_pkt_var.get()}")
                return

            udp_ip = self._udp_ip_var.get().strip()
            udp_filename_base = str(udp_cfg.get("filename", "data.bin"))
            base, ext = os.path.splitext(udp_filename_base)
            if not ext:
                ext = ".bin"
            udp_filename = f"{base}_{stamp}{ext}"
            udp_path = os.path.join(folder, udp_filename)
            self._last_udp_path = udp_path

            self._udp_receiver = UdpReceiver(
                local_port=udp_port,
                output_path=udp_path,
                source_ip=udp_ip,
                packet_size=udp_pkt_size,
                strip_header=bool(udp_cfg.get("strip_header", False)),
                header_size=int(udp_cfg.get("header_size", 13)),
                status_callback=lambda text: self._ui_queue.put(("status", text)),
            )
            self._udp_receiver.start()
        # ==================================================================

        # ==================================================================
        # ХУК СТАРТ (п.5 ТЗ): вызов вашей функции при старте.
        # Сама функция — ниже, метод user_hook_on_start().
        # ==================================================================
        try:
            self.user_hook_on_start()
        except Exception as exc:
            messagebox.showerror("Ошибка в функции старта", str(exc))
            self._status_var.set("Готов к работе")
            # Останавливаем UDP-приёмник, если он был запущен
            if self._udp_receiver is not None:
                self._udp_receiver.stop()
                self._udp_receiver = None
            return
        # ==================================================================

        # ---- переход в активное состояние ----
        self._active = True
        self._stop_flag.clear()
        self._done_flag = False
        self._done_reason = None
        self._error_text = None
        self._playback_started = False
        self._play_t0 = None
        self._total_duration = None
        self._segments = None
        self._seg_offsets = None
        self._stop_hook_called = False
        self._click_guard_until = time.monotonic() + 0.4
        self._reload_btn.configure(state="disabled")

        self._hotbtn.set_active(True)
        self._hotbtn.set_progress(0.0)
        self._status_var.set("Подготовка…")

        self._worker = threading.Thread(
            target=self._worker_run,
            args=(mode, angle, folder, log_path, wav_path, source_wav,
                  self._last_cfg),
            daemon=True)
        self._worker.start()
        

    # ============================== РАБОЧИЙ ПОТОК ==========================
    def _worker_run(self, mode, angle, folder, log_path, wav_path, source_wav, cfg):
        log = None
        reason = "finished"
        error_text = None

        def status(text):
            self._ui_queue.put(("status", text))

        try:
            start_dt = datetime.datetime.now()
            log = open(log_path, "w", encoding="utf-8")
            log.write("=" * 72 + "\n")
            log.write("ЛОГ ЗАПУСКА\n")
            log.write(f"Дата/время начала : {start_dt:%Y-%m-%d %H:%M:%S}\n")
            log.write(f"Angle            : {angle}\n")
            log.write(f"Режим             : {'WAV-файл' if mode == 'wav' else 'Генерация'}\n")
            log.write(f"Папка             : {folder}\n")
            if mode == "wav":
                log.write(f"WAV-файл          : {wav_path}\n")
                log.write(f"[{start_dt:%H:%M:%S}] СТАРТ: воспроизведение WAV-файла\n")
                log.write(f"Имя файла: {os.path.basename(wav_path)}\n")
                log.write(f"Путь:     {wav_path}\n")
            else:
                log.write(f"Файл настроек     : {self._cfg_path}\n")
                log.write(f"[{start_dt:%H:%M:%S}] СТАРТ: генерация тестового сигнала\n")
                log.write("Параметры генерации:\n")
                for line in json.dumps(cfg, ensure_ascii=False, indent=2).splitlines():
                    log.write("    " + line + "\n")
            # +++ запись параметров UDP-приёмника в лог +++
            if self._udp_receiver is not None:
                udp_cfg = cfg.get("udp_receiver", {})
                log.write("\nПараметры UDP-приёмника:\n")
                log.write(f"    Локальный порт : {self._udp_port_var.get()}\n")
                log.write(f"    IP источника   : "
                           f"{self._udp_ip_var.get() or '(любой)'}\n")
                log.write(f"    Размер пакета  : {self._udp_pkt_var.get()}\n")
                log.write(f"    Файл данных    : {self._last_udp_path}\n")
                log.write(f"    Без заголовка  : "
                           f"{udp_cfg.get('strip_header', False)}\n")
            log.write("=" * 72 + "\n\n")
            log.flush()

            # ---------- подготовка данных ----------
            if mode == "wav":
                status("Чтение WAV-файла…")
                data, fs, total = load_wav(wav_path)
                log.write(f"Длительность: {total:.1f} с\n\n")
            else:
                status("Генерация сигнала…")
                buf, segments, total = build_test_signal(cfg, self._stop_flag)

                if self._stop_flag.is_set():
                    raise AbortedError()

                # ---- 8. сохранение «Исходник.wav» ----
                status("Сохранение «Исходник.wav»…")
                try:
                    save_wav_int16(source_wav, buf, int(cfg["sample_rate"]),
                                   self._stop_flag)
                except AbortedError:
                    try:
                        os.remove(source_wav)     # удалить недописанный файл
                    except OSError:
                        pass
                    raise
                log.write(f"[{datetime.datetime.now():%H:%M:%S}] "
                          f"Сигнал сгенерирован и сохранён: {source_wav}\n")
                log.write(f"Длительность: {total:.1f} с\n\n")
                log.flush()

                data, fs = buf, int(cfg["sample_rate"])
                self._segments = segments
                self._seg_offsets = [s[0] for s in segments]

            # Сюда можно перенести вызов self.user_hook_on_start(), если ваша
            # функция должна выполняться непосредственно перед началом звука.

            if self._stop_flag.is_set():
                raise AbortedError()

            # ---- 7. расписание генерации в лог ----
            play_dt = datetime.datetime.now()
            if self._segments:
                log.write(f"[{play_dt:%H:%M:%S}] Начало воспроизведения. Расписание:\n")
                for off, _dur, desc in self._segments:
                    seg_dt = play_dt + datetime.timedelta(seconds=off)
                    log.write(f"  [{seg_dt:%H:%M:%S}] {desc}\n")
                log.write("\n")
                log.flush()

            # ---- воспроизведение ----
            self._total_duration = total
            self._play_t0 = time.monotonic()
            self._playback_started = True

            sd.play(data, fs)
            if self._stop_flag.is_set():          # защита от гонки «СТОП раньше звука»
                sd.stop()
            sd.wait()

            if self._stop_flag.is_set():
                reason = "manual"

        except AbortedError:
            reason = "manual"
        except Exception as exc:
            reason = "error"
            error_text = f"{type(exc).__name__}: {exc}"
            try:
                sd.stop()
            except Exception:
                pass
        finally:
            end_dt = datetime.datetime.now()
            if log is not None:
                try:
                    if reason == "finished":
                        log.write(f"\n[{end_dt:%H:%M:%S}] КОНЕЦ: воспроизведение завершено\n")
                    elif reason == "manual":
                        log.write(f"\n[{end_dt:%H:%M:%S}] КОНЕЦ: остановлено пользователем\n")
                    else:
                        log.write(f"\n[{end_dt:%H:%M:%S}] КОНЕЦ: ошибка — {error_text}\n")
                    log.write(f"Дата/время конца : {end_dt:%Y-%m-%d %H:%M:%S}\n")
                except Exception:
                    pass
                finally:
                    try:
                        log.close()
                    except Exception:
                        pass
            self._done_reason = reason
            self._error_text = error_text
            self._done_flag = True               # подхватывается потоком GUI

    # ============================ ОПРОС (GUI-поток) ========================
    def _poll(self):
        """Всегда работающий опросчик: очередь + прогресс воспроизведения."""
        # Сообщения от рабочих потоков (status, ping)
        self._process_ui_queue()

        if self._active:
            if self._done_flag:
                self._finish()
            elif self._playback_started and self._total_duration:
                elapsed = time.monotonic() - self._play_t0
                frac = min(1.0, elapsed / self._total_duration)
                self._hotbtn.set_progress(frac)
                udp_info = ""
                if self._udp_receiver is not None:
                    stats = self._udp_receiver.get_stats()
                    udp_info = f"  •  UDP: {stats['packets']} пак."
                    if stats["bad_size"] > 0 or stats["bad_source"] > 0:
                        udp_info += (f" (плохих: "
                                     f"{stats['bad_size'] + stats['bad_source']})")
                self._status_var.set(
                    f"Воспроизведение: {self._fmt_time(elapsed)} / "
                    f"{self._fmt_time(self._total_duration)}"
                    f"{self._current_segment(elapsed)}{udp_info}")

        self.after(200, self._poll)      # всегда перепланируем

    # ----------------------------------------------------------- ПИНГ ----
    def _check_ping(self):
        """Проверка связи с источником (в отдельном потоке, не блокирует GUI)."""
        if self._ping_in_progress:
            return
        ip = self._udp_ip_var.get().strip()
        if not ip:
            self._ping_indicator.set_state("unknown")
            self._ping_status_var.set("?")
            return

        self._ping_in_progress = True
        self._ping_indicator.set_state("unknown")
        self._ping_status_var.set("…")

        def worker():
            result = check_ping(ip, timeout=2)
            self._ui_queue.put(("ping", result))

        threading.Thread(target=worker, daemon=True).start()

    def _check_ping_periodic(self):
        """Автоматическая проверка каждые 60 секунд."""
        self._check_ping()
        self.after(60000, self._check_ping_periodic)

    def _process_ui_queue(self):
        """Разбор сообщений из очереди (от рабочих потоков)."""
        try:
            while True:
                kind, value = self._ui_queue.get_nowait()
                if kind == "status":
                    self._status_var.set(value)
                elif kind == "ping":
                    self._ping_in_progress = False
                    if value:
                        self._ping_indicator.set_state("ok")
                        self._ping_status_var.set("OK")
                    else:
                        self._ping_indicator.set_state("fail")
                        self._ping_status_var.set("нет")
        except queue.Empty:
            pass
        
    def _current_segment(self, elapsed):
        if not self._seg_offsets:
            return "  •  WAV-файл"
        i = bisect.bisect_right(self._seg_offsets, elapsed) - 1
        if 0 <= i < len(self._segments):
            off, dur, desc = self._segments[i]
            if elapsed < off + dur:
                return f"  •  {desc}"
        return ""

    def _finish(self):
        self._active = False
        self._playback_started = False
        self._worker = None
        self._hotbtn.set_active(False)
        self._reload_btn.configure(state="normal")

        reason = self._done_reason or "finished"
        if reason == "error":
            self._status_var.set(f"Ошибка: {self._error_text}")
            messagebox.showerror("Ошибка", self._error_text or "Неизвестная ошибка")
        elif reason == "manual":
            self._status_var.set("Остановлено пользователем")
        else:
            self._status_var.set("Воспроизведение завершено")

        # +++ Остановка UDP-приёмника, конвертация в WAV и запись статистики +++
        if self._udp_receiver is not None:
            stats = self._udp_receiver.get_stats()
            self._udp_receiver.stop(timeout=3.0)
            self._udp_receiver = None

            # ---- Конвертация BIN -> 4-канальный WAV ----
            wav_created_path = None
            wav_error = None
            if (self._last_udp_path
                    and os.path.isfile(self._last_udp_path)
                    and stats["packets"] > 0):
                try:
                    udp_cfg = (self._last_cfg or {}).get("udp_receiver", {})
                    sample_rate = int((self._last_cfg or {}).get(
                        "sample_rate", 46875))
                    wav_created_path, n_pkt, n_samples = convert_udp_bin_to_wav(
                        bin_path=self._last_udp_path,
                        sample_rate=sample_rate,
                        n_channels=4,
                        channel_size=160,
                        strip_header=bool(udp_cfg.get("strip_header", True)),
                        header_size=int(udp_cfg.get("header_size", 13)),
                    )
                except Exception as exc:
                    wav_error = f"{type(exc).__name__}: {exc}"

            # ---- Дописываем статистику и инфо о WAV в лог ----
            if self._last_log and os.path.isfile(self._last_log):
                try:
                    with open(self._last_log, "a", encoding="utf-8") as log:
                        log.write("\n" + "=" * 72 + "\n")
                        log.write("СТАТИСТИКА ПРИЁМА UDP\n")
                        log.write(f"Файл данных       : {self._last_udp_path}\n")
                        log.write(f"Принято пакетов   : {stats['packets']}\n")
                        log.write(f"Принято байт      : {stats['bytes']}\n")
                        log.write(f"Отброшено (размер): {stats['bad_size']}\n")
                        log.write(f"Отброшено (IP)    : {stats['bad_source']}\n")
                        if stats["error"]:
                            log.write(f"Ошибка приёма     : {stats['error']}\n")

                        if wav_created_path:
                            sample_rate = int((self._last_cfg or {}).get(
                                "sample_rate", 46875))
                            log.write("\nСоздан WAV-файл   : "
                                      f"{wav_created_path}\n")
                            log.write(f"Каналов           : 4\n")
                            log.write(f"Семплов в канале  : {n_samples}\n")
                            log.write(f"Длительность      : "
                                      f"{n_samples / sample_rate:.3f} с\n")
                        elif wav_error:
                            log.write(f"\nОшибка конвертации в WAV: {wav_error}\n")
                        log.write("=" * 72 + "\n")
                except Exception:
                    pass

            # ---- Итоговая строка статуса ----
            if reason in ("finished", "manual"):
                msg = (f"{'Завершено' if reason == 'finished' else 'Остановлено'}. "
                       f"UDP: принято {stats['packets']} пакетов "
                       f"({stats['bytes']} байт)")
                if wav_created_path:
                    msg += f", WAV: {os.path.basename(wav_created_path)}"
                elif wav_error:
                    msg += f", ошибка WAV: {wav_error}"
                self._status_var.set(msg)

        # ХУК СТОП (п.5 ТЗ) — см. метод user_hook_on_stop() ниже.
        self._call_stop_hook(reason)

    def _call_stop_hook(self, reason):
        if self._stop_hook_called:
            return
        self._stop_hook_called = True
        try:
            self.user_hook_on_stop(reason)
        except Exception as exc:
            messagebox.showerror("Ошибка в функции остановки", str(exc))

    @staticmethod
    def _fmt_time(seconds):
        seconds = max(0, int(round(seconds)))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _on_close(self):
        if self._active:
            if not messagebox.askyesno(
                    "Выход",
                    "Воспроизведение ещё не завершено.\nОстановить и выйти?"):
                return
            self._stop_flag.set()
            try:
                sd.stop()
            except Exception:
                pass
            if self._worker is not None:
                self._worker.join(timeout=5)
            self._call_stop_hook(self._done_reason or "manual")
        # +++ останавливаем UDP-приёмник при закрытии окна +++
        if self._udp_receiver is not None:
            self._udp_receiver.stop(timeout=3.0)
            self._udp_receiver = None
        self.destroy()

    # ######################################################################
    # #############  МЕСТО ДЛЯ ВАШЕЙ ФУНКЦИИ ПРИ СТАРТЕ  (п.5)  ############
    # ######################################################################
    # Вызывается при нажатии кнопки «СТАРТ», в потоке GUI, после выбора папки
    # и подготовки лога (до начала звука).
    #
    # Доступные данные:
    #   self._last_angle  — угол (int, -90..90)
    #   self._last_folder — папка результатов (созданная или выбранная)
    #   self._last_log    — путь к лог-файлу
    #   self._last_cfg    — словарь настроек генерации (config.json)
    #   self._last_udp_path — путь к бинарному файлу UDP-данных (или None)
    # ######################################################################
    def user_hook_on_start(self):
        # ---- ВСТАВЬТЕ ВАШ КОД ЗДЕСЬ ----
        # Пример:
        # my_start_function(self._last_angle, self._last_folder)
        pass

    # ######################################################################
    # ###########  МЕСТО ДЛЯ ВАШЕЙ ФУНКЦИИ ПРИ ОСТАНОВКЕ  (п.5)  ############
    # ######################################################################
    # Вызывается при остановке: вручную (кнопка «СТОП»), автоматически после
    # окончания воспроизведения или при ошибке.
    #
    # reason: "manual" | "finished" | "error"
    # ######################################################################
    def user_hook_on_stop(self, reason):
        # ---- ВСТАВЬТЕ ВАШ КОД ЗДЕСЬ ----
        # Пример:
        # my_stop_function(self._last_angle, self._last_folder)
        pass


if __name__ == "__main__":
    App().mainloop()